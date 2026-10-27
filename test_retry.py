import contextlib
import io
import json
from pathlib import Path
import queue
import tempfile
import threading
import tkinter as tk
import unittest
import urllib.error
from unittest.mock import patch

import desktop
import mastery as m
import test_mastery as base_tests
from private_state import PrivateStore
from runtime_control import RunControl, Stopped
from test_mastery import config


def response(answers):
    content = json.dumps({'answers': answers, 'confidence': .95}, ensure_ascii=False)
    return io.BytesIO(json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]}).encode())


class AnswerTests(unittest.TestCase):
    def setUp(self):
        self.q = m.Question('模拟多选题：选择需要的水果。', 'multiple', ['苹果', '香蕉', '橘子', '梨'])

    def test_common_formats_match_the_same_options(self):
        for raw in [['a', 'c'], 'A,C', 'AC', 'A、C', '答案：Ａ ｃ', '["A","C"]', ['苹果', '橘子'], ['A. 苹果', 'C：橘子']]:
            with self.subTest(raw=raw):
                self.assertEqual(m.model_answer(self.q, {'answers': raw, 'confidence': .9})[0], ['苹果', '橘子'])
        self.assertEqual(m.model_answer(self.q, {'answer': 'a', 'confidence': .9})[0], ['苹果'])

    def test_boolean_requires_unambiguous_judgment_options(self):
        q = m.Question('判断', 'single', ['错误', '正确'])
        for raw in [True, 'true', '对', '√']:
            self.assertEqual(m.model_answer(q, {'answers': raw, 'confidence': .9})[0], ['正确'])
        self.assertEqual(m.model_answer(q, {'answers': False, 'confidence': .9})[0], ['错误'])
        with self.assertRaises(m.AnswerFormatError):
            m.model_answer(self.q, {'answers': True, 'confidence': .9})

    def test_invalid_choices_and_cardinality_still_rejected(self):
        for raw in [['Z'], ['A', 'A'], [], 1, ['1'], ['A. 香蕉'], '我认为可能选 A 和 C']:
            with self.subTest(raw=raw), self.assertRaises(m.AnswerFormatError):
                m.model_answer(self.q, {'answers': raw, 'confidence': .9})
        with self.assertRaises(m.AnswerFormatError):
            m.model_answer(m.Question('single', 'single', self.q.options), {'answers': 'AC', 'confidence': .9})
        # Exact option text AB conflicts with interpreting it as labels A and B.
        with self.assertRaises(m.AnswerFormatError):
            m.model_answer(m.Question('ambiguous', 'multiple', ['AB', 'other']), {'answers': 'AB', 'confidence': .9})

    def test_compatible_output_uses_one_request(self):
        limits = m.Limits(config())
        with patch('urllib.request.urlopen', return_value=response('a,c')):
            self.assertEqual(m.DeepSeek(config(), limits, api_key='test').answer(self.q, [])[0], ['苹果', '橘子'])
        self.assertEqual(limits.calls, 1)

    def test_format_correction_is_bounded_and_counted(self):
        for outputs, fails in [([response('Z'), response(['A', 'C'])], False), ([response('Z'), response('Z')], True)]:
            with self.subTest(fails=fails):
                limits = m.Limits(config())
                with patch('urllib.request.urlopen', side_effect=outputs) as requests, contextlib.redirect_stdout(io.StringIO()):
                    ai = m.DeepSeek(config(), limits, api_key='test')
                    if fails:
                        with self.assertRaises(m.AnswerFormatError):
                            ai.answer(self.q, [])
                    else:
                        self.assertEqual(ai.answer(self.q, [])[0], ['苹果', '橘子'])
                self.assertEqual(limits.calls, 2)
                self.assertEqual(len(json.loads(requests.call_args.args[0].data)['messages']), 3)

    def test_network_errors_can_retry_but_auth_errors_stop(self):
        for error, expected in [(urllib.error.URLError('offline'), m.Retryable),
                                (urllib.error.HTTPError(m.API_URL, 401, 'auth', {}, None), m.Halt)]:
            with self.subTest(error=type(error).__name__):
                limits = m.Limits(config())
                with patch('urllib.request.urlopen', side_effect=error), self.assertRaises(expected) as result:
                    m.DeepSeek(config(), limits, api_key='test').answer(self.q, [])
                if expected is m.Halt:
                    self.assertNotIsInstance(result.exception, m.Retryable)
                self.assertEqual(limits.calls, 1)


class ControlTests(unittest.TestCase):
    def run_wait(self, decision):
        control = RunControl()
        results = []
        def worker():
            try:
                results.append(control.wait_for_retry('AI', 'format'))
            except Stopped as error:
                results.append(error)
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            while control.events.get(timeout=2)['kind'] != 'retry_needed':
                pass
            self.assertTrue(control.awaiting_retry.is_set())
            self.assertTrue(thread.is_alive())
            decision(control)
        finally:
            thread.join(2)
            if thread.is_alive():
                control.stop()
                thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(control.awaiting_retry.is_set())
        return results

    def test_waits_for_manual_retry(self):
        results = self.run_wait(lambda control: control.retry())
        self.assertIsInstance(results[0], float)

    def test_stop_releases_retry_wait(self):
        self.assertIsInstance(self.run_wait(lambda control: control.stop())[0], Stopped)

    def test_retries_only_failed_step_and_keeps_limits(self):
        control = RunControl()
        limits = m.Limits(config(), control)
        calls = []
        def action():
            limits.api_call()
            calls.append('attempt')
            if len(calls) == 1:
                raise m.AnswerFormatError('format')
            return 'ok'
        def operator():
            try:
                while control.events.get(timeout=2)['kind'] != 'retry_needed':
                    pass
                control.retry()
            except queue.Empty:
                control.stop()
        thread = threading.Thread(target=operator)
        thread.start()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(limits.retry_step('AI', action), 'ok')
        thread.join(2)
        self.assertEqual(limits.calls, 2)
        with self.assertRaises(m.Halt):
            limits.retry_step('fatal', lambda: (_ for _ in ()).throw(m.Halt('budget')))
        self.assertFalse(control.awaiting_retry.is_set())

    def test_manual_wait_excluded_from_deadline_but_budget_kept(self):
        control = RunControl()
        limits = m.Limits({**config(), 'max_api_calls': 1}, control)
        original_deadline = limits.deadline
        action = unittest.mock.Mock(side_effect=[m.Retryable('wait'), 'ok'])
        limits.api_call()
        with patch.object(control, 'wait_for_retry', return_value=30) as wait, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(limits.retry_step('read', action), 'ok')
        self.assertEqual(wait.call_count, 1)
        self.assertEqual(limits.deadline, original_deadline+30)
        with self.assertRaises(m.Halt):
            limits.api_call()

    def test_gui_reminder_and_retry_button(self):
        root = tk.Tk()
        root.withdraw()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                app = desktop.App(root, config(), private_store=PrivateStore(Path(tmp)))
                app.control = RunControl()
                app.control.awaiting_retry.set()
                app.handle_event({'kind': 'phase', 'status': '等待重试', 'message': 'AI'})
                with patch.object(root, 'bell') as reminder:
                    app.handle_event({'kind': 'retry_needed', 'stage': 'AI', 'message': 'format'})
                reminder.assert_called_once()
                self.assertEqual(str(app.retry_button['state']), 'normal')
                self.assertEqual(str(app.pause_button['state']), 'disabled')
                app.retry_button.invoke()
                self.assertTrue(app.control.retry_signal.is_set())
                self.assertEqual(str(app.retry_button['state']), 'disabled')
                app.handle_event({'kind': 'idle'})
                self.assertEqual(str(app.retry_button['state']), 'disabled')
            finally:
                root.update_idletasks()
                root.destroy()


class RetryBrowserTests(base_tests.BrowserTests):
    test_two_rounds_reordered_bank_hit_new_question_and_full_reviews = None
    test_catalogue_dom_resolves_real_hierarchy = None
    test_hidden_recommendation_questions_are_not_collected = None
    test_full_document_navigations_and_retry = None

    def automatic_operator(self, control, stages):
        finished = threading.Event()
        def operator():
            while not finished.is_set():
                try:
                    event = control.events.get(timeout=.1)
                except queue.Empty:
                    continue
                if event['kind'] == 'retry_needed':
                    stages.append(event['stage'])
                    control.retry()
        thread = threading.Thread(target=operator)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(finished.set)

    def test_ai_retry_preserves_round_and_all_attempts(self):
        self.page.goto((Path(__file__).parent/'tests'/'workflow.html').as_uri())
        cfg = {**config(), 'action_delay_seconds': .01, 'timeout_seconds': 3}
        control = RunControl()
        limits = m.Limits(cfg, control)
        stages = []
        self.automatic_operator(control, stages)
        class AI:
            calls = 0
            def answer(ai, q, history):
                ai.calls += 1
                limits.api_call()
                if ai.calls == 2:
                    raise m.AnswerFormatError('bad format')
                if q.stem == '2 + 2 = ?':
                    return ['3'], ''
                return (['2', '3'] if q.kind == 'multiple' else ['Paris']), ''
        ai = AI()
        with patch.object(m, 'ROOT', Path(self.tmp.name)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.Runner(m.BrowserAdapter(self.page, cfg, limits), self.bank, ai, cfg, 'chapter', 'point', limits).run(), 95)
        self.assertEqual(stages, ['AI 解答当前题目'])
        self.assertEqual(ai.calls, 4)
        self.assertEqual(self.bank.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 4)
        self.assertEqual(self.bank.db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0], 4)
        self.assertEqual(self.bank.db.execute("SELECT COUNT(*) FROM attempts WHERE source='bank'").fetchone()[0], 1)

    def test_result_timeout_does_not_resubmit_or_reconfirm(self):
        # A real browser fixture whose confirmation completes the result slowly.
        self.page.set_content('''<button id="submit">submit</button><button id="confirm" hidden>confirm</button>
          <section id="result" hidden><span id="score">95%</span></section>
          <script>window.submits=0;window.confirms=0;
          document.querySelector('#submit').onclick=()=>{submits++;document.querySelector('#confirm').hidden=false};
          document.querySelector('#confirm').onclick=()=>{confirms++;document.querySelector('#confirm').hidden=true;
          setTimeout(()=>document.querySelector('#result').hidden=false,800)};</script>''')
        cfg = {**config(), 'timeout_seconds': .2, 'action_delay_seconds': .01}
        cfg['selectors'] = {**cfg['selectors'], 'submit': '#submit', 'confirm': '#confirm', 'result': '#result', 'mastery': '#score'}
        control = RunControl()
        stages = []
        self.automatic_operator(control, stages)
        adapter = m.BrowserAdapter(self.page, cfg, m.Limits(cfg, control))
        adapter.frame = self.page.main_frame
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                adapter.submit()
            self.assertTrue(stages)
            self.assertTrue(all(stage == '等待已提交作业的结果' for stage in stages))
            self.assertEqual(self.page.evaluate('window.submits'), 1)
            self.assertEqual(self.page.evaluate('window.confirms'), 1)
            self.assertTrue(self.page.locator('#result').is_visible())
        finally:
            adapter.dispose()


if __name__ == '__main__':
    unittest.main(verbosity=2)
