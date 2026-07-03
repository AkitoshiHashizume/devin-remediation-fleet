"""Shared HTTP helper: bounded retries with exponential backoff.

Retries are opt-in per call. GETs pass retry=True freely; a non-idempotent POST
should only retry when the endpoint itself is idempotent (e.g. Devin session
creation, which dedupes server-side) — otherwise a post-success timeout could
duplicate the side effect.
"""
import time

import httpx

RETRYABLE_STATUS = (429, 500, 502, 503, 504)


def request_with_retry(method: str, url: str, *, headers: dict, retry: bool,
                       timeout: int = 30, attempts: int = 4,
                       **kwargs) -> httpx.Response:
    for attempt in range(attempts):
        resp = httpx.request(method, url, headers=headers, timeout=timeout, **kwargs)
        if retry and resp.status_code in RETRYABLE_STATUS and attempt < attempts - 1:
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
        return resp
    raise RuntimeError("unreachable")  # loop always returns or raises
