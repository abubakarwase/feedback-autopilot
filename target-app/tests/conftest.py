import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


@pytest.fixture(scope="session")
def app_server():
    handler = functools.partial(QuietHandler, directory=str(APP_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="session")
def base_url(app_server):
    return app_server


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args, browser_type):
    # No bundled Playwright browser installed: fall back to the system Google Chrome.
    if browser_type.name == "chromium" and not Path(browser_type.executable_path).exists():
        return {**browser_type_launch_args, "channel": "chrome"}
    return browser_type_launch_args
