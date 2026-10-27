import contextlib
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
from private_state import PrivateStore
from runtime_control import RunControl, Stopped
from test_mastery import config


class ApiLimitTests(unittest.TestCase):
    def wait_notice(self, control):
        while True:
            event = control.events.get(timeout=3)
            if event['kind'] == 'retry_needed':
                self.assertEqual(event.get('reason'), 'api_limit')
                return event

    def test_reaching_limit_needs_explicit_increase_and_retry_without_resetting_count(self):
        cfg = {**config(), 'max_api_calls': 1}
        control = RunControl()
        limits = m.Limits(cfg, control)
        limits.api_call()  # One previously consumed request.
        q = m.Question('Current question', 'single', ['yes', 'no'])
        raw = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'answers': ['A'], 'confidence': .95})}}]}
        results, finished = [], threading.Event()
        def worker():
            try:
                results.append(limits.retry_step('AI', lambda: m.DeepSeek(cfg, limits, api_key='test').answer(q, [])))
            except Exception as error:
                results.append(error)
            finally:
                finished.set()
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(raw).encode())) as requests, contextlib.redirect_stdout(io.StringIO()):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.wait_notice(control)
                self.assertEqual(limits.calls, 1)
                requests.assert_not_called()
                control.retry()  # Retry alone must not bypass the cap.
                self.wait_notice(control)
                requests.assert_not_called()
                self.assertEqual(limits.calls, 1)
                control.update_api_limit(2)
                self.assertFalse(finished.wait(.1))  # Saving does not auto-resume.
                control.retry()
                self.assertTrue(finished.wait(3))
                self.assertEqual(results, [(['yes'], '')])
                self.assertEqual(limits.calls, 2)
                self.assertEqual(limits.max_calls, 2)
                self.assertEqual(requests.call_count, 1)
                self.assertEqual(cfg['max_api_calls'], 1)
                with self.assertRaises(m.ApiLimitReached):
                    limits.api_call()
            finally:
                control.stop()
                thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_stop_releases_limit_wait_without_more_requests(self):
        control = RunControl()
        limits = m.Limits({**config(), 'max_api_calls': 1}, control)
        limits.api_call()
        results = []
        def worker():
            try:
                limits.retry_step('AI', limits.api_call)
            except Exception as error:
                results.append(error)
        with contextlib.redirect_stdout(io.StringIO()):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.wait_notice(control)
            finally:
                control.close()
                thread.join(3)
        self.assertIsInstance(results[0], Stopped)
        self.assertEqual(limits.calls, 1)

    def test_live_update_and_lowering_preserve_total_usage(self):
        control = RunControl()
        limits = m.Limits({**config(), 'max_api_calls': 3}, control)
        limits.api_call()
        limits.api_call()
        control.update_api_limit(7)
        control.update_api_limit(1)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(m.ApiLimitReached):
            limits.api_call()
        self.assertEqual(limits.max_calls, 1)
        self.assertEqual(limits.calls, 2)

    def test_invalid_updates_rejected_and_stopped_run_cannot_accept_updates(self):
        control = RunControl()
        for value in [0, -1, True, 2.5, '20', 100001]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                control.update_api_limit(value)
        control.stop()
        control.update_api_limit(200)
        self.assertIsNone(control.take_api_limit_update())

    def test_cli_limit_still_stops_with_clear_usage(self):
        limits = m.Limits({**config(), 'max_api_calls': 1})
        limits.api_call()
        with self.assertRaises(m.Halt) as result:
            limits.api_call()
        self.assertNotIsInstance(result.exception, m.Retryable)
        self.assertIn('1/1', str(result.exception))
        self.assertEqual(limits.calls, 1)


class ApiLimitGuiTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.tmp = tempfile.TemporaryDirectory()
        self.app = desktop.App(self.root, config(), private_store=PrivateStore(Path(self.tmp.name)))

    def tearDown(self):
        self.root.update_idletasks()
        self.root.destroy()
        self.tmp.cleanup()

    def test_save_applies_new_cap_to_running_task_without_resuming(self):
        app = self.app
        control = app.control = RunControl()
        app.worker = Mock()
        app.worker.is_alive.return_value = True
        control.awaiting_retry.set()
        app.fields['max_api_calls'].set('150')
        with patch.object(desktop, 'save_preferences') as save:
            app.save()
        self.assertEqual(save.call_args.args[0]['max_api_calls'], 150)
        self.assertEqual(control.take_api_limit_update(), 150)
        self.assertFalse(control.retry_signal.is_set())
        self.assertIn('断点重试', app.message.get())
        self.assertIn('150', app.message.get())

    def test_limit_notice_explains_how_to_resume(self):
        self.app.control = RunControl()
        with patch.object(self.root, 'bell'):
            self.app.handle_event({'kind': 'retry_needed', 'stage': 'AI', 'message': '100/100', 'reason': 'api_limit'})
        self.assertIn('保存设置', self.app.message.get())
        self.assertIn('总请求上限', self.app.message.get())
        self.assertEqual(str(self.app.retry_button['state']), 'normal')

    def test_save_after_stopping_does_not_restart_or_update_old_run(self):
        app = self.app
        app.control = RunControl()
        app.control.stop()
        app.worker = Mock()
        app.worker.is_alive.return_value = True
        app.fields['max_api_calls'].set('150')
        with patch.object(desktop, 'save_preferences'), patch.object(app, 'start') as start:
            app.save()
        start.assert_not_called()
        self.assertIsNone(app.control.take_api_limit_update())
        self.assertIn('下次开始', app.message.get())


if __name__ == '__main__':
    unittest.main(verbosity=2)
