import contextlib
import base64
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mastery as m
from test_mastery import config
from test_images import PIXEL_A


@unittest.skipUnless(os.name == 'nt', 'Windows browser window state test')
class MinimizedBrowserTests(unittest.TestCase):
    def test_two_rounds_with_another_tab_and_minimized_browser(self):
        from playwright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
            # Start minimized so the test does not interrupt the user's desktop.
            browser = pw.chromium.launch(channel='msedge', headless=False,
                                         args=[*m.BROWSER_BACKGROUND_ARGS, '--start-minimized'])
            try:
                context = browser.new_context(viewport={'width': 1440, 'height': 950})
                m.watch_image_responses(context)
                page = context.new_page()
                page.route('https://formula.invalid/synthetic.png', lambda route: route.fulfill(
                    status=200, content_type='image/png', body=base64.b64decode(PIXEL_A)))
                page.goto((Path(__file__).parent/'tests'/'workflow.html').as_uri())
                page.evaluate('''() => {
                    const image = document.createElement('img');
                    image.src = 'https://formula.invalid/synthetic.png';
                    image.style = 'width:24px;height:24px';
                    first.stem = '模拟公式 ' + image.outerHTML + ' 的结果？';
                    reordered.stem = first.stem;
                    show();
                }''')
                cdp = context.new_cdp_session(page)
                window_id = cdp.send('Browser.getWindowForTarget')['windowId']
                other = context.new_page()
                other.goto('data:text/html,<title>other tab</title>')
                cdp.send('Browser.setWindowBounds', {'windowId': window_id, 'bounds': {'windowState': 'minimized'}})
                page.wait_for_timeout(1500)
                self.assertEqual(cdp.send('Browser.getWindowBounds', {'windowId': window_id})['bounds']['windowState'], 'minimized')
                cfg = {**config(), 'action_delay_seconds': .01, 'timeout_seconds': 4,
                       'result_settle_seconds': .05, 'zero_score_grace_seconds': .2}
                limits = m.Limits(cfg)
                bank = m.Bank(Path(tmp)/'bank.sqlite3')
                seen_images = []
                class AI:
                    def answer(ai, q, history):
                        limits.api_call()
                        if q.kind == 'single':
                            seen_images.append(len(q.images))
                        return (['3'] if q.kind == 'single' else ['2', '3'] if q.kind == 'multiple' else ['Paris']), ''
                try:
                    adapter = m.BrowserAdapter(page, cfg, limits)
                    with patch.object(m, 'ROOT', Path(tmp)), contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(m.Runner(adapter, bank, AI(), cfg, 'chapter', 'point', limits).run(), 95)
                    self.assertEqual(limits.calls, 3)
                    self.assertEqual(seen_images, [1])
                    self.assertEqual(bank.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 4)
                    self.assertEqual(bank.db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0], 4)
                    self.assertEqual(cdp.send('Browser.getWindowBounds', {'windowId': window_id})['bounds']['windowState'], 'minimized')
                finally:
                    bank.close()
                    context.close()
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
