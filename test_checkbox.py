from pathlib import Path
import contextlib
import io
from unittest.mock import patch

import mastery as m
import test_mastery as base_tests


class ElementCheckboxTests(base_tests.BrowserTests):
    test_two_rounds_reordered_bank_hit_new_question_and_full_reviews = None
    test_catalogue_dom_resolves_real_hierarchy = None
    test_hidden_recommendation_questions_are_not_collected = None
    test_full_document_navigations_and_retry = None

    def prepare(self, old_profile=False):
        self.page.goto((Path(__file__).parent/'tests'/'element-checkbox.html').as_uri())
        cfg = {**base_tests.config(), 'action_delay_seconds': .01, 'timeout_seconds': .8}
        cfg['selectors'] = dict(cfg['selectors'])
        if old_profile:
            cfg['selectors'].update(option='.radio-view > li, .checkbox-view > li', option_text='.stem', selected='.checkedIcon')
        adapter = m.BrowserAdapter(self.page, cfg, m.Limits(cfg))
        adapter.choose_frame()
        return adapter

    def test_synthetic_checkbox_markup_is_extracted(self):
        adapter = self.prepare(old_profile=True)
        questions = adapter.questions()
        self.assertEqual(len(questions), 1)
        q = questions[0][0]
        self.assertEqual(q.kind, 'multiple')
        self.assertEqual(q.options, ['红色', '蓝色', '绿色', '黄色'])

    def test_native_checkboxes_select_and_unselect_existing_values(self):
        adapter = self.prepare(old_profile=True)
        q = adapter.questions()[0][0]
        adapter.fill(q, [q.options[0], q.options[2]])
        self.assertEqual(self.page.locator('input:checked').count(), 2)
        adapter.fill(q, [q.options[1], q.options[3]])
        self.assertEqual(self.page.locator('input:checked').evaluate_all('(nodes) => nodes.map(n => n.value)'), ['opt2', 'opt4'])

    def test_updated_profile_reads_checkbox_labels_without_letters(self):
        adapter = self.prepare()
        q = adapter.questions()[0][0]
        adapter.fill(q, q.options)
        self.assertEqual(self.page.locator('input:checked').count(), 4)
        self.assertFalse(any(value.startswith(('A ', 'B ', 'C ', 'D ')) for value in q.options))

    def test_multiple_choice_in_full_two_round_workflow(self):
        self.page.goto((Path(__file__).parent/'tests'/'workflow.html').as_uri())
        self.page.evaluate('''() => {
          const originalShow = show;
          show = () => {
            originalShow();
            const q = current[index];
            if (q.kind !== 'multiple') return;
            document.querySelector('.radio-view').outerHTML = '<div class="checkbox-view">' + q.options.map((v,i) =>
              '<label class="el-checkbox"><span class="el-checkbox__input"><input type="checkbox" value="'+v+'"></span><span class="el-checkbox__label"><span class="letterSort">'+String.fromCharCode(65+i)+'</span><pre class="preStyle">'+v+'</pre></span></label>'
            ).join('') + '</div>';
          };
          const originalSave = save;
          save = () => {
            const q = current[index];
            if (q.kind === 'multiple') answers[q.stem] = Array.from(document.querySelectorAll('.checkbox-view input:checked')).map(n => n.value);
            else originalSave();
          };
        }''')
        cfg = {**base_tests.config(), 'action_delay_seconds': .01, 'timeout_seconds': 4,
               'result_settle_seconds': .08, 'zero_score_grace_seconds': .18}
        cfg['selectors'] = {**cfg['selectors'], 'option': '.radio-view > li, .checkbox-view > li', 'option_text': '.stem', 'selected': '.checkedIcon'}
        limits = m.Limits(cfg)
        class FakeAI:
            def answer(ai, q, history):
                limits.api_call()
                return (['3'] if q.stem == '2 + 2 = ?' else ['2', '3'] if q.kind == 'multiple' else ['Paris']), 'test'
        adapter = m.BrowserAdapter(self.page, cfg, limits)
        with patch.object(m, 'ROOT', Path(self.tmp.name)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.Runner(adapter, self.bank, FakeAI(), cfg, 'chapter', 'point', limits).run(), 95)
        self.assertEqual(limits.calls, 3)
        self.assertEqual(self.bank.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 4)
        self.assertEqual(self.bank.db.execute("SELECT COUNT(*) FROM reviews WHERE outcome='correct'").fetchone()[0], 3)
