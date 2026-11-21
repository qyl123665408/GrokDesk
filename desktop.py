"""Native desktop hosting, Windows activation, and explicit browser fallback."""
from __future__ import annotations

import ctypes
import hashlib
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import webbrowser


class DesktopInstance:
    """Keep one desktop service per data folder and queue reopen requests."""

    def __init__(self, folder):
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        self.kernel.CreateMutexW.restype = ctypes.c_void_p
        self.kernel.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_bool, ctypes.c_wchar_p]
        self.kernel.CreateEventW.restype = ctypes.c_void_p
        self.kernel.SetEvent.argtypes = [ctypes.c_void_p]
        self.kernel.SetEvent.restype = ctypes.c_bool
        self.kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.kernel.WaitForSingleObject.restype = ctypes.c_ulong
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel.CloseHandle.restype = ctypes.c_bool
        self.handles = []
        self.events = {}
        name = 'Local\\GrokDesk-' + hashlib.sha256(str(Path(folder).resolve()).lower().encode('utf-8')).hexdigest()[:20]
        try:
            # Create the events before the mutex so a launch during startup is retained.
            for mode in ('desktop', 'browser'):
                event = self.kernel.CreateEventW(None, False, False, name + '-' + mode)
                if not event:
                    raise ctypes.WinError(ctypes.get_last_error())
                self.events[mode] = event
                self.handles.append(event)
            mutex = self.kernel.CreateMutexW(None, False, name)
            error = ctypes.get_last_error()
            if not mutex:
                raise ctypes.WinError(error)
            self.handles.append(mutex)
            self.is_primary = error != 183  # ERROR_ALREADY_EXISTS
        except BaseException:
            self.close()
            raise

    def activate(self, browser=False):
        if not self.kernel.SetEvent(self.events['browser' if browser else 'desktop']):
            raise ctypes.WinError(ctypes.get_last_error())

    def pending(self):
        for mode, event in self.events.items():
            status = self.kernel.WaitForSingleObject(event, 0)
            if status == 0:  # WAIT_OBJECT_0; the auto-reset event consumes this request.
                yield mode
            elif status != 258:  # WAIT_TIMEOUT
                raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        for handle in reversed(self.handles):
            self.kernel.CloseHandle(handle)
        self.handles.clear()


def find_browser():
    for relative in ('Google/Chrome/Application/chrome.exe', 'Microsoft/Edge/Application/msedge.exe'):
        for variable in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
            folder = Path(os.environ.get(variable, '.'))
            executable = folder / relative
            if executable.is_file():
                return executable
    return None


def restore_window():
    if os.name != 'nt':
        return False
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    user32.FindWindowW.restype = ctypes.c_void_p
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    window = user32.FindWindowW(None, 'Grok Desk')
    if not window:
        return False
    user32.ShowWindow(window, 9)  # SW_RESTORE
    user32.SetForegroundWindow(window)
    return True


def open_window(url, folder, browser=False, restore=False):
    if browser:
        webbrowser.open(url)
        return None
    if restore and restore_window():
        return None
    executable = find_browser()
    if executable:
        try:
            options = {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)}
            if os.name == 'nt':
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = 1  # SW_SHOWNORMAL, even when a hidden launcher starts us.
                options['startupinfo'] = startup
            return subprocess.Popen([
                str(executable), '--app=' + url,
                '--user-data-dir=' + str(Path(folder) / 'desktop-profile'),
                '--window-size=1320,880', '--no-first-run',
                '--no-default-browser-check', '--disable-background-mode',
            ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
        except OSError:
            pass
    webbrowser.open(url)
    return None


def _activate_desktop_window(window) -> bool:
    """Restore the pywebview window and activate only its own native HWND."""
    window.restore()
    window.show()
    if os.name != 'nt':
        return True
    native = window.native
    if native is None:
        return False
    handle = native.Handle
    handle = handle.ToInt64() if hasattr(handle, 'ToInt64') else int(handle)
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.IsWindow.argtypes, user.IsWindow.restype = [ctypes.c_void_p], ctypes.c_int
    user.IsWindowVisible.argtypes, user.IsWindowVisible.restype = [ctypes.c_void_p], ctypes.c_int
    user.IsIconic.argtypes, user.IsIconic.restype = [ctypes.c_void_p], ctypes.c_int
    user.GetForegroundWindow.argtypes, user.GetForegroundWindow.restype = [], ctypes.c_void_p
    user.GetWindowThreadProcessId.argtypes, user.GetWindowThreadProcessId.restype = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)], ctypes.c_uint32
    user.SetForegroundWindow.argtypes, user.SetForegroundWindow.restype = [ctypes.c_void_p], ctypes.c_int
    user.BringWindowToTop.argtypes, user.BringWindowToTop.restype = [ctypes.c_void_p], ctypes.c_int
    user.AttachThreadInput.argtypes, user.AttachThreadInput.restype = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int], ctypes.c_int
    if not user.IsWindow(handle):
        return False
    owner = ctypes.c_uint32()
    target_thread = user.GetWindowThreadProcessId(handle, ctypes.byref(owner))
    if owner.value != os.getpid():
        return False
    if not user.SetForegroundWindow(handle):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentThreadId.argtypes, kernel.GetCurrentThreadId.restype = [], ctypes.c_uint32
        current = kernel.GetCurrentThreadId()
        active_thread = user.GetWindowThreadProcessId(user.GetForegroundWindow(), None)
        attached = []
        try:
            for thread in {active_thread, target_thread} - {0, current}:
                if user.AttachThreadInput(current, thread, True):
                    attached.append(thread)
            user.BringWindowToTop(handle)
            user.SetForegroundWindow(handle)
        finally:
            for thread in reversed(attached):
                user.AttachThreadInput(current, thread, False)
    visible = bool(user.IsWindowVisible(handle))
    minimized = bool(user.IsIconic(handle))
    foreground = user.GetForegroundWindow() == handle
    # Windows can deny foreground focus even after its synchronous GUI calls
    # have restored the window. Record focus separately from completion.
    logging.getLogger('grokdesk.desktop').info(
        'Native desktop activation state: visible=%s minimized=%s foreground=%s',
        visible, minimized, foreground)
    return visible and not minimized


def run_desktop_window(url, folder, shutdown_requested, instance=None):
    """Run the native WebView2 GUI on the main thread until this app closes.

    The local HTTP server must already be running in its own thread. Closing
    the native window requests service shutdown; /api/exit destroys the window.
    Runtime/dependency errors propagate to the caller without opening a browser.
    """
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError('Grok Desk 原生窗口必须从主线程启动。')
    import webview

    logger = logging.getLogger('grokdesk.desktop')
    resources = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    storage = Path(folder) / 'webview-profile'
    storage.mkdir(parents=True, exist_ok=True)
    finished = threading.Event()
    webview.settings['ALLOW_DOWNLOADS'] = True
    webview.settings['ALLOW_FILE_URLS'] = False
    webview.settings['REMOTE_DEBUGGING_PORT'] = None
    window = webview.create_window('Grok Desk', url, width=1320, height=880,
                                   min_size=(880, 600), text_select=True,
                                   js_api=None)
    initialization_errors = []

    def initialized(renderer):
        # pywebview can fall back to the legacy MSHTML renderer even when
        # edgechromium was requested. Stop before creating that incompatible UI.
        if renderer != 'edgechromium':
            initialization_errors.append('未找到 Microsoft Edge WebView2 运行时，请安装后重新打开 Grok Desk。')
            return False

    window.events.initialized += initialized

    def closed(*_):
        finished.set()
        shutdown_requested.set()

    window.events.closed += closed

    def watch():
        while not finished.wait(0.2):
            if shutdown_requested.is_set():
                try:
                    window.destroy()
                except Exception:
                    logger.exception('Could not close the native desktop window')
                return
            if instance is None:
                continue
            try:
                modes = list(instance.pending())
            except Exception:
                logger.exception('Could not receive desktop activation requests')
                continue
            for mode in modes:
                if finished.is_set() or shutdown_requested.is_set():
                    return
                try:
                    if mode == 'browser':
                        webbrowser.open(url)
                    elif mode == 'desktop':
                        if _activate_desktop_window(window):
                            logger.info('Native desktop window activated')
                        else:
                            logger.warning('Could not restore the native desktop window')
                except Exception:
                    logger.exception('Could not activate the requested desktop window (%s)', mode)

    try:
        webview.start(watch, gui='edgechromium', debug=False,
                      storage_path=str(storage), private_mode=False,
                      icon=str(resources / 'web' / 'icon.ico'))
        if initialization_errors:
            raise RuntimeError(initialization_errors[0])
    finally:
        finished.set()
        shutdown_requested.set()
