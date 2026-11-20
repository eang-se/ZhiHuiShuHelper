import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import tkinter as tk
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import desktop
import mastery as m
from runtime_control import EventLog, RunControl, Stopped
import test_mastery as base_tests
from test_mastery import config
from private_state import PrivateStore


class ControlTests(unittest.TestCase):
    def test_pause_resume_and_stop(self):
        control = RunControl()
        control.pause()
        entered, finished = threading.Event(), threading.Event()
        result = []
        def wait():
            entered.set()
            result.append(control.checkpoint())
            finished.set()
        thread = threading.Thread(target=wait)
        thread.start()
        self.assertTrue(entered.wait(1))
        self.assertFalse(finished.wait(.1))
        control.resume()
        self.assertTrue(finished.wait(1))
        thread.join()
        self.assertGreater(result[0], 0)
        control.stop()
        with self.assertRaises(Stopped):
            m.Limits(config(), control).api_call()

    def test_stop_releases_paused_worker(self):
        control = RunControl()
        control.pause()
        stopped = threading.Event()
        def wait():
            try:
                control.checkpoint()
            except Stopped:
                stopped.set()
        thread = threading.Thread(target=wait)
        thread.start()
        control.stop()
        self.assertTrue(stopped.wait(1))
        thread.join()

    def test_log_lines_are_flushed(self):
        control = RunControl()
        log = EventLog(control)
        log.write('one\ntwo')
        log.flush()
        self.assertEqual([control.events.get_nowait()['message'] for _ in range(2)], ['one', 'two'])

    def test_sequence_skips_and_chapter_boundary(self):
        points = [{'knowledge_id': str(i), 'chapter_id': 'a' if i<4 else 'b', 'mastery': score}
                  for i, score in enumerate([10, 90, None, 20, 30])]
        target, skipped = m.following_points(points, points[0], 90, 'course')
        self.assertEqual(target['knowledge_id'], '2')
        self.assertEqual([reason for _, reason in skipped], ['已达标'])
        self.assertEqual(m.following_points(points, points[2], 90, 'course')[0]['knowledge_id'], '3')
        self.assertIsNone(m.following_points(points, points[3], 90, 'chapter')[0])
        self.assertEqual(m.following_points(points, points[3], 90, 'course')[0]['knowledge_id'], '4')
        with self.assertRaises(m.Halt):
            m.following_points(points+points[:1], points[0], 90, 'course')


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.private_tmp = tempfile.TemporaryDirectory()
        self.app = desktop.App(self.root, config(), private_store=PrivateStore(Path(self.private_tmp.name)))

    def tearDown(self):
        self.root.update_idletasks()
        self.root.destroy()
        self.private_tmp.cleanup()

    def test_event_updates_and_no_duplicate_completion_count(self):
        point = {'knowledge_id': '11', 'knowledge_title': '知识点', 'course_title': '课程', 'chapter_title': '章节', 'unit_title': '单元'}
        self.app.handle_event({'kind': 'metadata', **point})
        self.app.handle_event({'kind': 'round', 'round': 2})
        self.app.handle_event({'kind': 'score', 'mastery': 95})
        self.app.handle_event({'kind': 'answered', 'source': 'bank'})
        self.app.handle_event({'kind': 'metrics', 'api_calls': 3})
        for _ in range(2):
            self.app.handle_event({'kind': 'point_done', 'point': point, 'mastery': 95})
        self.assertEqual(self.app.point.get(), '知识点')
        self.assertEqual(self.app.score.get(), 95)
        self.assertEqual(self.app.counts['done'], 1)
        self.assertEqual(self.app.counts['bank'], 1)
        self.assertEqual(self.app.counts['api_calls'], 3)
        self.assertEqual(self.app.tree.item('11', 'values')[-1], '已达标')

    def test_preferences_exclude_api_key_and_preserve_limits(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(desktop, 'ROOT', Path(tmp)):
            cfg = self.app.preferences()
            desktop.save_preferences({**cfg, 'api_key': 'never-save', 'DEEPSEEK_API_KEY': 'never-save'})
            data = json.loads((Path(tmp)/'profile.json').read_text(encoding='utf-8'))
            self.assertNotIn('api_key', data)
            self.assertNotIn('DEEPSEEK_API_KEY', data)
            self.assertTrue(data['auto_next'])
            self.assertEqual(data['next_scope'], 'course')

    def test_status_hint_and_bottom_controls_fit_default_window(self):
        self.root.deiconify()
        self.root.update()
        # The footer was previously packed after a tall expandable pane and could vanish.
        labels = []
        def collect(widget):
            for child in widget.winfo_children():
                if 'textvariable' in child.keys() and str(child.cget('textvariable')) == str(self.app.message):
                    labels.append(child)
                collect(child)
        collect(self.root)
        self.assertEqual(len(labels), 1)
        self.assertTrue(labels[0].winfo_ismapped())
        self.assertGreater(labels[0].winfo_height(), 5)


class SequenceBrowserTests(base_tests.BrowserTests):
    # Reuse the browser fixture without inheriting the earlier tests twice.
    test_two_rounds_reordered_bank_hit_new_question_and_full_reviews = None
    test_catalogue_dom_resolves_real_hierarchy = None
    test_hidden_recommendation_questions_are_not_collected = None
    test_full_document_navigations_and_retry = None

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.tmp = tempfile.TemporaryDirectory()
        self.bank = m.Bank(Path(self.tmp.name)/'bank.sqlite3')

    def tearDown(self):
        self.context.close()
        self.bank.close()
        self.tmp.cleanup()

    def prepare(self, max_calls=100, construction=False, missing_score=False):
        context = self.page.context
        point_data = [('11', '101', 0), ('12', '101', 99), ('13', '102', 0), ('14', '102', None if missing_score else (0 if construction else 99))]
        blocks = []
        for chapter in ('101', '102'):
            cards = ''.join(f'<div knowledgeid="{pid}" id="knowledgeId-{pid}"><div class="item-title">Point {pid}</div>'
                            f'<div class="item-mask" style="height:30px;width:100px" onclick="location.href=\'/resource/{pid}\'">Open</div>'
                            +(f'<div role="progressbar" aria-valuenow="{score}"></div>' if score is not None else '')+'</div>'
                            for pid, cid, score in point_data if cid==chapter)
            blocks.append(f'<div id="knowledgeItem-{chapter}"><div class="content-list-title"><div class="title-left-text">Chapter {chapter}</div></div>'
                          f'<div class="conlist-sub-content"><div class="title-left-sub-text">Unit</div>{cards}</div></div>')
        graph = '<meta charset="utf-8"><div class="single-title">Course</div>'+''.join(blocks)
        fixture_dir = Path(__file__).parent/'tests'

        def route(request):
            url = urlparse(request.request.url)
            parts = url.path.strip('/').split('/')
            if parts[0] == 'singleCourse':
                html = graph
            elif parts[0] == 'resource' and construction == 'delayed' and parts[1] == '13':
                html = """<meta charset='utf-8'><div id='status'><span>知识点最高掌握度：</span><b>0%</b><button>建设中</button></div><script>setTimeout(() => {document.getElementById('status').innerHTML = `<button onclick=\"location.href='/masteryHistory/1/9/13'\">去提升</button>`}, 800);</script>"""
            elif parts[0] == 'resource' and construction and parts[1] == '13':
                html = '<meta charset="utf-8"><div>Some long resource description to ensure the parent is not matched as a short message</div><div style="display:flex;align-items:center;gap:8px"><span>知识点最高掌握度：</span><b>0%</b><button>建设中 ›</button></div>'
            elif parts[0] == 'resource':
                html = f'<meta charset="utf-8"><button onclick="location.href=\'/masteryHistory/1/9/{parts[1]}\'">去提升</button>'
            elif parts[0] == 'masteryHistory':
                pid = parts[-1]
                quiz = f'https://studentexamcomh5.zhihuishu.com/studentReviewTestOrExam/123/1/1/1/x/1?pointId={pid}&classId=9'
                html = f'<meta charset="utf-8"><div class="improve-btn" onclick="location.href=\'{quiz}\'">去提升 →</div>'
            elif parts[0] == 'studentReviewTestOrExam':
                pid = parse_qs(url.query)['pointId'][0]
                html = (fixture_dir/'navigation-quiz.html').read_text(encoding='utf-8')
                html = html.replace("'navigation-result.html'+location.search", f"'https://ai-smart-course-student-pro.zhihuishu.com/point/1/55/123/{pid}/9'")
            elif parts[0] == 'point':
                pid = parts[4]
                html = (fixture_dir/'navigation-result.html').read_text(encoding='utf-8')
                html = html.replace("location.search?'95%':'75%'", "'95%'")
                html = html.replace("'navigation-review.html'+location.search", f"'/examPreview/1/55/123/{pid}/9'")
            else:
                html = (fixture_dir/'navigation-review.html').read_text(encoding='utf-8')
            request.fulfill(status=200, content_type='text/html; charset=utf-8', body=html)

        context.route('https://ai-smart-course-student-pro.zhihuishu.com/**', route)
        context.route('https://studentexamcomh5.zhihuishu.com/**', route)
        self.page.goto('https://studentexamcomh5.zhihuishu.com/studentReviewTestOrExam/123/1/1/1/x/1?pointId=11&classId=9')
        cfg = {**config(), 'max_api_calls': max_calls, 'action_delay_seconds': .01, 'timeout_seconds': 3, 'auto_next': True, 'next_scope': 'course'}
        meta = {'course_id': '1', 'class_id': '9', 'knowledge_id': '11', 'chapter_id': '101', 'knowledge_title': 'Point 11', 'chapter_title': 'Chapter 101', 'unit_title': 'Unit', 'course_title': 'Course'}
        control = RunControl()
        limits = m.Limits(cfg, control)
        class FakeAI:
            def answer(ai, q, history):
                limits.api_call()
                return ['Yes'], 'test'
        return context, cfg, meta, control, limits, FakeAI()

    def test_complete_two_points_across_chapters_and_skip(self):
        context, cfg, meta, control, limits, ai = self.prepare()
        with patch.object(m, 'ROOT', Path(self.tmp.name)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.run_course(context, self.page, cfg, self.bank, limits, ai=ai, metadata=meta), 95)
        events = []
        while not control.events.empty():
            events.append(control.events.get_nowait())
        self.assertEqual([e['point']['knowledge_id'] for e in events if e['kind']=='point_done'], ['11', '13'])
        self.assertEqual([e['point']['knowledge_id'] for e in events if e['kind']=='point_skipped'], ['12', '14'])
        self.assertEqual(limits.calls, 2)
        self.assertEqual(self.bank.db.execute('SELECT COUNT(DISTINCT scope) FROM bank').fetchone()[0], 2)

    def test_missing_mastery_does_not_skip_a_working_quiz(self):
        context, cfg, meta, control, limits, ai = self.prepare(missing_score=True)
        with patch.object(m, 'ROOT', Path(self.tmp.name)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.run_course(context, self.page, cfg, self.bank, limits, ai=ai, metadata=meta), 95)
        events = []
        while not control.events.empty():
            events.append(control.events.get_nowait())
        self.assertEqual([e['point']['knowledge_id'] for e in events if e['kind'] == 'point_done'], ['11', '13', '14'])
        self.assertNotIn('14', [e['point']['knowledge_id'] for e in events if e['kind'] == 'point_skipped'])

    def test_temporary_construction_waits_then_enters_quiz(self):
        context, cfg, meta, control, limits, ai = self.prepare(construction='delayed')
        with patch.object(m, 'ROOT', Path(self.tmp.name)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.run_course(context, self.page, cfg, self.bank, limits, ai=ai, metadata=meta), 95)
        events = []
        while not control.events.empty():
            events.append(control.events.get_nowait())
        self.assertIn('13', [e['point']['knowledge_id'] for e in events if e['kind'] == 'point_done'])
        self.assertNotIn('13', [e['point']['knowledge_id'] for e in events if e['kind'] == 'point_skipped'])

    def test_persistent_construction_waits_for_manual_retry_instead_of_skip(self):
        context, cfg, meta, control, limits, ai = self.prepare(construction=True)
        cfg['timeout_seconds'] = 1
        nav = m.CourseNavigator(context, self.page, cfg, limits)
        with patch.object(control, 'wait_for_retry', side_effect=Stopped('manual stop')) as wait, contextlib.redirect_stdout(io.StringIO()), self.assertRaises(Stopped):
            nav.advance(meta)
        self.assertIn('可能是加载占位', wait.call_args.args[1])
        events = []
        while not control.events.empty():
            events.append(control.events.get_nowait())
        self.assertNotIn('13', [e['point']['knowledge_id'] for e in events if e['kind'] == 'point_skipped'])

    def test_unrelated_construction_text_does_not_skip(self):
        self.page.set_content('<div>视频建设中</div><div style="display:none">题库建设中</div>')
        self.assertFalse(m.CourseNavigator.under_construction(self.page.locator('body')))
        self.page.set_content('<div>题库建设中</div>')
        self.assertTrue(m.CourseNavigator.under_construction(self.page.locator('body')))

    def test_mastery_construction_button_requires_nearby_visible_label(self):
        self.page.set_content('<div style="display:flex;align-items:center;gap:10px"><span>知识点最高掌握度：</span><b>0%</b><button>建设中 ›</button></div>')
        self.assertTrue(m.CourseNavigator.under_construction(self.page.locator('body')))
        self.page.set_content('<div>知识点最高掌握度：0%<button>建设中</button></div>')
        self.assertTrue(m.CourseNavigator.under_construction(self.page.locator('body')))
        self.page.set_content('<div>知识点最高掌握度： 0%</div><div style="margin-top:100px"><button>建设中</button></div>')
        self.assertFalse(m.CourseNavigator.under_construction(self.page.locator('body')))
        self.page.set_content('<div style="display:flex"><span>知识点最高掌握度：</span><button style="display:none">建设中</button></div>')
        self.assertFalse(m.CourseNavigator.under_construction(self.page.locator('body')))
        self.page.set_content('<div style="display:flex"><span>知识点最高掌握度：</span><button>视频建设中</button></div>')
        self.assertFalse(m.CourseNavigator.under_construction(self.page.locator('body')))

    def test_manual_new_tab_or_same_tab_restarts_with_new_metadata(self):
        for mode in ('new_tab', 'same_tab'):
            with self.subTest(mode=mode):
                context, cfg, meta, control, limits, ai = self.prepare()
                cfg['auto_next'] = False
                done = []
                def wait_for_retry(stage, message, **kwargs):
                    selected = context.new_page() if mode == 'new_tab' else self.page
                    selected.goto('https://studentexamcomh5.zhihuishu.com/studentReviewTestOrExam/123/1/1/1/x/1?pointId=13&classId=9')
                    return 0
                def run_point(runner):
                    done.append((runner.knowledge, runner.scope, runner.adapter.page))
                    if runner.knowledge == '11':
                        limits.api_call()
                        q = m.Question('Synthetic question', 'single', ['Yes', 'No'])
                        self.bank.attempt('test-run', 1, runner.scope, '11', q, ['Yes'], 'ai')
                        return limits.retry_step('读取当前题目', lambda: (_ for _ in ()).throw(m.Retryable('test')))
                    return 95
                with patch.object(control, 'wait_for_retry', side_effect=wait_for_retry), patch.object(m.Runner, '_run', run_point), patch.object(m, 'ROOT', Path(self.tmp.name)), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(m.run_course(context, self.page, cfg, self.bank, limits, ai=ai, metadata=meta), 95)
                self.assertEqual([x[0] for x in done], ['11', '13'])
                self.assertNotEqual(done[0][1], done[1][1])
                self.assertEqual(m.platform_ids(done[1][2].url)['knowledge_id'], '13')
                self.assertEqual(limits.calls, 1)
                self.assertEqual(self.bank.db.execute("SELECT COUNT(*) FROM attempts WHERE run='test-run'").fetchone()[0], 1 if mode == 'new_tab' else 2)
                for tab in list(context.pages):
                    if tab != self.page:
                        tab.close()

    def test_manual_retry_rejects_ambiguous_new_points(self):
        context, cfg, meta, control, limits, ai = self.prepare()
        snapshot = m.retry_page_snapshot(context)
        for pid in ('13', '14'):
            tab = context.new_page()
            tab.goto(f'https://studentexamcomh5.zhihuishu.com/studentReviewTestOrExam/123/1/1/1/x/1?pointId={pid}&classId=9')
        with self.assertRaises(m.Retryable):
            m.manual_retry_page(context, cfg, meta, snapshot)

    def test_new_point_not_ready_keeps_retry_waiting(self):
        context, cfg, meta, control, limits, ai = self.prepare()
        snapshot = m.retry_page_snapshot(context)
        tab = context.new_page()
        context.route('**/studentReviewTestOrExam/**', lambda route: route.fulfill(status=200, content_type='text/html', body='<div>Loading</div>'))
        tab.goto('https://studentexamcomh5.zhihuishu.com/studentReviewTestOrExam/123/1/1/1/x/1?pointId=13&classId=9')
        with self.assertRaisesRegex(m.Retryable, '尚未加载完成'):
            m.manual_retry_page(context, cfg, meta, snapshot)

    def test_existing_unrelated_tab_does_not_hijack_retry(self):
        context, cfg, meta, control, limits, ai = self.prepare()
        tab = context.new_page()
        tab.goto('https://studentexamcomh5.zhihuishu.com/studentReviewTestOrExam/123/1/1/1/x/1?pointId=13&classId=9')
        snapshot = m.retry_page_snapshot(context)
        self.assertIsNone(m.manual_retry_page(context, cfg, meta, snapshot))

    def test_global_request_limit_not_reset_for_next_point(self):
        context, cfg, meta, control, limits, ai = self.prepare(max_calls=1)
        with patch.object(m, 'ROOT', Path(self.tmp.name)), patch.object(control, 'wait_for_retry', side_effect=Stopped('manual stop')) as wait, contextlib.redirect_stdout(io.StringIO()), self.assertRaises(Stopped):
            m.run_course(context, self.page, cfg, self.bank, limits, ai=ai, metadata=meta)
        self.assertEqual(wait.call_args.kwargs['reason'], 'api_limit')
        self.assertEqual(limits.calls, 1)
        self.assertEqual(self.bank.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 1)

    def test_increasing_api_limit_continues_next_point_without_repeating_completed_point(self):
        context, cfg, meta, control, limits, ai = self.prepare(max_calls=1)
        notices = []
        def operator():
            try:
                while True:
                    event = control.events.get(timeout=10)
                    if event['kind'] == 'retry_needed':
                        notices.append(event)
                        control.update_api_limit(2)
                        control.retry()
                        return
            except Exception:
                control.stop()
        thread = threading.Thread(target=operator)
        thread.start()
        try:
            with patch.object(m, 'ROOT', Path(self.tmp.name)), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(m.run_course(context, self.page, cfg, self.bank, limits, ai=ai, metadata=meta), 95)
        finally:
            thread.join(11)
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].get('reason'), 'api_limit')
        self.assertEqual(limits.calls, 2)
        self.assertEqual(self.bank.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 2)
        self.assertEqual(self.bank.db.execute('SELECT COUNT(DISTINCT knowledge) FROM attempts').fetchone()[0], 2)

    def test_starting_point_already_at_target_makes_no_api_request(self):
        context, cfg, meta, control, limits, ai = self.prepare()
        cfg['auto_next'] = False
        with patch.object(m, 'ROOT', Path(self.tmp.name)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.run_course(context, self.page, cfg, self.bank, limits, ai=ai, metadata={**meta, 'mastery': 96}), 96)
        self.assertEqual(limits.calls, 0)
        self.assertEqual(self.bank.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
