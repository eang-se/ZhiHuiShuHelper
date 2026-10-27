"""Local desktop controller. Browser and SQLite always run in their owner thread."""
from __future__ import annotations

import contextlib
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import mastery as m
from runtime_control import EventLog, RunControl, Stopped
from private_state import PrivateStore, PrivateStateError, CookieKeeper

ROOT = Path(__file__).resolve().parent


def load_preferences():
    path = ROOT/'profile.json'
    if not path.exists():
        path = ROOT/'profile.polymas.json'
    return json.loads(path.read_text(encoding='utf-8-sig'))


def save_preferences(cfg):
    # Keys and browser cookies are intentionally not part of UI preferences.
    cfg = {key: value for key, value in cfg.items() if key not in {'api_key', 'DEEPSEEK_API_KEY'}}
    m.validate_config(cfg)
    path = ROOT/'profile.json'
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def run_browser_session(context, page, cfg, bank, control, cookies, api_key, initial_url=None):
    """One user-started run. Only another Start click may create a new run."""
    if control.close_requested.is_set() or not context.pages:
        return
    try:
        if initial_url:
            page.goto(initial_url, wait_until='domcontentloaded', timeout=60000)
        control.emit('phase', status='等待进入知识点', message='请进入目标知识点；平台要求登录时登录一次即可自动保存')
        page = m.wait_for_work_page(context, page, cfg, control, on_poll=cookies.maybe_save)
        print('已进入知识点；可以最小化浏览器，保持本窗口运行。')
        cookies.maybe_save(force=True)
        limits = m.Limits(cfg, control, on_checkpoint=cookies.maybe_save)
        limits.check()
        m.run_course(context, page, cfg, bank, limits, ai=m.DeepSeek(cfg, limits, api_key=api_key))
        control.emit('phase', status='已完成', message='当前范围处理完成。浏览器保留，可查看结果')
    except Stopped as error:
        print(error)
        control.emit('phase', status='已停止', message='当前任务已停止，浏览器保留供检查')
    except Exception as error:
        message = str(error) if isinstance(error, m.Halt) else f'浏览器操作失败：{type(error).__name__}'
        print(message)
        control.emit('phase', status='需要处理', message=message)
        m.save_error_diagnostics(context, cfg)
    finally:
        bank.export(ROOT/'data'/'question-bank.json')
        control.emit('idle', message='可点击“初始化”回到未开始状态，或关闭浏览器')
    while not control.close_requested.is_set() and context.pages:
        try:
            cookies.maybe_save()
            context.pages[0].wait_for_timeout(150)
        except Exception:
            return


def browser_worker(cfg, browser, api_key, control):
    log = EventLog(control)
    bank = None
    with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        try:
            from playwright.sync_api import sync_playwright
            bank = m.Bank(ROOT/'data'/'question-bank.sqlite3')
            with sync_playwright() as pw:
                options = {} if browser == 'chromium' else {'channel': browser}
                control.emit('phase', status='启动中', message='正在打开独立浏览器')
                context = pw.chromium.launch_persistent_context(str(ROOT/'data'/'browser-profile'),
                    headless=False, viewport={'width': 1440, 'height': 950}, args=m.BROWSER_BACKGROUND_ARGS, **options)
                control.emit('browser', opened=True)
                cookies = CookieKeeper(context, PrivateStore(ROOT/'data'))
                try:
                    cookies.restore()
                    page = context.pages[0] if context.pages else context.new_page()
                    page.set_default_timeout(10000)
                    run_browser_session(context, page, cfg, bank, control, cookies, api_key, initial_url=cfg['url'])
                finally:
                    try:
                        cookies.maybe_save(force=True)
                    finally:
                        context.close()
                        control.emit('browser', opened=False)
        except Exception as error:
            message = str(error) if isinstance(error, m.Halt) else f'启动失败：{type(error).__name__}。请确认已安装依赖，且没有其他脚本占用同一浏览器。'
            control.emit('phase', status='需要处理', message=message)
            print(message)
        finally:
            if bank:
                bank.close()
            log.flush()
            control.emit('worker_done')


class App:
    def __init__(self, root, cfg=None, private_store=None):
        self.root = root
        self.cfg = cfg if cfg is not None else load_preferences()
        self.private_store = private_store or PrivateStore(ROOT/'data')
        self.key_save_job = None
        self.poll_job = None
        key_error = ''
        try:
            saved_key = self.private_store.load_key()
        except (PrivateStateError, OSError) as error:
            saved_key, key_error = '', str(error)
        self.control = None
        self.worker = None
        self.closing = False
        self.initializing = False
        self.completed_ids = set()
        self.skipped_ids = set()
        self.current_id = ''
        self.counts = {'done': 0, 'skipped': 0, 'bank': 0, 'ai': 0, 'api_calls': 0}
        root.title('智慧树 · 掌握度助手')
        root.geometry('1130x850')
        root.minsize(1040, 760)
        root.configure(bg='#edf2f1')
        root.protocol('WM_DELETE_WINDOW', self.on_close)
        root.bind('<Destroy>', self.on_destroy, add='+')
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10), background='#edf2f1')
        style.configure('TFrame', background='#edf2f1')
        style.configure('TLabelframe', background='#edf2f1', bordercolor='#d0dcda')
        style.configure('TLabelframe.Label', foreground='#284e47', font=('Microsoft YaHei UI', 10, 'bold'))
        style.configure('Header.TLabel', font=('Microsoft YaHei UI', 21, 'bold'), foreground='#173f36')
        style.configure('Muted.TLabel', foreground='#62736f')
        style.configure('Point.TLabel', font=('Microsoft YaHei UI', 14, 'bold'), foreground='#173f36')
        style.configure('State.TLabel', foreground='#176149', font=('Microsoft YaHei UI', 12, 'bold'))
        style.configure('TButton', padding=(9, 7))
        style.configure('Accent.TButton', background='#216b55', foreground='white')
        style.map('Accent.TButton', background=[('active', '#18523f'), ('disabled', '#9db1a9')])
        style.configure('Horizontal.TProgressbar', background='#2b8d6b', troughcolor='#dce6e1', thickness=15)
        style.configure('Treeview', rowheight=27, background='white', fieldbackground='white')

        self.state = tk.StringVar(value='未开始')
        self.message = tk.StringVar(value='在左侧输入 API Key，点击开始，然后在浏览器中进入第一个知识点。')
        self.course = tk.StringVar(value='等待自动读取课程')
        self.chapter = tk.StringVar(value='章节与知识单元将自动读取')
        self.point = tk.StringVar(value='请选择起始知识点')
        self.question = tk.StringVar(value='当前题目会显示在这里')
        self.score = tk.DoubleVar(value=0)
        self.score_text = tk.StringVar(value='掌握度 —')
        self.round_text = tk.StringVar(value='第 — 轮')
        self.metric_labels = {name: tk.StringVar(value='0') for name in self.counts}
        self.fields = {
            'model': tk.StringVar(value=self.cfg.get('model', 'deepseek-flash')),
            'target': tk.StringVar(value=str(self.cfg['target'])),
            'max_rounds': tk.StringVar(value=str(self.cfg['max_rounds'])),
            'max_api_calls': tk.StringVar(value=str(self.cfg['max_api_calls'])),
            'max_minutes': tk.StringVar(value=str(self.cfg['max_minutes']))}
        self.key = tk.StringVar(value=os.environ.get('DEEPSEEK_API_KEY', '') or saved_key)
        self.key_status = tk.StringVar(value='已自动读取本机保存的 API Key' if saved_key else '输入后自动加密保存到本机')
        self.browser = tk.StringVar(value=self.cfg.get('browser', 'msedge'))
        self.auto_next = tk.BooleanVar(value=self.cfg.get('auto_next', True))
        self.scope = tk.StringVar(value='本课程后续知识点' if self.cfg.get('next_scope', 'course') == 'course' else '仅当前章节')
        self.low_confidence = tk.StringVar(value='提示后继续' if self.cfg.get('low_confidence_action', 'continue') == 'continue' else '暂停人工确认')
        self.build()
        self.key.trace_add('write', self.schedule_key_save)
        if key_error:
            self.message.set(key_error)
        elif self.key.get() and self.key.get() != saved_key:
            self.schedule_key_save()
        self.poll_job = root.after(100, self.poll)

    def build(self):
        shell = ttk.Frame(self.root, padding=22)
        shell.pack(fill='both', expand=True)
        header = ttk.Frame(shell)
        header.pack(fill='x', pady=(0, 18))
        ttk.Label(header, text='智慧树 · 掌握度助手', style='Header.TLabel').pack(side='left')
        ttk.Label(header, textvariable=self.state, style='State.TLabel').pack(side='right', padx=12)
        ttk.Label(shell, textvariable=self.message, style='Muted.TLabel', wraplength=1050).pack(side='bottom', fill='x', pady=(10, 0))
        main = ttk.Frame(shell)
        main.pack(fill='both', expand=True)
        left = ttk.LabelFrame(main, text='运行设置', padding=16)
        left.pack(side='left', fill='y', padx=(0, 18))
        self.settings_widgets = []
        ttk.Label(left, text='DeepSeek API Key').pack(anchor='w')
        entry = ttk.Entry(left, textvariable=self.key, show='•', width=27)
        entry.pack(fill='x', pady=(6, 3))
        self.settings_widgets.append(entry)
        ttk.Label(left, textvariable=self.key_status, style='Muted.TLabel', font=('Microsoft YaHei UI', 8), wraplength=230).pack(anchor='w', pady=(0, 6))
        self.add_combo(left, '浏览器', self.browser, ['msedge', 'chrome', 'chromium'])
        ttk.Label(left, text='模型').pack(anchor='w', pady=(9, 3))
        model_entry = ttk.Entry(left, textvariable=self.fields['model'], width=27)
        model_entry.pack(fill='x')
        self.settings_widgets.append(model_entry)
        grid = ttk.Frame(left)
        grid.pack(fill='x', pady=(8, 0))
        for i, (name, title) in enumerate([('target', '目标掌握度 %'), ('max_rounds', '每点最多轮数'),
                                         ('max_api_calls', '总请求上限'), ('max_minutes', '总时限 分钟')]):
            cell = ttk.Frame(grid)
            cell.grid(row=i//2, column=i%2, sticky='ew', padx=(0 if i%2==0 else 8, 0), pady=4)
            ttk.Label(cell, text=title, font=('Microsoft YaHei UI', 9)).pack(anchor='w', pady=(0, 3))
            widget = ttk.Entry(cell, textvariable=self.fields[name], width=11)
            widget.pack(fill='x')
            self.settings_widgets.append(widget)
        self.next_checkbox = ttk.Checkbutton(left, text='达标后继续下一个知识点', variable=self.auto_next)
        self.next_checkbox.pack(anchor='w', pady=(12, 6))
        self.settings_widgets.append(self.next_checkbox)
        self.add_combo(left, '继续范围', self.scope, ['本课程后续知识点', '仅当前章节'])
        self.add_combo(left, '低置信度时', self.low_confidence, ['提示后继续', '暂停人工确认'])
        ttk.Label(left, text='已达标及没有练习入口的知识点\n会自动跳过。暂停期间不计入时限。',
                  style='Muted.TLabel', font=('Microsoft YaHei UI', 8)).pack(anchor='w', pady=(8, 6))
        ttk.Button(left, text='保存设置', command=self.save).pack(fill='x', pady=2)
        ttk.Button(left, text='打开题库文件夹', command=self.open_bank).pack(fill='x', pady=2)

        right = ttk.Frame(main)
        right.pack(side='left', fill='both', expand=True)
        context = ttk.LabelFrame(right, text='当前进度', padding=16)
        context.pack(fill='x')
        ttk.Label(context, textvariable=self.course, wraplength=680).pack(anchor='w')
        ttk.Label(context, textvariable=self.chapter, style='Muted.TLabel', wraplength=680).pack(anchor='w', pady=(6, 10))
        ttk.Label(context, textvariable=self.point, style='Point.TLabel', wraplength=680).pack(anchor='w')
        line = ttk.Frame(context)
        line.pack(fill='x', pady=(12, 4))
        ttk.Label(line, textvariable=self.score_text).pack(side='left')
        ttk.Label(line, textvariable=self.round_text, style='Muted.TLabel').pack(side='right')
        ttk.Progressbar(context, variable=self.score, maximum=100).pack(fill='x', pady=(3, 12))
        ttk.Label(context, textvariable=self.question, wraplength=670, style='Muted.TLabel').pack(anchor='w')
        metrics = ttk.Frame(right)
        metrics.pack(fill='x', pady=14)
        for i, (key, title) in enumerate([('done', '已完成知识点'), ('skipped', '已跳过'), ('bank', '题库命中'), ('ai', 'AI 作答'), ('api_calls', 'API 请求')]):
            metrics.columnconfigure(i, weight=1)
            frame = ttk.Frame(metrics)
            frame.grid(row=0, column=i, sticky='ew')
            ttk.Label(frame, textvariable=self.metric_labels[key], style='Point.TLabel').pack()
            ttk.Label(frame, text=title, style='Muted.TLabel', font=('Microsoft YaHei UI', 9)).pack(pady=3)
        buttons = ttk.Frame(right)
        buttons.pack(fill='x', pady=(0, 12))
        self.start_button = ttk.Button(buttons, text='开始', style='Accent.TButton', command=self.start)
        self.pause_button = ttk.Button(buttons, text='暂停', command=lambda: self.control and self.control.pause(), state='disabled')
        self.resume_button = ttk.Button(buttons, text='继续', command=lambda: self.control and self.control.resume(), state='disabled')
        self.retry_button = ttk.Button(buttons, text='断点重试', command=self.retry_step, style='Accent.TButton', state='disabled')
        self.stop_button = ttk.Button(buttons, text='停止', command=self.stop, state='disabled')
        self.close_button = ttk.Button(buttons, text='关闭浏览器', command=self.close_browser, state='disabled')
        for button in (self.start_button, self.pause_button, self.resume_button, self.retry_button, self.stop_button, self.close_button):
            button.pack(side='left', padx=(0, 8))
        restart_row = ttk.Frame(right)
        restart_row.pack(fill='x', pady=(0, 10))
        self.initialize_button = ttk.Button(restart_row, text='初始化', command=self.initialize)
        self.initialize_button.pack(side='left', padx=(0, 10))
        ttk.Label(restart_row, text='结束任务，回到未开始状态；保留登录和题库', style='Muted.TLabel', font=('Microsoft YaHei UI', 9)).pack(side='left')
        tree_frame = ttk.Frame(right)
        tree_frame.pack(fill='x', pady=(0, 12))
        self.tree = ttk.Treeview(tree_frame, columns=('point', 'chapter', 'score', 'status'), show='headings', height=5)
        for name, title, width in [('point', '知识点', 245), ('chapter', '所属章节', 170), ('score', '掌握度', 70), ('status', '状态', 110)]:
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, minwidth=60, stretch=name in {'point', 'chapter'})
        self.tree.pack(side='left', fill='both', expand=True)
        tree_scroll = ttk.Scrollbar(tree_frame, orient='vertical', command=self.tree.yview)
        tree_scroll.pack(side='right', fill='y')
        self.tree.configure(yscrollcommand=tree_scroll.set)
        ttk.Label(right, text='运行日志', style='Muted.TLabel').pack(anchor='w', pady=(0, 5))
        self.log = scrolledtext.ScrolledText(right, height=10, font=('Microsoft YaHei UI', 9), bg='#ffffff', fg='#304b44', relief='flat', wrap='word', state='disabled')
        self.log.pack(fill='both', expand=True)

    def add_combo(self, parent, title, variable, values):
        ttk.Label(parent, text=title).pack(anchor='w', pady=(8, 3))
        combo = ttk.Combobox(parent, textvariable=variable, values=values, state='readonly', width=25)
        combo.pack(fill='x')
        self.settings_widgets.append(combo)

    def preferences(self):
        cfg = dict(self.cfg)
        cfg['model'] = self.fields['model'].get().strip()
        for name in ('target', 'max_minutes'):
            cfg[name] = float(self.fields[name].get())
        for name in ('max_rounds', 'max_api_calls'):
            cfg[name] = int(self.fields[name].get())
        cfg.update(auto_next=self.auto_next.get(), next_scope='course' if self.scope.get() == '本课程后续知识点' else 'chapter',
                   low_confidence_action='continue' if self.low_confidence.get() == '提示后继续' else 'pause', browser=self.browser.get())
        if not cfg['model']:
            raise m.Halt('请输入模型名称。')
        m.validate_config(cfg)
        return cfg

    def save(self):
        try:
            cfg = self.preferences()
            save_preferences(cfg)
            self.cfg = cfg
            saved = self.flush_key()
            active = self.control and self.worker and self.worker.is_alive() and not self.control.stopped.is_set() and not self.initializing
            if active:
                self.control.update_api_limit(cfg['max_api_calls'])
                self.message.set(f"总请求上限 {cfg['max_api_calls']} 已保存并提交给当前任务；若正在等待上限调整，请点击“断点重试”。其他设置下次开始生效。")
                if not saved:
                    self.append_log('API Key 保存失败，请查看输入框下方提示。')
            else:
                self.message.set('设置与加密 API Key 已保存，下次开始时生效。' if saved else '设置已保存，API Key 保存失败，请查看输入框下方提示。')
        except (ValueError, m.Halt, OSError) as error:
            messagebox.showerror('设置有误', str(error), parent=self.root)

    def start(self):
        if self.initializing or (self.worker and self.worker.is_alive()):
            return
        try:
            cfg = self.preferences()
            if not self.key.get().strip():
                raise m.Halt('请先输入 DeepSeek API Key。')
            if not self.key.get().strip().isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in self.key.get().strip()):
                raise m.Halt('API Key 含空格或不支持的字符，请重新输入。')
            save_preferences(cfg)
            self.flush_key()
        except (ValueError, m.Halt, OSError) as error:
            messagebox.showerror('无法开始', str(error), parent=self.root)
            return
        self.cfg = cfg
        self.control = RunControl()
        self.reset_progress()
        self.start_button.configure(state='disabled')
        self.stop_button.configure(state='normal')
        self.pause_button.configure(state='normal')
        self.worker = threading.Thread(target=browser_worker, args=(cfg, self.browser.get(), self.key.get(), self.control), daemon=True)
        self.worker.start()

    def reset_progress(self):
        self.completed_ids.clear()
        self.skipped_ids.clear()
        self.current_id = ''
        self.tree.delete(*self.tree.get_children())
        for key in self.counts:
            self.counts[key] = 0
            self.metric_labels[key].set('0')
        self.course.set('等待自动读取课程')
        self.chapter.set('章节与知识单元将自动读取')
        self.point.set('请选择起始知识点')
        self.question.set('当前题目会显示在这里')
        self.round_text.set('第 — 轮')
        self.score.set(0)
        self.score_text.set('掌握度 —')

    def initialize(self):
        if self.initializing or self.closing:
            return
        self.initializing = True
        for button in (self.start_button, self.initialize_button, self.pause_button, self.resume_button,
                       self.retry_button, self.stop_button, self.close_button):
            button.configure(state='disabled')
        self.state.set('初始化中')
        self.message.set('正在结束当前操作、保存登录和题库并关闭任务浏览器，完成后回到未开始状态。')
        if self.control:
            self.control.close()
        self.finish_initialize()

    def finish_initialize(self):
        # worker_done is queued just before the thread exits: wait for actual exit.
        if not self.initializing or self.closing or (self.worker and self.worker.is_alive()):
            return
        self.worker = None
        self.control = None
        self.reset_progress()
        self.log.configure(state='normal')
        self.log.delete('1.0', 'end')
        self.log.configure(state='disabled')
        self.state.set('未开始')
        self.message.set('在左侧输入 API Key，点击开始，然后在浏览器中进入第一个知识点。')
        self.start_button.configure(state='normal')
        self.initialize_button.configure(state='normal')
        self.initializing = False

    def schedule_key_save(self, *_):
        if self.key_save_job:
            self.root.after_cancel(self.key_save_job)
        self.key_save_job = self.root.after(700, self.flush_key)

    def flush_key(self):
        if self.key_save_job:
            self.root.after_cancel(self.key_save_job)
            self.key_save_job = None
        try:
            self.private_store.save_key(self.key.get())
            self.key_status.set('已加密保存，重启后自动读取' if self.key.get().strip() else '已清除保存的 API Key')
            return True
        except PrivateStateError as error:
            self.key_status.set(str(error))
            return False
        except OSError:
            self.key_status.set('未能加密保存，请检查本机权限')
            return False

    def stop(self):
        if self.control:
            self.control.stop()

    def retry_step(self):
        if self.control:
            self.retry_button.configure(state='disabled')
            self.control.retry()

    def close_browser(self):
        if self.control:
            self.control.close()

    def open_bank(self):
        path = ROOT/'data'
        path.mkdir(exist_ok=True)
        os.startfile(path)

    def append_log(self, message):
        self.log.configure(state='normal')
        self.log.insert('end', f"{datetime.now():%H:%M:%S}  {message}\n")
        if int(self.log.index('end-1c').split('.')[0]) > 1200:
            self.log.delete('1.0', '201.0')
        self.log.see('end')
        self.log.configure(state='disabled')

    def point_row(self, point, status, score=None):
        key = point['knowledge_id']
        values = (point.get('knowledge_title', key), point.get('chapter_title', ''), '—' if score is None else f'{score:g}%', status)
        if self.tree.exists(key):
            self.tree.item(key, values=values)
        else:
            self.tree.insert('', 'end', iid=key, values=values)

    def handle_event(self, event):
        kind = event['kind']
        if self.initializing and kind != 'worker_done':
            return  # Ignore late progress/errors from the run being cleared.
        if kind == 'log':
            self.append_log(event['message'])
        elif kind == 'phase':
            self.state.set(event['status'])
            self.message.set(event.get('message', ''))
            active = event['status'] in {'运行中', '等待登录', '等待进入知识点', '切换知识点', '启动中'}
            paused = event['status'] in {'暂停中', '已暂停'}
            if self.control and self.control.close_requested.is_set():
                active = paused = False
            self.pause_button.configure(state='normal' if active else 'disabled')
            self.resume_button.configure(state='normal' if paused else 'disabled')
            if event['status'] != '等待重试':
                self.retry_button.configure(state='disabled')
        elif kind == 'retry_needed':
            if self.control and self.control.stopped.is_set():
                return  # A queued reminder belongs to the run being stopped/reset.
            self.retry_button.configure(state='normal')
            if event.get('reason') == 'api_limit':
                self.message.set('总请求次数已达上限。请提高左侧“总请求上限”，点击“保存设置”，再点击“断点重试”继续当前题。')
            else:
                self.message.set(f"{event['stage']}暂未完成。检查当前页面后点击“断点重试”，或点击“停止”。")
            self.append_log(f"已保留当前断点：{event['stage']}。等待手动重试。")
            self.root.bell()
        elif kind == 'metadata':
            self.current_id = event['knowledge_id']
            self.course.set(event['course_title'])
            self.chapter.set(event['chapter_title']+' / '+event.get('unit_title', ''))
            self.point.set(event['knowledge_title'])
            current_score = event.get('mastery')
            self.score.set(current_score if isinstance(current_score, (int, float)) else 0)
            self.score_text.set(f"掌握度 {current_score:g}%  /  目标 {self.cfg['target']:g}%" if isinstance(current_score, (int, float)) else '掌握度 —')
            self.round_text.set('第 — 轮')
            self.point_row(event, '正在处理', event.get('mastery'))
            self.tree.see(self.current_id)
        elif kind == 'round':
            self.round_text.set(f"第 {event['round']} / {self.cfg['max_rounds']} 轮")
        elif kind == 'question':
            self.question.set(event['question'])
        elif kind == 'score':
            self.score.set(event['mastery'])
            self.score_text.set(f"掌握度 {event['mastery']:g}%  /  目标 {self.cfg['target']:g}%")
            if self.tree.exists(self.current_id):
                values = list(self.tree.item(self.current_id, 'values'))
                values[2] = f"{event['mastery']:g}%"
                self.tree.item(self.current_id, values=values)
        elif kind == 'answered':
            self.counts['bank' if event['source'] == 'bank' else 'ai'] += 1
        elif kind == 'metrics':
            self.counts['api_calls'] = event['api_calls']
        elif kind == 'api_limit_changed':
            self.counts['api_calls'] = event['api_calls']
            self.append_log(f"当前运行已应用总请求上限 {event['max_api_calls']}，已用 {event['api_calls']} 次。")
        elif kind == 'point_done':
            key = event['point']['knowledge_id']
            self.completed_ids.add(key)
            self.counts['done'] = len(self.completed_ids)
            self.point_row(event['point'], '已达标', event['mastery'])
        elif kind == 'point_skipped':
            key = event['point']['knowledge_id']
            self.skipped_ids.add(key)
            self.counts['skipped'] = len(self.skipped_ids)
            self.point_row(event['point'], event['reason'], event['point'].get('mastery'))
        elif kind == 'catalogue':
            ids = [p['knowledge_id'] for p in event['points']]
            start = ids.index(event['current_id'])
            for p in event['points'][start+1:]:
                if self.cfg.get('next_scope', 'course') == 'chapter' and p['chapter_id'] != event['points'][start]['chapter_id']:
                    break
                if p['knowledge_id'] not in self.completed_ids | self.skipped_ids:
                    self.point_row(p, '待处理', p.get('mastery'))
        elif kind == 'browser':
            self.close_button.configure(state='normal' if event['opened'] else 'disabled')
        elif kind == 'idle':
            for button in (self.pause_button, self.resume_button, self.retry_button, self.stop_button):
                button.configure(state='disabled')
            self.initialize_button.configure(state='normal')
            self.message.set(event.get('message', '可初始化回到未开始状态，或关闭浏览器'))
        elif kind == 'worker_done':
            if self.closing:
                self.root.destroy()
                return
            if self.initializing:
                self.finish_initialize()
                return
            self.start_button.configure(state='normal')
            self.initialize_button.configure(state='normal')
            for button in (self.pause_button, self.resume_button, self.retry_button, self.stop_button, self.close_button):
                button.configure(state='disabled')
        for name, value in self.counts.items():
            self.metric_labels[name].set(str(value))

    def poll(self):
        self.poll_job = None
        if self.control:
            current_control = self.control
            for _ in range(150):
                try:
                    event = current_control.events.get_nowait()
                except queue.Empty:
                    break
                self.handle_event(event)
                if self.closing and event['kind'] == 'worker_done':
                    return
                if self.control is not current_control:
                    break
        self.finish_initialize()
        self.poll_job = self.root.after(100, self.poll)

    def on_destroy(self, event):
        if event.widget is not self.root:
            return
        for name in ('poll_job', 'key_save_job'):
            job = getattr(self, name)
            if job:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, name, None)

    def on_close(self):
        self.flush_key()
        if self.worker and self.worker.is_alive():
            self.closing = True
            self.control.close()
            self.message.set('正在停止并保存题库，当前网络请求结束后窗口会关闭。')
        else:
            self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # No request bodies, credentials, or page contents go into the startup error.
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, f'界面启动失败：{type(error).__name__}。请运行 Install.cmd 检查依赖。', '智慧树助手', 16)
