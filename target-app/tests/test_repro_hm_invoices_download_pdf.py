from playwright.sync_api import Page


def test_download_invoice_pdf(page: Page):
    page.goto("/#/invoices/INV-2291")
    with page.expect_download() as download_info:
        page.locator("#download-pdf").click()
    download = download_info.value
    assert download.suggested_filename == "INV-2291.pdf"
