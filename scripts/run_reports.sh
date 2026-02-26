#!/usr/bin/env bash
export GIT_SSH_COMMAND="ssh -i $HOME/.ssh/id_ed25519 -o IdentitiesOnly=yes"
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$REPO_DIR/logs"
mkdir -p "$LOG_DIR"

cd "$REPO_DIR"

echo "===== Run started $(date) =====" >> "$LOG_DIR/run_reports.log"

# --- Load conda for non-interactive shells (cron) ---
# Try common installs first; adjust if yours differs.
if [[ -f "$HOME/miniforge3/etc/profile.d/conda.sh" ]]; then
  source "$HOME/miniforge3/etc/profile.d/conda.sh"
elif [[ -f "$HOME/mambaforge/etc/profile.d/conda.sh" ]]; then
  source "$HOME/mambaforge/etc/profile.d/conda.sh"
elif [[ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]]; then
  source "$HOME/miniconda3/etc/profile.d/conda.sh"
elif [[ -f "$HOME/anaconda3/etc/profile.d/conda.sh" ]]; then
  source "$HOME/anaconda3/etc/profile.d/conda.sh"
else
  echo "ERROR: conda.sh not found. Set the correct path in run_reports.sh" >> "$LOG_DIR/run_reports.log"
  exit 1
fi

conda activate CCS

python "$REPO_DIR/scripts/run_reports.py" >> "$LOG_DIR/run_reports.log" 2>&1

# Git commit/push only if there are changes
if [[ -n "$(git status --porcelain)" ]]; then
  git add -A
  git commit -m "Auto update reports $(date -u +'%Y-%m-%dT%H:%M:%SZ')" || true
  export GIT_SSH_COMMAND="ssh -i $HOME/.ssh/id_ed25519 -o IdentitiesOnly=yes"
  git push || echo "git push failed at $(date)" >> "$LOG_DIR/run_reports.log"
fi