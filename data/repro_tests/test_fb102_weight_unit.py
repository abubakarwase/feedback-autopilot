"""FB-102: the weight column header says lbs, but the values are kg."""
from playwright.sync_api import Page, expect


def test_weight_header_says_kg(page: Page):
    page.goto("/#/shipments")
    expect(page.locator("#weight-header")).to_have_text("Weight (kg)", timeout=5000)
