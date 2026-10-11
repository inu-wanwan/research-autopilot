"""Zotero Connector automation, ported from the Phase 8 PoC (zotero-connector-poc @ 58b3871, "method C").

Verified there on macOS (Zotero 9.0.6, Connector 5.0.217, Playwright Chromium): OA saves with PDF and
sync-folder arrival. Unverified: Windows (Eagle), Drive cloud arrival, subscription content, real CAPTCHA
pages. The logic is kept as validated; only configuration moved to RA settings (``config.py``).
"""
