import json

from playwright.sync_api import Page, Route, expect


def test_feedback_posts_slack_message(page: Page):
    captured = []

    def handle(route: Route):
        assert route.request.method == "POST"
        captured.append(json.loads(route.request.post_data))
        route.fulfill(status=200, content_type="application/json", body='{"ok": true}')

    page.route("**/feedback", handle)
    page.goto("/?user=u-ben#/shipments")
    page.click("#feedback-button")
    expect(page.locator("#feedback-modal")).to_be_visible()
    page.fill("#feedback-text", "Weight column says lbs, but these are kg")
    page.click("#feedback-submit")

    expect(page.locator("#feedback-toast")).to_have_text("Thanks — feedback sent", timeout=15000)
    assert len(captured) == 1
    payload = captured[0]
    assert payload["type"] == "message"
    assert payload["channel"] == "C-FEEDBACK"
    assert payload["user"] == "u-ben"
    assert payload["text"] == "Weight column says lbs, but these are kg"
    assert payload["page_url"].endswith("/?user=u-ben#/shipments")
    assert float(payload["ts"]) > 0
    assert payload["client_ts"]
    assert payload["files"][0]["name"] == "screenshot.png"
    assert payload["files"][0]["mimetype"] == "image/png"
    assert payload["files"][0]["data_url"].startswith("data:image/png")
    expect(page.locator("#feedback-modal")).to_be_hidden()
