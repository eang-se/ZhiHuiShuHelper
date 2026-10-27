import contextlib
import hashlib
import io
import json
from pathlib import Path
import queue
import tempfile
import threading
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import desktop
import mastery as m
import test_mastery as base_tests
from private_state import PrivateStore
from runtime_control import RunControl, Stopped
from test_mastery import config


class IdentityTests(unittest.TestCase):
    def test_chinese_inline_spacing_matches_but_english_math_and_case_do_not(self):
        stem = '模拟题干：请选择 测试 选项。'
        q = m.Question(stem, 'multiple', ['选项甲', '选项乙', '选项丙', '选项丁'])
        self.assertEqual(q.key, m.Question(stem.replace('测试 选项', '测试选项'), q.kind, q.options).key)
        self.assertEqual(q.stem, stem)
        for first, second in [('ab c', 'abc'), ('x X', 'x x'), ('1 2', '12'), ('a - b', 'a + b'), ('测试，选项', '测试选项')]:
            self.assertNotEqual(m.Question(first, 'single', ['Yes', 'No']).key, m.Question(second, 'single', ['Yes', 'No']).key)

    def test_legacy_bank_merge_and_history_preserve_platform_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'bank.sqlite3'
            q = m.Question('测试 选项', 'multiple', ['one', 'two'])
            legacy_key = hashlib.sha256(json.dumps([q.stem, q.kind, sorted(q.options), q.blanks], ensure_ascii=False).encode()).hexdigest()
            self.assertNotEqual(legacy_key, q.key)
            bank = m.Bank(path)
            bank.put('scope', q, ['one'], False)
            bank.db.execute('UPDATE bank SET signature=?', (legacy_key,))
            bank.db.commit()
            bank.put('scope', q, ['two'], True, 'platform')
            bank.attempt('run', 1, 'scope', 'point', q, ['one'], 'ai')
            bank.feedback('run', 1, 'scope', 'point', q, '参考答案：B', 'wrong', 79)
            bank.db.execute('UPDATE attempts SET signature=?', (legacy_key,))
            bank.db.commit()
            bank.close()
            for _ in range(2):
                bank = m.Bank(path)
                self.assertEqual(bank.get('scope', q), ['two'])
                self.assertIn({'rejected_answer': ['one']}, bank.history('scope', q))
                for table in ('bank', 'attempts', 'reviews'):
                    self.assertEqual(bank.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0], 1)
                    self.assertEqual(bank.db.execute('SELECT signature FROM '+table).fetchone()[0], q.key)
                bank.close()

    def test_migration_prefers_verified_legacy_answer_and_keeps_option_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'bank.sqlite3'
            q = m.Question('中文 题干', 'single', ['A. A. literal', 'B. other'])
            legacy_key = hashlib.sha256(json.dumps([q.stem, q.kind, sorted(q.options), q.blanks], ensure_ascii=False).encode()).hexdigest()
            bank = m.Bank(path)
            bank.put('scope', q, ['A. literal'], True)
            bank.db.execute('UPDATE bank SET signature=?', (legacy_key,))
            bank.db.commit()
            bank.put('scope', q, ['other'], False)
            bank.close()
            bank = m.Bank(path)
            try:
                self.assertEqual(bank.get('scope', q), ['A. literal'])
                self.assertEqual(bank.db.execute('SELECT COUNT(*) FROM bank').fetchone()[0], 1)
            finally:
                bank.close()


class RestartControlTests(unittest.TestCase):
    def test_initialization_close_unwinds_retry_without_resuming(self):
        control = RunControl()
        outcome = []
        def wait():
            try:
                control.wait_for_retry('AI', 'format')
            except Stopped:
                outcome.append('unwound')
        thread = threading.Thread(target=wait)
        thread.start()
        try:
            while control.events.get(timeout=2)['kind'] != 'retry_needed':
                pass
            control.close()
            thread.join(2)
            self.assertEqual(outcome, ['unwound'])
            self.assertTrue(control.close_requested.is_set())
            self.assertTrue(control.stopped.is_set())
            self.assertFalse(control.awaiting_retry.is_set())
        finally:
            control.stop()
            thread.join(2)

    def test_close_prevents_later_resume_or_retry(self):
        control = RunControl()
        control.close()
        control.resume()
        control.retry()
        with self.assertRaises(Stopped):
            control.checkpoint()


class SessionCleanupTests(unittest.TestCase):
    def test_initialization_closes_browser_even_if_final_cookie_save_fails(self):
        for failure in (None, OSError('test save error')):
            with self.subTest(save_fails=failure is not None), tempfile.TemporaryDirectory() as tmp:
                control = RunControl()
                control.close()
                context = Mock()
                context.pages = [Mock()]
                cookies = Mock()
                cookies.maybe_save.side_effect = failure
                with patch.object(desktop, 'ROOT', Path(tmp)), patch.object(desktop, 'CookieKeeper', return_value=cookies), patch('playwright.sync_api.sync_playwright') as sync, patch.object(m, 'run_course') as run:
                    sync.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value = context
                    desktop.browser_worker(config(), 'msedge', 'test-key', control)
                launch = sync.return_value.__enter__.return_value.chromium.launch_persistent_context
                self.assertEqual(launch.call_args.kwargs['args'], m.BROWSER_BACKGROUND_ARGS)
                run.assert_not_called()
                cookies.maybe_save.assert_called_once_with(force=True)
                context.close.assert_called_once()
                events = []
                while not control.events.empty():
                    events.append(control.events.get_nowait())
                self.assertEqual(events[-1]['kind'], 'worker_done')
                self.assertIn({'kind': 'browser', 'opened': False}, events)


class RestartGuiTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.tmp = tempfile.TemporaryDirectory()
        self.app = desktop.App(self.root, config(), private_store=PrivateStore(Path(self.tmp.name)))

    def tearDown(self):
        self.root.update_idletasks()
        self.root.destroy()
        self.tmp.cleanup()

    def test_initialization_waits_for_worker_exit_then_restores_pre_start_state(self):
        app = self.app
        app.control = RunControl()
        app.worker = Mock()
        app.worker.is_alive.return_value = True
        original_worker = app.worker
        app.score.set(79)
        app.question.set('old question')
        app.counts['bank'] = 6
        app.key.set('test-key')
        app.fields['target'].set('93')
        app.append_log('old error')
        with patch.object(app, 'start') as start:
            app.initialize_button.invoke()
            start.assert_not_called()
        self.assertIs(app.worker, original_worker)
        self.assertTrue(app.control.close_requested.is_set())
        self.assertEqual(str(app.initialize_button['state']), 'disabled')
        self.assertEqual(str(app.start_button['state']), 'disabled')
        app.handle_event({'kind': 'worker_done'})
        self.assertTrue(app.initializing)
        app.handle_event({'kind': 'score', 'mastery': 99})
        app.handle_event({'kind': 'phase', 'status': '需要处理', 'message': 'late error'})
        self.assertEqual(app.state.get(), '初始化中')
        app.worker.is_alive.return_value = False
        with patch.object(app, 'start') as start:
            app.finish_initialize()
            start.assert_not_called()
        self.assertEqual(app.score.get(), 0)
        self.assertEqual(app.counts['bank'], 0)
        self.assertNotEqual(app.question.get(), 'old question')
        self.assertEqual(app.state.get(), '未开始')
        self.assertEqual(app.log.get('1.0', 'end-1c'), '')
        self.assertEqual(str(app.start_button['state']), 'normal')
        self.assertEqual(str(app.initialize_button['state']), 'normal')
        for button in (app.pause_button, app.resume_button, app.retry_button, app.stop_button, app.close_button):
            self.assertEqual(str(button['state']), 'disabled')
        self.assertIsNone(app.control)
        self.assertIsNone(app.worker)
        self.assertEqual(app.key.get(), 'test-key')
        self.assertEqual(app.fields['target'].get(), '93')

    def test_idle_initialization_never_starts_a_task(self):
        app = self.app
        app.control = RunControl()
        app.handle_event({'kind': 'idle', 'message': 'initialize here'})
        self.assertEqual(str(app.retry_button['state']), 'disabled')
        self.assertEqual(str(app.initialize_button['state']), 'normal')
        with patch.object(app, 'start') as start:
            app.initialize_button.invoke()
            app.initialize_button.invoke()
        start.assert_not_called()
        self.assertEqual(app.state.get(), '未开始')
        self.assertIsNone(app.worker)

    def test_poll_drains_old_events_and_finishes_initialization(self):
        app = self.app
        control = app.control = RunControl()
        app.worker = Mock()
        app.worker.is_alive.return_value = True
        app.initialize()
        control.emit('phase', status='已停止', message='stale')
        control.emit('worker_done')
        app.worker.is_alive.return_value = False
        # Poll must not dereference app.control after reset sets it to None.
        self.root.after_cancel(app.poll_job)
        app.poll_job = None
        app.poll()
        self.assertEqual(app.state.get(), '未开始')
        self.assertIsNone(app.control)

    def test_restart_controls_fit_minimum_window(self):
        self.root.geometry('1040x760')
        self.root.deiconify()
        self.root.update()
        self.assertTrue(self.app.initialize_button.winfo_ismapped())
        self.assertGreater(self.app.initialize_button.winfo_height(), 15)
        self.assertLess(self.app.initialize_button.winfo_rooty()+self.app.initialize_button.winfo_height(), self.root.winfo_rooty()+self.root.winfo_height())


class ReviewBrowserTests(base_tests.BrowserTests):
    test_two_rounds_reordered_bank_hit_new_question_and_full_reviews = None
    test_catalogue_dom_resolves_real_hierarchy = None
    test_hidden_recommendation_questions_are_not_collected = None
    test_full_document_navigations_and_retry = None

    def prepare(self):
        self.page.goto((Path(__file__).parent/'tests'/'workflow.html').as_uri())
        cfg = {**config(), 'action_delay_seconds': .01, 'timeout_seconds': 3, 'result_settle_seconds': .05}
        limits = m.Limits(cfg)
        class AI:
            calls = 0
            def answer(ai, q, history):
                limits.api_call()
                ai.calls += 1
                return (['3'] if q.kind == 'single' else ['2', '3'] if q.kind == 'multiple' else ['Paris']), ''
        return cfg, limits, AI()

    def test_chinese_quiz_review_spacing_difference_completes_and_hits_bank(self):
        cfg, limits, ai = self.prepare()
        stem = '模拟题干：请选择 测试 选项。'
        self.page.evaluate('''stem => {
          first.stem=stem; reordered.stem=stem; show();
          const review=document.querySelector('.line1-count-link');
          const original=review.onclick;
          review.onclick=()=>{original();document.querySelectorAll('.option-name').forEach(n=>n.textContent=n.textContent.replace('测试 选项','测试选项'))};
        }''', stem)
        with patch.object(m, 'ROOT', Path(self.tmp.name)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.Runner(m.BrowserAdapter(self.page, cfg, limits), self.bank, ai, cfg, 'chapter', 'point', limits).run(), 95)
        self.assertEqual(ai.calls, 3)
        self.assertEqual(self.bank.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 4)
        self.assertEqual(self.bank.db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0], 4)
        self.assertEqual(self.bank.db.execute("SELECT COUNT(*) FROM attempts WHERE source='bank'").fetchone()[0], 1)
        self.assertEqual(self.bank.get('chapter', m.Question(stem, 'single', ['3', '4'])), ['4'])

    def test_partial_review_manual_retry_does_not_duplicate_saved_reviews(self):
        cfg, limits, ai = self.prepare()
        adapter = m.BrowserAdapter(self.page, cfg, limits)
        original = adapter.review_rows
        calls = []
        def partial_once():
            rows = original()
            calls.append(1)
            return rows[:1] if len(calls) == 1 else rows
        control = RunControl()
        limits.control = control
        stages = []
        def operator():
            while True:
                try:
                    event = control.events.get(timeout=5)
                except queue.Empty:
                    control.stop()
                    return
                if event['kind'] == 'retry_needed':
                    stages.append(event['stage'])
                    control.retry()
                    return
        thread = threading.Thread(target=operator)
        thread.start()
        try:
            with patch.object(adapter, 'review_rows', side_effect=partial_once), patch.object(m, 'ROOT', Path(self.tmp.name)), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(m.Runner(adapter, self.bank, ai, cfg, 'chapter', 'point', limits).run(), 95)
        finally:
            thread.join(6)
        self.assertEqual(stages, ['核对并补全本轮解析'])
        self.assertEqual(self.bank.db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0], 4)

    def test_restart_from_review_only_page(self):
        self.page.goto((Path(__file__).parent/'tests'/'navigation-review.html').as_uri())
        cfg = {**config(), 'action_delay_seconds': .01, 'timeout_seconds': 3, 'result_settle_seconds': .05}
        limits = m.Limits(cfg)
        ai = Mock()
        ai.answer.return_value = (['Yes'], '')
        adapter = m.BrowserAdapter(self.page, cfg, limits)
        with patch.object(m, 'ROOT', Path(self.tmp.name)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.Runner(adapter, self.bank, ai, cfg, 'chapter', 'point', limits).run(), 95)
        ai.answer.assert_called_once()
        self.assertEqual(self.bank.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 1)

    def test_initialization_after_error_exits_without_starting_another_run(self):
        cfg, _, _ = self.prepare()
        control = RunControl()
        cookies = Mock()
        seen = []
        def run(context, page, settings, bank, limits, ai=None):
            self.assertIs(page, self.page)
            self.assertIs(context, self.page.context)
            self.assertIs(bank, self.bank)
            self.assertEqual(page.evaluate('document.cookie'), '')
            self.assertEqual(limits.calls, 0)
            limits.api_call()
            seen.append(limits)
            bank.put('chapter', m.Question('saved', 'single', ['yes', 'no']), ['yes'], True)
            raise m.Halt('previous error')
        def operator():
            while True:
                try:
                    event = control.events.get(timeout=5)
                except queue.Empty:
                    control.close()
                    return
                if event['kind'] == 'idle':
                    control.close()
                    return
        thread = threading.Thread(target=operator)
        thread.start()
        try:
            with patch.object(m, 'wait_for_work_page', return_value=self.page), patch.object(m, 'run_course', side_effect=run), patch.object(m, 'save_error_diagnostics'), patch.object(desktop, 'ROOT', Path(self.tmp.name)), contextlib.redirect_stdout(io.StringIO()):
                desktop.run_browser_session(self.page.context, self.page, cfg, self.bank, control, cookies, 'test-key')
        finally:
            thread.join(6)
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.bank.get('chapter', m.Question('saved', 'single', ['yes', 'no'])), ['yes'])
        self.assertTrue(control.close_requested.is_set())
        self.assertFalse(self.page.is_closed())
        cookies.restore.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
