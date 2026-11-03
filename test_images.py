import base64
import contextlib
import io
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import mastery as m
import test_mastery as base_tests
from test_mastery import config


# Synthetic pixels; no real course material or private image URLs.
PIXEL_A = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+nmSUAAAAASUVORK5CYII='
PIXEL_B = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='


class ImageAnswerTests(unittest.TestCase):
    def test_distinct_image_urls_with_same_bytes_match_and_remap_options(self):
        first_a, first_b = '【图片:' + 'a'*64 + '】', '【图片:' + 'b'*64 + '】'
        next_a, next_b = '【图片:' + 'c'*64 + '】', '【图片:' + 'd'*64 + '】'
        first = m.Question('示例', 'single', [first_a, first_b], images={first_a: b'first', first_b: b'second'})
        later = m.Question('示例', 'single', [next_b, next_a], images={next_a: b'first', next_b: b'second'})
        self.assertEqual(first.key, later.key)
        with tempfile.TemporaryDirectory() as tmp:
            bank = m.Bank(Path(tmp)/'bank.sqlite3')
            try:
                bank.put('scope', first, [first_a], True)
                bank.attempt('r', 1, 'scope', 'point', first, [first_b], 'ai')
                bank.feedback('r', 1, 'scope', 'point', first, '参考答案：A', 'wrong', 50)
                self.assertEqual(bank.get('scope', later), [next_a])
                self.assertIn({'rejected_answer': [next_b]}, bank.history('scope', later))
                self.assertIn({'correct_answer': [next_a], 'outcome': 'wrong'}, bank.history('scope', later))
            finally:
                bank.close()

    def test_ambiguous_identical_image_options_are_rejected(self):
        a, b = '【图片:' + 'a'*64 + '】', '【图片:' + 'b'*64 + '】'
        with self.assertRaises(m.Halt):
            m.Question('示例', 'single', [a, b], images={a: b'same', b: b'same'})

    def test_history_preserves_option_text_that_starts_with_a_letter(self):
        marker = '【图片:' + 'a'*64 + '】'
        q = m.Question('示例 ' + marker, 'single', ['A. A. literal', 'B. other'], images={marker: b'pixels'})
        with tempfile.TemporaryDirectory() as tmp:
            bank = m.Bank(Path(tmp)/'bank.sqlite3')
            try:
                bank.feedback('run', 1, 'scope', 'point', q, '参考答案：A. literal', 'correct', 100)
                self.assertIn({'correct_answer': ['A. literal'], 'outcome': 'correct'}, bank.history('scope', q))
            finally:
                bank.close()

    def test_existing_url_based_signatures_are_migrated_without_losing_records(self):
        old_marker, new_marker = '【图片:' + 'a'*64 + '】', '【图片:' + 'b'*64 + '】'
        old = m.Question('示例 ' + old_marker, 'single', ['yes', 'no'], images={old_marker: b'pixels'})
        new = m.Question('示例 ' + new_marker, 'single', ['yes', 'no'], images={new_marker: b'pixels'})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'bank.sqlite3'
            bank = m.Bank(path)
            bank.put('scope', old, ['yes'], True)
            bank.attempt('run', 1, 'scope', 'point', old, ['no'], 'ai')
            bank.feedback('run', 1, 'scope', 'point', old, '参考答案：A', 'wrong', 70)
            legacy = m.question_signature(old.stem, old.kind, old.options, old.blanks)
            for table in ('bank', 'attempts', 'reviews'):
                bank.db.execute('UPDATE '+table+' SET signature=?', (legacy,))
            bank.db.commit()
            bank.close()
            bank = m.Bank(path)
            try:
                self.assertEqual(bank.get('scope', new), ['yes'])
                self.assertIn({'rejected_answer': ['no']}, bank.history('scope', new))
                for table in ('bank', 'attempts', 'reviews'):
                    self.assertEqual(bank.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0], 1)
                    self.assertEqual(bank.db.execute('SELECT signature FROM '+table).fetchone()[0], new.key)
            finally:
                bank.close()

    def test_changed_image_content_never_reuses_existing_bank_answer(self):
        marker = '【图片:' + 'a' * 64 + '】'
        first = m.Question('示例 ' + marker, 'single', ['yes', 'no'], images={marker: b'image-one'})
        changed = m.Question('示例 ' + marker, 'single', ['yes', 'no'], images={marker: b'image-two'})
        with tempfile.TemporaryDirectory() as tmp:
            bank = m.Bank(Path(tmp)/'bank.sqlite3')
            try:
                bank.put('scope', first, ['yes'], True)
                bank.attempt('r', 1, 'scope', 'point', first, ['no'], 'ai')
                bank.feedback('r', 1, 'scope', 'point', first, '参考答案：A', 'wrong', 50)
                self.assertEqual(bank.get('scope', first), ['yes'])
                self.assertIsNone(bank.get('scope', changed))
                self.assertEqual(bank.history('scope', changed), [])
                self.assertTrue(bank.history('scope', first))
            finally:
                bank.close()
    def test_api_receives_image_bytes_with_matching_marker_and_no_source_url(self):
        marker = '【图片:' + 'a' * 64 + '】'
        q = m.Question('示例 ' + marker, 'single', ['red', 'blue'], images={marker: b'\x89PNG\r\n\x1a\nexample'})
        response = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'answers': ['A'], 'confidence': .9})}}]}
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(response).encode())) as request, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.DeepSeek(config(), m.Limits(config()), api_key='unit-test').answer(q, [])[0], ['red'])
        payload = json.loads(request.call_args.args[0].data)
        content = payload['messages'][1]['content']
        self.assertEqual(content[0]['type'], 'text')
        self.assertIn(marker, content[0]['text'])
        self.assertIn(marker, content[1]['text'])
        self.assertEqual(content[2]['type'], 'image_url')
        self.assertEqual(base64.b64decode(content[2]['image_url']['url'].split(',', 1)[1]), q.images[marker])
        self.assertEqual(content[2]['image_url']['detail'], 'original')
        self.assertNotIn('data:image', json.dumps(q.payload()))

    def test_text_only_request_keeps_original_string_format(self):
        q = m.Question('plain', 'single', ['yes', 'no'])
        response = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'answers': ['A'], 'confidence': .9})}}]}
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(response).encode())) as request:
            m.DeepSeek(config(), m.Limits(config()), api_key='unit-test').answer(q, [])
        self.assertIsInstance(json.loads(request.call_args.args[0].data)['messages'][1]['content'], str)


class ImageBrowserTests(base_tests.BrowserTests):
    test_two_rounds_reordered_bank_hit_new_question_and_full_reviews = None
    test_catalogue_dom_resolves_real_hierarchy = None
    test_hidden_recommendation_questions_are_not_collected = None
    test_full_document_navigations_and_retry = None

    def set_quiz(self):
        self.page.set_content(f'''<style>img{{width:22px;height:22px}}</style>
          <div class="questionContent"><div class="questionName"><div class="questionTitle"><span class="letterSortNum">单选题</span></div>
          <div class="centent-pre">公式 <img src="data:image/png;base64,{PIXEL_A}"> 是否正确？</div></div>
          <ul class="radio-view"><li><i></i><div class="stem">选项 <img src="data:image/png;base64,{PIXEL_B}"></div></li>
          <li><i></i><div class="stem">其它</div></li></ul></div>''')
        cfg = {**config(), 'timeout_seconds': 3}
        adapter = m.BrowserAdapter(self.page, cfg, m.Limits(cfg))
        adapter.choose_frame()
        return adapter

    def test_extracts_inline_formula_and_image_option_as_separate_images(self):
        adapter = self.set_quiz()
        q = adapter.questions()[0][0]
        self.assertEqual(q.kind, 'single')
        self.assertEqual(len(q.images), 2)
        self.assertIn('公式', q.stem)
        self.assertIn('是否正确', q.stem)
        self.assertIn('【图片:', q.stem)
        self.assertIn('【图片:', q.options[0])
        self.assertNotIn('data:image', json.dumps(q.payload()))
        self.assertTrue(all(png.startswith(b'\x89PNG\r\n\x1a\n') for png in q.images.values()))
        self.assertEqual(m.model_answer(q, {'answers': ['A'], 'confidence': .9})[0], [q.options[0]])

    def test_same_sources_match_review_and_verified_bank(self):
        adapter = self.set_quiz()
        q = adapter.questions()[0][0]
        self.page.set_content(f'''<style>img{{width:22px;height:22px}}</style><section class="exam-preview"><ul class="question-item">
          <div class="quest-type">单选题</div><div class="quest-title"><p class="option-name">公式 <img src="data:image/png;base64,{PIXEL_A}"> 是否正确？</p></div>
          <label class="el-radio"><span class="preStyle">选项 <img src="data:image/png;base64,{PIXEL_B}"></span></label>
          <label class="el-radio"><span class="preStyle">其它</span></label>
          <div class="analysis"><p class="answer-title">参考答案：A</p></div></ul></section>''')
        reviewed, _ = adapter.questions(review=True)[0]
        self.assertEqual(q.key, reviewed.key)
        answer = m.review_answer(reviewed, '参考答案：A')
        self.bank.put('chapter', reviewed, answer, True)
        self.assertEqual(self.bank.get('chapter', q), [q.options[0]])
        payload = self.bank.db.execute('SELECT question FROM bank').fetchone()[0]
        self.assertNotIn(PIXEL_A, payload)
        self.assertNotIn(PIXEL_B, payload)

    def test_different_urls_with_identical_pixels_match_review_and_image_options(self):
        m.watch_image_responses(self.page.context)
        def fulfill(route):
            image = PIXEL_B if 'option' in route.request.url else PIXEL_A
            route.fulfill(status=200, content_type='image/png', body=base64.b64decode(image))
        self.page.route('https://formula.invalid/**', fulfill)
        self.page.set_content('''<div class="questionContent"><div class="questionName"><div class="questionTitle"><span class="letterSortNum">单选题</span></div>
          <div class="centent-pre">公式 <img src="https://formula.invalid/quiz.png"> 是否正确？</div></div>
          <ul class="radio-view"><li><i></i><div class="stem">选项 <img src="https://formula.invalid/quiz-option.png"></div></li>
          <li><i></i><div class="stem">其它</div></li></ul></div>''')
        cfg = {**config(), 'timeout_seconds': 3}
        adapter = m.BrowserAdapter(self.page, cfg, m.Limits(cfg))
        adapter.choose_frame()
        original = adapter.questions()[0][0]
        self.page.set_content('''<section class="exam-preview"><ul class="question-item">
          <div class="quest-type">单选题</div><div class="quest-title"><p class="option-name">公式 <img src="https://formula.invalid/review.png"> 是否正确？</p></div>
          <label class="el-radio"><span class="preStyle">选项 <img src="https://formula.invalid/review-option.png"></span></label>
          <label class="el-radio"><span class="preStyle">其它</span></label>
          <div class="analysis"><p class="answer-title">参考答案：A</p></div></ul></section>''')
        reviewed, _ = adapter.questions(review=True)[0]
        self.assertNotEqual(original.stem, reviewed.stem)
        self.assertNotEqual(original.options[0], reviewed.options[0])
        self.assertEqual(original.key, reviewed.key)
        self.bank.put('chapter', reviewed, m.review_answer(reviewed, '参考答案：A'), True)
        self.assertEqual(self.bank.get('chapter', original), [original.options[0]])

    def test_broken_image_waits_instead_of_sending_incomplete_question(self):
        self.page.set_content('''<div class="questionContent"><div class="questionName"><div class="questionTitle"><span class="letterSortNum">单选题</span></div>
          <div class="centent-pre">问题<img src="data:image/png;base64,invalid"></div></div>
          <ul class="radio-view"><li><div class="stem">是</div></li><li><div class="stem">否</div></li></ul></div>''')
        cfg = {**config(), 'timeout_seconds': .4}
        adapter = m.BrowserAdapter(self.page, cfg, m.Limits(cfg))
        adapter.choose_frame()
        with self.assertRaises(m.Retryable):
            adapter.questions()

    def test_two_round_workflow_answers_image_question_and_reuses_verified_answer(self):
        m.watch_image_responses(self.page.context)
        self.page.route('https://formula.invalid/**', lambda route: route.fulfill(
            status=200, content_type='image/png', body=base64.b64decode(PIXEL_A)))
        self.page.goto((Path(__file__).parent/'tests'/'workflow.html').as_uri())
        self.page.evaluate('''() => {
          first.stem = '模拟公式 <img style="width:22px;height:22px" src="https://formula.invalid/quiz.png"> 的结果？';
          reordered.stem = '模拟公式 <img style="width:22px;height:22px" src="https://formula.invalid/repeat.png"> 的结果？';
          const review = document.querySelector('.line1-count-link');
          const original = review.onclick;
          review.onclick = () => { original(); document.querySelectorAll('.option-name img').forEach(img => img.src = 'https://formula.invalid/review.png'); };
          show();
        }''')
        cfg = {**config(), 'action_delay_seconds': .01, 'timeout_seconds': 4, 'result_settle_seconds': .05}
        limits = m.Limits(cfg)
        seen = []
        class AI:
            def answer(ai, q, history):
                limits.api_call()
                seen.append(q)
                return (['3'] if q.kind == 'single' else ['2', '3'] if q.kind == 'multiple' else ['Paris']), ''
        adapter = m.BrowserAdapter(self.page, cfg, limits)
        with patch.object(m, 'ROOT', Path(self.tmp.name)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.Runner(adapter, self.bank, AI(), cfg, 'chapter', 'point', limits).run(), 95)
        self.assertEqual(len(seen), 3)
        self.assertEqual(len(seen[0].images), 1)
        self.assertEqual(self.bank.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 4)
        self.assertEqual(self.bank.db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0], 4)
        self.assertEqual(self.bank.db.execute("SELECT COUNT(*) FROM attempts WHERE source='bank'").fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
