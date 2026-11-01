"""FB-101: 'Download PDF' on the invoice page does nothing."""
from playwright.sync_api import Page


def test_download_pdf_downloads_invoice(page: Page):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto("/#/invoices/INV-2291")

    with page.expect_download(timeout=5000) as dl:
        page.click("#download-pdf")

    assert dl.value.suggested_filename == "INV-2291.pdf"
    assert errors == []
