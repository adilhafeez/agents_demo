"""Headless Chromium for the scraper graph.

A template that bakes Chromium for the pinned Playwright is used as is. On one
that does not, Chromium is installed into the guest the first time this module
is imported. The check and install run on a background
thread: blocking the import would hold the Agent Server's /ok past the runtime's
startup window, and the session would fail before the first turn.

`langgraph dev` rejects blocking calls on the event loop, so every filesystem
or subprocess call made from async code goes through asyncio.to_thread.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import subprocess
import sys
import threading
from dataclasses import dataclass

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

INSTALL_LOG = "/tmp/playwright-install.log"
INSTALL_LOCK = "/tmp/playwright-install.lock"
INSTALL_DONE = "/tmp/playwright-install.ok"
INSTALL_TIMEOUT_S = int(os.environ.get("BROWSER_INSTALL_TIMEOUT_SECONDS", "480"))

PAGE_TEXT_CHARS = int(os.environ.get("PAGE_TEXT_CHARS", "6000"))
MAX_ELEMENTS = int(os.environ.get("PAGE_MAX_ELEMENTS", "80"))
NAV_TIMEOUT_MS = 30_000

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_installed = threading.Event()
_install_error: str | None = None


def _playwright_cli_env() -> dict[str, str]:
    """Environment for a `python -m playwright` child process."""
    return {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}


def _browser_present() -> bool:
    """Whether every download `install --only-shell chromium` would make is already on disk.

    A template that bakes the browser has its system libraries too, so finding
    it skips the install entirely, including the apt-get run --with-deps does.
    """
    out = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "--dry-run", "--only-shell", "chromium"],
        capture_output=True,
        text=True,
        env=_playwright_cli_env(),
        timeout=60,
    ).stdout
    locations = [
        line.split(":", 1)[1].strip()
        for line in out.splitlines()
        if line.strip().startswith("Install location:")
    ]
    return bool(locations) and all(os.path.isdir(path) for path in locations)


def _install() -> None:
    """Install headless Chromium and its system libraries, once per guest.

    The lock and marker live in /tmp because `langgraph dev` may import this
    module more than once (reloader, worker), and two concurrent apt runs fail.
    """
    global _install_error
    try:
        with open(INSTALL_LOCK, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if os.path.exists(INSTALL_DONE):
                return
            if _browser_present():
                open(INSTALL_DONE, "w").close()
                return
            with open(INSTALL_LOG, "a") as log:
                proc = subprocess.run(
                    [sys.executable, "-m", "playwright", "install", "--with-deps", "--only-shell", "chromium"],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=_playwright_cli_env(),
                    timeout=INSTALL_TIMEOUT_S,
                )
            if proc.returncode != 0:
                _install_error = f"playwright install exited {proc.returncode}; see {INSTALL_LOG} in the sandbox"
                return
            open(INSTALL_DONE, "w").close()
    except Exception as exc:  # noqa: BLE001 - surfaced to the model as a tool error
        _install_error = f"browser install failed: {exc}; see {INSTALL_LOG} in the sandbox"
    finally:
        _installed.set()


threading.Thread(target=_install, name="playwright-install", daemon=True).start()


class BrowserUnavailable(RuntimeError):
    pass


# Marks every visible interactive element with data-agent-ref="N" so the model
# can act on "element 12" instead of writing CSS selectors. Refs are reassigned
# on every snapshot, so a ref is only valid against the latest snapshot.
_SNAPSHOT_JS = """
(maxElements) => {
  document.querySelectorAll('[data-agent-ref]').forEach(e => e.removeAttribute('data-agent-ref'));
  const selector = [
    'a[href]', 'button', 'input:not([type=hidden])', 'textarea', 'select', 'summary',
    '[role=button]', '[role=link]', '[role=tab]', '[role=menuitem]', '[role=checkbox]',
  ].join(',');
  const elements = [];
  let n = 0;
  for (const el of document.querySelectorAll(selector)) {
    const box = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    if (box.width === 0 || box.height === 0 || style.visibility === 'hidden' || style.display === 'none') continue;
    n += 1;
    el.setAttribute('data-agent-ref', String(n));
    const tag = el.tagName.toLowerCase();
    const type = el.getAttribute('type');
    const value = el.type === 'password' ? '' : (el.value || '');
    const label = (el.innerText || value || el.getAttribute('aria-label') || el.getAttribute('placeholder')
      || el.getAttribute('title') || el.getAttribute('name') || '').trim().replace(/\\s+/g, ' ').slice(0, 80);
    elements.push({ref: n, tag: type ? `${tag}[${type}]` : tag, label, href: tag === 'a' ? el.getAttribute('href') : null});
    if (n >= maxElements) break;
  }
  const text = (document.body ? document.body.innerText : '').replace(/\\n{3,}/g, '\\n\\n').trim();
  // Where the viewport sits within that text: the first visible text node's
  // opening words, located in innerText. Without this every snapshot starts at
  // the top of the page, so scrolling never shows the model anything new.
  // Fixed and sticky elements (headers, sidebar contents) are visible at every
  // scroll position, so they cannot anchor it; and a phrase can occur more than
  // once (a heading and its contents entry), so take the occurrence nearest the
  // scroll-proportional estimate.
  const doc = document.scrollingElement || document.documentElement;
  const pinned = (el) => {
    for (; el && el !== document.body; el = el.parentElement) {
      const pos = getComputedStyle(el).position;
      if (pos === 'fixed' || pos === 'sticky') return true;
    }
    return false;
  };
  let offset = 0;
  if (window.scrollY > 0 && document.body) {
    const estimate = text.length * window.scrollY / Math.max(1, doc.scrollHeight);
    const range = document.createRange();
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      const words = node.textContent.trim().split(/\\s+/);
      if (words.join(' ').length < 20 || !node.parentElement) continue;
      range.selectNodeContents(node);
      const box = range.getBoundingClientRect();
      if (box.height === 0 || box.bottom <= 0 || box.top >= window.innerHeight) continue;
      if (pinned(node.parentElement)) continue;
      const phrase = words.slice(0, 6).join(' ');
      let best = -1;
      for (let at = text.indexOf(phrase); at >= 0; at = text.indexOf(phrase, at + 1)) {
        if (best < 0 || Math.abs(at - estimate) < Math.abs(best - estimate)) best = at;
      }
      if (best >= 0) offset = best;
      break;
    }
  }
  const scrollPct = doc.scrollHeight > window.innerHeight
    ? Math.min(100, Math.round(100 * (window.scrollY + window.innerHeight) / doc.scrollHeight))
    : 100;
  return {title: document.title, text, offset, scrollPct, elements};
}
"""


@dataclass
class _Tab:
    context: BrowserContext

    @property
    def page(self) -> Page:
        # A link with target=_blank opens a new page in the context; follow it.
        return self.context.pages[-1]


class Browsers:
    """One shared Chromium per process, one browser context per LangGraph thread.

    A session maps to a single LangGraph thread, so a follow-up turn
    ("now open the pricing page") lands on the page the previous turn left.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._tabs: dict[str, _Tab] = {}

    async def page(self, thread_id: str) -> Page:
        async with self._lock:
            browser = await self._ensure_browser()
            tab = self._tabs.get(thread_id)
            if tab is None or not tab.context.pages:
                context = await browser.new_context(
                    user_agent=USER_AGENT, viewport={"width": 1280, "height": 900}
                )
                context.set_default_timeout(10_000)
                context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
                await context.new_page()
                tab = _Tab(context)
                self._tabs[thread_id] = tab
            return tab.page

    async def _ensure_browser(self) -> Browser:
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        ready = await asyncio.to_thread(_installed.wait, INSTALL_TIMEOUT_S)
        if not ready:
            raise BrowserUnavailable(
                f"the browser is still installing after {INSTALL_TIMEOUT_S}s; try again shortly"
            )
        if _install_error:
            raise BrowserUnavailable(_install_error)
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        # A crashed Chromium invalidates every context it held.
        self._tabs.clear()
        self._browser = await self._playwright.chromium.launch(
            headless=True, args=["--disable-dev-shm-usage"]
        )
        return self._browser


async def settle(page: Page) -> None:
    """Give client-side rendering a moment after navigation, without hanging on chatty pages."""
    try:
        await page.wait_for_load_state("networkidle", timeout=5_000)
    except Exception:  # noqa: BLE001 - analytics beacons keep many pages from ever idling
        pass


async def snapshot(page: Page) -> str:
    """Render the page as text the model can read and act on."""
    data = await page.evaluate(_SNAPSHOT_JS, MAX_ELEMENTS)
    lines = [f"URL: {page.url}", f"Title: {data['title']}", ""]
    lines.append("Interactive elements (pass the number to click or type_text):")
    if not data["elements"]:
        lines.append("  (none visible)")
    for el in data["elements"]:
        entry = f"  [{el['ref']}] {el['tag']} \"{el['label']}\""
        if el["href"]:
            entry += f" -> {el['href']}"
        lines.append(entry)
    text = data["text"]
    offset = data["offset"]
    shown = text[offset : offset + PAGE_TEXT_CHARS]
    end = offset + len(shown)
    lines.append("")
    if offset > 0:
        more = "; scroll down for more" if end < len(text) else "; this is the end of the page"
        lines.append(
            f"Page text from the current scroll position (chars {offset}-{end} of {len(text)}, "
            f"{data['scrollPct']}% of the page scrolled{more}):"
        )
    elif end < len(text):
        lines.append(f"Page text (first {len(shown)} of {len(text)} chars; scroll down for more):")
    else:
        lines.append("Page text:")
    lines.append(shown)
    return "\n".join(lines)


def ref_locator(page: Page, ref: int):
    return page.locator(f'[data-agent-ref="{ref}"]')
