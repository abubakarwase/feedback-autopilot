import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import expect

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


# Playwright waits 30 s by default on a missing element; fail fast so a bad repro test does not stall the run.
expect.set_options(timeout=5000)


@pytest.fixture
def page(page):
    page.set_default_timeout(5000)
    return page
