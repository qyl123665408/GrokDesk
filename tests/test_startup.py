"""Check desktop startup and cleanup with real HTTP and mocked native windows."""
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import desktop


class ExitedLauncher:
    def __init__(self, status=0):
        self.status = status

    def poll(self):
        return self.status

    def wait(self, *args, **kwargs):
        raise AssertionError('The service must not wait for the browser launcher.')


class StartupTests(unittest.TestCase):
    def test_native_waits_for_api_exit_and_cleans_resources(self):
        self.run_service()

    def test_native_window_close_stops_service_and_preserves_history(self):
        self.run_service(close_native=True)

    def test_browser_mode_stays_alive_after_launcher_exit(self):
        self.run_service(arguments=['--browser'])

    def test_no_window_mode_loads_no_ui_or_desktop_instance(self):
        self.run_service(arguments=['--no-window'])

    @unittest.skipUnless(os.name == 'nt', 'Windows named activation events')
    def test_duplicate_launch_signals_native_instance_and_reuses_service(self):
        self.run_service(duplicate=True)

    def test_native_load_failure_stops_service_without_implicit_browser(self):
        with tempfile.TemporaryDirectory() as folder:
            servers = []
            original_create = app.create_server

            def create(*args, **kwargs):
                server = original_create(*args, **kwargs)
                servers.append(server)
                return server

            with patch.object(app, 'create_server', side_effect=create), \
                    patch.object(app, 'run_desktop_window', side_effect=RuntimeError('Synthetic native load failure')) as native, \
                    patch.object(app, 'open_window') as browser:
                with self.assertRaisesRegex(RuntimeError, 'Synthetic native load failure'):
                    app.main(['--data-dir', folder, '--port', '0'])
                native.assert_called_once()
                browser.assert_not_called()
            self.assertEqual(len(servers), 1)
            self.assertEqual(servers[0].socket.fileno(), -1)
            self.assert_service_stopped(servers[0].server_port)
            log = (Path(folder) / 'startup.log').read_text('utf-8')
            self.assertIn('Synthetic native load failure', log)
            self.assertIn('Local service stopped', log)
            self.assert_instance_released(folder)

    def assert_service_stopped(self, port):
        with httpx.Client(trust_env=False, timeout=0.6) as client:
            with self.assertRaises(httpx.TransportError):
                client.get(f'http://127.0.0.1:{port}/')

    def assert_instance_released(self, folder):
        if os.name == 'nt':
            instance = desktop.DesktopInstance(folder)
            try:
                self.assertTrue(instance.is_primary, 'Exit must release the named instance mutex.')
            finally:
                instance.close()

    def run_service(self, arguments=None, close_native=False, duplicate=False):
        arguments = arguments or []
        native_mode = not arguments
        with tempfile.TemporaryDirectory() as folder:
            ready = threading.Event()
            window_opened = threading.Event()
            native_closed = threading.Event()
            servers, failures, native_calls, activations = [], [], [], []
            original_create = app.create_server

            def create(*args, **kwargs):
                server = original_create(*args, **kwargs)
                servers.append(server)
                ready.set()
                return server

            def native_window(url, data_folder, shutdown_requested, instance=None):
                native_calls.append((url, data_folder, shutdown_requested, instance))
                window_opened.set()
                while not shutdown_requested.wait(0.02):
                    if native_closed.is_set():
                        shutdown_requested.set()
                        return
                    if instance:
                        activations.extend(instance.pending())

            def open_browser(*args, **kwargs):
                window_opened.set()
                return ExitedLauncher()

            def run():
                try:
                    app.main(['--data-dir', folder, '--port', '0'] + arguments)
                except BaseException as error:
                    failures.append(error)

            with patch.object(app, 'create_server', side_effect=create), \
                    patch.object(app, 'run_desktop_window', side_effect=native_window) as native, \
                    patch.object(app, 'open_window', side_effect=open_browser) as browser, \
                    patch.object(app, 'DesktopInstance', wraps=desktop.DesktopInstance) as instance_factory:
                thread = threading.Thread(target=run, daemon=True)
                thread.start()
                try:
                    self.assertTrue(ready.wait(10), failures)
                    if '--no-window' not in arguments:
                        self.assertTrue(window_opened.wait(10), failures)
                    server = servers[0]
                    url = f'http://127.0.0.1:{server.server_port}'
                    with httpx.Client(base_url=url, trust_env=False, timeout=5) as client:
                        html = client.get('/').raise_for_status().text
                        token = re.search(r'name="session-token" content="([^"]+)"', html)[1]
                        self.assertEqual(client.post('/api/exit', json={}).status_code, 403)
                        client.headers['X-GrokDesk-Token'] = token
                        bootstrap = client.get('/api/bootstrap').raise_for_status().json()
                        self.assertEqual(bootstrap['version'], '1.5.0')
                        self.assertEqual(Path(bootstrap['data_dir']).resolve(), Path(folder).resolve())
                        time.sleep(0.3)
                        self.assertTrue(thread.is_alive(), failures)
                        if native_mode:
                            native.assert_called_once_with(url, server.app.folder,
                                                           server.app.shutdown_requested,
                                                           instance=native_calls[0][3])
                            browser.assert_not_called()
                            if os.name == 'nt':
                                self.assertIsInstance(native_calls[0][3], desktop.DesktopInstance)
                            else:
                                self.assertIsNone(native_calls[0][3])
                        elif '--browser' in arguments:
                            native.assert_not_called()
                            browser.assert_called_once_with(url, server.app.folder,
                                                            browser=True, restore=False)
                        else:
                            native.assert_not_called()
                            browser.assert_not_called()
                            instance_factory.assert_not_called()
                        if duplicate:
                            for browser_mode in (False, True):
                                previous = len(activations)
                                duplicate_args = ['--data-dir', folder, '--port', '0']
                                if browser_mode:
                                    duplicate_args.append('--browser')
                                app.main(duplicate_args)
                                deadline = time.monotonic() + 5
                                while len(activations) == previous and time.monotonic() < deadline:
                                    time.sleep(0.02)
                                self.assertEqual(activations[previous:], ['browser' if browser_mode else 'desktop'])
                                self.assertEqual(len(servers), 1, 'Duplicate launch must reuse the existing service.')
                                self.assertEqual(native.call_count, 1)
                                browser.assert_not_called()
                        conversation = client.post('/api/conversations', json={}).raise_for_status().json()
                        if close_native:
                            native_closed.set()
                        else:
                            self.assertEqual(client.post('/api/exit', json={}).raise_for_status().json(), {'ok': True})
                    thread.join(8)
                    self.assertFalse(thread.is_alive(), 'Desktop exit must stop the local service.')
                    self.assertEqual(failures, [])
                    self.assertEqual(server.socket.fileno(), -1)
                    self.assert_service_stopped(server.server_port)
                    self.assertEqual(app.Database(Path(folder)).conversation(conversation['id'])['id'], conversation['id'])
                    log = (Path(folder) / 'startup.log').read_text('utf-8')
                    self.assertIn('service ready', log)
                    self.assertIn('Local service stopped', log)
                    self.assert_instance_released(folder)
                finally:
                    native_closed.set()
                    for server in servers:
                        server.app.shutdown_requested.set()
                    thread.join(10)


if __name__ == '__main__':
    unittest.main()
