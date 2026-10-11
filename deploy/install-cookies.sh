#!/usr/bin/env bash
# Install or verify YouTube cookies on the hosted instance.
#
# WHY: a datacenter IP makes YouTube answer "Sign in to confirm you're not a
# bot" regardless of player client or JS runtime. Cookies exported from a real
# browser session are the standard workaround.
#
# LIMITATION: YouTube binds cookies to the address they came from, so cookies
# exported from your home network may not authenticate this VM. If that is the
# case you need a residential egress proxy (MEDIA_GRAB_YTDLP_PROXY), which is
# the only fix that reliably reaches ~100% success.
#
# Usage (run ON THE VM):
#   bash deploy/install-cookies.sh check          # status only
#   bash deploy/install-cookies.sh install <local-path>
set -euo pipefail

ENV_FILE="${ENV_FILE:-/etc/mediagrab.env}"
COOKIE_DIR=/etc/mediagrab
COOKIE_FILE="$COOKIE_DIR/ytdlp-cookies.txt"
ACTION="${1:-check}"

log() { printf '[cookies] %s\n' "$*"; }

status() {
  if sudo grep -q "^MEDIA_GRAB_YTDLP_COOKIES=" "$ENV_FILE" 2>/dev/null; then
    log "configured: $(sudo grep '^MEDIA_GRAB_YTDLP_COOKIES=' "$ENV_FILE")"
  else
    log "not configured"
  fi
  if [ -s "$COOKIE_FILE" ]; then
    log "cookie file present: $COOKIE_FILE ($(sudo wc -l < "$COOKIE_FILE") lines, mode $(sudo stat -c %a "$COOKIE_FILE"))"
  else
    log "no cookie file at $COOKIE_FILE"
  fi
  if sudo grep -q "^MEDIA_GRAB_YTDLP_PROXY=" "$ENV_FILE" 2>/dev/null; then
    log "egress proxy configured (used for all yt-dlp requests)"
  else
    log "no egress proxy configured"
  fi
}

case "$ACTION" in
  check)
    status
    ;;

  install)
    SRC="${2:-}"
    if [ -z "$SRC" ] || [ ! -s "$SRC" ]; then
      echo "usage: $0 install <path-to-cookies.txt>" >&2
      exit 2
    fi
    # Netscape cookie format: either the comment header or the 7-field rows.
    if ! head -1 "$SRC" | grep -q "netscape"; then
      if ! awk 'NF==0 || $1 ~ /^#/ {next} NF>=7 {found=1} END{exit !found}' "$SRC"; then
        log "ERROR: $SRC does not look like a Netscape cookies.txt"
        log "Export it with: yt-dlp --cookies-from-browser chrome --cookies cookies.txt <any-url>"
        exit 3
      fi
    fi
    log "Installing cookie file"
    sudo install -m 600 -o root -g root "$SRC" "$COOKIE_FILE"
    if sudo grep -q "^MEDIA_GRAB_YTDLP_COOKIES=" "$ENV_FILE" 2>/dev/null; then
      sudo sed -i "s|^MEDIA_GRAB_YTDLP_COOKIES=.*|MEDIA_GRAB_YTDLP_COOKIES=$COOKIE_FILE|" "$ENV_FILE"
    else
      echo "MEDIA_GRAB_YTDLP_COOKIES=$COOKIE_FILE" | sudo tee -a "$ENV_FILE" >/dev/null
    fi
    log "Restarting service to pick up cookies"
    sudo systemctl restart mediagrab
    sleep 8
    systemctl is-active mediagrab
    log "Done. Verify with:"
    log "  curl -s -X POST http://127.0.0.1:8000/api/info -H 'Content-Type: application/json' -d '{\"url\":\"https://www.youtube.com/watch?v=aqz-KE-bpKQ\"}' | head -c 200"
    ;;

  remove)
    log "Removing cookie configuration"
    sudo sed -i "/^MEDIA_GRAB_YTDLP_COOKIES=/d" "$ENV_FILE" 2>/dev/null || true
    sudo rm -f "$COOKIE_FILE"
    sudo systemctl restart mediagrab
    log "Done."
    ;;

  *)
    echo "usage: $0 {check|install <file>|remove}" >&2
    exit 2
    ;;
esac