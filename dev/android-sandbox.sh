#!/usr/bin/env bash
# Run an Android tool with every place it writes redirected into .toolchain/.
#
#   dev/android-sandbox.sh adb devices
#   dev/android-sandbox.sh emulator -avd mediagrab
#
# env.sh redirects what the SDK documents (ANDROID_USER_HOME and friends), but
# adb ignores those and writes its key pair to $HOME/.android, and the emulator
# drops a console auth token in $HOME, scratch files in $TMPDIR and a
# registration file in $XDG_RUNTIME_DIR. Overriding
# HOME for the whole shell would break everything else, so it is overridden
# here, per command, instead.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLCHAIN="$ROOT/.toolchain"

if [ ! -f "$TOOLCHAIN/env.sh" ]; then
  echo "no toolchain; run: bash dev/setup-android.sh" >&2
  exit 1
fi
# shellcheck source=/dev/null
source "$TOOLCHAIN/env.sh"

export HOME="$TOOLCHAIN/home"
export TMPDIR="$TOOLCHAIN/tmp"
export ANDROID_TMP="$TMPDIR"
export ANDROID_AVD_HOME="$ANDROID_USER_HOME/avd"
export ANDROID_EMULATOR_HOME="$ANDROID_USER_HOME"
# The emulator registers running instances under $XDG_RUNTIME_DIR/avd.
export XDG_RUNTIME_DIR="$TOOLCHAIN/run"
mkdir -p "$HOME" "$TMPDIR" "$ANDROID_AVD_HOME"
mkdir -p -m 0700 "$XDG_RUNTIME_DIR"

export PATH="$ANDROID_HOME/emulator:$PATH"
exec "$@"
