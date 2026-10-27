"""Thread-safe controls and events shared by the GUI and the browser worker."""
import queue
import threading
import time

from mastery import Halt


class Stopped(Halt):
    pass


class RunControl:
    def __init__(self):
        self.events = queue.Queue()
        self.paused = threading.Event()
        self.stopped = threading.Event()
        self.close_requested = threading.Event()
        self.awaiting_retry = threading.Event()
        self.retry_signal = threading.Event()
        self.api_limit_updates = queue.Queue()

    def emit(self, kind, **values):
        self.events.put({'kind': kind, **values})

    def pause(self, message='等待当前操作结束后暂停'):
        if self.stopped.is_set() or self.awaiting_retry.is_set():
            return
        self.paused.set()
        self.emit('phase', status='暂停中', message=message)

    def resume(self):
        if self.stopped.is_set() or self.awaiting_retry.is_set():
            return
        self.paused.clear()
        self.emit('phase', status='运行中', message='已继续')

    def stop(self):
        self.stopped.set()
        self.paused.clear()
        self.retry_signal.set()
        self.emit('phase', status='停止中', message='等待当前网络请求或页面操作结束，不再发起新操作')

    def retry(self):
        if self.awaiting_retry.is_set() and not self.stopped.is_set():
            self.retry_signal.set()

    def update_api_limit(self, value):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100000:
            raise ValueError('总请求上限必须是 1 到 100000 的整数。')
        if not self.stopped.is_set():
            self.api_limit_updates.put(value)

    def take_api_limit_update(self):
        latest = None
        while True:
            try:
                latest = self.api_limit_updates.get_nowait()
            except queue.Empty:
                return latest

    def wait_for_retry(self, stage, message, reason=None):
        paused_time = self.checkpoint()
        self.retry_signal.clear()
        self.awaiting_retry.set()
        self.emit('phase', status='等待重试', message=f'{stage}：{message}')
        self.emit('retry_needed', stage=stage, message=message, reason=reason)
        start = time.monotonic()
        try:
            while not self.retry_signal.wait(.1):
                if self.stopped.is_set():
                    raise Stopped('已停止断点重试。')
            if self.stopped.is_set():
                raise Stopped('已停止断点重试。')
        finally:
            self.awaiting_retry.clear()
        self.emit('phase', status='运行中', message=f'正在从断点重试：{stage}')
        return paused_time+time.monotonic()-start

    def close(self):
        self.close_requested.set()
        self.stop()

    def checkpoint(self):
        if self.stopped.is_set():
            raise Stopped('已按你的操作停止；浏览器和题库保留。')
        start = time.monotonic()
        waited = False
        while self.paused.is_set():
            if not waited:
                self.emit('phase', status='已暂停', message='点击“继续”恢复，或点击“停止”结束')
                waited = True
            if self.stopped.wait(.1):
                raise Stopped('已停止。')
        if self.stopped.is_set():
            raise Stopped('已停止。')
        return time.monotonic()-start if waited else 0


class EventLog:
    """Line buffered log stream. Never receives the API Key input field."""
    def __init__(self, control):
        self.control = control
        self.buffer = ''

    def write(self, text):
        self.buffer += str(text)
        while '\n' in self.buffer:
            line, self.buffer = self.buffer.split('\n', 1)
            if line.strip():
                self.control.emit('log', message=line)
        return len(text)

    def flush(self):
        if self.buffer.strip():
            self.control.emit('log', message=self.buffer)
        self.buffer = ''
