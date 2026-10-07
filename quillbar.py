#!/usr/bin/env python3
"""
Quillbar - select text in any Windows app, rewrite it with AI, in place.

A single-line floating toolbar (Rewrite, Paraphrase, Formal, Friendly, Shorter,
Fix, Translate, Reply, Summarize ... plus your own buttons) that works in WeChat,
WhatsApp, Telegram, Outlook, browsers and every other app. Bring your own key
for any OpenAI-compatible API (OpenAI, DeepSeek, Qwen, Kimi, Gemini, Claude,
OpenRouter, Groq, DMXAPI, Ollama, LM Studio).

Run
    pip install PySide6 requests
    pythonw quillbar.py            (or: python quillbar.py)

Build a standalone exe
    pip install pyinstaller
    pyinstaller --onefile --windowed --name Quillbar quillbar.py

Triggers
    Ctrl+Alt+Space      show the bar for the selected text
    1-9 / Shift+1-9     run a button / preview it first   (while the bar is open)
    Ctrl+Alt+<key>      per-button shortcut, replaces instantly
    Mouse mode          bar pops up after you select text with the mouse (tray menu)
    Double-tap Ctrl     optional, Settings > Mouse & smart

Preview card  Enter replace/copy . Tab retry . C copy . D toggle diff . Esc close
              type in "Refine..." to adjust (e.g. "shorter", "add a thank-you")

Config  %APPDATA%\\Quillbar\\config.json (API keys encrypted with Windows DPAPI)
Requires Windows 10/11 and Python 3.10+.
"""
from __future__ import annotations
import base64
import copy
import ctypes
import datetime as _dt
import difflib
import html
import json
import os
import re
import requests
import sys
import threading
import time
import uuid

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from PySide6.QtCore import (QMimeData, QObject, QPoint, QRectF, QSize, Qt, QThread, QTimer, Signal)
from PySide6.QtGui import (QAction, QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QIcon,
                           QKeySequence, QPainter, QPen, QPixmap)
from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView, QApplication, QCheckBox, QComboBox,
                               QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QGraphicsDropShadowEffect,
                               QHBoxLayout, QHeaderView, QKeySequenceEdit, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
                               QSplitter, QStackedWidget, QSystemTrayIcon, QTableWidget, QTableWidgetItem,
                               QTextBrowser, QVBoxLayout, QWidget)


#==============================================================================
# win32: Thin ctypes layer over the Win32 calls Quillbar needs.
#==============================================================================

IS_WIN = sys.platform == "win32"

VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
VK_LWIN, VK_RWIN = 0x5B, 0x5C
VK_LBUTTON, VK_RBUTTON = 0x01, 0x02
MODIFIER_VKS = (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN)

if IS_WIN:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)

    ULONG_PTR = ctypes.c_size_t
    LONG_PTR = ctypes.c_ssize_t

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    if hasattr(user32, "SetWindowLongPtrW"):
        _SetWL, _GetWL = user32.SetWindowLongPtrW, user32.GetWindowLongPtrW
    else:  # 32-bit Python
        _SetWL, _GetWL = user32.SetWindowLongW, user32.GetWindowLongW
    _SetWL.argtypes = [wintypes.HWND, ctypes.c_int, LONG_PTR]
    _SetWL.restype = LONG_PTR
    _GetWL.argtypes = [wintypes.HWND, ctypes.c_int]
    _GetWL.restype = LONG_PTR
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]


# ---------------------------------------------------------------- windows

def _h(hwnd) -> int:
    return int(hwnd or 0)


def get_foreground() -> int:
    return _h(user32.GetForegroundWindow()) if IS_WIN else 0


def root_of(hwnd: int) -> int:
    if not IS_WIN or not hwnd:
        return 0
    return _h(user32.GetAncestor(hwnd, 2)) or hwnd  # GA_ROOT


def is_window(hwnd: int) -> bool:
    return bool(IS_WIN and hwnd and user32.IsWindow(hwnd))


def window_pid(hwnd: int) -> int:
    if not IS_WIN or not hwnd:
        return 0
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def is_own_window(hwnd: int) -> bool:
    return bool(hwnd) and window_pid(hwnd) == os.getpid()


def process_name(hwnd: int) -> str:
    """'WeChat', 'Telegram', 'OUTLOOK' ... (exe stem) for the window's process."""
    if not IS_WIN or not hwnd:
        return ""
    pid = window_pid(hwnd)
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.splitext(os.path.basename(buf.value))[0]
        return ""
    finally:
        kernel32.CloseHandle(h)


def window_title(hwnd: int) -> str:
    if not IS_WIN or not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def focus_window(hwnd: int, timeout: float = 0.6) -> bool:
    """Bring hwnd to the foreground. Returns True when it actually is."""
    if not is_window(hwnd):
        return False
    target = root_of(hwnd)
    if root_of(get_foreground()) == target:
        return True
    if user32.IsIconic(target):
        user32.ShowWindow(target, 9)  # SW_RESTORE
    fg = get_foreground()
    cur_tid = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = False
    if fg_tid and fg_tid != cur_tid:
        attached = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
    try:
        user32.BringWindowToTop(target)
        user32.SetForegroundWindow(target)
    finally:
        if attached:
            user32.AttachThreadInput(cur_tid, fg_tid, False)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if root_of(get_foreground()) == target:
            return True
        time.sleep(0.02)
    return False


def set_no_activate(hwnd: int, on: bool = True) -> None:
    """WS_EX_NOACTIVATE: clicks on our toolbar never steal focus from the chat box."""
    if not IS_WIN or not hwnd:
        return
    GWL_EXSTYLE, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW = -20, 0x08000000, 0x80
    style = _GetWL(hwnd, GWL_EXSTYLE)
    style = (style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW) if on else (style & ~WS_EX_NOACTIVATE)
    _SetWL(hwnd, GWL_EXSTYLE, style)


# ---------------------------------------------------------------- keyboard

def key_down(vk: int) -> bool:
    return bool(IS_WIN and (user32.GetAsyncKeyState(vk) & 0x8000))


def held_modifiers() -> list[int]:
    return [vk for vk in MODIFIER_VKS if key_down(vk)]


def wait_modifiers_released(timeout: float = 0.8) -> list[int]:
    """Hotkeys fire while the user still holds Ctrl/Alt. Sending Ctrl+C then would
    become Ctrl+Alt+C, so wait. Returns any modifiers still stuck after timeout."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not held_modifiers():
            return []
        time.sleep(0.01)
    return held_modifiers()


def _kbd(vk: int, up: bool) -> "INPUT":
    inp = INPUT()
    inp.type = 1  # INPUT_KEYBOARD
    inp.u.ki = KEYBDINPUT(vk, 0, 2 if up else 0, 0, 0)
    return inp


def send_ctrl(letter: str) -> bool:
    """Send Ctrl+<letter>. Releases stuck modifiers inside the Ctrl chord so a
    lone Alt-up can't open a menu bar."""
    if not IS_WIN:
        return False
    stuck = wait_modifiers_released()
    seq = [_kbd(VK_CONTROL, False)]
    seq += [_kbd(vk, True) for vk in stuck if vk != VK_CONTROL]
    vk = ord(letter.upper())
    seq += [_kbd(vk, False), _kbd(vk, True), _kbd(VK_CONTROL, True)]
    arr = (INPUT * len(seq))(*seq)
    return user32.SendInput(len(seq), arr, ctypes.sizeof(INPUT)) == len(seq)


def clipboard_seq() -> int:
    return int(user32.GetClipboardSequenceNumber()) if IS_WIN else 0


# ---------------------------------------------------------------- secrets (DPAPI)

def protect(text: str) -> str:
    """Encrypt with the current Windows user's key. Plain base64 elsewhere."""
    if not text:
        return ""
    raw = text.encode("utf-8")
    if not IS_WIN:
        return "b64:" + base64.b64encode(raw).decode()
    buf = ctypes.create_string_buffer(raw, len(raw))
    blob_in = DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(blob_in), "Quillbar", None, None, None, 0,
                                    ctypes.byref(blob_out)):
        return "b64:" + base64.b64encode(raw).decode()
    try:
        data = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)
    return "dpapi:" + base64.b64encode(data).decode()


def unprotect(token: str) -> str:
    if not token:
        return ""
    kind, _, payload = token.partition(":")
    try:
        data = base64.b64decode(payload)
    except Exception:
        return ""
    if kind == "b64":
        return data.decode("utf-8", "replace")
    if kind != "dpapi" or not IS_WIN:
        return ""
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0,
                                      ctypes.byref(blob_out)):
        return ""
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData).decode("utf-8", "replace")
    finally:
        kernel32.LocalFree(blob_out.pbData)


# ---------------------------------------------------------------- misc

_mutex = None


def single_instance(name: str = "Local\\QuillbarSingleton") -> bool:
    global _mutex
    if not IS_WIN:
        return True
    _mutex = kernel32.CreateMutexW(None, False, name)
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def startup_command() -> str:
    if getattr(sys, "frozen", False):  # built .exe
        return f'"{sys.executable}"'
    exe = sys.executable
    if exe.lower().endswith("python.exe"):  # no console window at boot
        cand = exe[:-10] + "pythonw.exe"
        exe = cand if os.path.exists(cand) else exe
    main = os.path.abspath(sys.argv[0])  # the script that launched us
    return f'"{exe}" "{main}"'


def is_launch_at_startup(app_name: str = "Quillbar") -> bool:
    if not IS_WIN:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, app_name)[0] == startup_command()
    except OSError:
        return False


def set_launch_at_startup(enabled: bool, app_name: str = "Quillbar") -> None:
    """HKCU Run key: per-user, no admin. Rewritten each launch so it follows the
    app if you move the folder."""
    if not IS_WIN:
        return
    import winreg
    cmd = startup_command()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, app_name, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, app_name)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------- mouse

if IS_WIN:
    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class CURSORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("hCursor", ctypes.c_void_p), ("ptScreenPos", POINT)]

    user32.LoadCursorW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.LoadCursorW.restype = ctypes.c_void_p
    user32.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
    _IBEAM = user32.LoadCursorW(None, ctypes.c_void_p(32513))  # IDC_IBEAM


def cursor_pos() -> tuple[int, int]:
    if not IS_WIN:
        return 0, 0
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def cursor_is_ibeam() -> bool:
    """True when the pointer is the text cursor, i.e. it's over editable/selectable text."""
    if not IS_WIN:
        return False
    ci = CURSORINFO()
    ci.cbSize = ctypes.sizeof(CURSORINFO)
    if not user32.GetCursorInfo(ctypes.byref(ci)):
        return False
    return bool(ci.hCursor) and ci.hCursor == _IBEAM


#==============================================================================
# hotkeys: System-wide hotkeys via Win32 RegisterHotKey.
#==============================================================================

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT, WM_APP_TEMP = 0x0312, 0x0012, 0x8000 + 1

_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT,
         "shift": MOD_SHIFT, "win": MOD_WIN, "meta": MOD_WIN}

_VK = {
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "ins": 0x2D, "insert": 0x2D, "del": 0x2E, "delete": 0x2E,
    "home": 0x24, "end": 0x23, "pgup": 0x21, "pageup": 0x21, "pgdown": 0x22, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "`": 0xC0, "~": 0xC0, "-": 0xBD, "=": 0xBB, "+": 0xBB, "[": 0xDB, "]": 0xDD,
    "\\": 0xDC, ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
}
_VK.update({f"f{i}": 0x6F + i for i in range(1, 25)})


def parse_hotkey(text: str, allow_bare: bool = False) -> tuple[int, int]:
    """'Ctrl+Alt+R' -> (MOD_CONTROL|MOD_ALT, 0x52). Raises ValueError."""
    s = (text or "").strip()
    if not s:
        raise ValueError("empty hotkey")
    if s.endswith("++"):
        parts, key = s[:-2].split("+"), "+"
    else:
        parts = s.split("+")
        key = parts.pop()
    mods = 0
    for p in parts:
        m = _MODS.get(p.strip().lower())
        if m is None:
            raise ValueError(f"unknown modifier '{p}'")
        mods |= m
    k = key.strip().lower()
    if len(k) == 1 and k.isascii() and k.isalnum():
        vk = ord(k.upper())
    elif k in _VK:
        vk = _VK[k]
    else:
        raise ValueError(f"unsupported key '{key}'")
    is_fkey = 0x70 <= vk <= 0x87
    if not mods and not (allow_bare or is_fkey):
        raise ValueError("add Ctrl, Alt, Shift or Win")
    return mods, vk


def normalize(text: str) -> str:
    """Canonical display form, e.g. 'alt+ctrl+r' -> 'Ctrl+Alt+R'."""
    mods, vk = parse_hotkey(text, allow_bare=True)
    out = [n for n, m in (("Ctrl", MOD_CONTROL), ("Alt", MOD_ALT),
                          ("Shift", MOD_SHIFT), ("Win", MOD_WIN)) if mods & m]
    key = text.strip()[-1:] if text.strip().endswith("++") else text.split("+")[-1].strip()
    out.append(key.upper() if len(key) == 1 else key[:1].upper() + key[1:].lower())
    return "+".join(out)


class HotkeyThread(QThread):
    """Owns a Win32 message loop. `triggered(name)` fires on the Qt main thread."""

    triggered = Signal(str)
    failed = Signal(list)  # names whose hotkey is already taken by another app

    def __init__(self, bindings: dict[str, str], parent=None):
        super().__init__(parent)
        self._bindings = dict(bindings)
        self._tid = 0
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._pending_temp: dict[str, str] = {}

    # called from the Qt thread -------------------------------------------
    def set_temp(self, bindings: dict[str, str] | None) -> None:
        """Short-lived hotkeys (1-9, Esc) active only while the toolbar is open."""
        if not IS_WIN:
            return
        with self._lock:
            self._pending_temp = dict(bindings or {})
        if self._ready.wait(1.0):
            user32.PostThreadMessageW(self._tid, WM_APP_TEMP, 0, 0)

    def stop(self) -> None:
        if IS_WIN and self._ready.wait(1.0):
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        self.wait(2000)

    # worker thread -------------------------------------------------------
    def run(self) -> None:  # noqa: C901
        if not IS_WIN:
            self._ready.set()
            return
        from ctypes import wintypes
        u32 = user32
        self._tid = kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        # create the thread's message queue before anyone posts to it
        u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)

        names: dict[int, str] = {}
        temp_ids: list[int] = []
        failures: list[str] = []
        for i, (name, combo) in enumerate(self._bindings.items(), start=1):
            try:
                mods, vk = parse_hotkey(combo)
            except ValueError:
                failures.append(name)
                continue
            if u32.RegisterHotKey(None, i, mods | MOD_NOREPEAT, vk):
                names[i] = name
            else:
                failures.append(name)
        self._ready.set()
        if failures:
            self.failed.emit(failures)

        try:
            while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    name = names.get(int(msg.wParam))
                    if name:
                        self.triggered.emit(name)
                elif msg.message == WM_APP_TEMP:
                    for hid in temp_ids:
                        u32.UnregisterHotKey(None, hid)
                        names.pop(hid, None)
                    temp_ids.clear()
                    with self._lock:
                        pending = dict(self._pending_temp)
                    for j, (name, combo) in enumerate(pending.items(), start=1000):
                        try:
                            mods, vk = parse_hotkey(combo, allow_bare=True)
                        except ValueError:
                            continue
                        if u32.RegisterHotKey(None, j, mods | MOD_NOREPEAT, vk):
                            names[j] = name
                            temp_ids.append(j)
        finally:
            for hid in list(names):
                u32.UnregisterHotKey(None, hid)


#==============================================================================
# config: Settings persisted as JSON in %APPDATA%\Quillbar\config.json.
#==============================================================================

APP_NAME = "Quillbar"
CONFIG_DIR = Path(os.environ.get("APPDATA") or Path.home() / ".config") / APP_NAME
CONFIG_PATH = CONFIG_DIR / "config.json"

TOOLBAR_KEY = "__toolbar__"

SYSTEM_PROMPT = (
    "You are a writing assistant built into the user's chat and email apps "
    "(WeChat, WhatsApp, Telegram, Outlook, Gmail). You get an instruction and a "
    "piece of text the user wrote. Apply the instruction and return ONLY the "
    "resulting text: no preamble, no quotes, no explanations, no markdown unless "
    "the input uses it. Keep the original language unless told to translate. "
    "Keep names, numbers, links, emojis and line breaks. If the text is a "
    "question or request addressed to someone else, do NOT answer it; only "
    "transform it."
)

# Used for actions that produce something new from the text (reply, summary...).
GENERATE_PROMPT = (
    "You are a writing assistant built into the user's chat and email apps. You get "
    "an instruction and a piece of text. Follow the instruction and return ONLY the "
    "requested output: no preamble, no explanations about what you did. Answer in "
    "the language of the text unless told otherwise. Plain text; use simple '- ' "
    "bullets only if asked for a list."
)

# kind: transform = rewrite the user's own text; generate = new output (reply, summary)
# mode: replace = swap in place; preview = show result card first; copy = clipboard only
_A = [
    # id, name, hotkey, kind, mode, enabled, in_toolbar, prompt
    ("rewrite", "Rewrite", "Ctrl+Alt+R", "transform", "replace", True, True,
     "Rewrite this to read more clearly and naturally. Keep meaning, tone and roughly the same length."),
    ("paraphrase", "Paraphrase", "Ctrl+Alt+P", "transform", "replace", True, True,
     "Paraphrase this using different wording and sentence structure. Keep the exact meaning."),
    ("formal", "Formal", "Ctrl+Alt+F", "transform", "replace", True, True,
     "Make this formal and professional, suitable for work email. Polite, precise, no slang."),
    ("friendly", "Friendly", "Ctrl+Alt+K", "transform", "replace", True, True,
     "Make this warm, friendly and casual, like a message to a colleague you get on with."),
    ("shorter", "Shorter", "Ctrl+Alt+S", "transform", "replace", True, True,
     "Make this shorter and more direct. Cut filler; keep every key point."),
    ("fix", "Fix", "Ctrl+Alt+G", "transform", "replace", True, True,
     "Fix spelling, grammar and punctuation only. Change nothing else."),
    ("translate", "Translate", "Ctrl+Alt+T", "transform", "replace", True, True,
     "Translate this. If it is mainly in {lang1}, translate it into {lang2}; otherwise "
     "translate it into {lang1}. Natural and fluent, same tone."),
    ("reply", "Reply", "Ctrl+Alt+Y", "generate", "preview", True, True,
     "This is a message I received in {app}. Write my reply to it: same language, "
     "matching tone, natural and brief. Return only the reply."),
    ("english", "To English", "Ctrl+Alt+E", "transform", "replace", True, True,
     "Translate this into natural, fluent English."),
    ("summarize", "Summarize", "", "generate", "preview", True, True,
     "Summarize this in 2-4 short sentences."),
    ("keypoints", "Key points", "", "generate", "preview", True, True,
     "List the key points and any action items as short '- ' bullets."),
    ("explain", "Explain", "", "generate", "preview", True, True,
     "Explain what this means in simple words. If it's in another language, explain in {lang1}."),
    ("polite", "More polite", "", "transform", "replace", False, True,
     "Make this more polite and tactful without changing the request."),
    ("confident", "Confident", "", "transform", "replace", False, True,
     "Make this sound confident and assertive. Remove hedging and apologies."),
    ("clear", "Plain & clear", "", "transform", "replace", False, True,
     "Rewrite in plain style: short sentences, clear verbs, active voice, no jargon."),
    ("persuasive", "Persuasive", "", "transform", "replace", False, True,
     "Make this persuasive and compelling while staying honest and natural."),
    ("expand", "Expand", "", "transform", "replace", False, True,
     "Expand this with a bit more detail and context. Keep the same tone."),
    ("bullets", "Bullets", "", "transform", "replace", False, True,
     "Turn this into concise '- ' bullet points."),
    ("emojify", "Add emoji", "", "transform", "replace", False, True,
     "Add a few fitting emojis to this chat message. Do not change the words."),
    ("chinese", "To Chinese", "", "transform", "replace", False, True,
     "Translate this into natural Simplified Chinese."),
]
DEFAULT_ACTIONS = [
    {"id": i, "name": n, "hotkey": h, "kind": k, "mode": m, "enabled": e,
     "in_toolbar": t, "prompt": p, "provider": ""}
    for i, n, h, k, m, e, t, p in _A
]

DEFAULT_APP_RULES = [
    {"app": "WeChat, Weixin, QQ", "instruction":
     "This is a chat message in {app}. Keep it conversational and brief. No greeting or "
     "sign-off unless the original has one."},
    {"app": "WhatsApp, Telegram, Signal, Line, Discord, Slack, Teams", "instruction":
     "This is a chat message in {app}. Keep it conversational and brief. No email formatting."},
    {"app": "OUTLOOK, olk, Thunderbird, Foxmail, Mail", "instruction":
     "This is an email in {app}. Keep email structure (greeting, body, sign-off) if present."},
]

CHAT_APPS = "WeChat, Weixin, QQ, WhatsApp, Telegram, Signal, Line, Discord, Slack, Teams"

PROVIDER_PRESETS = {
    "OpenAI": ("https://api.openai.com/v1", "gpt-4.1-mini"),
    "DeepSeek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "Qwen (DashScope)": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "Kimi (Moonshot)": ("https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    "Google Gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash"),
    "Anthropic Claude": ("https://api.anthropic.com/v1", "claude-haiku-4-5"),
    "DMXAPI": ("https://www.dmxapi.com/v1", "gpt-4.1-mini"),
    "OpenRouter": ("https://openrouter.ai/api/v1", "openai/gpt-4.1-mini"),
    "Groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "Ollama (local)": ("http://localhost:11434/v1", "llama3.1:8b"),
    "LM Studio (local)": ("http://localhost:1234/v1", "local-model"),
}


def new_provider(preset: str = "OpenAI") -> dict:
    base, model = PROVIDER_PRESETS.get(preset, PROVIDER_PRESETS["OpenAI"])
    return {"id": uuid.uuid4().hex[:8], "name": preset, "base_url": base,
            "api_key": "", "model": model, "temperature": 0.4}


DEFAULTS = {
    "version": 2,
    "toolbar_hotkey": "Ctrl+Alt+Space",
    "theme": "auto",            # auto | dark | light
    "max_visible": 6,
    "show_numbers": True,
    "show_ask": True,
    "restore_clipboard": True,
    "launch_at_startup": True,
    "system_prompt": SYSTEM_PROMPT,
    "double_tap": "off",        # off | ctrl | shift  -> opens the bar
    "mouse_mode": False,        # pop the bar after a mouse text selection
    "mouse_ibeam_only": True,
    "mouse_exclude": "explorer, Taskmgr, mstsc, devenv, Photoshop, steam",
    "smart_select": True,       # nothing selected in a chat app -> use whole input box
    "smart_select_apps": CHAT_APPS,
    "redact": False,            # mask emails / phones / card numbers before sending
    "keep_history": True,       # in-memory only, cleared on exit
    "style": "",                # "About me & my style", added to every prompt
    "lang1": "English",
    "lang2": "Simplified Chinese",
    "app_rules": DEFAULT_APP_RULES,
    "known_defaults": [],
    "active_provider": "",
    "providers": [],
    "actions": DEFAULT_ACTIONS,
}


class Config:
    def __init__(self, data: dict):
        self.data = data

    # --- persistence -----------------------------------------------------
    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Config":
        data = copy.deepcopy(DEFAULTS)
        if path.exists():
            try:
                stored = json.loads(path.read_text("utf-8"))
                data.update({k: v for k, v in stored.items() if k in DEFAULTS})
            except (OSError, ValueError):
                pass
        for p in data["providers"]:
            p["api_key"] = unprotect(p.pop("api_key_enc", "")) or p.get("api_key", "")
        if not data["providers"]:
            p = new_provider("OpenAI")
            data["providers"] = [p]
        if data["active_provider"] not in {p["id"] for p in data["providers"]}:
            data["active_provider"] = data["providers"][0]["id"]
        # Offer newly shipped built-in buttons once, never re-adding deleted ones.
        have = {a.get("id") for a in data["actions"]}
        known = set(data["known_defaults"]) | have
        taken = {a.get("hotkey") for a in data["actions"] if a.get("hotkey")}
        for d in DEFAULT_ACTIONS:
            if d["id"] not in known:
                new = copy.deepcopy(d)
                if new["hotkey"] in taken:
                    new["hotkey"] = ""
                data["actions"].append(new)
        data["known_defaults"] = sorted(known | {d["id"] for d in DEFAULT_ACTIONS})
        defaults_by_id = {d["id"]: d for d in DEFAULT_ACTIONS}
        for a in data["actions"]:
            a.setdefault("id", uuid.uuid4().hex[:8])
            d = defaults_by_id.get(a["id"], {})
            a.setdefault("enabled", True)
            a.setdefault("in_toolbar", True)
            a.setdefault("hotkey", "")
            a.setdefault("kind", d.get("kind", "transform"))
            a.setdefault("mode", d.get("mode", "replace"))
            a.setdefault("provider", "")
        return cls(data)

    def save(self, path: Path = CONFIG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        out = copy.deepcopy(self.data)
        for p in out["providers"]:
            p["api_key_enc"] = protect(p.pop("api_key", ""))
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, indent=2, ensure_ascii=False), "utf-8")
        os.replace(tmp, path)

    # --- helpers ---------------------------------------------------------
    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v

    @property
    def provider(self) -> dict:
        pid = self.data["active_provider"]
        return next((p for p in self.data["providers"] if p["id"] == pid), self.data["providers"][0])

    def provider_for(self, action: dict | None) -> dict:
        pid = (action or {}).get("provider") or ""
        return next((p for p in self.data["providers"] if p["id"] == pid), self.provider)

    @property
    def actions(self) -> list[dict]:
        return [a for a in self.data["actions"] if a.get("enabled", True)]

    @property
    def toolbar_actions(self) -> list[dict]:
        return [a for a in self.actions if a.get("in_toolbar", True)]

    def action(self, aid: str) -> dict | None:
        return next((a for a in self.actions if a["id"] == aid), None)

    def bindings(self) -> dict[str, str]:
        b = {TOOLBAR_KEY: self.data["toolbar_hotkey"]}
        for a in self.actions:
            if a.get("hotkey"):
                b[a["id"]] = a["hotkey"]
        return b

    @property
    def is_configured(self) -> bool:
        p = self.provider
        local = "localhost" in p["base_url"] or "127.0.0.1" in p["base_url"]
        return bool(p.get("model")) and (bool(p.get("api_key")) or local)


#==============================================================================
# theme: 
#==============================================================================

DARK = {
    "bg": "#1B1B1F", "bg2": "#232329", "border": "#2E2E36", "text": "#ECECF1",
    "dim": "#8B8B96", "hover": "#2A2A31", "press": "#34343C",
    "accent": "#7AA2FF", "accent_text": "#0B0F1A", "danger": "#FF7A7A",
    "add_bg": "#1E3A2B", "add_fg": "#8EE6B0", "del_fg": "#FF8F8F",
}
LIGHT = {
    "bg": "#FFFFFF", "bg2": "#F5F5F7", "border": "#E3E3E8", "text": "#18181B",
    "dim": "#74747E", "hover": "#F0F0F3", "press": "#E6E6EB",
    "accent": "#3E6BFF", "accent_text": "#FFFFFF", "danger": "#D93A3A",
    "add_bg": "#DDF5E6", "add_fg": "#11633A", "del_fg": "#C23B3B",
}


def system_dark() -> bool:
    try:
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
        except OSError:
            pass
    return True


def palette(mode: str) -> dict:
    dark = system_dark() if mode == "auto" else mode == "dark"
    return DARK if dark else LIGHT


def qc(hex_: str, alpha: int | None = None) -> QColor:
    c = QColor(hex_)
    if alpha is not None:
        c.setAlpha(alpha)
    return c


def ui_font(pt: float = 9.5, weight: QFont.Weight = QFont.Weight.Medium) -> QFont:
    f = QFont()
    f.setFamilies(["Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI",
                   "Yu Gothic UI", "Inter", "Sans Serif"])
    f.setPointSizeF(pt)
    f.setWeight(weight)
    return f


def settings_qss(p: dict) -> str:
    return f"""
    QWidget {{ color: {p['text']}; font-size: 10pt; }}
    QDialog, #page {{ background: {p['bg']}; }}
    QLabel#h1 {{ font-size: 15pt; font-weight: 600; }}
    QLabel#hint {{ color: {p['dim']}; font-size: 9pt; }}
    QLabel#section {{ color: {p['dim']}; font-size: 8.5pt; font-weight: 600;
                      letter-spacing: 1px; padding-top: 6px; }}
    QListWidget#nav {{ background: {p['bg2']}; border: none; padding: 10px 6px;
                       outline: 0; }}
    QListWidget#nav::item {{ padding: 9px 12px; border-radius: 7px; margin: 1px 2px; }}
    QListWidget#nav::item:selected {{ background: {p['hover']}; color: {p['text']}; }}
    QListWidget#nav::item:hover:!selected {{ background: {p['hover']}; }}
    QListWidget#list {{ background: {p['bg2']}; border: 1px solid {p['border']};
                        border-radius: 8px; padding: 4px; outline: 0; }}
    QListWidget#list::item {{ padding: 7px 8px; border-radius: 6px; }}
    QListWidget#list::item:selected {{ background: {p['hover']}; color: {p['text']}; }}
    QLineEdit, QPlainTextEdit, QComboBox, QDoubleSpinBox, QSpinBox, QKeySequenceEdit {{
        background: {p['bg2']}; border: 1px solid {p['border']}; border-radius: 7px;
        padding: 6px 8px; selection-background-color: {p['accent']};
        selection-color: {p['accent_text']}; }}
    QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QDoubleSpinBox:focus,
    QSpinBox:focus {{ border: 1px solid {p['accent']}; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox QAbstractItemView {{ background: {p['bg2']}; border: 1px solid {p['border']};
        selection-background-color: {p['hover']}; selection-color: {p['text']}; }}
    QPushButton {{ background: {p['bg2']}; border: 1px solid {p['border']};
                   border-radius: 7px; padding: 6px 14px; }}
    QPushButton:hover {{ background: {p['hover']}; }}
    QPushButton:pressed {{ background: {p['press']}; }}
    QPushButton#primary {{ background: {p['accent']}; color: {p['accent_text']};
                           border: none; font-weight: 600; }}
    QPushButton#primary:hover {{ background: {p['accent']}; }}
    QPushButton#danger {{ color: {p['danger']}; }}
    QCheckBox {{ spacing: 8px; }}
    QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px;
                            border: 1px solid {p['border']}; background: {p['bg2']}; }}
    QCheckBox::indicator:checked {{ background: {p['accent']}; border: 1px solid {p['accent']}; }}
    QScrollBar:vertical {{ background: transparent; width: 8px; }}
    QScrollBar::handle:vertical {{ background: {p['border']}; border-radius: 4px; min-height: 24px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QTableWidget {{ background: {p['bg2']}; border: 1px solid {p['border']}; border-radius: 8px;
                    gridline-color: {p['border']}; outline: 0; }}
    QTableWidget::item:selected {{ background: {p['hover']}; color: {p['text']}; }}
    QHeaderView::section {{ background: {p['bg']}; color: {p['dim']}; border: none;
                            border-bottom: 1px solid {p['border']}; padding: 6px; font-weight: 600; }}
    QToolTip {{ background: {p['bg2']}; color: {p['text']}; border: 1px solid {p['border']};
               padding: 4px 6px; }}
    QMenu {{ background: {p['bg']}; border: 1px solid {p['border']}; padding: 4px;
             border-radius: 8px; }}
    QMenu::item {{ padding: 6px 18px 6px 12px; border-radius: 5px; }}
    QMenu::item:selected {{ background: {p['hover']}; }}
    QMenu::separator {{ height: 1px; background: {p['border']}; margin: 4px 6px; }}
    """


#==============================================================================
# llm: OpenAI-compatible chat client. One code path covers OpenAI, DeepSeek, Qwen,
#==============================================================================

class LLMError(Exception):
    pass


def normalize_base(base: str) -> str:
    """Accept what people paste: trailing '/', full '/chat/completions' or '/models' URL."""
    b = (base or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/models"):
        if b.lower().endswith(suffix):
            b = b[: -len(suffix)].rstrip("/")
    if b and "://" not in b:
        b = "https://" + b
    return b


def _url(base: str, path: str) -> str:
    return normalize_base(base) + path


def _candidates(base: str) -> list[str]:
    """Bases to try, in order: as typed, +/v1, then with 'www.' (relays such as
    DMXAPI serve the API on www.<domain> while the bare domain is the website)."""
    from urllib.parse import urlsplit, urlunsplit
    out = [base]
    if not base.endswith("/v1"):
        out.append(base + "/v1")
    parts = urlsplit(base)
    host = parts.hostname or ""
    if host and not host.startswith("www.") and host.count(".") == 1:
        www = urlunsplit(parts._replace(netloc=parts.netloc.replace(host, "www." + host, 1)))
        out.append(www)
        if not www.endswith("/v1"):
            out.append(www + "/v1")
    return list(dict.fromkeys(out))


def _with_v1_fallback(provider: dict, path: str, send):
    """On 404, retry likely-correct bases once each and remember the one that works."""
    bases = _candidates(normalize_base(provider["base_url"]))
    first = None
    for b in bases:
        r = send(b + path)
        if first is None:
            first = r
        if r.status_code != 404:
            if b != bases[0]:
                provider["base_url"] = b
            return r, b + path
    tried = "\n".join("tried " + b + path for b in bases)
    return first, tried


def _headers(p: dict) -> dict:
    h = {"Content-Type": "application/json"}
    if p.get("api_key"):
        h["Authorization"] = f"Bearer {p['api_key']}"
        if "anthropic.com" in p.get("base_url", ""):
            h["x-api-key"] = p["api_key"]
            h["anthropic-version"] = "2023-06-01"
    if "openrouter.ai" in p.get("base_url", ""):
        h["HTTP-Referer"] = "https://github.com/quillbar"
        h["X-Title"] = "Quillbar"
    return h


def _error_text(r: requests.Response, url: str = "") -> str:
    ctype = r.headers.get("content-type", "") if hasattr(r, "headers") else ""
    if "html" in ctype.lower():
        msg = "got a web page, not an API. Base URL points at the website, not the API host"
    else:
        try:
            j = r.json()
            err = j.get("error", j)
            msg = err.get("message") if isinstance(err, dict) else str(err)
        except ValueError:
            msg = (r.text or "")[:200]
    hint = {401: "check API key", 403: "key lacks access", 404: "wrong base URL or model",
            429: "rate limit or no credit"}.get(r.status_code, "")
    low = msg.lower()
    if "no available channel" in low or "model_not_found" in low or "does not exist" in low:
        hint = "URL and key OK. This model isn't enabled for your key: press Fetch models"
    where = f"\n{url}" if url else ""
    return f"HTTP {r.status_code}{' (' + hint + ')' if hint else ''}: {msg}{where}"


def build_messages(system: str, instruction: str, text: str) -> list[dict]:
    user = (f"Instruction: {instruction}\n\n"
            "Return only the transformed text.\n\n"
            f"<text>\n{text}\n</text>")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def complete(provider: dict, system: str, instruction: str, text: str,
             timeout: float = 60) -> str:
    body = {"model": provider["model"],
            "messages": build_messages(system, instruction, text),
            "temperature": float(provider.get("temperature", 0.4)),
            "stream": False}
    try:
        r, url = _with_v1_fallback(provider, "/chat/completions", lambda u: requests.post(
            u, headers=_headers(provider), json=body, timeout=timeout))
    except requests.Timeout:
        raise LLMError("Request timed out")
    except requests.RequestException as e:
        raise LLMError(f"Network error: {e.__class__.__name__}")
    if r.status_code >= 400:
        raise LLMError(_error_text(r, url))
    try:
        content = r.json()["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError):
        raise LLMError("Unexpected response from provider")
    out = clean_output(content, text)
    if not out:
        raise LLMError("Model returned empty text")
    return out


def list_models(provider: dict, timeout: float = 15) -> list[str]:
    try:
        r, url = _with_v1_fallback(provider, "/models", lambda u: requests.get(
            u, headers=_headers(provider), timeout=timeout))
    except requests.RequestException as e:
        raise LLMError(f"Network error: {e.__class__.__name__}")
    if r.status_code >= 400:
        raise LLMError(_error_text(r, url))
    data = r.json().get("data", [])
    return sorted({m.get("id", "") for m in data if m.get("id")})


_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"^```[\w-]*\n(.*?)\n?```$", re.S)
_TAG = re.compile(r"^<text>\s*(.*?)\s*</text>$", re.S)


def clean_output(s: str, original: str = "") -> str:
    """Strip reasoning blocks, wrapping fences, tags and quotes models add."""
    s = _THINK.sub("", s).strip()
    m = _FENCE.match(s)
    if m and not original.lstrip().startswith("```"):
        s = m.group(1).strip()
    m = _TAG.match(s)
    if m:
        s = m.group(1).strip()
    for q in ('"', "'", "“", "「", "『"):
        close = {"“": "”", "「": "」", "『": "』"}.get(q, q)
        if len(s) > 1 and s.startswith(q) and s.endswith(close) \
                and not original.strip().startswith(q):
            s = s[1:-1].strip()
            break
    return s


def split_ws(text: str) -> tuple[str, str, str]:
    """Keep the user's leading/trailing whitespace (selected newline etc.)."""
    core = text.strip()
    if not core:
        return text, "", ""
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    return lead, core, trail


def stream(provider: dict, system: str, instruction: str, text: str, on_delta,
           cancelled=lambda: False, timeout: float = 60) -> str:
    """Streaming variant. Calls on_delta(chunk) as text arrives; returns cleaned text.
    Falls back transparently when a provider ignores stream=true."""
    body = {"model": provider["model"],
            "messages": build_messages(system, instruction, text),
            "temperature": float(provider.get("temperature", 0.4)),
            "stream": True}
    try:
        r, url = _with_v1_fallback(provider, "/chat/completions", lambda u: requests.post(
            u, headers=_headers(provider), json=body, timeout=timeout, stream=True))
    except requests.Timeout:
        raise LLMError("Request timed out")
    except requests.RequestException as e:
        raise LLMError(f"Network error: {e.__class__.__name__}")
    if r.status_code >= 400:
        raise LLMError(_error_text(r, url))
    ctype = r.headers.get("content-type", "")
    parts: list[str] = []
    if "event-stream" not in ctype:
        try:
            content = r.json()["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMError("Unexpected response from provider")
        on_delta(content)
        parts.append(content)
    else:
        try:
            for raw in r.iter_lines(decode_unicode=False):
                if cancelled():
                    r.close()
                    raise LLMError("Cancelled")
                if not raw or not raw.startswith(b"data:"):
                    continue
                data = raw[5:].strip()
                if data == b"[DONE]":
                    break
                try:
                    j = json.loads(data.decode("utf-8"))
                    delta = j["choices"][0].get("delta", {}).get("content") or ""
                except (ValueError, KeyError, IndexError, TypeError):
                    continue
                if delta:
                    parts.append(delta)
                    on_delta(delta)
        except requests.RequestException as e:
            raise LLMError(f"Stream interrupted: {e.__class__.__name__}")
    out = clean_output("".join(parts), text)
    if not out:
        raise LLMError("Model returned empty text")
    return out


#==============================================================================
# prompts: Prompt assembly: placeholders, personal style, per-app rules, privacy masking.
#==============================================================================

class _SafeDict(dict):
    def __missing__(self, key):  # leave unknown {braces} untouched
        return "{" + key + "}"


def context(cfg, app: str) -> dict:
    now = _dt.datetime.now()
    return _SafeDict(app=app or "this app", date=now.strftime("%Y-%m-%d"),
                     time=now.strftime("%H:%M"), weekday=now.strftime("%A"),
                     lang1=cfg["lang1"], lang2=cfg["lang2"])


def fill(template: str, ctx: dict) -> str:
    try:
        return template.format_map(ctx)
    except (ValueError, IndexError):  # stray single brace in a user prompt
        return template


def app_rule(cfg, app: str) -> str:
    a = (app or "").lower()
    if not a:
        return ""
    for rule in cfg["app_rules"]:
        names = [n.strip().lower() for n in rule.get("app", "").split(",") if n.strip()]
        if a in names:
            return rule.get("instruction", "")
    return ""


def system_for(cfg, action: dict | None, app: str) -> str:
    kind = (action or {}).get("kind", "transform")
    base = cfg["system_prompt"] if kind == "transform" else GENERATE_PROMPT
    ctx = context(cfg, app)
    parts = [base]
    if cfg["style"].strip():
        parts.append("About the user and their preferred style (apply when writing for them):\n"
                     + cfg["style"].strip())
    rule = app_rule(cfg, app)
    if rule:
        parts.append(fill(rule, ctx))
    return "\n\n".join(parts)


def instruction_for(cfg, template: str, app: str) -> str:
    return fill(template, context(cfg, app))


def is_smart_select_app(cfg, app: str) -> bool:
    names = [n.strip().lower() for n in cfg["smart_select_apps"].split(",") if n.strip()]
    return bool(app) and app.lower() in names


# ------------------------------------------------------------------ redaction

_PATTERNS = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("PHONE", re.compile(r"(?<![\w])\+?\d[\d\s().-]{6,}\d(?![\w])")),
]


def redact(text: str) -> tuple[str, dict[str, str]]:
    """Replace emails, card and phone numbers with tokens like [EMAIL_1]."""
    mapping: dict[str, str] = {}
    seen: dict[str, str] = {}
    out = text
    for label, rx in _PATTERNS:
        def sub(m, label=label):
            val = m.group(0)
            if label == "PHONE" and sum(c.isdigit() for c in val) < 7:
                return val
            if val not in seen:
                tok = f"[{label}_{sum(1 for t in mapping if t.startswith('[' + label)) + 1}]"
                seen[val] = tok
                mapping[tok] = val
            return seen[val]
        out = rx.sub(sub, out)
    return out, mapping


def restore(text: str, mapping: dict[str, str]) -> str:
    for tok, val in mapping.items():
        text = text.replace(tok, val)
    return text


REDACT_NOTE = ("Tokens like [EMAIL_1] or [PHONE_1] are placeholders for private data. "
               "Keep every token exactly as written.")


#==============================================================================
# diffview: Word-level diff (CJK-aware) rendered as HTML for the preview card.
#==============================================================================

# words, single CJK/kana/hangul chars, whitespace runs, punctuation
_TOK = re.compile(r"[぀-ヿ㐀-鿿가-힯]|\w+|\s+|[^\w\s]", re.U)


def tokens(s: str) -> list[str]:
    return _TOK.findall(s)


def word_count(s: str) -> int:
    """Latin words + one per CJK character (how people count Chinese/Japanese)."""
    cjk = len(re.findall(r"[぀-ヿ㐀-鿿가-힯]", s))
    latin = len(re.findall(r"[^\W\d_]+|\d+", re.sub(r"[぀-ヿ㐀-鿿가-힯]", " ", s)))
    return cjk + latin


def _esc(s: str) -> str:
    return html.escape(s).replace("\n", "<br>")


def diff_html(old: str, new: str, p: dict) -> str:
    a, b = tokens(old), tokens(new)
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    out = []
    add = f"background:{p['add_bg']};color:{p['add_fg']};border-radius:3px;"
    rem = f"color:{p['del_fg']};text-decoration:line-through;"
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            out.append(_esc("".join(b[j1:j2])))
            continue
        if op in ("replace", "delete"):
            out.append(f'<span style="{rem}">{_esc("".join(a[i1:i2]))}</span>')
        if op in ("replace", "insert"):
            out.append(f'<span style="{add}">{_esc("".join(b[j1:j2]))}</span>')
    return "".join(out)


def plain_html(s: str) -> str:
    return _esc(s)


def change_ratio(old: str, new: str) -> float:
    return 1 - difflib.SequenceMatcher(a=tokens(old), b=tokens(new), autojunk=False).ratio()


#==============================================================================
# textio: Grab the selected text from the focused app, and paste a replacement back
#==============================================================================

def _pump(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)


class TextBridge:
    def __init__(self, restore_clipboard: bool = True):
        self.restore_clipboard = restore_clipboard

    @property
    def cb(self):
        return QApplication.clipboard()

    def _snapshot(self) -> QMimeData | None:
        md = self.cb.mimeData()
        if md is None:
            return None
        copy = QMimeData()
        for f in md.formats():
            try:
                copy.setData(f, md.data(f))
            except Exception:
                pass
        return copy

    def _restore(self, snap: QMimeData | None) -> None:
        if not self.restore_clipboard:
            return
        if snap is None or not snap.formats():
            self.cb.clear()
        else:
            self.cb.setMimeData(snap)

    def capture(self, select_all: bool = False) -> str | None:
        """Ctrl+C in the foreground app. Returns None when nothing was selected
        (clipboard sequence number didn't change -> no stale text is used)."""
        snap = self._snapshot()
        if select_all:
            send_ctrl("a")
            _pump(0.05)
        before = clipboard_seq()
        if not send_ctrl("c"):
            return None
        end = time.monotonic() + 0.45
        changed = False
        while time.monotonic() < end:
            QApplication.processEvents()
            if clipboard_seq() != before:
                changed = True
                _pump(0.03)  # let the source app finish writing all formats
                break
            time.sleep(0.01)
        text = self.cb.text() if changed else None
        self._restore(snap)
        return text

    def paste(self, hwnd: int, text: str) -> bool:
        """Focus the original window and paste. False -> left on clipboard."""
        if not focus_window(hwnd):
            self.cb.setText(text)
            return False
        snap = self._snapshot()
        self.cb.setText(text)
        _pump(0.04)
        ok = send_ctrl("v")
        if not ok:
            return False
        # Restore late: some apps (WeChat) read the clipboard lazily.
        QTimer.singleShot(900, lambda: self._restore(snap))
        return True


#==============================================================================
# toolbar: The single-line floating toolbar.
#==============================================================================

SHADOW = 16


def _shift() -> bool:
    from PySide6.QtWidgets import QApplication
    return bool(QApplication.keyboardModifiers() & Qt.ShiftModifier) or key_down(VK_SHIFT)
RADIUS = 11
BAR_H = 38


class BarButton(QAbstractButton):
    def __init__(self, text: str, number: str = "", tip: str = "", parent=None,
                 glyph: bool = False):
        super().__init__(parent)
        self.setText(text)
        self.number = number
        self.glyph = glyph
        self.p = DARK
        self.setToolTip(tip)
        self.setCursor(Qt.PointingHandCursor)
        self.setFont(ui_font(11.5 if glyph else 9.5,
                                   QFont.Weight.Normal if glyph else QFont.Weight.Medium))
        self.setAttribute(Qt.WA_Hover)

    def sizeHint(self) -> QSize:
        fm = QFontMetrics(self.font())
        w = fm.horizontalAdvance(self.text()) + (20 if not self.glyph else 16)
        if self.number:
            w += QFontMetrics(ui_font(8)).horizontalAdvance(self.number) + 5
        return QSize(max(w, 30), BAR_H - 10)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self.isDown():
            p.setPen(Qt.NoPen)
            p.setBrush(qc(self.p["press"]))
            p.drawRoundedRect(r, 7, 7)
        elif self.underMouse():
            p.setPen(Qt.NoPen)
            p.setBrush(qc(self.p["hover"]))
            p.drawRoundedRect(r, 7, 7)
        x = 10 if not self.glyph else 8
        if self.number:
            nf = ui_font(8, QFont.Weight.Medium)
            p.setFont(nf)
            p.setPen(qc(self.p["dim"]))
            nw = QFontMetrics(nf).horizontalAdvance(self.number)
            p.drawText(QRectF(x, 0, nw, self.height()), Qt.AlignVCenter, self.number)
            x += nw + 5
        p.setFont(self.font())
        p.setPen(qc(self.p["text"] if not self.glyph else self.p["dim"])
                 if not self.underMouse() else qc(self.p["text"]))
        p.drawText(QRectF(x, 0, self.width() - x, self.height()), Qt.AlignVCenter, self.text())


class Mark(QWidget):
    """Brand dot; doubles as a spinner while busy."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(22, BAR_H - 10)
        self.p = DARK
        self.spinning = False
        self.angle = 0
        self._t = QTimer(self, interval=16)
        self._t.timeout.connect(self._tick)

    def spin(self, on: bool):
        self.spinning = on
        self._t.start() if on else self._t.stop()
        self.update()

    def _tick(self):
        self.angle = (self.angle + 8) % 360
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QRectF(5, (self.height() - 12) / 2, 12, 12)
        acc = qc(self.p["accent"])
        if self.spinning:
            p.setPen(QPen(qc(self.p["border"]), 2))
            p.drawEllipse(c)
            pen = QPen(acc, 2)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawArc(c, -self.angle * 16, 100 * 16)
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(acc)
            p.drawEllipse(c.adjusted(2, 2, -2, -2))


class Pill(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.p = DARK

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(qc(self.p["border"]), 1))
        p.setBrush(qc(self.p["bg"]))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), RADIUS, RADIUS)


class Sep(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.p = DARK
        self.setFixedSize(9, BAR_H - 16)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setPen(QPen(qc(self.p["border"]), 1))
        p.drawLine(4, 0, 4, self.height())


class Toolbar(QWidget):
    action = Signal(str, bool)  # id, preview (Shift held)
    history = Signal()
    ask = Signal(str)
    settings = Signal()
    cancel = Signal()
    asking = Signal()

    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.p = DARK
        self.mode = "idle"  # idle | ask | busy
        self._native_set = False
        self._show_ask = True
        self.visible_ids: list[str] = []

        outer = QHBoxLayout(self)
        outer.setContentsMargins(SHADOW, SHADOW - 4, SHADOW, SHADOW + 4)
        self.pill = Pill(self)
        fx = QGraphicsDropShadowEffect(self.pill, blurRadius=28, offset=QPoint(0, 6))
        fx.setColor(QColor(0, 0, 0, 90))
        self.pill.setGraphicsEffect(fx)
        outer.addWidget(self.pill)

        self.row = QHBoxLayout(self.pill)
        self.row.setContentsMargins(4, 5, 5, 5)
        self.row.setSpacing(1)

        self.mark = Mark(self.pill)
        self.row.addWidget(self.mark)
        self.sep1 = Sep(self.pill)
        self.row.addWidget(self.sep1)

        self.btn_box = QWidget(self.pill)
        self.btn_row = QHBoxLayout(self.btn_box)
        self.btn_row.setContentsMargins(0, 0, 0, 0)
        self.btn_row.setSpacing(1)
        self.row.addWidget(self.btn_box)

        self.edit = QLineEdit(self.pill)
        self.edit.setPlaceholderText("Describe a change…  ↵")
        self.edit.setFrame(False)
        self.edit.setFixedWidth(320)
        self.edit.setFont(ui_font(9.5, QFont.Weight.Normal))
        self.edit.returnPressed.connect(self._submit_ask)
        self.edit.installEventFilter(self)
        self.row.addWidget(self.edit)

        self.busy_label = QLabel(self.pill)
        self.busy_label.setFont(ui_font(9.5))
        self.row.addWidget(self.busy_label)
        self.busy_hint = QLabel("Esc", self.pill)
        self.busy_hint.setFont(ui_font(8))
        self.row.addSpacing(10)
        self.row.addWidget(self.busy_hint)

        self.sep2 = Sep(self.pill)
        self.row.addWidget(self.sep2)
        self.ask_btn = BarButton("✎", tip="Custom instruction", glyph=True, parent=self.pill)
        self.ask_btn.clicked.connect(self.enter_ask)
        self.row.addWidget(self.ask_btn)
        self.more_btn = BarButton("⋯", tip="More", glyph=True, parent=self.pill)
        self.more_btn.clicked.connect(self._more)
        self.row.addWidget(self.more_btn)

        self._overflow: list[dict] = []
        self._buttons: list[BarButton] = []

    # ------------------------------------------------------------------ build
    def apply_theme(self, p: dict):
        self.p = p
        for w in (self.pill, self.mark, self.sep1, self.sep2, self.ask_btn, self.more_btn,
                  *self._buttons):
            w.p = p
            w.update()
        self.busy_label.setStyleSheet(f"color:{p['text']}")
        self.busy_hint.setStyleSheet(
            f"color:{p['dim']}; border:1px solid {p['border']}; border-radius:4px; padding:0 4px;")
        self.edit.setStyleSheet(
            f"QLineEdit{{background:transparent;color:{p['text']};padding:0 6px;"
            f"selection-background-color:{p['accent']};}}")

    def set_actions(self, actions: list[dict], max_visible: int, show_numbers: bool,
                    show_ask: bool):
        for b in self._buttons:
            self.btn_row.removeWidget(b)
            b.hide()
            b.deleteLater()
        self._buttons.clear()
        visible, self._overflow = actions[:max_visible], actions[max_visible:]
        for i, a in enumerate(visible):
            num = str(i + 1) if show_numbers and i < 9 else ""
            tip = a["name"] + (f"   {i + 1}" if i < 9 else "") + \
                (f"   ·   {a['hotkey']}" if a.get("hotkey") else "")
            b = BarButton(a["name"], num, tip, self.btn_box)
            b.p = self.p
            b.clicked.connect(lambda _=False, aid=a["id"]: self.action.emit(aid, _shift()))
            self.btn_row.addWidget(b)
            self._buttons.append(b)
        self.ask_btn.setVisible(show_ask)
        self.visible_ids = [a["id"] for a in visible]

    def _set_mode(self, mode: str):
        self.mode = mode
        idle, ask, busy = mode == "idle", mode == "ask", mode == "busy"
        self.btn_box.setVisible(idle)
        self.edit.setVisible(ask)
        self.busy_label.setVisible(busy)
        self.busy_hint.setVisible(busy)
        self.sep2.setVisible(idle)
        self.ask_btn.setVisible(idle and self._show_ask)
        self.more_btn.setVisible(idle)
        self.mark.spin(busy)
        anchor = self.geometry().center().x() if self.isVisible() else None
        self.pill.adjustSize()
        self.adjustSize()
        if anchor is not None:
            self.move(anchor - self.width() // 2, self.y())
            self._clamp()

    # ------------------------------------------------------------------ show
    def show_at(self, pos: QPoint, show_ask: bool = True):
        self._show_ask = show_ask
        self._set_mode("idle")
        self.adjustSize()
        x = pos.x() - self.width() // 2
        y = pos.y() - self.height() - 4 + SHADOW  # sit just above the cursor
        scr = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        if y < scr.availableGeometry().top():
            y = pos.y() + 14
        self.move(x, y)
        self._clamp()
        self.show()
        self.raise_()
        if not self._native_set:
            set_no_activate(int(self.winId()), True)
            self._native_set = True

    def _clamp(self):
        scr = QGuiApplication.screenAt(self.geometry().center()) or QGuiApplication.primaryScreen()
        g = scr.availableGeometry()
        x = min(max(self.x(), g.left()), g.right() - self.width())
        y = min(max(self.y(), g.top()), g.bottom() - self.height())
        self.move(x, y)

    def set_busy(self, label: str):
        self.busy_label.setText(label)
        self._set_mode("busy")

    def contains_global(self, pt: QPoint) -> bool:
        return self.pill.rect().contains(self.pill.mapFromGlobal(pt))

    def hide(self):
        if self.mode == "ask":
            set_no_activate(int(self.winId()), True)
        self.mark.spin(False)
        self.mode = "idle"
        super().hide()

    # ------------------------------------------------------------------ ask
    def enter_ask(self):
        self.asking.emit()
        self._set_mode("ask")
        set_no_activate(int(self.winId()), False)
        self.activateWindow()
        focus_window(int(self.winId()), timeout=0.2)
        self.edit.clear()
        self.edit.setFocus()

    def _submit_ask(self):
        text = self.edit.text().strip()
        set_no_activate(int(self.winId()), True)
        if text:
            self.ask.emit(text)
        else:
            self.cancel.emit()

    def eventFilter(self, obj, ev):
        if obj is self.edit and ev.type() == ev.Type.KeyPress and ev.key() == Qt.Key_Escape:
            self.cancel.emit()
            return True
        return super().eventFilter(obj, ev)

    # ------------------------------------------------------------------ more
    def _more(self):
        m = QMenu(self)
        m.setStyleSheet(settings_qss(self.p))
        for a in self._overflow:
            label = a["name"] + (f"\t{a['hotkey']}" if a.get("hotkey") else "")
            m.addAction(label, lambda aid=a["id"]: self.action.emit(aid, _shift()))
        if self._overflow:
            m.addSeparator()
        m.addAction("History…", self.history.emit)
        m.addAction("Settings…", self.settings.emit)
        m.addAction("Close\tEsc", self.cancel.emit)
        m.exec(self.more_btn.mapToGlobal(QPoint(0, self.more_btn.height() + 6)))


class Toast(QWidget):
    """Small non-activating status bubble."""

    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.p = DARK
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SHADOW, SHADOW - 4, SHADOW, SHADOW + 4)
        self.pill = Pill(self)
        fx = QGraphicsDropShadowEffect(self.pill, blurRadius=24, offset=QPoint(0, 5))
        fx.setColor(QColor(0, 0, 0, 80))
        self.pill.setGraphicsEffect(fx)
        lay.addWidget(self.pill)
        inner = QHBoxLayout(self.pill)
        inner.setContentsMargins(14, 8, 14, 8)
        self.label = QLabel(self.pill)
        self.label.setFont(ui_font(9.5))
        self.label.setMaximumWidth(460)
        self.label.setWordWrap(True)
        inner.addWidget(self.label)
        self._timer = QTimer(self, singleShot=True)
        self._timer.timeout.connect(self.hide)
        self._native_set = False

    def apply_theme(self, p: dict):
        self.p = p
        self.pill.p = p

    def show_message(self, text: str, error: bool = False, ms: int = 2600,
                     near: QPoint | None = None):
        self.label.setText(text)
        self.label.setStyleSheet(f"color:{self.p['danger'] if error else self.p['text']}")
        self.adjustSize()
        pos = near or QCursor.pos()
        self.move(pos.x() - self.width() // 2, pos.y() - self.height() - 2 + SHADOW)
        scr = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        g = scr.availableGeometry()
        self.move(min(max(self.x(), g.left()), g.right() - self.width()),
                  min(max(self.y(), g.top()), g.bottom() - self.height()))
        self.show()
        self.raise_()
        if not self._native_set:
            set_no_activate(int(self.winId()), True)
            self._native_set = True
        self._timer.start(ms)


#==============================================================================
# preview: Result card: streams the answer, shows a word-level diff, lets you Replace,
#==============================================================================

WIDTH = 540


class PreviewCard(QWidget):
    accept = Signal()
    copy = Signal()
    retry = Signal()
    refine = Signal(str)
    closed = Signal()
    refine_focus = Signal(bool)

    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.p = DARK
        self.original = ""
        self.result = ""
        self.streaming = False
        self.show_diff = True
        self.can_diff = True
        self._native_set = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW, SHADOW - 4, SHADOW, SHADOW + 4)
        self.pill = Pill(self)
        fx = QGraphicsDropShadowEffect(self.pill, blurRadius=30, offset=QPoint(0, 8))
        fx.setColor(QColor(0, 0, 0, 100))
        self.pill.setGraphicsEffect(fx)
        outer.addWidget(self.pill)
        self.pill.setFixedWidth(WIDTH)

        v = QVBoxLayout(self.pill)
        v.setContentsMargins(14, 10, 12, 12)
        v.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(6)
        self.mark = Mark(self.pill)
        self.title = QLabel(self.pill)
        self.title.setFont(ui_font(9, QFont.Weight.DemiBold))
        self.meta = QLabel(self.pill)
        self.meta.setFont(ui_font(8.5, QFont.Weight.Normal))
        self.close_btn = QPushButton("✕", self.pill)
        self.close_btn.setFixedSize(24, 22)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.clicked.connect(self.closed.emit)
        head.addWidget(self.mark)
        head.addWidget(self.title)
        head.addWidget(self.meta, 1)
        head.addWidget(self.close_btn)
        v.addLayout(head)

        self.body = QTextBrowser(self.pill)
        self.body.setFrameShape(QTextBrowser.NoFrame)
        self.body.setOpenLinks(False)
        self.body.setFont(ui_font(10.5, QFont.Weight.Normal))
        self.body.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.body.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        v.addWidget(self.body)

        stats = QHBoxLayout()
        self.stats = QLabel(self.pill)
        self.stats.setFont(ui_font(8.5, QFont.Weight.Normal))
        self.diff_btn = self._ghost("Diff  D")
        self.diff_btn.clicked.connect(self.toggle_diff)
        stats.addWidget(self.stats, 1)
        stats.addWidget(self.diff_btn)
        v.addLayout(stats)

        self.refine_edit = QLineEdit(self.pill)
        self.refine_edit.setPlaceholderText("Refine…  e.g. shorter, add a thank-you, more casual  ↵")
        self.refine_edit.setFont(ui_font(9.5, QFont.Weight.Normal))
        self.refine_edit.returnPressed.connect(self._submit_refine)
        self.refine_edit.installEventFilter(self)
        v.addWidget(self.refine_edit)

        foot = QHBoxLayout()
        foot.setSpacing(6)
        self.retry_btn = self._ghost("Retry  Tab")
        self.retry_btn.clicked.connect(self.retry.emit)
        self.copy_btn = self._ghost("Copy  C")
        self.copy_btn.clicked.connect(self.copy.emit)
        self.ok_btn = QPushButton("Replace  ↵", self.pill)
        self.ok_btn.setCursor(Qt.PointingHandCursor)
        self.ok_btn.clicked.connect(self.accept.emit)
        foot.addWidget(self.retry_btn)
        foot.addWidget(self.copy_btn)
        foot.addStretch(1)
        foot.addWidget(self.ok_btn)
        v.addLayout(foot)

    def _ghost(self, text: str) -> QPushButton:
        b = QPushButton(text, self.pill)
        b.setCursor(Qt.PointingHandCursor)
        b.setProperty("ghost", True)
        return b

    # ----------------------------------------------------------------- theme
    def apply_theme(self, p: dict):
        self.p = p
        self.pill.p = p
        self.mark.p = p
        self.title.setStyleSheet(f"color:{p['text']}")
        self.meta.setStyleSheet(f"color:{p['dim']}")
        self.stats.setStyleSheet(f"color:{p['dim']}")
        self.body.setStyleSheet(
            f"QTextBrowser{{background:transparent;color:{p['text']};"
            f"selection-background-color:{p['accent']};}}")
        btn = (f"QPushButton{{background:transparent;color:{p['dim']};border:none;"
               f"padding:4px 8px;border-radius:6px;}}"
               f"QPushButton:hover{{background:{p['hover']};color:{p['text']};}}")
        for b in (self.close_btn, self.retry_btn, self.copy_btn, self.diff_btn):
            b.setStyleSheet(btn)
        self.ok_btn.setStyleSheet(
            f"QPushButton{{background:{p['accent']};color:{p['accent_text']};border:none;"
            f"padding:6px 14px;border-radius:7px;font-weight:600;}}"
            f"QPushButton:disabled{{background:{p['hover']};color:{p['dim']};}}")
        self.refine_edit.setStyleSheet(
            f"QLineEdit{{background:{p['bg2']};color:{p['text']};border:1px solid {p['border']};"
            f"border-radius:7px;padding:6px 8px;}}"
            f"QLineEdit:focus{{border:1px solid {p['accent']};}}")

    # ----------------------------------------------------------------- flow
    def start(self, title: str, meta: str, original: str, primary: str, can_diff: bool):
        self.original = original
        self.result = ""
        self.can_diff = can_diff
        self.title.setText(title)
        self.meta.setText(meta)
        self.ok_btn.setText(f"{primary}  ↵")
        self.diff_btn.setVisible(can_diff)
        self._set_busy(True)
        self.body.setPlainText("")
        self.stats.setText("Writing…")
        self._fit()

    def append(self, chunk: str):
        self.result += chunk
        self.body.setPlainText(self.result)
        self.body.verticalScrollBar().setValue(self.body.verticalScrollBar().maximum())
        self._fit()

    def finish(self, text: str):
        self.result = text
        self._set_busy(False)
        self._render()
        a, b = word_count(self.original), word_count(text)
        msg = f"{a} → {b} words"
        if self.can_diff:
            pct = round(change_ratio(self.original, text) * 100)
            msg += f"  ·  {pct}% changed"
        self.stats.setText(msg)

    def fail(self, err: str):
        self._set_busy(False)
        self.ok_btn.setEnabled(False)
        self.copy_btn.setEnabled(False)
        self.body.setHtml(f'<span style="color:{self.p["danger"]}">{plain_html(err)}</span>')
        self.stats.setText("")
        self._fit()

    def _set_busy(self, on: bool):
        self.streaming = on
        self.mark.spin(on)
        for b in (self.ok_btn, self.copy_btn, self.retry_btn, self.diff_btn):
            b.setEnabled(not on)
        self.refine_edit.setEnabled(not on)

    def toggle_diff(self):
        if self.can_diff and not self.streaming:
            self.show_diff = not self.show_diff
            self._render()

    def _render(self):
        if self.can_diff and self.show_diff:
            self.body.setHtml(diff_html(self.original, self.result, self.p))
        else:
            self.body.setHtml(plain_html(self.result))
        self._fit()

    def _fit(self):
        doc = self.body.document()
        doc.setTextWidth(self.body.viewport().width() or WIDTH - 40)
        h = int(doc.size().height()) + 8
        self.body.setFixedHeight(max(44, min(h, 320)))
        self.pill.adjustSize()
        self.adjustSize()
        self._clamp()

    # ----------------------------------------------------------------- window
    def show_near(self, anchor: QPoint):
        self.move(anchor.x() - self.width() // 2, anchor.y())
        self._clamp()
        self.show()
        self.raise_()
        if not self._native_set:
            set_no_activate(int(self.winId()), True)
            self._native_set = True

    def _clamp(self):
        if not self.isVisible():
            return
        scr = QGuiApplication.screenAt(self.geometry().center()) or QGuiApplication.primaryScreen()
        g = scr.availableGeometry()
        self.move(min(max(self.x(), g.left()), g.right() - self.width()),
                  min(max(self.y(), g.top()), g.bottom() - self.height()))

    def contains_global(self, pt: QPoint) -> bool:
        return self.pill.rect().contains(self.pill.mapFromGlobal(pt))

    # ----------------------------------------------------------------- refine
    def eventFilter(self, obj, ev):
        if obj is self.refine_edit:
            t = ev.type()
            if t == ev.Type.MouseButtonPress and not self.refine_edit.hasFocus():
                self.refine_focus.emit(True)
                set_no_activate(int(self.winId()), False)
                self.activateWindow()
                focus_window(int(self.winId()), timeout=0.2)
                self.refine_edit.setFocus()
            elif t == ev.Type.KeyPress and ev.key() == Qt.Key_Escape:
                self._leave_refine()
                return True
        return super().eventFilter(obj, ev)

    def _leave_refine(self):
        set_no_activate(int(self.winId()), True)
        self.refine_edit.clearFocus()
        self.refine_focus.emit(False)

    def _submit_refine(self):
        text = self.refine_edit.text().strip()
        self.refine_edit.clear()
        self._leave_refine()
        if text:
            self.refine.emit(text)

    def hide(self):
        if self.refine_edit.hasFocus():
            self._leave_refine()
        self.mark.spin(False)
        super().hide()


#==============================================================================
# extras: History window and double-tap modifier detector.
#==============================================================================

@dataclass
class Entry:
    action: str
    app: str
    original: str
    result: str
    when: float = field(default_factory=time.time)


class History:
    """In-memory only. Nothing written to disk; cleared on exit."""

    def __init__(self, size: int = 50):
        self.items: deque[Entry] = deque(maxlen=size)

    def add(self, e: Entry):
        self.items.appendleft(e)

    def clear(self):
        self.items.clear()


class HistoryDialog(QDialog):
    def __init__(self, history: History, palette: dict, parent=None):
        super().__init__(parent)
        self.history = history
        self.setWindowTitle("Quillbar History")
        self.resize(820, 480)
        self.setStyleSheet(settings_qss(palette))
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        top = QHBoxLayout()
        h = QLabel("History")
        h.setObjectName("h1")
        hint = QLabel("Last 50 rewrites this session. Memory only — gone when Quillbar quits.")
        hint.setObjectName("hint")
        top.addWidget(h)
        top.addSpacing(12)
        top.addWidget(hint, 1)
        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear)
        top.addWidget(clear)
        root.addLayout(top)

        split = QSplitter(Qt.Horizontal)
        self.list = QListWidget()
        self.list.setObjectName("list")
        split.addWidget(self.list)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(10, 0, 0, 0)
        self.orig = QPlainTextEdit(readOnly=True)
        self.res = QPlainTextEdit(readOnly=True)
        for label, box in (("ORIGINAL", self.orig), ("RESULT", self.res)):
            row = QHBoxLayout()
            l = QLabel(label)
            l.setObjectName("section")
            b = QPushButton("Copy")
            b.clicked.connect(lambda _=False, box=box: QApplication.clipboard().setText(
                box.toPlainText()))
            row.addWidget(l, 1)
            row.addWidget(b)
            rv.addLayout(row)
            rv.addWidget(box, 1)
        split.addWidget(right)
        split.setSizes([260, 560])
        root.addWidget(split, 1)
        self.list.currentRowChanged.connect(self._show)
        self._fill()

    def _fill(self):
        self.list.clear()
        for e in self.history.items:
            stamp = time.strftime("%H:%M", time.localtime(e.when))
            snippet = " ".join(e.original.split())[:40]
            it = QListWidgetItem(f"{stamp}  {e.action} · {e.app or '?'}\n{snippet}")
            self.list.addItem(it)
        if self.history.items:
            self.list.setCurrentRow(0)
        else:
            self.orig.setPlainText("")
            self.res.setPlainText("Nothing yet.")

    def _show(self, row: int):
        if 0 <= row < len(self.history.items):
            e = self.history.items[row]
            self.orig.setPlainText(e.original)
            self.res.setPlainText(e.result)

    def _clear(self):
        self.history.clear()
        self._fill()


class DoubleTap(QThread):
    """Detects a clean double tap of Ctrl or Shift (tap, tap within 350 ms, no other
    key in between). Polls key state; no keyboard hook, no admin."""

    tapped = Signal()
    VKS = {"ctrl": (0xA2, 0xA3), "shift": (0xA0, 0xA1)}

    def __init__(self, which: str, parent=None):
        super().__init__(parent)
        self.which = self.VKS.get(which, ())
        self._run = True

    def stop(self):
        self._run = False
        self.wait(500)

    def run(self):
        if not IS_WIN or not self.which:
            return
        others = [vk for vk in range(0x01, 0xFF)
                  if vk not in self.which and vk not in (0x10, 0x11, 0xA0, 0xA1, 0xA2, 0xA3)]
        down_at = 0.0
        was_down = False
        dirty = False          # another key was pressed during this tap
        last_tap = 0.0
        while self._run:
            now = time.monotonic()
            is_down = any(key_down(vk) for vk in self.which)
            # Scan other keys only while a tap is in progress -> ~0% CPU when idle.
            watching = is_down or was_down or (last_tap and now - last_tap < 0.35)
            if watching and any(key_down(vk) for vk in others):
                dirty = True
                last_tap = 0.0
            if is_down and not was_down:
                down_at, dirty = now, False
            elif was_down and not is_down:
                clean = not dirty and now - down_at < 0.3
                if clean and last_tap and now - last_tap < 0.35:
                    last_tap = 0.0
                    self.tapped.emit()
                else:
                    last_tap = now if clean else 0.0
            was_down = is_down
            time.sleep(0.012)


class MouseWatcher(QThread):
    """PopClip-style trigger: emits `selected` after a drag-select or double-click.
    Polls button state (no hook). Copies nothing: capture happens on button click."""

    selected = Signal()

    def __init__(self, ibeam_only: bool = True, parent=None):
        super().__init__(parent)
        self.ibeam_only = ibeam_only
        self._run = True

    def stop(self):
        self._run = False
        self.wait(500)

    @staticmethod
    def classify(down_xy, up_xy, held_s, last_click, now):
        """Pure gesture logic (unit-tested). Returns ('drag'|'double'|'click', click_rec)."""
        dx, dy = up_xy[0] - down_xy[0], up_xy[1] - down_xy[1]
        dist = (dx * dx + dy * dy) ** 0.5
        if dist >= 8 and held_s >= 0.12:
            return "drag", None
        if last_click:
            (lx, ly), lt = last_click
            if now - lt < 0.45 and abs(up_xy[0] - lx) < 6 and abs(up_xy[1] - ly) < 6:
                return "double", None
        return "click", (up_xy, now)

    def run(self):
        if not IS_WIN:
            return
        was_down = False
        down_xy = (0, 0)
        down_t = 0.0
        ibeam = False
        last_click = None
        while self._run:
            is_down = key_down(VK_LBUTTON)
            now = time.monotonic()
            if is_down and not was_down:
                down_xy, down_t = cursor_pos(), now
                ibeam = cursor_is_ibeam()
            elif was_down and not is_down:
                kind, rec = self.classify(down_xy, cursor_pos(), now - down_t,
                                          last_click, now)
                last_click = rec
                if kind != "click" and (ibeam or not self.ibeam_only) \
                        and not key_down(VK_CONTROL) \
                        and not key_down(VK_MENU):
                    time.sleep(0.12)  # let the app finish updating the selection
                    if not key_down(VK_LBUTTON):
                        self.selected.emit()
            was_down = is_down
            time.sleep(0.015)


#==============================================================================
# settings: 
#==============================================================================

class _Relay(QObject):
    done = Signal(str, object)  # tag, result-or-exception


class HotkeyEdit(QWidget):
    changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.edit = QKeySequenceEdit(self)
        try:
            self.edit.setMaximumSequenceLength(1)
        except AttributeError:
            pass
        self.edit.keySequenceChanged.connect(self._emit)
        clear = QPushButton("Clear", self)
        clear.clicked.connect(lambda: self.set_text(""))
        lay.addWidget(self.edit, 1)
        lay.addWidget(clear)

    def _emit(self, *_):
        self.changed.emit(self.text())

    def set_text(self, s: str):
        self.edit.setKeySequence(QKeySequence.fromString(s or "", QKeySequence.PortableText))
        self._emit()

    def text(self) -> str:
        seq = self.edit.keySequence()
        if seq.isEmpty():
            return ""
        raw = QKeySequence(seq[0]).toString(QKeySequence.PortableText)
        raw = raw.replace("Meta+", "Win+")
        try:
            return normalize(raw)
        except ValueError:
            return raw


def _section(text: str) -> QLabel:
    l = QLabel(text.upper())
    l.setObjectName("section")
    return l


def _hint(text: str) -> QLabel:
    l = QLabel(text)
    l.setObjectName("hint")
    l.setWordWrap(True)
    return l


class SettingsDialog(QDialog):
    saved = Signal()

    def __init__(self, cfg: Config, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.d = copy.deepcopy(cfg.data)
        self.relay = _Relay()
        self.relay.done.connect(self._on_async)
        self.setWindowTitle("Quillbar Settings")
        self.resize(860, 580)
        self.setStyleSheet(settings_qss(palette(self.d["theme"])))

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(180)
        for name in ("General", "Mouse & smart", "Personalize", "AI Providers", "Buttons"):
            self.nav.addItem(name)
        root.addWidget(self.nav)

        right = QWidget()
        right.setObjectName("page")
        rv = QVBoxLayout(right)
        rv.setContentsMargins(24, 20, 24, 16)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._general())
        self.pages.addWidget(self._smart())
        self.pages.addWidget(self._personal())
        self.pages.addWidget(self._providers())
        self.pages.addWidget(self._actions())
        rv.addWidget(self.pages, 1)
        foot = QHBoxLayout()
        foot.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save")
        save.setObjectName("primary")
        save.clicked.connect(self._save)
        foot.addWidget(cancel)
        foot.addWidget(save)
        rv.addLayout(foot)
        root.addWidget(right, 1)

        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setCurrentRow(0)

    # ================================================================ general
    def _general(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        h = QLabel("General")
        h.setObjectName("h1")
        v.addWidget(h)
        f = QFormLayout()
        f.setLabelAlignment(Qt.AlignLeft)
        f.setHorizontalSpacing(18)
        f.setVerticalSpacing(10)

        self.tb_hotkey = HotkeyEdit()
        self.tb_hotkey.set_text(self.d["toolbar_hotkey"])
        f.addRow("Show toolbar", self.tb_hotkey)
        self.theme_box = QComboBox()
        self.theme_box.addItems(["auto", "dark", "light"])
        self.theme_box.setCurrentText(self.d["theme"])
        f.addRow("Theme", self.theme_box)
        self.max_vis = QSpinBox()
        self.max_vis.setRange(1, 12)
        self.max_vis.setValue(self.d["max_visible"])
        self.max_vis.setFixedWidth(90)
        f.addRow("Buttons on bar", self.max_vis)
        v.addLayout(f)

        self.chk_numbers = QCheckBox("Show number hints (press 1–9 while bar is open)")
        self.chk_numbers.setChecked(self.d["show_numbers"])
        self.chk_ask = QCheckBox("Show ✎ custom-instruction button")
        self.chk_ask.setChecked(self.d["show_ask"])
        self.chk_clip = QCheckBox("Restore my clipboard after replacing text")
        self.chk_clip.setChecked(self.d["restore_clipboard"])
        self.chk_start = QCheckBox("Launch at Windows startup")
        self.chk_start.setChecked(self.d["launch_at_startup"])
        for c in (self.chk_numbers, self.chk_ask, self.chk_clip, self.chk_start):
            v.addWidget(c)

        v.addWidget(_section("System prompt"))
        self.sys_prompt = QPlainTextEdit(self.d["system_prompt"])
        self.sys_prompt.setMinimumHeight(110)
        v.addWidget(self.sys_prompt, 1)
        reset = QPushButton("Reset prompt")
        reset.clicked.connect(lambda: self.sys_prompt.setPlainText(SYSTEM_PROMPT))
        row = QHBoxLayout()
        row.addWidget(_hint("Avoid Ctrl+Space: it switches IME (Chinese/Japanese input)."))
        row.addStretch()
        row.addWidget(reset)
        v.addLayout(row)
        return w

    # ================================================================ smart
    def _smart(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        h = QLabel("Mouse & smart")
        h.setObjectName("h1")
        v.addWidget(h)

        v.addWidget(_section("Mouse mode"))
        self.chk_mouse = QCheckBox("Show the bar automatically after I select text with the mouse")
        self.chk_mouse.setChecked(self.d["mouse_mode"])
        self.chk_ibeam = QCheckBox("Only when the pointer is a text cursor (I-beam) — fewer false pop-ups")
        self.chk_ibeam.setChecked(self.d["mouse_ibeam_only"])
        v.addWidget(self.chk_mouse)
        v.addWidget(self.chk_ibeam)
        f = QFormLayout()
        f.setHorizontalSpacing(18)
        self.mouse_exclude = QLineEdit(self.d["mouse_exclude"])
        f.addRow("Never pop up in", self.mouse_exclude)
        self.dtap = QComboBox()
        self.dtap.addItems(["off", "ctrl", "shift"])
        self.dtap.setCurrentText(self.d["double_tap"])
        self.dtap.setFixedWidth(120)
        f.addRow("Double-tap to open bar", self.dtap)
        v.addLayout(f)
        v.addWidget(_hint("Mouse mode never copies anything until you click a button, so "
                          "dragging files or windows is safe."))

        v.addWidget(_section("Smart select"))
        self.chk_smart = QCheckBox("Nothing selected? Use the whole message box (Ctrl+A) in these apps:")
        self.chk_smart.setChecked(self.d["smart_select"])
        v.addWidget(self.chk_smart)
        self.smart_apps = QLineEdit(self.d["smart_select_apps"])
        v.addWidget(self.smart_apps)

        v.addWidget(_section("Privacy"))
        self.chk_redact = QCheckBox("Mask emails, phone and card numbers before sending to the AI")
        self.chk_redact.setChecked(self.d["redact"])
        self.chk_hist = QCheckBox("Keep a history of this session's rewrites (memory only)")
        self.chk_hist.setChecked(self.d["keep_history"])
        v.addWidget(self.chk_redact)
        v.addWidget(self.chk_hist)
        v.addWidget(_hint("App names are the program name, e.g. WeChat, Telegram, OUTLOOK, "
                          "chrome. Comma-separated."))
        v.addStretch(1)
        return w

    # ============================================================= personal
    def _personal(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        h = QLabel("Personalize")
        h.setObjectName("h1")
        v.addWidget(h)
        v.addWidget(_section("About me & my style"))
        self.style_edit = QPlainTextEdit(self.d["style"])
        self.style_edit.setPlaceholderText(
            "Added to every request. e.g.\nI'm Alex, a teacher. British spelling. "
            "Sign emails 'Best, Alex'. Never use the word 'delve'.")
        self.style_edit.setFixedHeight(84)
        v.addWidget(self.style_edit)

        v.addWidget(_section("Translate button"))
        row = QHBoxLayout()
        self.lang1 = QComboBox()
        self.lang2 = QComboBox()
        langs = ["English", "Simplified Chinese", "Traditional Chinese", "Japanese", "Korean",
                 "Arabic", "Urdu", "Hindi", "Tamil", "French", "German", "Spanish", "Russian"]
        for box, val in ((self.lang1, self.d["lang1"]), (self.lang2, self.d["lang2"])):
            box.setEditable(True)
            box.addItems(langs)
            box.setCurrentText(val)
        row.addWidget(self.lang1, 1)
        row.addWidget(QLabel("  ⇄  "))
        row.addWidget(self.lang2, 1)
        v.addLayout(row)
        v.addWidget(_hint("Translate flips between the two: text in the first becomes the "
                          "second, anything else becomes the first."))

        v.addWidget(_section("App rules"))
        self.rules = QTableWidget(0, 2)
        self.rules.setHorizontalHeaderLabels(["Apps (comma-separated)", "Extra instruction"])
        self.rules.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        self.rules.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.rules.setColumnWidth(0, 210)
        self.rules.verticalHeader().setVisible(False)
        self.rules.setWordWrap(True)
        for r in self.d["app_rules"]:
            self._add_rule_row(r.get("app", ""), r.get("instruction", ""))
        v.addWidget(self.rules, 1)
        br = QHBoxLayout()
        add = QPushButton("Add rule")
        add.clicked.connect(lambda: self._add_rule_row("", ""))
        rm = QPushButton("Remove")
        rm.clicked.connect(lambda: self.rules.removeRow(self.rules.currentRow()))
        br.addWidget(_hint("Placeholders work everywhere: {app} {date} {time} {lang1} {lang2}"), 1)
        br.addWidget(add)
        br.addWidget(rm)
        v.addLayout(br)
        return w

    def _add_rule_row(self, app: str, instr: str):
        r = self.rules.rowCount()
        self.rules.insertRow(r)
        self.rules.setItem(r, 0, QTableWidgetItem(app))
        self.rules.setItem(r, 1, QTableWidgetItem(instr))
        self.rules.resizeRowToContents(r)

    def _collect_rules(self) -> list[dict]:
        out = []
        for r in range(self.rules.rowCount()):
            app = (self.rules.item(r, 0).text() if self.rules.item(r, 0) else "").strip()
            ins = (self.rules.item(r, 1).text() if self.rules.item(r, 1) else "").strip()
            if app and ins:
                out.append({"app": app, "instruction": ins})
        return out

    # ============================================================== providers
    def _providers(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        h = QLabel("AI Providers")
        h.setObjectName("h1")
        v.addWidget(h)
        v.addWidget(_hint("Any OpenAI-compatible endpoint. Keys are encrypted with your "
                          "Windows account (DPAPI) and never leave this PC except to the provider."))
        body = QHBoxLayout()
        left = QVBoxLayout()
        self.plist = QListWidget()
        self.plist.setObjectName("list")
        self.plist.setFixedWidth(210)
        left.addWidget(self.plist, 1)
        brow = QHBoxLayout()
        self.preset_box = QComboBox()
        self.preset_box.addItems(list(PROVIDER_PRESETS))
        add = QPushButton("Add")
        add.clicked.connect(self._add_provider)
        brow.addWidget(self.preset_box, 1)
        brow.addWidget(add)
        left.addLayout(brow)
        body.addLayout(left)

        form_w = QWidget()
        f = QFormLayout(form_w)
        f.setContentsMargins(18, 0, 0, 0)
        f.setVerticalSpacing(10)
        self.p_name = QLineEdit()
        self.p_url = QLineEdit()
        self.p_key = QLineEdit()
        self.p_key.setEchoMode(QLineEdit.Password)
        self.p_key.setPlaceholderText("sk-…  (leave blank for local models)")
        key_row = QHBoxLayout()
        key_row.addWidget(self.p_key, 1)
        show = QPushButton("Show")
        show.setCheckable(True)
        show.toggled.connect(lambda on: self.p_key.setEchoMode(
            QLineEdit.Normal if on else QLineEdit.Password))
        key_row.addWidget(show)
        self.p_model = QComboBox()
        self.p_model.setEditable(True)
        model_row = QHBoxLayout()
        model_row.addWidget(self.p_model, 1)
        self.fetch_btn = QPushButton("Fetch models")
        self.fetch_btn.clicked.connect(self._fetch_models)
        model_row.addWidget(self.fetch_btn)
        self.p_temp = QDoubleSpinBox()
        self.p_temp.setRange(0, 2)
        self.p_temp.setSingleStep(0.1)
        self.p_temp.setButtonSymbols(QDoubleSpinBox.NoButtons)
        self.p_temp.setFixedWidth(90)
        f.addRow("Name", self.p_name)
        f.addRow("Base URL", self.p_url)
        f.addRow("API key", key_row)
        f.addRow("Model", model_row)
        f.addRow("Temperature", self.p_temp)

        act_row = QHBoxLayout()
        self.p_active = QPushButton("Use this provider")
        self.p_active.setObjectName("primary")
        self.p_active.clicked.connect(self._set_active)
        self.test_btn = QPushButton("Test")
        self.test_btn.clicked.connect(self._test)
        rm = QPushButton("Remove")
        rm.setObjectName("danger")
        rm.clicked.connect(self._remove_provider)
        act_row.addWidget(self.p_active)
        act_row.addWidget(self.test_btn)
        act_row.addStretch()
        act_row.addWidget(rm)
        f.addRow("", act_row)
        self.p_status = _hint("")
        f.addRow("", self.p_status)
        body.addWidget(form_w, 1)
        v.addLayout(body, 1)

        for wdg, sig in ((self.p_name, "textEdited"), (self.p_url, "textEdited"),
                         (self.p_key, "textEdited")):
            getattr(wdg, sig).connect(self._store_provider)
        self.p_model.editTextChanged.connect(self._store_provider)
        self.p_temp.valueChanged.connect(self._store_provider)
        self.plist.currentRowChanged.connect(self._load_provider)
        self._refresh_plist()
        return w

    def _cur_provider(self) -> dict | None:
        i = self.plist.currentRow()
        return self.d["providers"][i] if 0 <= i < len(self.d["providers"]) else None

    def _refresh_plist(self, select: int | None = None):
        cur = self.plist.currentRow() if select is None else select
        self.plist.blockSignals(True)
        self.plist.clear()
        for p in self.d["providers"]:
            mark = "●  " if p["id"] == self.d["active_provider"] else "    "
            self.plist.addItem(mark + p["name"])
        self.plist.blockSignals(False)
        self.plist.setCurrentRow(max(0, min(cur, len(self.d["providers"]) - 1)))
        self._load_provider(self.plist.currentRow())

    def _load_provider(self, _row: int):
        p = self._cur_provider()
        if not p:
            return
        self._loading = True
        self.p_name.setText(p["name"])
        self.p_url.setText(p["base_url"])
        self.p_key.setText(p.get("api_key", ""))
        self.p_model.clear()
        self.p_model.setEditText(p["model"])
        self.p_temp.setValue(float(p.get("temperature", 0.4)))
        active = p["id"] == self.d["active_provider"]
        self.p_active.setText("Active" if active else "Use this provider")
        self.p_active.setEnabled(not active)
        self.p_status.setText("")
        self._loading = False

    def _store_provider(self, *_):
        if getattr(self, "_loading", False):
            return
        p = self._cur_provider()
        if not p:
            return
        p.update(name=self.p_name.text().strip() or "Provider",
                 base_url=self.p_url.text().strip(), api_key=self.p_key.text().strip(),
                 model=self.p_model.currentText().strip(), temperature=self.p_temp.value())
        item = self.plist.currentItem()
        if item:
            mark = "●  " if p["id"] == self.d["active_provider"] else "    "
            item.setText(mark + p["name"])

    def _add_provider(self):
        self.d["providers"].append(new_provider(self.preset_box.currentText()))
        self._refresh_plist(len(self.d["providers"]) - 1)
        self.p_key.setFocus()

    def _remove_provider(self):
        if len(self.d["providers"]) <= 1:
            return
        p = self._cur_provider()
        self.d["providers"].remove(p)
        if p["id"] == self.d["active_provider"]:
            self.d["active_provider"] = self.d["providers"][0]["id"]
        self._refresh_plist()

    def _set_active(self):
        p = self._cur_provider()
        if p:
            self.d["active_provider"] = p["id"]
            self._refresh_plist()

    def _run_async(self, tag: str, fn):
        def work():
            try:
                self.relay.done.emit(tag, fn())
            except Exception as e:  # noqa: BLE001
                self.relay.done.emit(tag, e)
        threading.Thread(target=work, daemon=True).start()

    def _test(self):
        p = copy.deepcopy(self._cur_provider())
        self.p_status.setText("Testing…")
        self.test_btn.setEnabled(False)
        self._run_async("test", lambda: (complete(
            p, self.sys_prompt.toPlainText(), "Fix spelling and grammar only.",
            "helo, i am testting this tool"), p["base_url"]))

    def _fetch_models(self):
        p = copy.deepcopy(self._cur_provider())
        self.p_status.setText("Fetching models…")
        self.fetch_btn.setEnabled(False)
        self._run_async("models", lambda: list_models(p))

    def _on_async(self, tag: str, res):
        self.test_btn.setEnabled(True)
        self.fetch_btn.setEnabled(True)
        if isinstance(res, Exception):
            self.p_status.setText(f"✗ {res}")
            return
        if tag == "test":
            out, base = res
            note = ""
            if normalize_base(base) != normalize_base(self.p_url.text()):
                self.p_url.setText(base)
                self._store_provider()
                note = "  (base URL fixed to …/v1)"
            self.p_status.setText(f"✓ Works{note}  →  “{out}”")
        else:
            cur = self.p_model.currentText()
            self.p_model.blockSignals(True)
            self.p_model.clear()
            self.p_model.addItems(res)
            self.p_model.setEditText(cur)
            self.p_model.blockSignals(False)
            self.p_status.setText(f"✓ {len(res)} models. Pick one from the list.")

    # ================================================================ actions
    def _actions(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        h = QLabel("Buttons")
        h.setObjectName("h1")
        v.addWidget(h)
        v.addWidget(_hint("Drag to reorder. The first buttons appear on the bar and get "
                          "number keys 1–9; the rest go under ⋯. Each can have its own "
                          "global shortcut that runs instantly, no bar."))
        body = QHBoxLayout()
        left = QVBoxLayout()
        self.alist = QListWidget()
        self.alist.setObjectName("list")
        self.alist.setFixedWidth(210)
        self.alist.setDragDropMode(QAbstractItemView.InternalMove)
        self.alist.model().rowsMoved.connect(self._reorder_actions)
        left.addWidget(self.alist, 1)
        brow = QHBoxLayout()
        for label, fn in (("Add", self._add_action), ("Duplicate", self._dup_action)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            brow.addWidget(b)
        left.addLayout(brow)
        body.addLayout(left)

        form_w = QWidget()
        f = QFormLayout(form_w)
        f.setContentsMargins(18, 0, 0, 0)
        f.setVerticalSpacing(10)
        self.a_name = QLineEdit()
        self.a_name.setMaxLength(18)
        self.a_hotkey = HotkeyEdit()
        self.a_prompt = QPlainTextEdit()
        self.a_prompt.setPlaceholderText("What should the AI do with the selected text?\n"
                                         "e.g. Reply politely declining, keep it under 2 lines.")
        self.a_enabled = QCheckBox("Enabled")
        self.a_toolbar = QCheckBox("Show on bar")
        chk = QHBoxLayout()
        chk.setSpacing(20)
        chk.addWidget(self.a_enabled)
        chk.addWidget(self.a_toolbar)
        chk.addStretch()
        self.a_kind = QComboBox()
        self.a_kind.addItem("Rewrite my text", "transform")
        self.a_kind.addItem("Write something new (reply, summary…)", "generate")
        self.a_mode = QComboBox()
        self.a_mode.addItem("Replace instantly", "replace")
        self.a_mode.addItem("Preview first (diff, refine)", "preview")
        self.a_mode.addItem("Copy to clipboard", "copy")
        self.a_provider = QComboBox()
        f.addRow("Label", self.a_name)
        f.addRow("Shortcut", self.a_hotkey)
        f.addRow("Instruction", self.a_prompt)
        f.addRow("Type", self.a_kind)
        f.addRow("Result", self.a_mode)
        f.addRow("Model", self.a_provider)
        f.addRow("", chk)
        f.addRow("", _hint("Tip: hold Shift when clicking a button (or Shift+number) to "
                           "preview just this once."))
        rrow = QHBoxLayout()
        reset = QPushButton("Restore defaults")
        reset.clicked.connect(self._reset_actions)
        rm = QPushButton("Delete")
        rm.setObjectName("danger")
        rm.clicked.connect(self._remove_action)
        rrow.addWidget(reset)
        imp = QPushButton("Import…")
        imp.clicked.connect(self._import_actions)
        exp = QPushButton("Export…")
        exp.clicked.connect(self._export_actions)
        rrow.addWidget(imp)
        rrow.addWidget(exp)
        rrow.addStretch()
        rrow.addWidget(rm)
        f.addRow("", rrow)
        body.addWidget(form_w, 1)
        v.addLayout(body, 1)

        self.a_name.textEdited.connect(self._store_action)
        self.a_hotkey.changed.connect(self._store_action)
        self.a_prompt.textChanged.connect(self._store_action)
        self.a_enabled.toggled.connect(self._store_action)
        self.a_toolbar.toggled.connect(self._store_action)
        for box in (self.a_kind, self.a_mode, self.a_provider):
            box.currentIndexChanged.connect(self._store_action)
        self.alist.currentRowChanged.connect(self._load_action)
        self._refresh_alist(0)
        return w

    def _action_label(self, a: dict) -> str:
        s = a["name"]
        if a.get("hotkey"):
            s += f"    {a['hotkey']}"
        return s if a.get("enabled", True) else f"({s})"

    def _cur_action(self) -> dict | None:
        i = self.alist.currentRow()
        return self.d["actions"][i] if 0 <= i < len(self.d["actions"]) else None

    def _refresh_alist(self, select: int | None = None):
        cur = self.alist.currentRow() if select is None else select
        self.alist.blockSignals(True)
        self.alist.clear()
        for a in self.d["actions"]:
            it = QListWidgetItem(self._action_label(a))
            it.setData(Qt.UserRole, a["id"])
            self.alist.addItem(it)
        self.alist.blockSignals(False)
        if self.d["actions"]:
            self.alist.setCurrentRow(max(0, min(cur, len(self.d["actions"]) - 1)))
            self._load_action(self.alist.currentRow())

    def _load_action(self, _row: int):
        a = self._cur_action()
        if not a:
            return
        self._aloading = True
        self.a_name.setText(a["name"])
        self.a_hotkey.set_text(a.get("hotkey", ""))
        self.a_prompt.setPlainText(a["prompt"])
        self.a_enabled.setChecked(a.get("enabled", True))
        self.a_toolbar.setChecked(a.get("in_toolbar", True))
        self.a_kind.setCurrentIndex(max(0, self.a_kind.findData(a.get("kind", "transform"))))
        self.a_mode.setCurrentIndex(max(0, self.a_mode.findData(a.get("mode", "replace"))))
        self.a_provider.clear()
        self.a_provider.addItem("Default (active provider)", "")
        for p in self.d["providers"]:
            self.a_provider.addItem(f"{p['name']} · {p['model']}", p["id"])
        self.a_provider.setCurrentIndex(max(0, self.a_provider.findData(a.get("provider", ""))))
        self._aloading = False

    def _store_action(self, *_):
        if getattr(self, "_aloading", False):
            return
        a = self._cur_action()
        if not a:
            return
        a.update(name=self.a_name.text().strip() or "Untitled", hotkey=self.a_hotkey.text(),
                 prompt=self.a_prompt.toPlainText().strip(),
                 enabled=self.a_enabled.isChecked(), in_toolbar=self.a_toolbar.isChecked(),
                 kind=self.a_kind.currentData() or "transform",
                 mode=self.a_mode.currentData() or "replace",
                 provider=self.a_provider.currentData() or "")
        item = self.alist.currentItem()
        if item:
            item.setText(self._action_label(a))

    def _reorder_actions(self, *_):
        by_id = {a["id"]: a for a in self.d["actions"]}
        self.d["actions"] = [by_id[self.alist.item(i).data(Qt.UserRole)]
                             for i in range(self.alist.count())]

    def _add_action(self):
        self.d["actions"].append({"id": uuid.uuid4().hex[:8], "name": "Custom", "hotkey": "",
                                  "prompt": "", "enabled": True, "in_toolbar": True,
                                  "kind": "transform", "mode": "replace", "provider": ""})
        self._refresh_alist(len(self.d["actions"]) - 1)
        self.a_name.setFocus()
        self.a_name.selectAll()

    def _dup_action(self):
        a = self._cur_action()
        if not a:
            return
        b = dict(a, id=uuid.uuid4().hex[:8], name=a["name"][:14] + " 2", hotkey="")
        self.d["actions"].insert(self.alist.currentRow() + 1, b)
        self._refresh_alist(self.alist.currentRow() + 1)

    def _remove_action(self):
        a = self._cur_action()
        if a and len(self.d["actions"]) > 1:
            self.d["actions"].remove(a)
            self._refresh_alist()

    def _reset_actions(self):
        if QMessageBox.question(self, "Restore defaults",
                                "Replace all buttons with the defaults?") == QMessageBox.Yes:
            self.d["actions"] = copy.deepcopy(DEFAULT_ACTIONS)
            self._refresh_alist(0)

    def _export_actions(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export buttons", "quillbar-buttons.json",
                                              "JSON (*.json)")
        if path:
            data = [{k: v for k, v in a.items() if k != "provider"} for a in self.d["actions"]]
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"quillbar_buttons": 1, "actions": data}, fh, indent=2,
                          ensure_ascii=False)

    def _import_actions(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import buttons", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as fh:
                items = json.load(fh).get("actions", [])
        except (OSError, ValueError, AttributeError) as e:
            QMessageBox.warning(self, "Import", f"Not a Quillbar buttons file ({e}).")
            return
        taken = {a.get("hotkey") for a in self.d["actions"] if a.get("hotkey")}
        added = 0
        for it in items:
            if not isinstance(it, dict) or not it.get("name") or not it.get("prompt"):
                continue
            hk = it.get("hotkey", "")
            self.d["actions"].append({
                "id": uuid.uuid4().hex[:8], "name": str(it["name"])[:18],
                "prompt": str(it["prompt"]), "hotkey": "" if hk in taken else hk,
                "enabled": bool(it.get("enabled", True)),
                "in_toolbar": bool(it.get("in_toolbar", True)),
                "kind": it.get("kind", "transform"), "mode": it.get("mode", "replace"),
                "provider": ""})
            added += 1
        self._refresh_alist(len(self.d["actions"]) - 1)
        QMessageBox.information(self, "Import", f"Added {added} button(s).")

    # ================================================================== save
    def _validate(self) -> str | None:
        seen: dict[str, str] = {}
        combos = [("Show toolbar", self.d["toolbar_hotkey"])]
        combos += [(a["name"], a.get("hotkey", "")) for a in self.d["actions"]
                   if a.get("enabled", True)]
        if not self.d["toolbar_hotkey"]:
            return "Set a shortcut for showing the toolbar."
        for owner, combo in combos:
            if not combo:
                continue
            try:
                key = normalize(combo)
                parse_hotkey(combo)
            except ValueError as e:
                return f"“{owner}”: shortcut {combo} is not usable ({e})."
            if key in seen:
                return f"{key} is used by both “{seen[key]}” and “{owner}”."
            seen[key] = owner
        for a in self.d["actions"]:
            if a.get("enabled", True) and not a["prompt"]:
                return f"“{a['name']}” needs an instruction."
        return None

    def _save(self):
        self.d.update(toolbar_hotkey=self.tb_hotkey.text(), theme=self.theme_box.currentText(),
                      max_visible=self.max_vis.value(), show_numbers=self.chk_numbers.isChecked(),
                      show_ask=self.chk_ask.isChecked(),
                      restore_clipboard=self.chk_clip.isChecked(),
                      launch_at_startup=self.chk_start.isChecked(),
                      mouse_mode=self.chk_mouse.isChecked(),
                      mouse_ibeam_only=self.chk_ibeam.isChecked(),
                      mouse_exclude=self.mouse_exclude.text().strip(),
                      double_tap=self.dtap.currentText(),
                      smart_select=self.chk_smart.isChecked(),
                      smart_select_apps=self.smart_apps.text().strip(),
                      redact=self.chk_redact.isChecked(),
                      keep_history=self.chk_hist.isChecked(),
                      style=self.style_edit.toPlainText().strip(),
                      lang1=self.lang1.currentText().strip() or "English",
                      lang2=self.lang2.currentText().strip() or "Simplified Chinese",
                      app_rules=self._collect_rules(),
                      system_prompt=self.sys_prompt.toPlainText().strip() or SYSTEM_PROMPT)
        err = self._validate()
        if err:
            QMessageBox.warning(self, "Quillbar", err)
            return
        self.cfg.data = self.d
        self.cfg.save()
        self.saved.emit()
        self.accept()


#==============================================================================
# app: Glue: trigger (hotkey / double-tap / mouse) -> capture -> bar -> LLM ->
#==============================================================================

PAGE_PROVIDERS = 3
_T_ESC, _T_NUM, _T_ENTER, _T_TAB, _T_COPY, _T_DIFF = (
    "__esc__", "__num_", "__enter__", "__tab__", "__copy__", "__diff__")
# keys meaning "the user went back to typing" -> close the bar
_TYPING_VKS = list(range(0x41, 0x5B)) + [0x20, 0x0D, 0x08, 0x2E, 0xBA, 0xBC, 0xBE, 0xBF]
_DIGIT_VKS = list(range(0x30, 0x3A))


@dataclass
class Session:
    hwnd: int
    app: str
    text: str | None          # None = not captured yet (mouse mode)
    source: str = "key"       # key | mouse


@dataclass
class Run:
    action: dict | None
    label: str
    instruction: str
    mode: str                 # replace | preview | copy
    kind: str                 # transform | generate
    lead: str = ""
    core: str = ""
    trail: str = ""
    diff_base: str = ""
    mapping: dict = field(default_factory=dict)
    result: str = ""


def make_icon(accent: str = "#7AA2FF") -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#1B1B1F"))
    p.drawRoundedRect(QRectF(2, 2, 60, 60), 15, 15)
    p.setBrush(QColor(accent))
    p.drawRoundedRect(QRectF(14, 28, 36, 8), 4, 4)
    p.drawEllipse(QRectF(38, 14, 12, 12))
    p.end()
    return QIcon(pm)


class Controller(QObject):
    _delta = Signal(int, str)
    _done = Signal(int, str, str)

    def __init__(self, app: QApplication):
        super().__init__()
        self.app = app
        self.cfg = Config.load()
        self.bridge = TextBridge(self.cfg["restore_clipboard"])
        self.toolbar = Toolbar()
        self.card = PreviewCard()
        self.toast = Toast()
        self.history = History()
        self.session: Session | None = None
        self.run: Run | None = None
        self.busy = False
        self.job = 0
        self.paused = False
        self.settings_dlg: SettingsDialog | None = None
        self.history_dlg: HistoryDialog | None = None
        self.hk: HotkeyThread | None = None
        self.dtap: DoubleTap | None = None
        self.mouse: MouseWatcher | None = None
        self._held_at_open: set[int] = set()
        self._mouse_deadline = 0.0

        tb = self.toolbar
        tb.action.connect(self.on_action)
        tb.ask.connect(self.on_ask)
        tb.settings.connect(self.open_settings)
        tb.history.connect(self.open_history)
        tb.cancel.connect(self.dismiss)
        tb.asking.connect(self._on_asking)
        c = self.card
        c.accept.connect(self.card_accept)
        c.copy.connect(self.card_copy)
        c.retry.connect(self.card_retry)
        c.refine.connect(self.card_refine)
        c.closed.connect(self.dismiss)
        c.refine_focus.connect(lambda on: self._temp_keys("none" if on else "preview"))
        self._delta.connect(self._on_delta)
        self._done.connect(self._on_done)

        self.watch = QTimer(self, interval=60)
        self.watch.timeout.connect(self._watchdog)

        self._build_tray()
        self.apply_config()
        if not self.cfg.is_configured:
            QTimer.singleShot(400, self.open_settings_providers)

    # ================================================================= setup
    def _build_tray(self):
        self.tray = QSystemTrayIcon(make_icon(), self.app)
        self.tray.setToolTip("Quillbar")
        m = QMenu()
        self.tray_hint = QAction("", m)
        self.tray_hint.setEnabled(False)
        m.addAction(self.tray_hint)
        m.addSeparator()
        self.mouse_act = QAction("Mouse mode (pop up on selection)", m, checkable=True)
        self.mouse_act.toggled.connect(self.set_mouse_mode)
        m.addAction(self.mouse_act)
        m.addAction("History…", self.open_history)
        m.addAction("Settings…", self.open_settings)
        m.addSeparator()
        self.pause_act = QAction("Pause Quillbar", m, checkable=True)
        self.pause_act.toggled.connect(self.set_paused)
        m.addAction(self.pause_act)
        self.startup_act = QAction("Start with Windows", m, checkable=True)
        self.startup_act.toggled.connect(self.set_startup)
        m.addAction(self.startup_act)
        m.addSeparator()
        m.addAction("Quit", self.quit)
        self.tray_menu = m
        self.tray.setContextMenu(m)
        self.tray.activated.connect(
            lambda r: self.open_settings() if r == QSystemTrayIcon.DoubleClick else None)
        self.tray.show()

    def apply_config(self):
        p = palette(self.cfg["theme"])
        self.toolbar.apply_theme(p)
        self.card.apply_theme(p)
        self.toast.apply_theme(p)
        self.tray_menu.setStyleSheet(settings_qss(p))
        self.bridge.restore_clipboard = self.cfg["restore_clipboard"]
        self.tray_hint.setText(f"Select text, press {self.cfg['toolbar_hotkey']}")
        if not self.cfg["keep_history"]:
            self.history.clear()
        self._sync_check(self.mouse_act, self.cfg["mouse_mode"])
        self._sync_startup()
        self.restart_listeners()

    @staticmethod
    def _sync_check(act: QAction, on: bool):
        act.blockSignals(True)
        act.setChecked(on)
        act.blockSignals(False)

    def _sync_startup(self):
        try:
            set_launch_at_startup(self.cfg["launch_at_startup"])
        except OSError as e:
            self.tray.showMessage("Quillbar", f"Couldn't set auto-start: {e}",
                                  QSystemTrayIcon.Warning, 5000)
        self._sync_check(self.startup_act, self.cfg["launch_at_startup"])

    def set_startup(self, on: bool):
        self.cfg["launch_at_startup"] = on
        self.cfg.save()
        self._sync_startup()

    def set_mouse_mode(self, on: bool):
        self.cfg["mouse_mode"] = on
        self.cfg.save()
        self.restart_listeners()
        self.toast.show_message("Mouse mode on: select text to see the bar" if on
                                else "Mouse mode off")

    def stop_listeners(self):
        for attr in ("hk", "dtap", "mouse"):
            t = getattr(self, attr)
            if t:
                t.stop()
                setattr(self, attr, None)

    def restart_listeners(self):
        self.stop_listeners()
        if self.paused:
            return
        self.hk = HotkeyThread(self.cfg.bindings())
        self.hk.triggered.connect(self.on_hotkey)
        self.hk.failed.connect(self._hotkey_failed)
        self.hk.start()
        if self.cfg["double_tap"] in ("ctrl", "shift"):
            self.dtap = DoubleTap(self.cfg["double_tap"])
            self.dtap.tapped.connect(self.open_toolbar)
            self.dtap.start()
        if self.cfg["mouse_mode"]:
            self.mouse = MouseWatcher(self.cfg["mouse_ibeam_only"])
            self.mouse.selected.connect(self.on_mouse_select)
            self.mouse.start()

    def _hotkey_failed(self, names: list[str]):
        labels = []
        for n in names:
            if n == TOOLBAR_KEY:
                labels.append(f"toolbar ({self.cfg['toolbar_hotkey']})")
            else:
                a = self.cfg.action(n)
                if a:
                    labels.append(f"{a['name']} ({a['hotkey']})")
        if labels:
            self.tray.showMessage("Quillbar: shortcut already in use",
                                  "Taken by another app: " + ", ".join(labels) +
                                  ". Change it in Settings.", QSystemTrayIcon.Warning, 6000)

    def set_paused(self, on: bool):
        self.paused = on
        self.dismiss()
        self.restart_listeners()

    # ============================================================== triggers
    def on_hotkey(self, name: str):
        if name == _T_ESC:
            self.dismiss()
        elif name.startswith(_T_NUM):
            idx = name[len(_T_NUM):]
            shift = idx.endswith("s")
            i = int(idx.rstrip("s"))
            if self.toolbar.isVisible() and i < len(self.toolbar.visible_ids):
                self.on_action(self.toolbar.visible_ids[i], shift)
        elif name == _T_ENTER:
            self.card_accept()
        elif name == _T_TAB:
            self.card_retry()
        elif name == _T_COPY:
            self.card_copy()
        elif name == _T_DIFF:
            self.card.toggle_diff()
        elif name == TOOLBAR_KEY:
            self.open_toolbar()
        else:
            self.run_direct(name)

    def _temp_keys(self, state: str):
        """bar: 1-9, Shift+1-9, Esc | preview: Enter, Tab, C, D, Esc | none."""
        if not self.hk:
            return
        keys: dict[str, str] = {}
        if state == "bar":
            keys[_T_ESC] = "Esc"
            for i in range(min(9, len(self.toolbar.visible_ids))):
                keys[f"{_T_NUM}{i}"] = str(i + 1)
                keys[f"{_T_NUM}{i}s"] = f"Shift+{i + 1}"
        elif state == "preview":
            keys = {_T_ESC: "Esc", _T_ENTER: "Enter", _T_TAB: "Tab", _T_COPY: "C", _T_DIFF: "D"}
        elif state == "busy":
            keys = {_T_ESC: "Esc"}
        self.hk.set_temp(keys)

    def _target(self) -> tuple[int, str] | None:
        hwnd = get_foreground()
        if not hwnd or is_own_window(hwnd):
            return None
        return root_of(hwnd), process_name(hwnd)

    def _capture(self, app: str) -> str | None:
        text = self.bridge.capture()
        if (not text or not text.strip()) and self.cfg["smart_select"] \
                and is_smart_select_app(self.cfg, app):
            text = self.bridge.capture(select_all=True)
        if not text or not text.strip():
            self.toast.show_message("Select some text first")
            return None
        if len(text) > 12000:
            self.toast.show_message("Selection too long (max ~12k characters)", error=True)
            return None
        return text

    def _begin(self) -> Session | None:
        t = self._target()
        if not t:
            return None
        text = self._capture(t[1])
        return Session(t[0], t[1], text) if text else None

    def _show_bar(self, source: str):
        self.toolbar.set_actions(self.cfg.toolbar_actions, self.cfg["max_visible"],
                                 self.cfg["show_numbers"] and source == "key",
                                 self.cfg["show_ask"])
        self.toolbar.show_at(QCursor.pos(), self.cfg["show_ask"])
        if source == "key":
            self._temp_keys("bar")
        self._held_at_open = {vk for vk in _TYPING_VKS if key_down(vk)}
        self._mouse_deadline = time.monotonic() + 6
        self.watch.start()

    def open_toolbar(self):
        if self.busy or self.card.isVisible():
            return
        if self.toolbar.isVisible():
            if self.session and self.session.source == "mouse":
                self.dismiss()  # fall through: reopen with keyboard semantics
            else:
                self.dismiss()
                return
        s = self._begin()
        if s:
            self.session = s
            self._show_bar("key")

    def on_mouse_select(self):
        """Mouse mode: show the bar without copying anything yet."""
        if self.busy or self.card.isVisible() or self.paused:
            return
        if self.toolbar.isVisible() and self.session and self.session.source == "key":
            return
        t = self._target()
        if not t:
            return
        excluded = {n.strip().lower() for n in self.cfg["mouse_exclude"].split(",") if n.strip()}
        if t[1].lower() in excluded:
            return
        self.session = Session(t[0], t[1], None, "mouse")
        self._show_bar("mouse")

    def run_direct(self, aid: str):
        if self.busy or self.card.isVisible():
            return
        a = self.cfg.action(aid)
        if not a:
            return
        if self.toolbar.isVisible() and self.session:
            self.on_action(aid)
            return
        s = self._begin()
        if s:
            self.session = s
            self._start_action(a, key_down(VK_SHIFT))

    # =============================================================== actions
    def _ensure_text(self) -> bool:
        s = self.session
        if not s:
            return False
        if s.text is None:  # mouse mode: copy now, the chat app still has focus
            s.text = self._capture(s.app)
            if s.text is None:
                self.dismiss()
                return False
        return True

    def on_action(self, aid: str, preview: bool = False):
        a = self.cfg.action(aid)
        if a and self.session and not self.busy and self._ensure_text():
            self._start_action(a, preview)

    def _on_asking(self):
        # Mouse mode: grab the selection now, before the ask box takes focus.
        self._temp_keys("none")
        if self.session and self.session.text is None:
            self._ensure_text()

    def on_ask(self, instruction: str):
        if self.session and not self.busy and self._ensure_text():
            self._go(Run(None, "Custom", instruction, "replace", "transform"))

    def _start_action(self, a: dict, preview: bool):
        mode = "preview" if preview else a.get("mode", "replace")
        self._go(Run(a, a["name"], a["prompt"], mode, a.get("kind", "transform")))

    def _go(self, run: Run, source_text: str | None = None):
        refine = source_text is not None
        if not self.cfg.is_configured:
            self.dismiss()
            self.toast.show_message("Add an API key in Settings first", error=True)
            self.open_settings_providers()
            return
        s = self.session
        if source_text is None:
            run.lead, run.core, run.trail = split_ws(s.text)
            run.diff_base = run.core
            source_text = run.core
        payload, run.mapping = (redact(source_text) if self.cfg["redact"]
                                else (source_text, {}))
        system = system_for(self.cfg, {"kind": "transform" if refine else run.kind},
                                    s.app)
        if run.mapping:
            system += "\n\n" + REDACT_NOTE
        instruction = instruction_for(self.cfg, run.instruction, s.app)
        provider = copy.deepcopy(self.cfg.provider_for(run.action))
        self.run = run
        self.busy = True
        self.job += 1
        job = self.job

        if run.mode == "preview":
            anchor = self.toolbar.geometry().bottomLeft() + QPoint(self.toolbar.width() // 2, -18) \
                if self.toolbar.isVisible() else QCursor.pos() + QPoint(0, 16)
            self.toolbar.hide()
            primary = "Replace" if run.kind == "transform" else "Copy"
            self.card.start(run.label, f"{s.app or 'app'} · {provider['model']}",
                            run.diff_base, primary, can_diff=run.kind == "transform")
            if not self.card.isVisible():
                self.card.show_near(anchor)
            self._temp_keys("busy")
            self.watch.start()

            def work():
                try:
                    out = stream(provider, system, instruction, payload,
                                     lambda d: self._delta.emit(job, d),
                                     cancelled=lambda: job != self.job)
                    self._done.emit(job, out, "")
                except Exception as e:  # noqa: BLE001
                    self._done.emit(job, "", str(e) or e.__class__.__name__)
        else:
            verb = {"Rewrite": "Rewriting", "Paraphrase": "Paraphrasing", "Fix": "Fixing",
                    "Shorter": "Shortening", "Translate": "Translating"}.get(run.label, run.label)
            if not self.toolbar.isVisible():
                self.toolbar.set_actions([], 0, False, False)
                self.toolbar.show_at(QCursor.pos(), False)
                self.watch.start()
            self.toolbar.set_busy(f"{verb}…")
            self._temp_keys("busy")

            def work():
                try:
                    out = complete(provider, system, instruction, payload)
                    self._done.emit(job, out, "")
                except Exception as e:  # noqa: BLE001
                    self._done.emit(job, "", str(e) or e.__class__.__name__)

        threading.Thread(target=work, daemon=True).start()

    def _on_delta(self, job: int, chunk: str):
        if job == self.job and self.card.isVisible():
            self.card.append(chunk)

    def _on_done(self, job: int, out: str, err: str):
        if job != self.job:
            return  # cancelled / superseded
        self.busy = False
        run, s = self.run, self.session
        if not run or not s:
            return
        if not err:
            out = restore(out, run.mapping)
            run.result = out
            if self.cfg["keep_history"]:
                self.history.add(Entry(run.label, s.app, run.diff_base, out))

        if run.mode == "preview":
            if err:
                self.card.fail(err)
            else:
                self.card.finish(out)
            self._temp_keys("preview")
            return

        self.dismiss(keep_session=True)
        if err:
            self.toast.show_message(err, error=True, ms=5000)
        elif run.mode == "copy":
            QApplication.clipboard().setText(self._final(run))
            self.toast.show_message("Copied — paste with Ctrl+V")
        else:
            self._paste(self._final(run))

    @staticmethod
    def _final(run: Run) -> str:
        return run.lead + run.result + run.trail if run.kind == "transform" else run.result

    def _paste(self, text: str):
        s = self.session
        if s and self.bridge.paste(s.hwnd, text):
            return
        where = s.app if s and s.app else "the original window"
        self.toast.show_message(f"Couldn't return to {where}. Result copied — paste with Ctrl+V.",
                                ms=5000)

    # ============================================================ preview
    def card_accept(self):
        run = self.run
        if not self.card.isVisible() or self.busy or not run or not run.result:
            return
        self.dismiss(keep_session=True)
        if run.kind == "transform":
            self._paste(self._final(run))
        else:
            QApplication.clipboard().setText(run.result)
            self.toast.show_message("Copied — click the reply box and press Ctrl+V")

    def card_copy(self):
        run = self.run
        if not self.card.isVisible() or self.busy or not run or not run.result:
            return
        QApplication.clipboard().setText(self._final(run))
        self.dismiss(keep_session=True)
        self.toast.show_message("Copied")

    def card_retry(self):
        run = self.run
        if not self.card.isVisible() or self.busy or not run:
            return
        fresh = Run(run.action, run.label, run.instruction, "preview", run.kind)
        self._go(fresh)

    def card_refine(self, how: str):
        run = self.run
        if self.busy or not run or not run.result or not self.session:
            return
        nxt = Run(run.action, run.label, f"Revise this text: {how}. Keep everything else.",
                  "preview", run.kind, run.lead, run.core, run.trail, run.diff_base)
        self._go(nxt, source_text=run.result)

    # ============================================================ teardown
    def dismiss(self, keep_session: bool = False):
        if self.busy:
            self.job += 1  # orphan the running request
            self.busy = False
            self.toast.show_message("Cancelled")
        self.watch.stop()
        self.toolbar.hide()
        self.card.hide()
        if self.hk:
            self.hk.set_temp(None)
        if not keep_session:
            self.session = None

    def _watchdog(self):
        """Close on click elsewhere, app switch, typing; mouse-mode bar also times out."""
        bar, card = self.toolbar.isVisible(), self.card.isVisible()
        if not (bar or card) or not self.session:
            return
        if self.toolbar.mode == "ask" or self.card.refine_edit.hasFocus():
            return
        pos = QCursor.pos()
        over = (bar and self.toolbar.contains_global(pos)) or (card and self.card.contains_global(pos))
        fg = root_of(get_foreground())
        if fg and fg != self.session.hwnd and not is_own_window(fg):
            self.dismiss()
            return
        if self.busy and not card:
            return
        if (key_down(VK_LBUTTON) or key_down(VK_RBUTTON)) and not over:
            self.dismiss()
            return
        mouse_bar = self.session.source == "mouse" and bar and not card
        vks = _TYPING_VKS + (_DIGIT_VKS if mouse_bar else [])
        if card:  # Enter/C/D are our keys while the card is open
            vks = [vk for vk in vks if vk not in (0x0D, 0x43, 0x44)]
        down = {vk for vk in vks if key_down(vk)}
        self._held_at_open &= down
        if not (key_down(VK_CONTROL) or key_down(VK_MENU)) \
                and down - self._held_at_open:
            self.dismiss()
            return
        if mouse_bar and not self.busy:
            if over:
                self._mouse_deadline = time.monotonic() + 4
            elif time.monotonic() > self._mouse_deadline:
                self.dismiss()

    # ============================================================ windows
    def open_history(self):
        self.dismiss()
        self.history_dlg = HistoryDialog(self.history, palette(self.cfg["theme"]))
        self.history_dlg.setWindowIcon(make_icon())
        self.history_dlg.show()
        self.history_dlg.raise_()
        self.history_dlg.activateWindow()

    def open_settings(self, page: int = 0):
        self.dismiss()
        if self.settings_dlg and self.settings_dlg.isVisible():
            self.settings_dlg.raise_()
            self.settings_dlg.activateWindow()
            return
        # Global hotkeys would swallow keys typed into the shortcut fields.
        self.stop_listeners()
        self.settings_dlg = SettingsDialog(self.cfg)
        self.settings_dlg.setWindowIcon(make_icon())
        self.settings_dlg.nav.setCurrentRow(page)
        self.settings_dlg.saved.connect(self.apply_config)
        self.settings_dlg.finished.connect(lambda _: self.restart_listeners())
        self.settings_dlg.show()
        self.settings_dlg.raise_()
        self.settings_dlg.activateWindow()

    def open_settings_providers(self):
        self.open_settings(page=PAGE_PROVIDERS)

    def quit(self):
        self.dismiss()
        self.stop_listeners()
        self.tray.hide()
        self.app.quit()


#==============================================================================
# main: Entry point
#==============================================================================

def main() -> int:
    if IS_WIN:
        try:  # crisp text on high-DPI screens; Qt sets this too, harmless if already set
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName("Quillbar")
    app.setQuitOnLastWindowClosed(False)
    if not single_instance():
        QMessageBox.information(None, "Quillbar", "Quillbar is already running (see tray).")
        return 0
    app.controller = Controller(app)  # keep a reference for the app lifetime
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
