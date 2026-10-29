#!/usr/bin/env bash
# ../run.sh on the langgraph-ocr team template, with this directory's prompt.
#
#   custom_image/run.sh                       # sends custom_image/prompt1.txt
#   custom_image/run.sh "Read the chart on https://example.com"
set -euo pipefail
export MANIFEST=custom_image/scraper.yaml
export PROMPT_FILE="${PROMPT_FILE:-custom_image/prompt1.txt}"
exec "$(dirname "$0")/../run.sh" "$@"
