#!/usr/bin/env bash
# Push the working tree to the hosted VM and restart the service.
#
# Push-based (§1.4 Decision #15): the exact revision here is what runs, rather
# than whatever happens to be on origin/main. Re-runnable.
#
# Auth: no environment variable is required. `freestyle login` persists a
# session to ~/.freestyle/config.json and the CLI uses that, so an absent
# FREESTYLE_API_KEY does not mean unauthenticated -- only the whoami probe
# below can tell.
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
STAGE="$(mktemp -t mediagrab-deploy.XXXXXX.tar.gz)"
trap 'rm -f "$STAGE"' EXIT

if ! npx --yes freestyle@latest whoami >/dev/null 2>&1; then
  echo "[deploy] ERROR: not authenticated." >&2
  echo "[deploy] Run 'npx freestyle@latest login', or export FREESTYLE_API_KEY." >&2
  exit 1
fi

# --team is passed only when explicitly set; an empty flag value confuses the CLI.
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

# Stage to a file rather than piping into `tar -xzf -` on the far end: the CLI
# does not reliably forward stdin to a piped remote tar, which fails with
# "Refusing to read archive contents from terminal". Uploading the archive and
# extracting it in a second call is slower by one round trip and actually works.
echo "[deploy] Staging working tree"
tar -C "$REPO_ROOT" \
    --exclude='.git' --exclude='.venv' --exclude='downloads' \
    --exclude='dist' --exclude='build' --exclude='ffmpeg' \
    --exclude='__pycache__' --exclude='.pytest_cache' \
    --exclude='./.toolchain' --exclude='./android' --exclude='./deno' \
    -czf "$STAGE" .

echo "[deploy] Syncing to $VM_ID:$REMOTE_DIR"
npx --yes freestyle@latest vm scp "$STAGE" "$VM_ID:/tmp/mediagrab-deploy.tar.gz"
fs "mkdir -p '$REMOTE_DIR' && tar -xzf /tmp/mediagrab-deploy.tar.gz -C '$REMOTE_DIR' && rm -f /tmp/mediagrab-deploy.tar.gz"

# Provisioning needs root (apt, /etc/mediagrab.env, systemctl), and the VM
# service account has passwordless sudo. This deliberately does NOT run
# provision.sh: that script git-clones into $APP_DIR when it finds no .git,
# which both conflicts with this push-based deploy and fails on a non-empty
# tree. Packages and the venv are already provisioned; only the service needs
# restarting for new code to load.
echo "[deploy] Restarting service"
fs "sudo systemctl restart mediagrab"

echo "[deploy] Verifying"
for i in $(seq 1 30); do
  if fs "curl -fsS http://127.0.0.1:8000/api/capabilities" >/dev/null 2>&1; then
    fs "curl -fsS http://127.0.0.1:8000/api/capabilities | head -c 300; echo"
    echo "[deploy] Done."
    exit 0
  fi
  sleep 1
done

echo "[deploy] ERROR: service did not become ready" >&2
fs "sudo journalctl -u mediagrab -n 50 --no-pager || true"
exit 1