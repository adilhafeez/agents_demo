# The web summarizer on your own image

The same graph as [`../scraper.yaml`](../scraper.yaml), on a team template built
from your own container image. The stock `langgraph` template already has
Chromium; this image adds [Tesseract](https://github.com/tesseract-ocr/tesseract),
so the graph can read text that is drawn inside images: comics, charts,
infographics, scanned documents.

```bash
export MODEL_ACCESS_KEY=...   # DigitalOcean model access key
custom_image/run.sh                          # sends custom_image/prompt1.txt
custom_image/run.sh "Read the chart on https://example.com and explain it"
```

`prompt1.txt` asks for the latest [xkcd](https://xkcd.com) comic, whose words
exist only in the image, and rules out other sites. On the stock template the
page snapshot carries the comic's title and image link and nothing it says, so
the agent can only guess; left free to browse, it looks the comic up on a
transcript site instead.

## Files

| File | What it is |
|---|---|
| `Dockerfile` | Source image for the `langgraph-ocr` team template. |
| `scraper.yaml` | The manifest: `../scraper.yaml` plus `template: langgraph-ocr`. |
| `prompt1.txt` | The default prompt. |
| `run.sh` | Runs `../run.sh` with this manifest and prompt. |

## One-time setup: the template

You need Docker and a DigitalOcean Container Registry in your team.

```bash
REF=registry.digitalocean.com/<your-registry>/langgraph-ocr:v1
docker build --platform linux/amd64 -t "$REF" custom_image
docker push "$REF"
doctl harness-runtime template create --name langgraph-ocr \
  --base-template langgraph-base --source-oci-ref "$REF"
doctl harness-runtime template list-builds langgraph-ocr   # wait for succeeded
```

The template build takes about five minutes. To change the image later, push
a new tag and run `doctl harness-runtime template update langgraph-ocr
--source-oci-ref <new-ref>`; new sessions use the latest successful build.

## How it works

- **The image is the system the graph runs on.** A team template on
  `langgraph-base` runs the platform's LangGraph server inside your image's
  root filesystem, so the graph sees your system packages: here Chromium's
  libraries, the browser under `/ms-playwright`, and `tesseract`.
- **Python packages come from the platform, not your image.** The template
  keeps your files, system packages and `ENV`, but not Python packages
  installed in the image: the graph runs on the platform's Python, which ships
  the LangGraph server and `langchain-openai`. Anything else, here
  `playwright`, comes from `../requirements.txt`. A template built from
  `langgraph-base` before 2026-10-30 has no `langchain-openai`; rebuild it with
  `template update`.
- **The tool appears only where it works.** `scraper_agent/ocr.py` looks for
  `tesseract` on `PATH`. The graph offers its `read_images` tool only when it
  is there, so the same code runs unchanged on the stock template.
- **What `read_images` does.** It takes the six largest images on the current
  page (at least 150x80 pixels), waits for each to load, runs Tesseract on it,
  and returns its text with the image's `alt` and `title`.

## Rules for your own image

- `linux/amd64`, Debian or Ubuntu based.
- `playwright` in `../requirements.txt` must equal `PLAYWRIGHT_VERSION` in the
  Dockerfile. Each Playwright version looks for its own browser build, so a
  mismatch downloads a second browser at session start.
- Do not run `playwright install` unconditionally at runtime: `--with-deps`
  runs `apt-get` every time.
