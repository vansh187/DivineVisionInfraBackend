"""Response headers for PDFs that carry personal data (letters, receipts)."""

# Never cached by browsers / proxies; never content-sniffed.
PRIVATE_PDF_HEADERS = {"Cache-Control": "no-store, private", "Pragma": "no-cache",
                       "X-Content-Type-Options": "nosniff"}
