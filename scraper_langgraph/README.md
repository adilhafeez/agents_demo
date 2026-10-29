# Web summarizer with LangGraph

A LangGraph agent, running as a hosted agent on DigitalOcean, opens a website in
a real headless Chromium, reads the pages and writes a Markdown summary. The
graph is the code in this directory: the platform clones it at a pinned commit
when the session starts. Chromium and Playwright come with the stock
`langgraph` template, so there is nothing to build first.
[`custom_image/`](custom_image/README.md) runs the same graph on your own
container image, which adds OCR so the agent can read text inside images.

```bash
export MODEL_ACCESS_KEY=...   # DigitalOcean model access key
./run.sh                                     # sends prompt1.txt
./run.sh "Summarize https://example.com"
```

## Files

| File | What it is |
|---|---|
| `scraper.yaml` | The agent manifest. |
| `langgraph.json`, `scraper_agent/` | The LangGraph graph and its browser tools. |
| `requirements.txt` | The graph's Python dependencies, installed at session start. |
| `prompt1.txt` | The default prompt. |
| `run.sh` | Create a session, run one prompt, save the summary, remove the session. |
| `custom_image/` | The same demo on a team template built from your own image. |

## Requirements

- `doctl` with the `harness-runtime` commands, authenticated to your team.
- A DigitalOcean model access key in `MODEL_ACCESS_KEY` (control panel,
  Inference). doctl expands it from your environment when it reads the
  manifest, so the key is never written to disk here.

## How it works

- **Browser.** The `langgraph` template ships headless Chromium under
  `/ms-playwright` and the Playwright Python package (1.63.0), so the first
  browser action opens a page right away. `playwright` in `requirements.txt`
  must equal that version: each Playwright version looks for its own browser
  build, and a mismatch downloads a second browser. On a template without
  Chromium, `scraper_agent/browser.py` installs it in the background as the
  session starts, which adds about half a minute to the first browser action.
  It checks with `playwright install --dry-run` first, so it never runs
  `apt-get` when the browser is already there.
- **Graph.** `browse` calls the browser tools (`open_url`, `click`,
  `type_text`, `scroll`, `go_back`, `read_page`, `screenshot`) until it has
  what you asked for, up to `MAX_BROWSE_STEPS` per turn. `summarize` then
  writes the answer from the page snapshots only and saves it to
  `artifacts/summary.md`.
- **Pages as text.** Each snapshot lists the visible interactive elements by
  number and the page text from the current scroll position, so the model acts
  on "element 12" instead of writing CSS selectors. The list is capped at 80
  (`PAGE_MAX_ELEMENTS`), so content comes before navigation, headers and
  footers, and a short link such as "Read more" is labeled with its card's
  heading. Only the newest snapshot
  keeps its element list, since older numbers are stale, and `summarize` sees
  page text alone; that keeps each model call small.
- **Approvals.** With `APPROVE_SUBMITS: "true"`, any search or form submit
  pauses the run with an approval request. `run.sh` answers it with `ON_HITL`.
- **Failures.** A failed browser action (a timeout, an aborted navigation)
  comes back to the model as a tool result, so it tries another page instead
  of ending the run.
- **Progress.** Every browser step, its result and the summary step are
  appended to `artifacts/progress.log`, which `run.sh` polls.
- **Follow-ups.** One session is one LangGraph thread, so a follow-up prompt
  continues on the page the last turn left open.
- **Model.** `kimi-k3` on DigitalOcean Inference, set by
  `HARNESS_INFERENCE_MODEL`. Set `OPENAI_API_KEY` (and optionally `MODEL`)
  instead to use OpenAI.

## Running it

`run.sh` clones this repo at its current `HEAD` unless you set
`SCRAPER_REPO` and `SCRAPER_REPO_SHA` (a full commit SHA) to point at your own
fork. It gives the session a unique name, sends the prompt, prints the answer
on stdout, saves `artifacts/summary.md` as `summary-<session>.md`, and removes
the session, including when the run fails. Its exit code is the prompt's: 0
completed, 1 failed, 124 timed out.

| Variable | Default | What it does |
|---|---|---|
| `TIMEOUT` | `900` | Seconds to wait for the run to finish |
| `ON_HITL` | `approve` | How to answer approval requests: `approve` or `reject` |
| `DOCTL_FLAGS` | none | Extra global doctl flags, such as `-u <api-url>` |
| `DETAILS` | `1` | Poll the sandbox during the run and print startup and browsing progress; `0` turns it off |
| `POLL_SECONDS` | `2` | Seconds between polls |
| `MANIFEST` | `scraper.yaml` | The manifest to create the session from, relative to this directory |
| `PROMPT_FILE` | `prompt1.txt` | The prompt to send when none is given on the command line |

While the prompt runs, lines starting with `|` come from the sandbox: the
startup steps (clone, virtualenv, `pip install`, server start, or the error
that stopped startup), then one line per browser step from the graph's
`artifacts/progress.log`, with the model's time for that step and the title of
each page it got back.

## When it breaks

| Symptom | Cause | What to do |
|---|---|---|
| 401 `Unable to authenticate you` | The model access key is wrong or revoked | Export a current `MODEL_ACCESS_KEY` |
| 429 `Rate limit exceeded` on the first call | Your team has no quota for the model | Pick another model in `HARNESS_INFERENCE_MODEL` |
| First browser action is slow, or `startup: installed` lists `playwright` | The template has no Chromium for the pinned Playwright, so the graph installs it | Keep `playwright` in `requirements.txt` equal to the template's version |
| Summary says a page was blocked | Login wall, captcha or a bot check | Pick another site. Many sites block headless browsers |
