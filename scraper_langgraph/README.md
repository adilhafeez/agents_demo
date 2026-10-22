# Web summarizer with LangGraph

A LangGraph agent, running as a hosted agent on DigitalOcean, opens a website in
a real headless Chromium, reads the pages and writes a Markdown summary. The
graph is the code in this directory: the platform clones it at a pinned commit
when the session starts. Chromium comes from a team template you build once.

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
| `template/Dockerfile` | Source image for the `langgraph-chromium` team template. |
| `prompt1.txt` | The default prompt. |
| `run.sh` | Create a session, run one prompt, save the summary, remove the session. |

## Requirements

- `doctl` with the `harness-runtime` commands, authenticated to your team.
- A DigitalOcean model access key in `MODEL_ACCESS_KEY` (control panel,
  Inference). doctl expands it from your environment when it reads the
  manifest, so the key is never written to disk here.
- For the Chromium template: Docker and a DigitalOcean Container Registry in
  your team.

## One-time setup: the Chromium template

The template layers `template/Dockerfile` onto the stock LangGraph template, so
every session starts with Chromium and its system libraries already installed.

```bash
REF=registry.digitalocean.com/<your-registry>/langgraph-chromium:v1
docker build --platform linux/amd64 -t "$REF" template
docker push "$REF"
doctl harness-runtime template create --name langgraph-chromium \
  --base-template langgraph-base --source-oci-ref "$REF"
doctl harness-runtime template list-builds langgraph-chromium   # wait for succeeded
```

The build takes about five minutes. To skip the template, delete the
`template:` line in `scraper.yaml`; the graph then installs Chromium on its
first browser action, which adds a few minutes to that turn.

Rules for your own browser template:

- The image must be `linux/amd64` and Debian or Ubuntu based.
- The template keeps the files under `/ms-playwright`, the system libraries and
  the image's `ENV`. It does not keep Python packages you install in the image;
  the graph's packages come from `requirements.txt`.
- `playwright` in `requirements.txt` must equal `PLAYWRIGHT_VERSION` in the
  Dockerfile. Each Playwright version looks for its own browser build, so a
  mismatch makes the graph download a second browser at session start.
- Do not run `playwright install` unconditionally at runtime; `--with-deps`
  runs `apt-get` every time. `scraper_agent/browser.py` checks with
  `playwright install --dry-run` first and skips the install when the browser
  is present.
- To change the image, push a new tag and run `doctl harness-runtime template
  update langgraph-chromium --source-oci-ref <new-ref>`. New sessions use the
  latest successful build.

## How it works

- **Graph.** `browse` calls the browser tools (`open_url`, `click`,
  `type_text`, `scroll`, `go_back`, `read_page`, `screenshot`) until it has
  what you asked for, up to `MAX_BROWSE_STEPS` per turn. `summarize` then
  writes the answer from the page snapshots only and saves it to
  `artifacts/summary.md`.
- **Pages as text.** Each snapshot lists the visible interactive elements by
  number and the page text from the current scroll position, so the model acts
  on "element 12" instead of writing CSS selectors.
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

While the prompt runs, lines starting with `|` come from the sandbox: the
startup steps (clone, virtualenv, `pip install`, server start, or the error
that stopped startup), then one line per browser step from the graph's
`artifacts/progress.log`, with the model's time for that step and the title of
each page it got back.

## When it breaks

| Symptom | Cause | What to do |
|---|---|---|
| Session create fails on the `langgraph-chromium` template | The template does not exist in your team, or has no successful build | Do the one-time setup, or delete the `template:` line |
| 401 `Unable to authenticate you` | The model access key is wrong or revoked | Export a current `MODEL_ACCESS_KEY` |
| 429 `Rate limit exceeded` on the first call | Your team has no quota for the model | Pick another model in `HARNESS_INFERENCE_MODEL` |
| First browser action takes minutes | Chromium is being installed: no template, or a Playwright version mismatch | Use the template and keep the two versions equal |
| Summary says a page was blocked | Login wall, captcha or a bot check | Pick another site. Many sites block headless browsers |
