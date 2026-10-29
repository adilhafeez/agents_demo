"""Text inside a page's images, read with Tesseract.

Page snapshots carry only the HTML text, so a comic, a chart or a scanned
document reads as nothing. `AVAILABLE` is true only on a template whose image
installs `tesseract` (see custom_image/), and the graph offers its image tool
only then.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

TESSERACT = shutil.which("tesseract")
AVAILABLE = TESSERACT is not None

MAX_IMAGES = 6
MAX_CANDIDATES = 60
MAX_CHARS = 2000
# Icons, avatars and logos are below this; reading them only adds noise.
MIN_WIDTH, MIN_HEIGHT = 150, 80
OCR_TIMEOUT_S = 30


def _ocr(png: bytes) -> str:
    proc = subprocess.run(
        [TESSERACT, "stdin", "stdout"],
        input=png,
        capture_output=True,
        timeout=OCR_TIMEOUT_S,
        check=True,
    )
    text = proc.stdout.decode("utf-8", "replace")
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


# Resolves once the image has loaded or failed, or after 5s; a lazy image only
# starts loading once it is scrolled into view.
_WAIT_LOADED_JS = """img => img.complete || new Promise(done => {
  img.addEventListener('load', done, {once: true});
  img.addEventListener('error', done, {once: true});
  setTimeout(done, 5000);
})"""


async def read_images(page: Page) -> str:
    """OCR the largest images on the page, up to MAX_IMAGES, reported in page order."""
    images = page.locator("img")
    candidates = []
    for i in range(min(await images.count(), MAX_CANDIDATES)):
        try:
            box = await images.nth(i).bounding_box()
        except PlaywrightError:
            continue
        if box and box["width"] >= MIN_WIDTH and box["height"] >= MIN_HEIGHT:
            candidates.append((box["width"] * box["height"], i, box))
    chosen = sorted(sorted(candidates, reverse=True)[:MAX_IMAGES], key=lambda c: c[1])

    found: list[str] = []
    for _, i, box in chosen:
        image = images.nth(i)
        try:
            await image.scroll_into_view_if_needed(timeout=5000)
            await image.evaluate(_WAIT_LOADED_JS)
            png = await image.screenshot(timeout=5000)
            alt = (await image.get_attribute("alt") or "").strip()
            title = (await image.get_attribute("title") or "").strip()
        except PlaywrightError:
            continue
        try:
            text = await asyncio.to_thread(_ocr, png)
        except (subprocess.SubprocessError, OSError) as exc:
            text = f"(could not read this image: {exc})"
        lines = [f"Image {len(found) + 1} ({int(box['width'])}x{int(box['height'])})"]
        if alt:
            lines.append(f"alt: {alt[:300]}")
        if title:
            lines.append(f"title: {title[:300]}")
        lines.append(f"text: {text[:MAX_CHARS] or '(no text found)'}")
        found.append("\n".join(lines))
    if not found:
        return f"No images of at least {MIN_WIDTH}x{MIN_HEIGHT} pixels on {page.url}."
    return f"Text read from the images on {page.url}:\n\n" + "\n\n".join(found)
