#!/usr/bin/env bash
# Push the working tree to the hosted VM and restart the service.
#
# Push-based (§1.4 Decision #15): the exact revision here is what runs, rather
# than whatever happens to be on origin/main. Re-runnable.
#
# Requires FREESTYLE_API_KEY. Never commits the key.
#
#   ./deploy/push.sh            # deploy current working tree
#   ./deploy/push.sh --commit   # commit pending changes first, then deploy
set -euo pipefail

VM_ID="${VM_ID:-vm-4ea425a9eae64c4bacd5fde0b2d9a99d}"
# Left empty so the CLI uses the authenticated active team. An explicit --team
# would need the real team id (51fa42a6-...), not the acct- prefix form.
TEAM_ID="${TEAM_ID:-}"
REMOTE_DIR="${REMOTE_DIR:-/opt/mediagrab}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -z "${FREESTYLE_API_KEY:-}" ]; then
  # `freestyle login` stores the credential locally, so an absent env var does
  # not necessarily mean unauthenticated. Let the CLI decide.
  if ! npx --yes freestyle@latest whoami >/dev/null 2>&1; then
    echo "[deploy] ERROR: not authenticated." >&2
    echo "[deploy] Run 'npx freestyle@latest login' or export FREESTYLE_API_KEY." >&2
    exit 1
  fi
fi

fs() {
  if [ -n "$TEAM_ID" ]; then
    npx --yes freestyle@latest vm ssh "$VM_ID" --team "$TEAM_ID" --exec "$1"
  else
    npx --yes freestyle@latest vm ssh "$VM_ID" --exec "$1"
  fi
}

if [ "${1:-}" = "--commit" ]; then
  git -C "$REPO_ROOT" add -A
  git -C "$REPO_ROOT" commit -m "${2:-deploy: update hosted instance}"
  echo "[deploy] committed"
fi

echo "[deploy] Syncing working tree to $VM_ID:$REMOTE_DIR"
# --delete keeps the remote free of stale files, but must not remove the
# downloaded media or the env file.
fs "mkdir -p '$REMOTE_DIR'"
tar -C "$REPO_ROOT" \
    --exclude='.git' --exclude='.venv' --exclude='downloads' \
    --exclude='dist' --exclude='build' --exclude='ffmpeg' \
    --exclude='__pycache__' --exclude='.pytest_cache' \
    -czf - . | fs "tar -xzf - -C '$REMOTE_DIR'"

echo "[deploy] Provisioning"
fs "bash '$REMOTE_DIR/deploy/provision.sh'"

echo "[deploy] Verifying"
fs "curl -fsS http://127.0.0.1:8000/api/capabilities | head -c 300; echo"
echo "[deploy] Done."