Hand-written fallback repro tests for FB-101 (Download PDF) and FB-102 (weight unit), used if the LLM-written test is behind at 16:40.
Copy one into `target-app/tests/` (it uses that folder's `conftest.py` for `base_url`); it fails on the current app.
It passes once `data/patches/<signal>.diff` is applied: `git apply data/patches/FB-101.diff`.
