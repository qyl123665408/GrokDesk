"""Exercise the production native window with synthetic, local-only content.

Run using the project venv after installing requirements.lock.txt. The script
uses the production main() and WebView2, with no debugging port or JS bridge.
Uploads use a synthetic File/DataTransfer (not the native file picker). Native
download save dialogs are observed and canceled only when owned by this PID.
Clipboard capability is inspected without reading or writing the clipboard.
Downloads use trusted, in-view DevTools input without moving the system mouse
or enabling a remote debugging port.
"""
import ctypes
from ctypes import wintypes as W
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
import webview
import app
from test_app import SyntheticProvider


def wait_for(callback, description, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = callback()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError('Timed out: ' + description)


def owned_dialogs():
    """Enumerate only this test process's visible native dialog windows."""
    user = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    user.EnumWindows.argtypes = [callback_type, W.LPARAM]
    user.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
    user.GetClassNameW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
    user.IsWindowVisible.argtypes = [W.HWND]
    result = []

    @callback_type
    def collect(hwnd, _):
        pid, name = W.DWORD(), ctypes.create_unicode_buffer(256)
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid() and user.IsWindowVisible(hwnd):
            user.GetClassNameW(hwnd, name, len(name))
            if name.value == '#32770':
                result.append(int(hwnd))
        return True

    user.EnumWindows(collect, 0)
    return result


def cancel_owned_dialog(hwnd):
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
    user.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
    pid = W.DWORD()
    user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    assert pid.value == os.getpid(), 'Refusing to touch another process dialog'
    assert user.PostMessageW(hwnd, 0x0111, 2, 0), 'Could not cancel owned SaveFileDialog'
    wait_for(lambda: hwnd not in owned_dialogs(), 'owned save dialog to close', 5)


def trusted_click(window, selector):
    """Queue a real WebView2 click in CSS pixels, returning its pending tasks.

    DownloadStarting opens a modal native dialog, which can delay completion of
    the release command. The worker must await these tasks after canceling that
    dialog; neither this helper nor the GUI thread waits on their results.
    """
    from System import Action

    position = window.evaluate_js("""(() => {
        const selector = %s;
        const element = document.querySelector(selector);
        if (!element) throw new Error('Missing click target: ' + selector);
        element.scrollIntoView({block:'center', inline:'center', behavior:'instant'});
        const rect = element.getBoundingClientRect();
        const left = Math.max(0, rect.left), right = Math.min(innerWidth, rect.right);
        const top = Math.max(0, rect.top), bottom = Math.min(innerHeight, rect.bottom);
        if (right <= left || bottom <= top) throw new Error('Invisible click target: ' + selector);
        const x = (left + right) / 2, y = (top + bottom) / 2;
        const hit = document.elementFromPoint(x, y);
        if (!hit || !element.contains(hit)) throw new Error('Covered click target: ' + selector);
        if (!window.__nativeUiClicks) window.__nativeUiClicks = [];
        element.addEventListener('click', event => window.__nativeUiClicks.push({
            selector, isTrusted:event.isTrusted,
            userActivationActive:navigator.userActivation.isActive
        }), {capture:true, once:true});
        return {x, y};
    })()""" % json.dumps(selector))
    tasks = []

    def dispatch():
        core = window.native.webview.CoreWebView2
        for kind, buttons in (('mousePressed', 1), ('mouseReleased', 0)):
            parameters = dict(type=kind, x=position['x'], y=position['y'],
                              button='left', buttons=buttons, clickCount=1)
            tasks.append(core.CallDevToolsProtocolMethodAsync(
                'Input.dispatchMouseEvent', json.dumps(parameters)))

    window.native.Invoke(Action(dispatch))
    assert len(tasks) == 2, 'Could not queue trusted press and release'
    return tasks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--download-order', choices=['md-first', 'attachment-first'], default='md-first')
    arguments = parser.parse_args()
    assert os.name == 'nt', 'This test requires Windows and WebView2'
    output = ROOT / 'test-output'
    output.mkdir(exist_ok=True)
    provider = ThreadingHTTPServer(('127.0.0.1', 0), SyntheticProvider)
    SyntheticProvider.payloads = []
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    results = {'passed': False, 'download_order': arguments.download_order, 'checks': [], 'untested': [
        'native file picker selection (upload uses synthetic File/DataTransfer)',
        'actual clipboard read/write (existing clipboard remains untouched)',
        'persisted download contents (native save dialogs are canceled)',
    ]}
    complete = threading.Event()
    failures = []
    native_events = []
    responses = []
    report = output / ('native-ui-report-' + arguments.download_order + '.json')
    with tempfile.TemporaryDirectory(prefix='native-ui-', dir=output) as temporary:
        folder = Path(temporary)
        app.Settings(folder).update({
            'base_url': f'http://127.0.0.1:{provider.server_port}',
            'api_key': 'synthetic-native-ui-key-never-valid-on-real-services',
            'model': 'grok-desk-synthetic',
        })

        def exercise():
            window = None
            client = None
            try:
                window = wait_for(lambda: webview.windows[0] if webview.windows else None,
                                  'production native window')
                assert window.events.loaded.wait(30), 'Native page did not load'
                wait_for(lambda: window.evaluate_js(
                    "typeof state === 'object' && state.settings.has_api_key && "
                    "document.querySelector('#dataLocation').textContent.length > 0"),
                    'production frontend bootstrap')
                window.evaluate_js("""window.__nativeUiErrors = [];
                    window.addEventListener('error', e => window.__nativeUiErrors.push(e.message));
                    window.addEventListener('unhandledrejection', e =>
                        window.__nativeUiErrors.push(String(e.reason?.message || e.reason)));
                    true;""")
                info = window.evaluate_js("""({
                    url: location.origin,
                    dataDir: document.querySelector('#dataLocation').textContent,
                    token: document.querySelector('meta[name="session-token"]').content,
                    modes: Array.from(document.querySelector('#controlMode').options,
                        o => [o.value, o.textContent.trim()]),
                    secureContext: window.isSecureContext,
                    clipboardWriteAvailable: typeof navigator.clipboard?.writeText === 'function',
                    fileReaderAvailable: typeof FileReader === 'function',
                    fileConstructorAvailable: typeof File === 'function',
                    apiKeys: Object.keys(window.pywebview?.api || {})
                })""")
                assert Path(info['dataDir']).resolve() == folder.resolve()
                assert info['modes'] == [['confirm', '需要确认'], ['auto', '完全权限']]
                assert info['secureContext'] and info['clipboardWriteAvailable']
                assert info['fileReaderAvailable'] and info['fileConstructorAvailable']
                assert info['apiKeys'] == [], info['apiKeys']
                results['checks'] += ['production WebView2 frontend bootstrap',
                                      'exactly two permission modes',
                                      'clipboard API capability without clipboard access',
                                      'no exposed native JS API functions']
                client = httpx.Client(base_url=info['url'], trust_env=False, timeout=5,
                                     headers={'X-GrokDesk-Token': info['token']})

                def response_received(response):
                    responses.append({'url': urlsplit(response.url).path,
                                      'status': response.status_code,
                                      'disposition': response.headers.get('Content-Disposition', '')})

                window.events.response_received += response_received
                from System import Action

                def install_observers():
                    core = window.native.webview.CoreWebView2

                    def download(_sender, args):
                        operation = args.DownloadOperation
                        native_events.append({'event': 'DownloadStarting',
                                              'url': urlsplit(str(operation.Uri)).path,
                                              'mime': str(operation.MimeType),
                                              'cancel': bool(args.Cancel)})

                    def permission(_sender, args):
                        native_events.append({'event': 'PermissionRequested',
                                              'kind': str(args.PermissionKind),
                                              'state': str(args.State),
                                              'user_initiated': bool(args.IsUserInitiated)})

                    def navigation(_sender, args):
                        native_events.append({'event': 'NavigationStarting',
                                              'url': urlsplit(str(args.Uri)).path,
                                              'user_initiated': bool(args.IsUserInitiated)})

                    # Observe before the existing handler opens its modal dialog;
                    # preserve the production handler and all permission defaults.
                    core.DownloadStarting -= window.native.browser.on_download_starting
                    core.DownloadStarting += download
                    core.DownloadStarting += window.native.browser.on_download_starting
                    core.PermissionRequested += permission
                    core.NavigationStarting += navigation

                window.native.Invoke(Action(install_observers))
                window.evaluate_js("""(() => {
                    const transfer = new DataTransfer();
                    transfer.items.add(new File(['synthetic native UI attachment'],
                        'native-ui.txt', {type:'text/plain'}));
                    const input = document.querySelector('#fileInput');
                    input.files = transfer.files;
                    input.dispatchEvent(new Event('change', {bubbles:true}));
                    return true;
                })()""")
                wait_for(lambda: window.evaluate_js(
                    "state.uploading === 0 && state.attachments.length === 1 && "
                    "document.querySelector('#attachmentTray').textContent.includes('native-ui.txt')"),
                    'real FileReader upload from synthetic File')
                results['checks'].append('file input change through FileReader and backend attachment')
                window.evaluate_js("""document.querySelector('#messageInput').value =
                    '本地原生窗口合成测试';
                    document.querySelector('#messageInput').dispatchEvent(new Event('input'));
                    document.querySelector('#sendButton').click(); true;""")
                wait_for(lambda: window.evaluate_js(
                    "!state.busy && state.active?.messages?.length === 2 && "
                    "state.active.messages[1].status === 'complete' && "
                    "document.querySelector('#messages').textContent.includes('本地测试回复')"),
                    'UI chat streamed reply')
                assert len(SyntheticProvider.payloads) == 1
                payload = json.dumps(SyntheticProvider.payloads[0], ensure_ascii=False)
                assert 'synthetic native UI attachment' in payload
                assert '本地原生窗口合成测试' in payload
                results['checks'].append('send button to local provider to rendered streaming reply')

                downloads = [
                    ('Markdown export', '#exportChat'),
                    ('attachment download', '.file-card[download]'),
                ]
                if arguments.download_order == 'attachment-first':
                    downloads.reverse()
                for label, selector in downloads:
                    results['last_download'] = label
                    before = set(owned_dialogs())
                    input_tasks = trusted_click(window, selector)
                    dialog = wait_for(lambda: next((handle for handle in owned_dialogs()
                                                   if handle not in before), None),
                                      label + ' native SaveFileDialog', 15)
                    cancel_owned_dialog(dialog)
                    for task in input_tasks:
                        wait_for(lambda pending=task: pending.IsCompleted,
                                 label + ' trusted input completion', 5)
                        _ = task.Result  # Surface failed DevTools calls on the worker thread.
                    results['checks'].append(label + ' opens native save dialog and cancels')
                results['download_clicks'] = window.evaluate_js('window.__nativeUiClicks || []')
                assert len(results['download_clicks']) == 2, results['download_clicks']
                assert all(click['isTrusted'] and click['userActivationActive']
                           for click in results['download_clicks']), results['download_clicks']
                results['checks'].append('both downloads use trusted clicks with active user gestures')

                errors = window.evaluate_js('window.__nativeUiErrors')
                assert errors == [], errors
                results['checks'].append('no captured frontend errors or unhandled rejections')
                client.post('/api/exit', json={}).raise_for_status()
                results['checks'].append('authenticated exit requested')
            except BaseException:
                failures.append(traceback.format_exc())
                results['failure'] = failures[-1]
                if window:
                    try:
                        results['frontend'] = window.evaluate_js("""({
                            path: location.pathname,
                            errors: window.__nativeUiErrors || [],
                            busy: state.busy,
                            messages: state.active?.messages?.length,
                            links: Array.from(document.querySelectorAll('.file-card[download]'), a => ({
                                href: new URL(a.href).pathname, download: a.download,
                                target:a.target, text:a.textContent.trim(), connected:a.isConnected
                            })),
                            toasts: document.querySelector('#toastStack').textContent
                        })""")
                    except Exception as error:
                        results['snapshot_error'] = str(error)
            finally:
                results['native_events'] = list(native_events)
                results['responses'] = list(responses)
                report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
                for dialog in owned_dialogs():
                    try:
                        cancel_owned_dialog(dialog)
                    except Exception:
                        pass
                if failures and window:
                    try:
                        if client:
                            client.post('/api/exit', json={})
                        else:
                            window.destroy()
                    except Exception:
                        window.destroy()
                if client:
                    client.close()
                complete.set()

        threading.Thread(target=exercise, name='native-ui-exercise', daemon=True).start()
        try:
            app.main(['--port', '0', '--data-dir', str(folder)])
            assert complete.wait(5), 'Native UI worker did not finish'
            if failures:
                raise AssertionError(failures[0])
            results['checks'].append('native window closes and production main returns')
            results['passed'] = True
        finally:
            provider.shutdown()
            provider.server_close()
    report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(results, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
