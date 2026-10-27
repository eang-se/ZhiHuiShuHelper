import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import mastery as m


def config():
    return json.loads((Path(__file__).parent/'profile.polymas.json').read_text(encoding='utf-8'))


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bank = m.Bank(Path(self.tmp.name)/'bank.sqlite3')
        self.q = m.Question('Which number?', 'single', ['A. 1', 'B. 2'])

    def tearDown(self):
        self.bank.close()
        self.tmp.cleanup()

    def test_options_reordered_match_but_changed_do_not(self):
        self.assertEqual(self.q.key, m.Question('Which number?', 'single', ['2', '1']).key)
        self.assertNotEqual(self.q.key, m.Question('Which number?', 'single', ['1', '3']).key)

    def test_case_and_math_are_not_destroyed(self):
        self.assertNotEqual(m.Question('x + X', 'single', ['Yes', 'No']).key,
                            m.Question('x + x', 'single', ['Yes', 'No']).key)

    def test_ai_predictions_not_reused_until_verified(self):
        self.bank.put('c', self.q, ['1'], False)
        self.assertIsNone(self.bank.get('c', self.q))
        self.bank.put('c', self.q, ['2'], True)
        self.assertEqual(self.bank.get('c', m.Question('Which number?', 'single', ['2', '1'])), ['2'])

    def test_scopes_and_invalidated_answers(self):
        self.bank.put('chapter1', self.q, ['1'], True)
        self.assertIsNone(self.bank.get('chapter2', self.q))
        self.bank.invalidate('chapter1', self.q)
        self.assertIsNone(self.bank.get('chapter1', self.q))

    def test_all_attempts_reviews_and_rejected_answers_are_archived(self):
        self.bank.attempt('r', 1, 'c', 'point', self.q, ['1'], 'ai')
        self.bank.feedback('r', 1, 'c', 'point', self.q, '参考答案：B', 'wrong', 75)
        self.assertIn({'rejected_answer': ['1']}, self.bank.history('c', self.q))
        out = Path(self.tmp.name)/'export.json'
        self.bank.export(out)
        chapter = json.loads(out.read_text(encoding='utf-8'))['chapters']['c']
        self.assertEqual(chapter['attempts'][0]['answer'], ['1'])
        self.assertEqual(chapter['reviews'][0]['mastery'], 75)

    def test_mastery_requires_unique_value(self):
        self.assertEqual(m.parse_mastery('75%'), 75)
        self.assertEqual(m.parse_mastery('掌握度：90.5%'), 90.5)
        for value in ['最好成绩 100%', '6题 答对5题 75%', '75% 95%', '101%', '-2%', 'NaN']:
            with self.subTest(value=value), self.assertRaises(m.Halt):
                m.parse_mastery(value)

    def test_ai_output_cardinality_range_and_confidence(self):
        answer, _, _ = m.model_answer(self.q, {'answers': ['B'], 'confidence': .9})
        self.assertEqual(answer, ['2'])
        for values in [['Z'], ['A', 'B'], [], ['B', 'B']]:
            with self.subTest(values=values), self.assertRaises(m.Halt):
                m.model_answer(self.q, {'answers': values, 'confidence': .9})
        with self.assertRaises(m.Halt):
            m.model_answer(self.q, {'answers': ['B'], 'confidence': float('nan')})

    def test_multi_and_text_answers(self):
        multi = m.Question('prime', 'multiple', ['2', '3', '4'])
        self.assertEqual(m.review_answer(multi, '参考答案：A、B'), ['2', '3'])
        self.assertIsNone(m.review_answer(multi, '参考答案：Z'))
        text = m.Question('fill', 'text', [], 2)
        self.assertEqual(m.review_answer(text, '参考答案：one||two'), ['one', 'two'])
        self.assertIsNone(m.review_answer(text, 'one and two'))

    def test_config_enforces_limits_and_required_selectors(self):
        cfg = config()
        m.validate_config(cfg)
        for key, value in [('max_rounds', 0), ('max_rounds', 1.5), ('max_api_calls', 0), ('max_minutes', float('nan'))]:
            altered = {**cfg, key: value}
            with self.subTest(key=key, value=value), self.assertRaises(m.Halt):
                m.validate_config(altered)

    def test_platform_url_ids_are_stable_across_new_papers(self):
        a = m.platform_ids('https://studentexamcomh5.zhihuishu.com/studentReviewTestOrExam/123/1/1/456/x/1?pointId=789&classId=234&paperId=1')
        b = m.platform_ids('https://ai-smart-course-student-pro.zhihuishu.com/point/456/999/123/789/234?nodeName=test')
        self.assertEqual(a, b)
        with self.assertRaises(m.Halt):
            m.platform_ids('https://example.com/point/456/999/123/789/234')

    def test_api_limits_count_each_request(self):
        limits = m.Limits({**config(), 'max_api_calls': 1})
        limits.api_call()
        with self.assertRaises(m.Halt):
            limits.api_call()

    def test_catalogue_groups_by_chapter_id(self):
        meta = {'course_id': '1', 'chapter_id': '2', 'knowledge_id': '3', 'chapter_title': 'chapter', 'course_title': 'course'}
        one = self.bank.catalogue(meta)
        two = self.bank.catalogue({**meta, 'knowledge_id': '4'})
        self.assertEqual(one, two)
        out = Path(self.tmp.name)/'export.json'
        self.bank.export(out)
        self.assertEqual(len(json.loads(out.read_text(encoding='utf-8'))['chapters'][one]['knowledge_points']), 2)

    def test_api_json_and_http_retry_accounting(self):
        cfg = config()
        limits = m.Limits(cfg)
        raw = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'answers': ['B'], 'confidence': .95})}}]}
        error = urllib.error.HTTPError(m.API_URL, 429, 'rate limited', {}, None)
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'unit-test-key'}), \
             patch('urllib.request.urlopen', side_effect=[error, io.BytesIO(json.dumps(raw).encode())]) as mocked, \
             patch('time.sleep'):
            answer, _ = m.DeepSeek(cfg, limits).answer(self.q, [])
        self.assertEqual(answer, ['2'])
        self.assertEqual(limits.calls, 2)
        payload = json.loads(mocked.call_args.args[0].data)
        self.assertEqual(payload['response_format'], {'type': 'json_object'})

    def test_api_rejects_truncated_output(self):
        raw = {'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}]}
        cfg = config()
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'unit-test-key'}), \
             patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(raw).encode())), self.assertRaises(m.Halt):
            m.DeepSeek(cfg, m.Limits(cfg)).answer(self.q, [])

    def test_low_confidence_continues_as_unverified_suggestion(self):
        raw = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'answers': ['B'], 'confidence': .2, 'explanation': 'uncertain'})}}]}
        cfg = config()
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'unit-test-key'}), \
             patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(raw).encode())), \
             contextlib.redirect_stdout(io.StringIO()):
            values, explanation = m.DeepSeek(cfg, m.Limits(cfg)).answer(self.q, [])
        self.assertEqual(values, ['2'])
        self.assertIn('20%', explanation)
        self.bank.put('chapter', self.q, values, False, explanation)
        self.assertIsNone(self.bank.get('chapter', self.q))

    def test_low_confidence_pause_can_continue_or_stop(self):
        raw = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'answers': ['B'], 'confidence': .2})}}]}
        cfg = {**config(), 'low_confidence_action': 'pause'}
        for decision in ['', 'q']:
            with self.subTest(decision=decision), patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'unit-test-key'}), \
                 patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(raw).encode())), \
                 patch('builtins.input', return_value=decision), contextlib.redirect_stdout(io.StringIO()):
                if decision:
                    with self.assertRaises(m.Halt):
                        m.DeepSeek(cfg, m.Limits(cfg)).answer(self.q, [])
                else:
                    self.assertEqual(m.DeepSeek(cfg, m.Limits(cfg)).answer(self.q, [])[0], ['2'])

    def test_low_confidence_stop_remains_available(self):
        raw = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'answers': ['B'], 'confidence': .2})}}]}
        cfg = {**config(), 'low_confidence_action': 'stop'}
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'unit-test-key'}), \
             patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(raw).encode())), \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaises(m.Halt):
            m.DeepSeek(cfg, m.Limits(cfg)).answer(self.q, [])

    def test_errors_keep_browser_open_until_user_acknowledges(self):
        class FakeContext:
            pages = [object()]
        context = FakeContext()
        with patch('builtins.input', return_value='') as prompt, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            m.hold_browser_on_error(context, 'run', config(), m.Halt('test'))
        prompt.assert_called_once()
        self.assertEqual(len(context.pages), 1)
        with patch('builtins.input') as prompt:
            m.hold_browser_on_error(context, 'run', {**config(), 'keep_browser_on_error': False}, m.Halt('test'))
        prompt.assert_not_called()


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        browser = os.environ.get('TEST_BROWSER', 'msedge' if os.name == 'nt' else 'chromium')
        options = {} if browser == 'chromium' else {'channel': browser}
        cls.browser = cls.pw.chromium.launch(headless=True, **options)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.tmp = tempfile.TemporaryDirectory()
        self.bank = m.Bank(Path(self.tmp.name)/'bank.sqlite3')

    def tearDown(self):
        self.page.close()
        self.bank.close()
        self.tmp.cleanup()

    def test_two_rounds_reordered_bank_hit_new_question_and_full_reviews(self):
        self.page.goto((Path(__file__).parent/'tests'/'workflow.html').as_uri())
        cfg = {**config(), 'action_delay_seconds': .01, 'timeout_seconds': 3}
        limits = m.Limits(cfg)

        class FakeAI:
            calls = 0
            def answer(ai, q, history):
                limits.api_call()
                ai.calls += 1
                if q.stem == '2 + 2 = ?':
                    return ['3'], 'Deliberately wrong on first attempt'
                if q.kind == 'multiple':
                    return ['2', '3'], 'Prime numbers'
                return ['Paris'], 'Capital'

        ai = FakeAI()
        adapter = m.BrowserAdapter(self.page, cfg, limits)
        runner = m.Runner(adapter, self.bank, ai, cfg, 'demo-chapter', 'demo-point', limits)
        with patch.object(m, 'ROOT', Path(self.tmp.name)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.run(), 95)
        self.assertEqual(ai.calls, 3)
        self.assertEqual(self.bank.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 4)
        self.assertEqual(self.bank.db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0], 4)
        self.assertEqual(self.bank.db.execute("SELECT COUNT(*) FROM attempts WHERE source='bank'").fetchone()[0], 1)
        self.assertEqual(self.bank.get('demo-chapter', m.Question('2 + 2 = ?', 'single', ['4', '3'])), ['4'])
        self.assertEqual(self.page.locator('.charts-label-rate').inner_text(), '95%')

    def test_catalogue_dom_resolves_real_hierarchy(self):
        self.page.set_content('''<div class="single-title">课程名</div><div id="knowledgeItem-222">
          <div class="content-list-title"><div class="title-left-text">章节名</div></div>
          <div class="conlist-sub-content"><div class="title-left-sub-text">单元名</div>
          <div id="knowledgeId-333"><div class="item-title">知识点名</div></div></div></div>''')
        meta = self.page.evaluate(m.CATALOGUE_SCRIPT, '333')
        self.assertEqual(meta['chapter_id'], '222')
        self.assertEqual(meta['chapter_title'], '章节名')
        self.assertEqual(meta['unit_title'], '单元名')
        self.assertEqual(meta['knowledge_title'], '知识点名')

    def test_hidden_recommendation_questions_are_not_collected(self):
        self.page.goto((Path(__file__).parent/'tests'/'workflow.html').as_uri())
        cfg = config()
        adapter = m.BrowserAdapter(self.page, cfg, m.Limits(cfg))
        adapter.choose_frame()
        self.assertEqual([q.stem for q, _ in adapter.questions()], ['2 + 2 = ?'])

    def test_full_document_navigations_and_retry(self):
        self.page.goto((Path(__file__).parent/'tests'/'navigation-quiz.html').as_uri())
        cfg = {**config(), 'action_delay_seconds': .01, 'timeout_seconds': 3}
        limits = m.Limits(cfg)

        class FakeAI:
            def answer(ai, q, history):
                limits.api_call()
                return ['Yes'], 'Test answer'

        adapter = m.BrowserAdapter(self.page, cfg, limits)
        with patch.object(m, 'ROOT', Path(self.tmp.name)), contextlib.redirect_stdout(io.StringIO()):
            score = m.Runner(adapter, self.bank, FakeAI(), cfg, 'chapter', 'point', limits).run()
        self.assertEqual(score, 95)
        self.assertEqual(limits.calls, 1)
        self.assertEqual(self.bank.db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
