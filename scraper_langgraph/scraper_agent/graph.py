"""Web summarizer graph for DigitalOcean hosted agents (langgraph).

    START -> browse <-> tools
               |
               v
           summarize -> END

`browse` drives a headless browser with tools until it has what the user asked
for; `summarize` writes the answer from what was actually on the pages. Only
`summarize` produces answer text on the session's event stream; see
`_without_text` for what that takes on the `browse` side.

Tool signatures must keep real (not stringified) annotations: LangChain finds
the injected `config: RunnableConfig` parameter by its type, so this module
must not use `from __future__ import annotations`.
"""

import asyncio
import functools
import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt

from scraper_agent import ocr
from scraper_agent.browser import (
    Browsers,
    BrowserUnavailable,
    PlaywrightError,
    ref_locator,
    settle,
    snapshot,
)

MAX_STEPS = int(os.environ.get("MAX_BROWSE_STEPS", "15"))
ARTIFACT_DIR = os.environ.get("ARTIFACT_DIR", "/workspace/artifacts")
APPROVE_SUBMITS = os.environ.get("APPROVE_SUBMITS", "true").lower() not in ("0", "false", "no")

browsers = Browsers()


def _thread_id(config: RunnableConfig) -> str:
    return str((config.get("configurable") or {}).get("thread_id") or "default")


_tab_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def _one_at_a_time(fn):
    """Run a thread's browser tools in order.

    The model may call several tools in one step and ToolNode runs them
    concurrently, but they share one tab: two parallel open_url calls would
    both return whichever page loaded last.
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        async with _tab_locks[_thread_id(kwargs.get("config") or {})]:
            return await fn(*args, **kwargs)

    return wrapper


# --- tools -----------------------------------------------------------------


@tool
@_one_at_a_time
async def open_url(url: str, config: RunnableConfig) -> str:
    """Navigate to a URL and return the page: its interactive elements and visible text."""
    if not re.match(r"^https?://", url):
        url = "https://" + url
    page = await browsers.page(_thread_id(config))
    try:
        response = await page.goto(url, wait_until="domcontentloaded")
    except PlaywrightError as exc:
        # A navigation still in flight (a client-side redirect, or the error
        # page of a failed load) aborts a new one; once it settles, one retry
        # starts clean.
        if "ERR_ABORTED" not in str(exc) and "interrupted by another navigation" not in str(exc):
            raise
        await settle(page)
        response = await page.goto(url, wait_until="domcontentloaded")
    await settle(page)
    status = f"HTTP {response.status}\n" if response is not None else ""
    return status + await snapshot(page)


@tool
@_one_at_a_time
async def read_page(config: RunnableConfig) -> str:
    """Re-read the current page. Use after content changed without a navigation."""
    page = await browsers.page(_thread_id(config))
    return await snapshot(page)


@tool
@_one_at_a_time
async def click(ref: int, config: RunnableConfig) -> str:
    """Click the element numbered `ref` in the latest page snapshot, then return the resulting page."""
    page = await browsers.page(_thread_id(config))
    target = ref_locator(page, ref)
    if await target.count() == 0:
        return f"No element [{ref}] on the current page. Call read_page for fresh numbers."
    await target.first.click()
    page = await browsers.page(_thread_id(config))
    await settle(page)
    return await snapshot(page)


@tool
@_one_at_a_time
async def type_text(ref: int, text: str, config: RunnableConfig, submit: bool = False) -> str:
    """Type into the input numbered `ref`. Set submit=true to press Enter afterwards (search boxes, forms)."""
    page = await browsers.page(_thread_id(config))
    target = ref_locator(page, ref)
    if await target.count() == 0:
        return f"No element [{ref}] on the current page. Call read_page for fresh numbers."
    if submit and APPROVE_SUBMITS:
        # Pauses the run and surfaces an approval request on the session. On resume LangGraph
        # re-runs this tool from the top, so everything above must be safe to repeat.
        decision = interrupt(
            {"action": "submit", "url": page.url, "element": ref, "text": text,
             "question": f"Submit {text!r} on {page.url}?"}
        )
        if decision is not True:
            reason = f" The user said: {decision}" if isinstance(decision, str) and decision else ""
            return f"The user declined this submission. Do not retry it.{reason}"
    await target.first.fill(text)
    if submit:
        await target.first.press("Enter")
        await settle(page)
        page = await browsers.page(_thread_id(config))
    return await snapshot(page)


@tool
@_one_at_a_time
async def scroll(config: RunnableConfig, direction: Literal["down", "up"] = "down") -> str:
    """Scroll one screen down or up, for pages that load more content as you scroll."""
    page = await browsers.page(_thread_id(config))
    # Two screens per step: a snapshot already shows several screens of text,
    # so one screen mostly re-reads what the model has seen.
    dy = await page.evaluate("() => window.innerHeight * 2")
    before = await page.evaluate("() => window.scrollY")
    await page.mouse.wheel(0, dy if direction == "down" else -dy)
    await asyncio.sleep(0.8)
    after = await page.evaluate("() => window.scrollY")
    if after == before:
        # Some pages swallow wheel events; scrolling the window directly still works.
        await page.evaluate("(dy) => window.scrollBy(0, dy)", dy if direction == "down" else -dy)
        await asyncio.sleep(0.3)
        after = await page.evaluate("() => window.scrollY")
    text = await snapshot(page)
    if after == before:
        return (
            "The page did not move: it is already at the "
            f"{'bottom' if direction == 'down' else 'top'}, or its content scrolls inside a frame. "
            "Do not scroll again; use what you have or open another page.\n\n" + text
        )
    return text


@tool
@_one_at_a_time
async def go_back(config: RunnableConfig) -> str:
    """Go back to the previous page."""
    page = await browsers.page(_thread_id(config))
    await page.go_back(wait_until="domcontentloaded")
    await settle(page)
    return await snapshot(page)


@tool
@_one_at_a_time
async def screenshot(name: str, config: RunnableConfig) -> str:
    """Save a PNG of the current viewport to the sandbox workspace. Returns the file path."""
    page = await browsers.page(_thread_id(config))
    png = await page.screenshot()
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._") or "page"
    path = os.path.join(ARTIFACT_DIR, f"{safe}.png")
    await asyncio.to_thread(_write_bytes, path, png)
    return f"Saved {path}"


@tool
@_one_at_a_time
async def read_images(config: RunnableConfig) -> str:
    """Read the text drawn inside the current page's images: comics, charts, infographics, scans.

    Page snapshots contain only HTML text, so use this when what you need is in a picture.
    """
    page = await browsers.page(_thread_id(config))
    return await ocr.read_images(page)


TOOLS = [open_url, read_page, click, type_text, scroll, go_back, screenshot]
if ocr.AVAILABLE:
    TOOLS.append(read_images)


def _write_bytes(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


PROGRESS_LOG = os.path.join(ARTIFACT_DIR, "progress.log")


def _append_line(path: str, line: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(line.replace("\n", " ") + "\n")


async def _progress(line: str) -> None:
    """One line per step to artifacts/progress.log, which run.sh polls to show the run live."""
    await asyncio.to_thread(_append_line, PROGRESS_LOG, line)


def _result_line(message: ToolMessage) -> str:
    lines = _text(message).splitlines()
    if not lines:
        return "(empty result)"
    title = next((line for line in lines if line.startswith("Title:")), "")
    return f"{lines[0][:100]}{', ' + title[:100] if title and title != lines[0] else ''}"


# --- model -----------------------------------------------------------------

_model = None
_model_lock = asyncio.Lock()


def _build_model():
    from langchain_openai import ChatOpenAI

    # A key and its endpoint travel together. The platform sets HARNESS_INFERENCE_BASE_URL
    # (DigitalOcean inference) in every session, so falling back to it for an
    # OpenAI key sends that key to the wrong provider and gets a 401.
    if os.environ.get("OPENAI_API_KEY"):
        api_key = os.environ["OPENAI_API_KEY"]
        base_url = os.environ.get("OPENAI_BASE_URL")
        model_name = os.environ.get("MODEL")
    elif os.environ.get("HARNESS_INFERENCE_API_KEY"):
        api_key = os.environ["HARNESS_INFERENCE_API_KEY"]
        base_url = os.environ.get("HARNESS_INFERENCE_BASE_URL")
        model_name = os.environ.get("HARNESS_INFERENCE_MODEL") or os.environ.get("MODEL")
    else:
        raise RuntimeError(
            "no model credentials: set OPENAI_API_KEY or HARNESS_INFERENCE_API_KEY in the manifest"
        )
    return ChatOpenAI(
        model=model_name or "gpt-5.5",
        api_key=api_key,
        base_url=base_url or None,
        # Without this, ChatOpenAI on a custom base_url reports no token usage
        # and the platform bills the turn as zero tokens.
        stream_usage=True,
    )


async def model():
    # Built off the event loop: constructing the OpenAI client loads CA bundles
    # from disk, which `langgraph dev` flags as a blocking call.
    global _model
    async with _model_lock:
        if _model is None:
            _model = await asyncio.to_thread(_build_model)
    return _model


# --- graph -----------------------------------------------------------------

BROWSE_PROMPT = """You are a web research agent with a real headless browser.

Your job in this phase is to gather evidence, not to write the answer. Use the
tools to open pages, follow links, search, scroll and read until you have what
the user asked for. Then reply with one short line saying you are done; a
separate step writes the summary from the pages you visited.

Rules:
- Element numbers come from the most recent snapshot only. After any click,
  navigation or scroll, use the numbers from the new snapshot.
- Prefer the site's own navigation over guessing URLs.
- Stop as soon as you have enough. You have {remaining} of {max_steps} browser
  steps left this turn.
- If a page blocks you (login wall, captcha, 403), say so and move on; do not
  try to get around it.
- Never enter credentials or personal data.
"""

SUMMARY_PROMPT = """You write the final answer for a web research agent.

Using only what appears in the tool results above (page snapshots), answer the
user's request. Do not add facts that were not on the pages. If something the
user asked for could not be found or a page could not be loaded, say so.

Format, in Markdown:

# <short title>

**TL;DR:** two sentences at most.

## Key points
- 3 to 7 bullets, most important first.

## Details
Short paragraphs or a table, only if the request needs them.

## Sources
- every URL you drew on.
"""

SUMMARY_NOW = """Browsing is over; do not ask for or describe any more browser steps.
Write the final answer to my original request now, in the Markdown format from
the system message, using only what appeared in the page snapshots above. If
something I asked for was not on the pages you reached, say so in the answer."""


def _turn_start(messages: list[BaseMessage]) -> int:
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            return i
    return 0


def _steps_this_turn(messages: list[BaseMessage]) -> int:
    return sum(
        1 for m in messages[_turn_start(messages):] if isinstance(m, AIMessage) and m.tool_calls
    )


_ELEMENTS_RE = re.compile(r"^Interactive elements .*?\n\n", re.DOTALL | re.MULTILINE)


def _without_elements(message: ToolMessage) -> ToolMessage:
    text = _ELEMENTS_RE.sub("", _text(message), count=1)
    return ToolMessage(content=text, tool_call_id=message.tool_call_id)


def _compact(messages: list[BaseMessage], keep_elements: bool = True) -> list[BaseMessage]:
    """Shrink page snapshots, which are the bulk of the context.

    Snapshots from earlier turns are elided. In this turn only the newest
    snapshot keeps its element list, because element numbers from any older one
    are stale; with keep_elements=False none keeps it.
    """
    start = _turn_start(messages)
    last_tool = max((i for i, m in enumerate(messages) if isinstance(m, ToolMessage)), default=-1)
    out: list[BaseMessage] = []
    for i, m in enumerate(messages):
        if isinstance(m, ToolMessage):
            if i < start:
                m = ToolMessage(content="[page snapshot from an earlier turn, elided]", tool_call_id=m.tool_call_id)
            elif not keep_elements or i != last_tool:
                m = _without_elements(m)
        out.append(m)
    return out


async def browse(state: MessagesState, config: RunnableConfig) -> dict:
    messages = state["messages"]
    steps = _steps_this_turn(messages)
    if isinstance(messages[-1], HumanMessage):
        await _progress(f"turn: {_text(messages[-1])[:120]}")
    results = []
    for m in reversed(messages):
        if not isinstance(m, ToolMessage):
            break
        results.append(m)
    for m in reversed(results):
        await _progress(f"  <- {_result_line(m)}")
    if steps >= MAX_STEPS:
        await _progress(f"step limit ({MAX_STEPS}) reached; summarizing")
        return {}
    llm = (await model()).bind_tools(TOOLS).with_config(tags=["nostream"])
    prompt = SystemMessage(BROWSE_PROMPT.format(remaining=MAX_STEPS - steps, max_steps=MAX_STEPS))
    started = time.monotonic()
    response = await llm.ainvoke([prompt, *_compact(messages)], config)
    took = time.monotonic() - started
    if not response.tool_calls:
        await _progress(f"model {took:.1f}s: done browsing after {steps} steps")
        return {}
    for call in response.tool_calls:
        args = json.dumps(call.get("args") or {}, ensure_ascii=False)[:120]
        await _progress(f"step {steps + 1}/{MAX_STEPS} (model {took:.1f}s): {call['name']} {args}")
    return {"messages": [_without_text(response)]}


def _without_text(message: AIMessage) -> AIMessage:
    """Drop narration from a tool-calling message.

    `nostream` only stops token streaming. LangGraph's messages stream still
    emits every message a node returns, and the runtime turns any assistant text in it
    into answer tokens, so browse-phase prose would be spliced into the answer.
    """
    if isinstance(message.content, str):
        content = ""
    else:
        content = [
            p for p in message.content if not (isinstance(p, str) or p.get("type") == "text")
        ]
    return message.model_copy(update={"content": content})


def after_browse(state: MessagesState) -> Literal["tools", "summarize"]:
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return "summarize"


async def summarize(state: MessagesState, config: RunnableConfig) -> dict:
    llm = await model()
    # The instruction has to come last. After a long browse the system prompt is
    # hundreds of thousands of tokens back, and a conversation that ends on a page
    # snapshot gets continued as more browsing narration instead of an answer.
    finish = HumanMessage(SUMMARY_NOW)
    await _progress("writing the summary")
    started = time.monotonic()
    response = await llm.ainvoke(
        [SystemMessage(SUMMARY_PROMPT), *_compact(state["messages"], keep_elements=False), finish], config
    )
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    await asyncio.to_thread(
        _write_bytes,
        os.path.join(ARTIFACT_DIR, "summary.md"),
        f"<!-- generated {stamp} -->\n{_text(response)}\n".encode(),
    )
    await _progress(f"summary written (model {time.monotonic() - started:.1f}s)")
    return {"messages": [response]}


def _text(message: BaseMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    return "".join(
        part if isinstance(part, str) else part.get("text", "") for part in message.content
    )


def _tool_error(error: PlaywrightError | BrowserUnavailable) -> str:
    """A failed browser action as the tool's result, so the model can try another way.

    ToolNode handles only the exception types in this annotation; anything else
    still fails the run.
    """
    first_line = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
    return (
        f"The browser action failed: {first_line}\n"
        "Try another link or page, or answer from what you already have."
    )


builder = StateGraph(MessagesState)
builder.add_node("browse", browse)
builder.add_node("tools", ToolNode(TOOLS, handle_tool_errors=_tool_error))
builder.add_node("summarize", summarize)
builder.add_edge(START, "browse")
builder.add_conditional_edges("browse", after_browse)
builder.add_edge("tools", "browse")
builder.add_edge("summarize", END)

graph = builder.compile()
