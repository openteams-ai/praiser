"""Shared cached HTTP for REST-based forges (Gitea, GitLab, …).

A tiny retry-and-cache GET that prefers ``httpx`` and falls back to stdlib
``urllib``, mirroring ``github_client`` so the core runs dependency-free. Each
forge builds its own URLs and auth headers and calls :func:`fetch_text`; the
per-forge transport wrappers keep their own method names so they stay easy to
fake in tests.
"""

import re
import time
import urllib.error
import urllib.request

from ..cache import Cache

_URL_RE = re.compile(r"""https?://[^\s)"'<>\]]+""")


def extract_urls(text: str | None) -> list[str]:
    """All http(s) URLs in a blob of text (profile bio / README)."""
    return _URL_RE.findall(text or "")

try:  # optional accelerator
    import httpx  # type: ignore
except ImportError:  # pragma: no cover
    httpx = None

USER_AGENT = "praiser/0.1 (+https://github.com)"
NOT_FOUND = "__404__"


def make_session():
    """An httpx client if available, else None (urllib fallback).

    ``follow_redirects=True`` so httpx matches urllib's default and both follow
    3xx (cgit ``plain/`` and some raw endpoints redirect); otherwise the 301
    body would be returned as if it were the file.
    """
    return httpx.Client(timeout=30.0, follow_redirects=True) if httpx is not None else None


def fetch_text(
    session,
    url: str,
    *,
    headers: dict[str, str],
    cache: Cache,
    cache_key: str,
    max_retries: int = 3,
) -> str | None:
    """GET ``url`` as text, cached. None on 404/≥400/network failure.

    404 is cached (as a sentinel) so repeated misses don't re-hit the network;
    transient 5xx/connection errors are retried with linear backoff.
    """
    cached = cache.get(cache_key, default=None)
    if cached is not None:
        return None if cached == NOT_FOUND else cached
    for attempt in range(max_retries):
        try:
            if session is not None:
                resp = session.get(url, headers=headers)
                status, data = resp.status_code, resp.content
            else:
                req = urllib.request.Request(url, headers=headers, method="GET")
                try:
                    with urllib.request.urlopen(req) as r:
                        status, data = r.status, r.read()
                except urllib.error.HTTPError as e:
                    status, data = e.code, e.read()
        except Exception:
            time.sleep(1.0 * (attempt + 1))
            continue
        if status in (429, 502, 503, 504):  # throttled / transient: back off
            time.sleep(1.5 * (attempt + 1))
            continue
        if status == 404:
            cache.set(cache_key, NOT_FOUND)
            return None
        if status >= 400:
            return None
        text = data.decode("utf-8", errors="replace")
        cache.set(cache_key, text)
        return text
    return None
