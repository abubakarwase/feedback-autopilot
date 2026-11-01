Hand-written fallback repro tests for FB-101 (Download PDF), FB-102 (weight unit), FB-104 (Pices label) and FB-105 (Totla label), used if the LLM-written test is behind.
Copy one into `target-app/tests/` (it uses that folder's `conftest.py` for `base_url`); it fails on the current app.
It passes once `data/patches/<signal>.diff` is applied: `git apply data/patches/FB-101.diff`.
