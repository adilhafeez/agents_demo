#!/usr/bin/env bash
# One shot: create a session, run one prompt, save the summary, remove the session.
#
#   ./run.sh                                  # sends prompt1.txt
#   ./run.sh "Summarize https://example.com"
#
# Every progress line on stderr is stamped with the seconds since the script
# started ([+  12.3s]), so you can see when each step and each agent action
# (tool calls, approvals) began. The answer on stdout is left unstamped.
#
# Needs MODEL_ACCESS_KEY. SCRAPER_REPO defaults to this demo's public repo and
# SCRAPER_REPO_SHA to that repo's current HEAD; set both to run your own fork.
# DOCTL_FLAGS adds global doctl flags, e.g. DOCTL_FLAGS="-u <api-url>" for a non-default API endpoint.
# TIMEOUT (seconds, default 900) and ON_HITL (approve|reject, default approve) tune the run.
set -euo pipefail
cd "$(dirname "$0")"

: "${MODEL_ACCESS_KEY:?export MODEL_ACCESS_KEY (a DigitalOcean model access key)}"
export SCRAPER_REPO="${SCRAPER_REPO:-https://github.com/adilhafeez/agents_demo.git}"
if [ -z "${SCRAPER_REPO_SHA:-}" ]; then
  SCRAPER_REPO_SHA=$(git ls-remote "$SCRAPER_REPO" HEAD | cut -f1)
  [ -n "$SCRAPER_REPO_SHA" ] || { echo "could not resolve HEAD of $SCRAPER_REPO; set SCRAPER_REPO_SHA" >&2; exit 1; }
fi
export SCRAPER_REPO_SHA

manifest=scraper.yaml
prompt="${1:-$(cat prompt1.txt)}"
name="web-summarizer-langgraph-$(date +%Y%m%d-%H%M%S)"

# bash 3.2 (macOS /bin/sh) has no sub-second clock, so perl keeps the time.
now() { perl -MTime::HiRes=time -e 'printf "%.3f", time'; }
T0=$(now)
export T0
stamp() {
  perl -MTime::HiRes=time -e '$| = 1; while (<STDIN>) { printf "[+%6.1fs] %s", time - $ENV{T0}, $_ }'
}
mark() { echo "== $*" | stamp >&2; }
since() { perl -e 'printf "%.1f", $ARGV[1] - $ARGV[0]' "$1" "$2"; }

dt() {
  # shellcheck disable=SC2086 # DOCTL_FLAGS is deliberately word-split
  doctl harness-runtime "$@" ${DOCTL_FLAGS:-}
}

# Every command after create addresses the session by its ID once known.
session="$name"

cleanup() {
  mark "removing session $session"
  if dt remove "$session" >/dev/null 2>&1; then
    mark "removed"
  else
    mark "could not remove $session; it is still billing: doctl harness-runtime remove $session"
  fi
}
trap cleanup EXIT

mark "creating session $name"
dt create -f "$manifest" --name "$name" 2>&1 | stamp >&2
t_ready=$(now)
session_id=$(dt show "$name" --format SessionID --no-header 2>/dev/null | tr -d '[:space:]') || session_id=""
if [ -n "$session_id" ]; then
  session="$session_id"
  mark "session id $session_id"
else
  mark "could not look up the session id; using the name"
fi
mark "session ready; sending prompt"

# stdout (the answer) goes straight through on fd 3; only stderr is stamped.
status=0
{ dt prompt "$session" --timeout "${TIMEOUT:-900}" --on-hitl "${ON_HITL:-approve}" "$prompt" \
    2>&1 1>&3 3>&- | stamp >&2; } 3>&1 || status=$?
t_done=$(now)
mark "run finished, exit $status"

if dt download "$session" --workspace-path artifacts/summary.md --save-to "summary-$name.md" >/dev/null 2>&1; then
  mark "saved summary-$name.md"
fi
mark "timing: create $(since "$T0" "$t_ready")s, prompt $(since "$t_ready" "$t_done")s (session ${session_id:-$name})"
exit "$status"
