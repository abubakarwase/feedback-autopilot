"""FB-104: the booking form labels the pieces field "Pices"."""
from playwright.sync_api import Page, expect


def test_pieces_label_spelled_right(page: Page):
    page.goto("/#/booking")
    label = page.locator("#booking-form label").filter(has=page.locator('input[name="pieces"]'))
    expect(label).to_have_text("Pieces", timeout=5000)
