from playwright.sync_api import Page, expect


def test_booking_submit_shows_confirmation(page: Page):
    page.goto("/#/booking")
    form = page.locator("#booking-form")
    form.locator('input[name="origin"]').fill("fra")
    form.locator('input[name="destination"]').fill("ord")
    form.locator('input[name="pieces"]').fill("3")
    form.locator('input[name="weight"]').fill("120.5")
    form.locator('button[type="submit"]').click()

    conf = page.locator("#booking-confirmation")
    expect(conf).to_be_visible()
    expect(conf).to_contain_text("BKG-2026-")
    expect(conf).to_contain_text("FRA → ORD")
