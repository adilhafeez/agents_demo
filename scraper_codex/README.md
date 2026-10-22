# Web summarizer with Codex

Codex, running as a hosted agent on DigitalOcean, opens a website in a real
headless browser, reads the pages and writes a Markdown summary. One command
creates the session, sends the prompt, saves the summary and removes the
session.

```bash
export MODEL_ACCESS_KEY=...   # DigitalOcean model access key
./run.sh                                     # sends prompt1.txt
./run.sh "Summarize https://example.com"
```

## Files

| File | What it is |
|---|---|
| `scraper.yaml` | The agent manifest, including the `web-summarizer` skill. |
| `prompt1.txt` | The default prompt. |
| `run.sh` | Create a session, run one prompt, save the summary, remove the session. |

## Requirements

- `doctl` with the `harness-runtime` commands, authenticated to your team.
- A DigitalOcean model access key in `MODEL_ACCESS_KEY` (control panel,
  Inference). doctl expands it from your environment when it reads the
  manifest, so the key is never written to disk here.

## How it works

- **Browser.** The codex image includes a headless Chromium, and the runtime
  tells the agent about it. Codex writes its own Playwright script against it.
  The skill asks for one reusable script and at most three browse commands,
  because every command is a model turn and page text piles up in the context.
- **Commands.** The agent's commands run inside its own microVM, which is the
  isolation boundary, so those scripts can reach the network.
- **Approvals.** `permissions.default: allow`. Without it Codex runs under its
  `untrusted` approval policy and stops for approval before most commands. Form
  submits are forbidden by the skill's written rules instead.
- **Model.** `kimi-k3` on DigitalOcean Inference, set by
  `HARNESS_INFERENCE_MODEL` in the manifest. The endpoint is set by the
  platform.

## Running it

`run.sh` gives the session a unique name, sends the prompt, prints the answer
on stdout, saves `artifacts/summary.md` from the session as
`summary-<session>.md`, and removes the session, including when the run fails.
Its exit code is the prompt's: 0 completed, 1 failed, 124 timed out.

Every progress line on stderr carries the seconds since the script started, so
you can read off when the session became ready, when the run started and when
each tool call began. The answer on stdout is unstamped.

| Variable | Default | What it does |
|---|---|---|
| `TIMEOUT` | `900` | Seconds to wait for the run to finish |
| `ON_HITL` | `approve` | How to answer any approval request: `approve` or `reject` |
| `DOCTL_FLAGS` | none | Extra global doctl flags, such as `-u <api-url>` |

To keep a session for several prompts instead:

```bash
doctl harness-runtime create -f scraper.yaml
doctl harness-runtime prompt web-summarizer-codex --timeout 900 "$(cat prompt1.txt)"
doctl harness-runtime download web-summarizer-codex --workspace-path artifacts/summary.md --save-to summary.md
doctl harness-runtime remove web-summarizer-codex
```

## When it breaks

| Symptom | Cause | What to do |
|---|---|---|
| 401 `Unable to authenticate you` | The model access key is wrong or revoked | Export a current `MODEL_ACCESS_KEY` |
| 429 `Rate limit exceeded` on the first call | Your team has no quota for the model | Pick another model in `HARNESS_INFERENCE_MODEL` |
| The agent downloads a browser | It ignored the skill | Harmless but slow. Name the skill in the prompt: "use the web-summarizer skill" |
| Answer with no browsing before it | The model answered from memory | Ask again and name the URL |
| No `summary-<session>.md` saved | The agent replied without writing `artifacts/summary.md` | The answer is still on stdout |
| Summary says a page was blocked | Login wall, captcha or a bot check | Pick another site. Many sites block headless browsers |
