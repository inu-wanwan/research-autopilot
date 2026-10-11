"""Detect "are you a robot?" interstitials (CAPTCHA, Cloudflare, etc.).

Detection only: when one is found the target is recorded as `blocked_bot_check`
and skipped. Nothing here solves, waits out, or evades the check.
"""

from __future__ import annotations

from playwright.sync_api import Page

SHORT_PAGE_CHARS = 3000

# Lower-cased substrings of page title / visible text.
TEXT_MARKERS = (
    "just a moment",
    "verify you are human",
    "verifying you are human",
    "are you a robot",
    "i'm not a robot",
    "checking your browser",
    "unusual traffic",
    "please complete the security check",
    "access denied",
    "attention required",
    "bot detection",
    "captcha",
    "ロボットではありません",
)

# Substrings of (i)frame / page URLs belonging to challenge providers.
URL_MARKERS = (
    "challenges.cloudflare.com",
    "/cdn-cgi/challenge-platform",
    "google.com/recaptcha",
    "recaptcha.net",
    "hcaptcha.com",
    "captcha-delivery.com",  # DataDome
    "perimeterx",
    "px-captcha",
    "/sorry/index",  # Google "unusual traffic"
)


def classify(title: str, text: str, urls: list[str]) -> str | None:
    """Return a short evidence string if the page looks like a bot check, else None."""
    # Challenge interstitials are short. A long page is real content even if it
    # mentions CAPTCHAs or embeds a reCAPTCHA login widget, so never flag it.
    if len(text) > SHORT_PAGE_CHARS:
        return None
    hay = f"{title}\n{text}".lower()
    for m in TEXT_MARKERS:
        if m in hay:
            return f"text:{m}"
    for u in urls:
        lu = u.lower()
        for m in URL_MARKERS:
            if m in lu:
                return f"url:{m}"
    return None


def detect(page: Page) -> str | None:
    try:
        title = page.title()
        text = page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:
        title, text = "", ""
    urls = [page.url] + [f.url for f in page.frames]
    return classify(title, text, urls)
