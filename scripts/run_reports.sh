#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$REPO_DIR/logs"
mkdir -p "$LOG_DIR"

cd "$REPO_DIR"

# Run inside conda env WITHOUT needing "conda activate"
conda run -n CCS python "$REPO_DIR/scripts/run_reports.py" >> "$LOG_DIR/run_reports.log" 2>&1

# Git commit/push only if there are changes
if [[ -n "$(git status --porcelain)" ]]; then
  git add -A
  git commit -m "Auto update reports $(date -u +'%Y-%m-%dT%H:%M:%SZ')" || true
  git push
fi