"""GitHub release checks stay bounded and compare stable numeric versions."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


class UpdateCheckTests(unittest.TestCase):
    @staticmethod
    def response(status_code, **kwargs):
        request = httpx.Request('GET', app.LATEST_RELEASE_API)
        return httpx.Response(status_code, request=request, **kwargs)

    def test_reports_newer_release(self):
        response = self.response(200, json={'tag_name': 'v1.6.0'})
        with patch.object(app.httpx, 'get', return_value=response) as request:
            result = app.check_for_updates()

        self.assertEqual(result['current_version'], '1.5.1')
        self.assertEqual(result['latest_version'], '1.6.0')
        self.assertTrue(result['update_available'])
        self.assertEqual(result['release_url'], app.RELEASE_PAGE_URL)
        request.assert_called_once_with(
            app.LATEST_RELEASE_API,
            headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'GrokDesk/1.5.1'},
            timeout=4.0,
        )

    def test_equal_release_is_current(self):
        with patch.object(app.httpx, 'get', return_value=self.response(200, json={'tag_name': 'v1.5.1'})):
            result = app.check_for_updates()
        self.assertFalse(result['update_available'])

    def test_missing_public_release_is_reported(self):
        with patch.object(app.httpx, 'get', return_value=self.response(404)):
            with self.assertRaisesRegex(app.UserError, '没有找到可公开访问'):
                app.check_for_updates()

    def test_malformed_release_tag_is_rejected(self):
        with patch.object(app.httpx, 'get', return_value=self.response(200, json={'tag_name': 'latest'})):
            with self.assertRaisesRegex(app.UserError, '版本号格式不正确'):
                app.check_for_updates()

    def test_timeout_is_reported_without_waiting_indefinitely(self):
        with patch.object(app.httpx, 'get', side_effect=httpx.TimeoutException('timeout')):
            with self.assertRaisesRegex(app.UserError, '检查更新超时'):
                app.check_for_updates()


if __name__ == '__main__':
    unittest.main()
