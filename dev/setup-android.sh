#!/usr/bin/env bash
# Install the Android build toolchain *inside the repository*.
#
# Nothing here touches the host system. The JDK, the Gradle cache, the Android
# SDK and the AVD home all live under .toolchain/, which is gitignored, so
# cleanup is a single `rm -rf .toolchain` and `unset` of a few variables.
#
# This is deliberate: M4 is the first milestone that cannot be verified without
# a compiler. The desktop milestone shipped with CI green and still had five
# defects that only a real run could find (ARCHITECTURE.md §13). Writing an APK
# blind would repeat that at larger scale.
#
# Usage:
#   bash dev/setup-android.sh          # install anything missing
#   bash dev/setup-android.sh --force  # re-download everything
#
# Then, in any shell that builds the app:
#   source .toolchain/env.sh
set -euo pipefail

TOOLCHAIN="${TOOLCHAIN:-$PWD/.toolchain}"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

JDK_MAJOR="17"
GRADLE_VERSION="8.9"
CMDLINE_TOOLS="11076708"   # cmdline-tools 13.0
ANDROID_PLATFORM="android-35"
ANDROID_BUILD_TOOLS="35.0.0"

log() { printf '[android] %s\n' "$*"; }

mkdir -p "$TOOLCHAIN"

# --- JDK ---------------------------------------------------------------------
# Temurin. A tarball rather than an installer because an installer writes to
# /usr/lib/jvm and /usr/bin, which is exactly what we are avoiding.
#
# The URL is resolved through the Adoptium API rather than constructed. The
# naming is inconsistent in a way that is easy to get wrong -- the GitHub tag
# encodes the build as jdk-17.0.20.1+1 while the archive filename writes it
# as 17.0.20.1_1. Guessing produced a 404 on the first attempt; asking does
# not go stale.
install_jdk() {
  local dest="$TOOLCHAIN/jdk"
  if [ -x "$dest/bin/javac" ] && [ "$FORCE" -eq 0 ]; then
    log "JDK present: $("$dest/bin/java" -version 2>&1 | head -1)"
    return
  fi

  log "Resolving latest Temurin JDK $JDK_MAJOR"
  local url
  # Extract the first .tar.gz URL rather than parsing fields: the JSON spacing
  # differs between the list and the single-asset endpoint, and a
  # field-position parse breaks silently on it.
  url="$(curl -fsSL --retry 3 \
    "https://api.adoptium.net/v3/assets/latest/${JDK_MAJOR}/hotspot?architecture=x64&image_type=jdk&os=linux&vendor=eclipse" \
    | grep -o 'https://[^"]*\.tar\.gz' | head -1)"

  if [ -z "$url" ]; then
    log "ERROR: could not resolve a JDK download URL from the Adoptium API"
    exit 1
  fi

  log "Downloading $(basename "$url")"
  curl -fsSL --retry 3 -o "$TOOLCHAIN/jdk.tar.gz" "$url"
  rm -rf "$dest" "$TOOLCHAIN/jdk-extract"
  mkdir -p "$TOOLCHAIN/jdk-extract"
  tar -xzf "$TOOLCHAIN/jdk.tar.gz" -C "$TOOLCHAIN/jdk-extract"
  # The archive has a versioned top-level directory; flatten it so the path is
  # stable across JDK upgrades.
  mv "$TOOLCHAIN/jdk-extract"/*/ "$dest"
  rm -rf "$TOOLCHAIN/jdk-extract" "$TOOLCHAIN/jdk.tar.gz"
  log "JDK installed: $("$dest/bin/java" -version 2>&1 | head -1)"
}

# --- Android SDK -------------------------------------------------------------
install_sdk() {
  local dest="$TOOLCHAIN/android-sdk"

  # sdkmanager is a shell script that needs a JVM on PATH. Without this the
  # install step fails silently behind `>/dev/null` and leaves an SDK
  # containing only cmdline-tools.
  export JAVA_HOME="$TOOLCHAIN/jdk"
  export PATH="$JAVA_HOME/bin:$PATH"

  if [ -x "$dest/cmdline-tools/latest/bin/sdkmanager" ] && [ "$FORCE" -eq 0 ]; then
    log "Android cmdline-tools present"
  else
    local url="https://dl.google.com/android/repository/commandlinetools-linux-${CMDLINE_TOOLS}_latest.zip"
    log "Downloading Android commandline-tools"
    curl -fsSL --retry 3 -o "$TOOLCHAIN/cmdline-tools.zip" "$url"

    rm -rf "$dest/cmdline-tools"
    # sdkmanager refuses to run unless it sits at
    # <sdk_root>/cmdline-tools/<something>/bin -- "latest" is the
    # conventional name and is what it expects to find.
    mkdir -p "$TOOLCHAIN/cmdline-extract"
    unzip -q "$TOOLCHAIN/cmdline-tools.zip" -d "$TOOLCHAIN/cmdline-extract"
    mkdir -p "$dest/cmdline-tools"
    mv "$TOOLCHAIN/cmdline-extract/cmdline-tools" "$dest/cmdline-tools/latest"
    rm -rf "$TOOLCHAIN/cmdline-extract" "$TOOLCHAIN/cmdline-tools.zip"
    log "cmdline-tools installed"
  fi

  export ANDROID_HOME="$dest"
  export ANDROID_SDK_ROOT="$dest"
  local sdkmanager="$dest/cmdline-tools/latest/bin/sdkmanager"

  if [ "$FORCE" -eq 0 ] && [ -d "$dest/platforms/$ANDROID_PLATFORM" ] \
     && [ -d "$dest/build-tools/$ANDROID_BUILD_TOOLS" ]; then
    log "Platform $ANDROID_PLATFORM and build-tools $ANDROID_BUILD_TOOLS present"
    return
  fi

  log "Accepting SDK licences"
  # Piped `y` rather than --licenses alone, because a non-interactive stdin
  # makes sdkmanager exit non-zero even when it accepts everything.
  yes 2>/dev/null | "$sdkmanager" --sdk_root="$dest" --licenses >/dev/null 2>&1 || true

  log "Installing $ANDROID_PLATFORM, build-tools $ANDROID_BUILD_TOOLS, platform-tools"
  # Output is captured rather than discarded: a failed install must not look
  # like a successful one, which is exactly what happened before JAVA_HOME was
  # exported here.
  if ! "$sdkmanager" --sdk_root="$dest" \
      "platform-tools" \
      "platforms;$ANDROID_PLATFORM" \
      "build-tools;$ANDROID_BUILD_TOOLS" 2>&1 | tail -20; then
    log "ERROR: sdkmanager failed"
    exit 1
  fi

  for expected in "platforms/$ANDROID_PLATFORM" "build-tools/$ANDROID_BUILD_TOOLS" "platform-tools"; do
    if [ ! -d "$dest/$expected" ]; then
      log "ERROR: expected $dest/$expected is missing after install"
      exit 1
    fi
  done
  log "Android SDK installed"
}

# --- Gradle ------------------------------------------------------------------
# Needed to *generate* the wrapper in android/gradlew; after that the wrapper
# is self-sufficient and the project builds without this install.
install_gradle() {
  local dest="$TOOLCHAIN/gradle-$GRADLE_VERSION"
  if [ -x "$dest/bin/gradle" ] && [ "$FORCE" -eq 0 ]; then
    log "Gradle present: $("$dest/bin/gradle" --version 2>/dev/null | grep -m1 Gradle || echo "$GRADLE_VERSION")"
    return
  fi

  local zip="gradle-${GRADLE_VERSION}-bin.zip"
  local url="https://services.gradle.org/distributions/$zip"
  log "Downloading $zip"
  curl -fsSL --retry 3 -o "$TOOLCHAIN/$zip" "$url"
  rm -rf "$dest"
  unzip -q "$TOOLCHAIN/$zip" -d "$TOOLCHAIN"
  rm -f "$TOOLCHAIN/$zip"
  log "Gradle installed: $("$dest/bin/gradle" --version 2>/dev/null | grep -m1 Gradle)"
}

# --- env ---------------------------------------------------------------------
write_env() {
  cat > "$TOOLCHAIN/env.sh" <<EOF
# Source this to build the Android app:  source .toolchain/env.sh
# Everything is scoped to this directory; nothing on the host is modified.
export TOOLCHAIN="$TOOLCHAIN"
export JAVA_HOME="$TOOLCHAIN/jdk"
export ANDROID_HOME="$TOOLCHAIN/android-sdk"
export ANDROID_SDK_ROOT="$TOOLCHAIN/android-sdk"
export GRADLE_USER_HOME="$TOOLCHAIN/gradle-home"
# Keeps the AVD/keystore directory inside the repo too.
export ANDROID_USER_HOME="$TOOLCHAIN/android-home"
export PATH="\$JAVA_HOME/bin:$TOOLCHAIN/gradle-$GRADLE_VERSION/bin:\$ANDROID_HOME/cmdline-tools/latest/bin:\$ANDROID_HOME/platform-tools:\$PATH"
EOF
  log "Wrote $TOOLCHAIN/env.sh"
}

install_jdk
install_gradle
install_sdk
write_env

log "Done. Next:"
log "  source .toolchain/env.sh"
log "  cd android && ./gradlew assembleDebug"