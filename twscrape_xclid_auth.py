"""Vendored fix for twscrape XClIdGen breakage (X scout dark since 2026-07-20).

PROBLEM
  twscrape's XClIdGen.create() bootstraps the x-client-transaction-id header by
  fetching https://x.com/tesla with a COOKIE-LESS client. Since ~2026-07-20 X
  serves logged-out clients a stripped web build: 1 script chunk instead of ~685,
  and none of them reference the ondemand.s-*.js indices file. Result:
  "Couldn't get XClientTxId indices script" -> every request dies.
  Upstream: https://github.com/vladkens/twscrape/issues/320 (open, unfixed)

MEASURED (2026-07-27, probe on lumen)
  anonymous     -> 1 script,   0 indices hits, FAIL
  cookies-only  -> 685 scripts, ondemand.s.b4e1519a.js, 4 indices, OK
  acc.make_client() -> HTTP 401 (sends GraphQL Authorization: Bearer, which the
                       HTML endpoint rejects) -- do NOT use it for this fetch.

FIX
  Bootstrap XClIdGen with a browser-like client: the account's session cookies,
  a chrome UA, and NO GraphQL auth headers. Everything else is upstream code.

NOTE: INDICES_FILE_RE is NOT broken and is deliberately left alone.

Pinned to twscrape 0.19.1. Structural check below fails loud if upstream moves.
"""

from __future__ import annotations

import asyncio
import os

import bs4

from twscrape import xclid
from twscrape import queue_client as _qc
from twscrape.accounts_pool import AccountsPool
from twscrape.http import make_client as _make_http_client
from twscrape.logger import logger
from twscrape.utils import parse_proxy

PINNED_VERSION = "0.19.1"
BOOTSTRAP_URL = "https://x.com/tesla"

_installed = False
_db_file: str | None = None


class XClIdAuthError(RuntimeError):
    """Raised when the authenticated bootstrap cannot be performed."""


def _check_structure() -> None:
    """Fail loud if upstream renamed what we depend on, rather than silently
    falling back to the broken anonymous path."""
    missing = [
        n
        for n in ("get_tw_page_text", "load_keys", "XClIdGen")
        if not hasattr(xclid, n)
    ]
    if missing:
        raise XClIdAuthError(
            f"twscrape.xclid missing {missing} - vendored patch "
            f"{__file__} needs review against the installed version"
        )
    if not hasattr(_qc, "XClIdGenStore"):
        raise XClIdAuthError("twscrape.queue_client.XClIdGenStore not found")


def _browser_client(acc):
    """Session cookies + chrome UA, no GraphQL auth headers (those 401 here)."""
    proxies = [os.getenv("TWS_PROXY"), acc.proxy]
    proxies = [x for x in proxies if x]
    return _make_http_client(
        proxy=parse_proxy(proxies[0]) if proxies else None,
        headers={"user-agent": "@chrome"},
        cookies=acc.cookies,
    )


async def _create_authed(username: str):
    if not _db_file:
        raise XClIdAuthError("twscrape_xclid_auth.install(db_file) not called")

    acc = await AccountsPool(_db_file).get_account(username)
    if acc is None:
        raise XClIdAuthError(f"account {username!r} not in {_db_file}")
    if not acc.cookies.get("auth_token"):
        raise XClIdAuthError(
            f"account {username!r} has no auth_token cookie - re-add it with "
            f"`twscrape --db {_db_file} add_cookie {username}`"
        )

    clt = _browser_client(acc)
    try:
        text = await xclid.get_tw_page_text(BOOTSTRAP_URL, clt)
        soup = bs4.BeautifulSoup(text, "html.parser")
        vk_bytes, anim_key = await xclid.load_keys(soup, clt)
        return xclid.XClIdGen(vk_bytes, anim_key)
    finally:
        await clt.aclose()


async def _patched_get(cls, username: str, fresh: bool = False):
    """Drop-in for XClIdGenStore.get - same cache/retry contract as upstream,
    but bootstraps through the account's own session."""
    if username in cls.items and not fresh:
        return cls.items[username]

    last = None
    for attempt in (1, 2, 3):
        try:
            gen = await _create_authed(username)
            cls.items[username] = gen
            return gen
        except Exception as e:
            last = e
            logger.warning(
                f"XClIdGen(authed) attempt {attempt}/3 for {username} failed: "
                f"{type(e).__name__}: {e}"
            )
            await asyncio.sleep(1)

    raise _qc.AbortReqError(
        f"Failed to create XClIdGen for {username} via authenticated bootstrap "
        f"({type(last).__name__}: {last}). This is an X web-build / header "
        f"failure, NOT necessarily an expired cookie. "
        f"See https://github.com/vladkens/twscrape/issues/320"
    )


def install(db_file: str) -> None:
    """Patch XClIdGenStore.get to bootstrap via an authenticated session."""
    global _installed, _db_file
    _db_file = db_file
    if _installed:
        return

    _check_structure()

    try:
        from importlib.metadata import version

        found = version("twscrape")
        if found != PINNED_VERSION:
            logger.warning(
                f"twscrape {found} installed, vendored xclid patch pinned to "
                f"{PINNED_VERSION} - re-verify {__file__} "
                f"(upstream issue #320 may be fixed; if so, drop this patch)"
            )
    except Exception:
        pass

    _qc.XClIdGenStore.get = classmethod(_patched_get)
    _qc.XClIdGenStore.items.clear()
    _installed = True
    logger.info("twscrape xclid: authenticated bootstrap patch installed")
