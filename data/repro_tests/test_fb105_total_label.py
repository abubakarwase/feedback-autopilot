"""FB-105: the invoice total row is labelled "Totla"."""
from playwright.sync_api import Page, expect


def test_invoice_total_label_spelled_right(page: Page):
    page.goto("/#/invoices/INV-2291")
    expect(page.locator("#invoice-lines .total-row td").first).to_have_text("Total", timeout=5000)
