"""Real Windows input smoke test; all clicks and text target this script's own window.

Run manually on an unlocked desktop with 150% display scaling. No provider,
clipboard, user files, or screenshot artifacts are used.
"""
import ctypes
from ctypes import wintypes as W
import json
from pathlib import Path
import sys
import threading
import time

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root if (root / 'computer_control.py').is_file() else root.parent / 'backend'))
from computer_control import DesktopController

assert sys.platform == 'win32', 'This native smoke test requires Windows.'
user = ctypes.WinDLL('user32', use_last_error=True)
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
user.SetThreadDpiAwarenessContext.argtypes, user.SetThreadDpiAwarenessContext.restype = [W.HANDLE], W.HANDLE
assert user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4)), 'Cannot enable physical desktop coordinates.'
user.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID]
user.CreateWindowExW.restype = W.HWND
kernel.GetModuleHandleW.argtypes, kernel.GetModuleHandleW.restype = [W.LPCWSTR], W.HMODULE
user.GetForegroundWindow.restype = user.GetFocus.restype = W.HWND
user.ShowWindow.argtypes = [W.HWND, ctypes.c_int]
user.SetForegroundWindow.argtypes, user.SetForegroundWindow.restype = [W.HWND], W.BOOL
user.GetDpiForWindow.argtypes, user.GetDpiForWindow.restype = [W.HWND], W.UINT
user.GetWindowRect.argtypes, user.GetWindowRect.restype = [W.HWND, ctypes.POINTER(W.RECT)], W.BOOL
user.GetCursorPos.argtypes, user.GetCursorPos.restype = [ctypes.POINTER(W.POINT)], W.BOOL
user.ClientToScreen.argtypes, user.ClientToScreen.restype = [W.HWND, ctypes.POINTER(W.POINT)], W.BOOL
user.SetCursorPos.argtypes, user.SetCursorPos.restype = [ctypes.c_int, ctypes.c_int], W.BOOL
user.SetFocus.argtypes, user.SetFocus.restype = [W.HWND], W.HWND
user.WindowFromPoint.argtypes, user.WindowFromPoint.restype = [W.POINT], W.HWND
user.PeekMessageW.argtypes, user.PeekMessageW.restype = [ctypes.POINTER(W.MSG), W.HWND, W.UINT, W.UINT, W.UINT], W.BOOL
user.TranslateMessage.argtypes = [ctypes.POINTER(W.MSG)]
user.DispatchMessageW.argtypes, user.DispatchMessageW.restype = [ctypes.POINTER(W.MSG)], ctypes.c_ssize_t
user.GetWindowTextW.argtypes, user.GetWindowTextW.restype = [W.HWND, W.LPWSTR, ctypes.c_int], ctypes.c_int
user.SendMessageW.argtypes, user.SendMessageW.restype = [W.HWND, W.UINT, W.WPARAM, W.LPARAM], ctypes.c_ssize_t
user.GetKeyState.argtypes, user.GetKeyState.restype = [ctypes.c_int], ctypes.c_short
user.DestroyWindow.argtypes, user.DestroyWindow.restype = [W.HWND], W.BOOL
window_proc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
user.SetWindowLongPtrW.argtypes, user.SetWindowLongPtrW.restype = [W.HWND, ctypes.c_int, ctypes.c_void_p], ctypes.c_void_p
user.CallWindowProcW.argtypes, user.CallWindowProcW.restype = [ctypes.c_void_p, W.HWND, W.UINT, W.WPARAM, W.LPARAM], ctypes.c_ssize_t


def pump(seconds=.2):
    deadline = time.monotonic() + seconds
    message = W.MSG()
    while time.monotonic() < deadline:
        while user.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
            user.TranslateMessage(ctypes.byref(message))
            user.DispatchMessageW(ctypes.byref(message))
        time.sleep(.01)


def contents(handle=None):
    buffer = ctypes.create_unicode_buffer(4096)
    user.GetWindowTextW(edit if handle is None else handle, buffer, len(buffer))
    return buffer.value


window = edit = approval = approval_edit = None
old_procs, callbacks, wheel_events = {}, {}, []
click_events = []
selection_trace = []
key_trace = []


def selection(stage):
    start, end = W.DWORD(), W.DWORD()
    user.SendMessageW(edit, 0x00B0, ctypes.addressof(start), ctypes.addressof(end))  # EM_GETSEL
    result = (start.value, end.value)
    selection_trace.append({'stage': stage, 'selection': result, 'edit_focused': user.GetFocus() == edit})
    return result


def observe(handle, restore_focus=False):
    @window_proc
    def callback(hwnd, message, wparam, lparam):
        if message == 0x0201:
            point = W.POINT(ctypes.c_short(lparam & 0xFFFF).value, ctypes.c_short((lparam >> 16) & 0xFFFF).value)
            user.ClientToScreen(hwnd, ctypes.byref(point))
            click_events.append((hwnd, point.x, point.y))
        if handle == edit and message in (0x0100, 0x0101, 0x0104, 0x0105):
            key_trace.append({'message': {0x0100: 'keydown', 0x0101: 'keyup', 0x0104: 'syskeydown', 0x0105: 'syskeyup'}[message],
                              'vk': int(wparam), 'shift_state': int(user.GetKeyState(0x10)),
                              'ctrl_state': int(user.GetKeyState(0x11)),
                              'shift_down': bool(user.GetKeyState(0x10) & 0x8000),
                              'ctrl_down': bool(user.GetKeyState(0x11) & 0x8000),
                              'lparam': int(lparam), 'edit_focused': user.GetFocus() == edit})
        if message == 0x0007 and restore_focus:  # WM_SETFOCUS: remember this app's input.
            user.SetFocus(edit)
            return 0
        if message == 0x020A:  # WM_MOUSEWHEEL: prove the wheel reached our own window.
            wheel_events.append((hwnd, ctypes.c_short((wparam >> 16) & 0xFFFF).value,
                                 ctypes.c_short(lparam & 0xFFFF).value,
                                 ctypes.c_short((lparam >> 16) & 0xFFFF).value))
        if handle == edit and message in (0x0007, 0x0008):  # Focus transitions may reset a plain EDIT selection.
            event = 'setfocus' if message == 0x0007 else 'killfocus'
            selection(event + '-before-default')
            result = user.CallWindowProcW(old_procs[handle], hwnd, message, wparam, lparam)
            selection(event + '-after-default')
            return result
        return user.CallWindowProcW(old_procs[handle], hwnd, message, wparam, lparam)
    callbacks[handle] = callback  # Keep native callback objects alive until destruction.
    old_procs[handle] = user.SetWindowLongPtrW(handle, -4, ctypes.cast(callback, ctypes.c_void_p))
    assert old_procs[handle], 'Cannot install isolated window observation callback.'


controller, cancel = DesktopController(), threading.Event()
try:
    module = kernel.GetModuleHandleW(None)
    window = user.CreateWindowExW(0, 'STATIC', 'GrokDesk isolated native input test', 0x10CF0000, 160, 160, 720, 280, None, None, module, None)
    assert window, 'Cannot create isolated test window.'
    edit = user.CreateWindowExW(0x200, 'EDIT', '', 0x50010080, 30, 65, 640, 90, window, None, module, None)
    assert edit, 'Cannot create test text input.'
    observe(window, restore_focus=True)
    observe(edit)
    approval = user.CreateWindowExW(0, 'STATIC', 'GrokDesk isolated approval focus test', 0x00CF0000, 940, 160, 520, 240, None, None, module, None)
    assert approval, 'Cannot create isolated approval window.'
    approval_marker = 'APPROVAL WINDOW MUST NOT RECEIVE MODEL INPUT'
    approval_edit = user.CreateWindowExW(0x200, 'EDIT', approval_marker, 0x50010080, 20, 55, 460, 85, approval, None, module, None)
    assert approval_edit, 'Cannot create isolated approval input.'
    user.ShowWindow(window, 5)
    assert user.SetForegroundWindow(window), 'Windows refused to focus the isolated test window.'
    pump()
    dpi = user.GetDpiForWindow(window)
    assert dpi == 144, f'Expected 150% scaling (144 DPI), got {dpi} DPI.'
    assert user.GetForegroundWindow() == window, 'Test window lost foreground before capture.'
    screen = controller.capture()
    assert screen.foreground == window, 'Screenshot foreground must be the isolated test window.'
    rect = W.RECT()
    assert user.GetWindowRect(edit, ctypes.byref(rect))
    target_x, target_y = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
    x = round((target_x - screen.left) * screen.width / screen.desktop_width)
    y = round((target_y - screen.top) * screen.height / screen.desktop_height)

    def execute(action, steal_focus=False):
        assert user.GetForegroundWindow() == window, 'Foreground changed; no further input sent.'
        label = action['type'] + ':' + '+'.join(action.get('keys', []))
        selection(label + '-before-focus-change')
        if steal_focus:
            user.ShowWindow(approval, 5)
            assert user.SetForegroundWindow(approval), 'Cannot activate isolated approval window.'
            user.SetFocus(approval_edit)
            approval_rect = W.RECT()
            assert user.GetWindowRect(approval_edit, ctypes.byref(approval_rect))
            assert user.SetCursorPos((approval_rect.left + approval_rect.right) // 2,
                                     (approval_rect.top + approval_rect.bottom) // 2)
            pump()
            assert user.GetForegroundWindow() == approval and user.GetFocus() == approval_edit, 'Approval simulation did not take focus.'
            selection(label + '-approval-focused')
        assert controller.execute(action, screen, cancel), 'Desktop action was cancelled.'
        pump()
        assert user.GetForegroundWindow() == window, 'Controller failed to restore captured foreground.'
        assert contents(approval_edit) == approval_marker, 'Model input reached the isolated approval window.'
        selection(label + '-after-action')

    execute({'type': 'click', 'x': x, 'y': y})
    point = W.POINT()
    assert user.GetCursorPos(ctypes.byref(point))
    tolerance = max(screen.desktop_width / screen.width, screen.desktop_height / screen.height) + 1
    assert any(event[0] == edit and abs(event[1] - target_x) <= tolerance and abs(event[2] - target_y) <= tolerance for event in click_events), f'Mapped click did not reach target input: clicks={click_events!r} expected={(target_x, target_y)}'
    assert user.GetFocus() == edit, 'Mapped click did not focus the isolated text input.'
    screen = controller.capture()
    original = '中文输入测试 😀🚀 ABC123'
    execute({'type': 'type', 'text': original}, steal_focus=True)
    assert contents() == original, f'Unicode input mismatch: {contents()!r}'
    # Test key focus restoration once, then keep focus through the selection and
    # replacement. A fresh EDIT activation may itself reset the caret/selection;
    # repeatedly stealing focus would conflate that application behavior with input.
    execute({'type': 'key', 'keys': ['CTRL', 'HOME']}, steal_focus=True)
    assert user.GetFocus() == edit, 'Restored foreground did not restore its input focus.'
    assert selection('after-ctrl-home') == (0, 0), f'CTRL+HOME did not move to start: selection={selection_trace!r}, keys={key_trace!r}'
    execute({'type': 'key', 'keys': ['CTRL', 'SHIFT', 'END']})
    utf16_length = len(original.encode('utf-16-le')) // 2
    assert selection('after-ctrl-shift-end') == (0, utf16_length), f'CTRL+SHIFT+END did not select all UTF-16 units: selection={selection_trace!r}, keys={key_trace!r}'
    replacement = '快捷键替换成功 🎉'
    execute({'type': 'type', 'text': replacement})
    assert contents() == replacement, f'Keyboard shortcut selection failed: {contents()!r}'
    assert user.SetCursorPos(target_x, target_y)
    assert user.WindowFromPoint(W.POINT(target_x, target_y)) == edit, 'Scroll target is not the isolated input.'
    screen = controller.capture()
    assert (screen.cursor_x, screen.cursor_y) == (target_x, target_y), 'Capture lost original wheel position.'
    execute({'type': 'scroll', 'direction': 'down', 'amount': 2}, steal_focus=True)
    assert user.GetCursorPos(ctypes.byref(point))
    assert any(event[0] in (window, edit) and event[1:] == (-240, target_x, target_y) for event in wheel_events), f'Scroll did not reach original isolated input: {wheel_events!r}'
    print(json.dumps({'passed': True, 'dpi': dpi, 'display_scale': '150%', 'screenshot': [screen.width, screen.height], 'desktop': [screen.desktop_width, screen.desktop_height], 'checks': ['physical-coordinate screenshot mapping', 'isolated input click and focus', 'Chinese and emoji Unicode input', 'CTRL+HOME and CTRL+SHIFT+END replacement', 'type and key restore captured foreground after approval focus theft', 'scroll restores original cursor and delivers native wheel message'], 'selection_trace': selection_trace, 'key_trace': key_trace}, ensure_ascii=False))
except BaseException:
    print(json.dumps({'passed': False, 'selection_trace': selection_trace, 'key_trace': key_trace}, ensure_ascii=False), flush=True)
    raise
finally:
    cancel.set()
    if approval:
        user.DestroyWindow(approval)
    if window:
        user.DestroyWindow(window)
