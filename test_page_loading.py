import contextlib
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
from unittest.mock import patch

import mastery as m
import test_mastery as base_tests


class LoadingBrowserTests(base_tests.BrowserTests):
    test_two_rounds_reordered_bank_hit_new_question_and_full_reviews = None
    test_catalogue_dom_resolves_real_hierarchy = None
    test_hidden_recommendation_questions_are_not_collected = None
    test_full_document_navigations_and_retry = None

    def cfg(self):
        return {**base_tests.config(), 'action_delay_seconds': .01, 'timeout_seconds': 4,
                'result_settle_seconds': .08, 'zero_score_grace_seconds': .18}

    def delayed_grade(self, score, initial_score=0):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(handler):
                time.sleep(.6)
                body = json.dumps({'score': score, 'total': 2}).encode()
                handler.send_response(200)
                handler.send_header('Content-Type', 'application/json')
                handler.send_header('Access-Control-Allow-Origin', '*')
                handler.end_headers()
                handler.wfile.write(body)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        thread.start()
        try:
            self.page.goto((Path(__file__).parent/'tests'/'workflow.html').as_uri())
            self.page.evaluate('''({url, initial}) => {
              document.querySelector('.reviewDone').onclick = () => {
                document.querySelector('#quiz').innerHTML = '';
                const result = document.querySelector('.line1-left');
                result.hidden = false;
                result.querySelector('.charts-label-rate').textContent = initial + '%';
                result.querySelector('.line1-count-total-num').textContent = initial ? '2' : '0';
                fetch(url).then(r => r.json()).then(data => {
                  result.querySelector('.charts-label-rate').textContent = data.score + '%';
                  result.querySelector('.line1-count-total-num').textContent = data.total;
                  window.gradeLoaded = true;
                });
              };
            }''', {'url': f'http://127.0.0.1:{server.server_port}/grade', 'initial': initial_score})
            cfg = self.cfg()
            adapter = m.BrowserAdapter(self.page, cfg, m.Limits(cfg))
            adapter.choose_frame()
            adapter.expected_question_count = 2
            adapter.submit()
            self.assertEqual(adapter.score(), score)
            self.assertTrue(self.page.evaluate('window.gradeLoaded === true'))
            self.assertEqual(adapter.result_question_count, 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)

    def test_initial_zero_waits_for_grade_request_to_complete(self):
        self.delayed_grade(95)

    def test_genuine_zero_is_valid_after_result_data_loads(self):
        self.delayed_grade(0)

    def test_unchanged_score_does_not_require_a_value_change(self):
        self.delayed_grade(75, initial_score=75)

    def test_zero_result_refresh_is_read_only_and_bounded(self):
        url = 'https://ai-smart-course-student-pro.zhihuishu.com/point/1/55/123/11/9'
        template = (Path(__file__).parent/'tests'/'navigation-result.html').read_text(encoding='utf-8')
        for final_score in (95, 0):
            with self.subTest(final_score=final_score):
                requests = []
                def serve(route):
                    requests.append(route.request.url)
                    score = 0 if len(requests) == 1 else final_score
                    route.fulfill(status=200, content_type='text/html; charset=utf-8',
                                  body=template.replace("location.search?'95%':'75%'", repr(f'{score}%')))
                self.page.route(url, serve)
                self.page.goto(url)
                cfg = self.cfg()
                adapter = m.BrowserAdapter(self.page, cfg, m.Limits(cfg))
                adapter.choose_frame()
                adapter.expected_question_count = 1
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(adapter.score(), final_score)
                self.assertEqual(len(requests), 2)
                self.page.unroute(url, serve)

    def test_review_and_second_round_wait_for_hydration(self):
        self.page.goto((Path(__file__).parent/'tests'/'workflow.html').as_uri())
        self.page.evaluate('''() => {
          const open = document.querySelector('.line1-count-link');
          const originalOpen = open.onclick;
          open.onclick = () => {
            document.querySelector('.exam-preview').hidden = false;
            document.querySelector('.reviews').innerHTML = '<ul class="question-item"></ul>';
            setTimeout(() => {
              originalOpen();
              const node = document.querySelector('.answer-title');
              const originalAnswer = node.textContent;
              node.textContent = '参考答案：';
              setTimeout(() => node.textContent = originalAnswer, 180);
            }, 180);
          };
          const retry = document.querySelector('.submit');
          const originalRetry = retry.onclick;
          retry.onclick = () => {
            document.querySelector('.exam-preview').hidden = true;
            document.querySelector('.line1-left').hidden = true;
            document.querySelector('#quiz').innerHTML = '<div class="questionContent"></div>';
            setTimeout(originalRetry, 300);
          };
        }''')
        cfg = self.cfg()
        limits = m.Limits(cfg)
        class FakeAI:
            def answer(ai, q, history):
                limits.api_call()
                return (['3'] if q.stem == '2 + 2 = ?' else ['2', '3'] if q.kind == 'multiple' else ['Paris']), 'test'
        adapter = m.BrowserAdapter(self.page, cfg, limits)
        with patch.object(m, 'ROOT', Path(self.tmp.name)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.Runner(adapter, self.bank, FakeAI(), cfg, 'chapter', 'point', limits).run(), 95)
        self.assertEqual(self.bank.db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0], 4)
        self.assertEqual(self.bank.db.execute('SELECT MAX(round) FROM attempts').fetchone()[0], 2)

    def test_replaced_iframe_rebinds_and_waits_for_stem(self):
        question = (Path(__file__).parent/'tests'/'navigation-quiz.html').read_text(encoding='utf-8')
        self.page.set_content('<iframe id="old"></iframe>')
        self.page.evaluate('''question => {
          document.querySelector('#old').srcdoc = '<div class="line1-left">result</div><div class="exam-preview"><div class="submit" onclick="parent.rebuild()">重新答题</div></div>';
          window.rebuild = () => {
            document.querySelector('#old').remove();
            const fresh = document.createElement('iframe');
            fresh.srcdoc = '<div class="questionContent"></div>';
            document.body.appendChild(fresh);
            setTimeout(() => fresh.contentDocument.body.innerHTML = question, 300);
          };
        }''', question)
        self.page.frame_locator('#old').locator('.submit').wait_for()
        cfg = self.cfg()
        adapter = m.BrowserAdapter(self.page, cfg, m.Limits(cfg))
        adapter.choose_frame()
        old_frame = adapter.frame
        adapter.retry()
        self.assertNotEqual(adapter.frame, old_frame)
        self.assertEqual(adapter.questions()[0][0].stem, 'Navigation question')


if __name__ == '__main__':
    import unittest
    unittest.main(verbosity=2)
