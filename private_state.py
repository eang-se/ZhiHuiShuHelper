"""Current-user Windows DPAPI storage. Never write plaintext credentials."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import time


class PrivateStateError(RuntimeError):
    pass


class DataBlob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def protect(payload: bytes, decrypt=False) -> bytes:
    if os.name != 'nt':
        raise PrivateStateError('凭据加密保存需要 Windows。')
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(payload)
    source = DataBlob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = DataBlob()
    if decrypt:
        function = crypt.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
        args = (ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        function = crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(DataBlob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
        args = (ctypes.byref(source), 'Zhihuishu mastery local credentials', None, None, None, 1, ctypes.byref(target))
    function.restype = wintypes.BOOL
    if not function(*args):
        raise PrivateStateError('本机凭据无法解密，请重新输入 API Key 或重新登录。' if decrypt else '本机凭据加密失败，未保存明文。')
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel.LocalFree(ctypes.cast(target.pbData, ctypes.c_void_p))


class PrivateStore:
    header = b'ZHS-DPAPI-1\x00'

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def _path(self, kind):
        return self.directory/('api-key.dpapi' if kind == 'key' else 'browser-cookies.dpapi')

    def _write(self, kind, obj):
        payload = json.dumps({'version': 1, **obj}, ensure_ascii=False).encode('utf-8')
        encrypted = self.header+protect(payload)
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(kind)
        temp = path.with_suffix('.dpapi.tmp')
        temp.write_bytes(encrypted)
        temp.replace(path)

    def _read(self, kind):
        path = self._path(kind)
        if not path.exists():
            return {}
        try:
            data = path.read_bytes()
            if not data.startswith(self.header):
                raise ValueError('format')
            obj = json.loads(protect(data[len(self.header):], decrypt=True))
            if not isinstance(obj, dict) or obj.get('version') != 1:
                raise ValueError('format')
            return obj
        except (OSError, ValueError, TypeError):
            raise PrivateStateError('保存的本机凭据无法读取，请重新输入或重新登录。') from None

    def load_key(self):
        key = self._read('key').get('api_key', '')
        if not isinstance(key, str):
            raise PrivateStateError('保存的 API Key 格式无效，请重新输入。')
        return key

    def save_key(self, key):
        key = key.strip()
        if not key:
            self._path('key').unlink(missing_ok=True)
            return
        if not key.isascii() or any(ord(char) <= 32 or ord(char) == 127 for char in key):
            raise PrivateStateError('API Key 格式无效，未保存。')
        self._write('key', {'api_key': key})

    def load_cookies(self):
        cookies = self._read('cookies').get('cookies', [])
        if not isinstance(cookies, list) or any(not isinstance(c, dict) for c in cookies):
            raise PrivateStateError('保存的登录状态格式无效，请重新登录。')
        return cookies

    def save_cookies(self, cookies):
        self._write('cookies', {'cookies': cookies})


class CookieKeeper:
    def __init__(self, context, store, report=print, interval=15):
        self.context, self.store, self.report = context, store, report
        self.interval = interval
        self.last_save = 0
        self.last_digest = None
        self.warned = False

    @staticmethod
    def identity(cookie):
        return (cookie.get('name'), cookie.get('domain'), cookie.get('path'), json.dumps(cookie.get('partitionKey'), sort_keys=True))

    def restore(self):
        try:
            saved = self.store.load_cookies()
            existing = {self.identity(c) for c in self.context.cookies()}
            now = time.time()
            missing = [c for c in saved if (c.get('expires', -1) == -1 or c.get('expires', 0) > now) and self.identity(c) not in existing]
            if missing:
                self.context.add_cookies(missing)
                self.report('已恢复本机保存的浏览器会话。若平台登录已过期，请重新登录一次。')
            return len(missing)
        except Exception:
            self.report('已保存的浏览器会话未能恢复；可正常登录，登录后会自动更新保存。')
            return 0

    def maybe_save(self, force=False):
        now = time.monotonic()
        if not force and now-self.last_save < self.interval:
            return
        self.last_save = now
        try:
            cookies = self.context.cookies()
            digest = hashlib.sha256(json.dumps(cookies, sort_keys=True).encode()).digest()
            if digest != self.last_digest:
                self.store.save_cookies(cookies)
                self.last_digest = digest
                self.warned = False
        except Exception:
            if not self.warned:
                self.report('本次登录状态未能保存；已保留上一次快照。')
                self.warned = True
