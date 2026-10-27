import os
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import desktop
from private_state import CookieKeeper, PrivateStateError, PrivateStore
import test_mastery as base_tests


class PrivateStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PrivateStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_key_is_encrypted_and_can_be_cleared(self):
        key = 'sk-unit-test-not-a-real-key'
        self.store.save_key(key)
        self.assertEqual(PrivateStore(Path(self.tmp.name)).load_key(), key)
        self.assertNotIn(key.encode(), (Path(self.tmp.name)/'api-key.dpapi').read_bytes())
        self.store.save_key('')
        self.assertEqual(self.store.load_key(), '')

    def test_corrupt_file_has_no_plaintext_fallback(self):
        (Path(self.tmp.name)/'api-key.dpapi').write_bytes(b'not-valid-encrypted-data')
        with self.assertRaises(PrivateStateError):
            self.store.load_key()

    def test_cookie_save_is_throttled_and_unchanged_data_is_not_rewritten(self):
        context = Mock()
        context.cookies.return_value = [{'name': 'session', 'value': 'test-value'}]
        store = Mock()
        keeper = CookieKeeper(context, store, report=lambda *_: None, interval=15)
        with patch('private_state.time.monotonic', side_effect=[100, 105, 120]):
            keeper.maybe_save()
            keeper.maybe_save()
            keeper.maybe_save()
        self.assertEqual(context.cookies.call_count, 2)
        store.save_cookies.assert_called_once()


class RememberedKeyGuiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PrivateStore(Path(self.tmp.name))
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        self.root.update_idletasks()
        self.root.destroy()
        self.tmp.cleanup()

    def test_saved_key_is_loaded_automatically(self):
        self.store.save_key('sk-saved-test-key')
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': ''}):
            app = desktop.App(self.root, base_tests.config(), private_store=self.store)
        self.assertEqual(app.key.get(), 'sk-saved-test-key')

    def test_environment_override_is_saved_automatically(self):
        self.store.save_key('sk-previous-test-key')
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'sk-new-environment-test-key'}):
            desktop.App(self.root, base_tests.config(), private_store=self.store)
        self.root.after(850, self.root.quit)
        self.root.mainloop()
        self.assertEqual(self.store.load_key(), 'sk-new-environment-test-key')

    def test_editing_key_saves_without_clicking_save_or_start(self):
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': ''}):
            app = desktop.App(self.root, base_tests.config(), private_store=self.store)
        app.key.set('sk-autosaved-test-key')
        self.root.after(850, self.root.quit)
        self.root.mainloop()
        self.assertEqual(self.store.load_key(), 'sk-autosaved-test-key')
        app.key.set('')
        app.flush_key()
        self.assertEqual(self.store.load_key(), '')


class CookieBrowserTests(base_tests.BrowserTests):
    test_two_rounds_reordered_bank_hit_new_question_and_full_reviews = None
    test_catalogue_dom_resolves_real_hierarchy = None
    test_hidden_recommendation_questions_are_not_collected = None
    test_full_document_navigations_and_retry = None

    def cookie(self, name='session', value='unit-session-value', expires=-1):
        return {'name': name, 'value': value, 'domain': 'fixture.test', 'path': '/',
                'expires': expires, 'secure': True, 'httpOnly': True, 'sameSite': 'Lax'}

    def test_session_cookies_survive_browser_context_restart(self):
        store = PrivateStore(Path(self.tmp.name))
        original = self.browser.new_context()
        original.add_cookies([self.cookie()])
        CookieKeeper(original, store).maybe_save(force=True)
        original.close()
        self.assertNotIn(b'unit-session-value', (Path(self.tmp.name)/'browser-cookies.dpapi').read_bytes())
        fresh = self.browser.new_context()
        try:
            keeper = CookieKeeper(fresh, store, report=lambda *_: None)
            self.assertEqual(keeper.restore(), 1)
            restored = fresh.cookies()[0]
            self.assertEqual(restored['value'], 'unit-session-value')
            self.assertEqual(restored['expires'], -1)
            self.assertTrue(restored['httpOnly'])
        finally:
            fresh.close()

    def test_expired_cookies_skipped_and_newer_existing_cookie_preserved(self):
        store = PrivateStore(Path(self.tmp.name))
        store.save_cookies([self.cookie('expired', expires=time.time()-60), self.cookie('current', 'old-value'), self.cookie('session')])
        context = self.page.context
        context.add_cookies([self.cookie('current', 'new-value')])
        keeper = CookieKeeper(context, store, report=lambda *_: None)
        self.assertEqual(keeper.restore(), 1)
        values = {c['name']: c['value'] for c in context.cookies()}
        self.assertNotIn('expired', values)
        self.assertEqual(values['current'], 'new-value')

    def test_logged_out_state_replaces_previous_snapshot(self):
        store = PrivateStore(Path(self.tmp.name))
        context = self.page.context
        context.add_cookies([self.cookie()])
        keeper = CookieKeeper(context, store)
        keeper.maybe_save(force=True)
        context.clear_cookies()
        keeper.maybe_save(force=True)
        self.assertEqual(store.load_cookies(), [])
        self.assertEqual(CookieKeeper(context, store).restore(), 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
