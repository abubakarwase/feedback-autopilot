from playwright.sync_api import Page, expect


def test_shipments_renders_and_nav_works(page: Page):
    page.goto("/")
    expect(page.locator("#shipments-table tbody tr")).to_have_count(8)
    expect(page.locator("#user-name")).to_have_text("Anna (Ops)")

    page.click('a[data-route="booking"]')
    expect(page.locator("#booking-form")).to_be_visible()

    page.click('a[data-route="invoice"]')
    expect(page.locator("#invoice-lines")).to_be_visible()
    expect(page.locator("#download-pdf")).to_be_visible()

    page.click('a[data-route="shipments"]')
    expect(page.locator("#shipments-table tbody tr")).to_have_count(8)
