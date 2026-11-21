"""Compatibility entry point for packaged native WebView2 desktop verification.

All arguments, including --binary and --expected-version, are handled by the
native verifier. This entry point no longer tests browser app-mode hosting.
"""
from pathlib import Path
import runpy


if __name__ == '__main__':
    runpy.run_path(str(Path(__file__).with_name('verify_native_desktop_release.py')),
                   run_name='__main__')