# Developer tooling

## `setup-android.sh`

Installs the Android build toolchain **inside the repository**, under
`.toolchain/` (gitignored). Nothing is written to `/usr/lib/jvm`, `/usr/bin`,
`~/.gradle` or `~/.android` — `GRADLE_USER_HOME` and `ANDROID_USER_HOME` are
both redirected into the same directory.

```bash
bash dev/setup-android.sh          # installs anything missing
source .toolchain/env.sh           # required in any shell that builds
cd android && ./gradlew assembleDebug
```

Cleanup is `rm -rf .toolchain`.

`env.sh` exports `JAVA_HOME`, `ANDROID_HOME`, `ANDROID_SDK_ROOT`,
`GRADLE_USER_HOME`, `ANDROID_USER_HOME` and prepends the JDK, Gradle and
sdkmanager to `PATH`.

Why it exists: M4 is the first milestone that cannot be verified without a
compiler. The desktop milestone shipped with CI green and still had five
defects that only a real run on real hardware found (ARCHITECTURE.md §13).
Writing an APK blind would repeat that at larger scale.

## `android-sandbox.sh`

Runs an Android tool with everything it writes kept under `.toolchain/`:

```bash
dev/android-sandbox.sh adb devices
dev/android-sandbox.sh emulator -avd mediagrab -no-window -gpu swangle_indirect
```

`env.sh` covers the variables the SDK documents, but adb writes its key pair
to `$HOME/.android` regardless, and the emulator writes to `$HOME`, `$TMPDIR`
and `$XDG_RUNTIME_DIR`. This wrapper overrides those per command rather than
for the whole shell. Use it for every `adb`, `emulator`, `avdmanager` and
`sdkmanager` call.

The `mediagrab` AVD is an x86_64 API 34 image. Debug APKs carry x86_64 natives
for it; see `android/app/build.gradle.kts` for why ARM translation is not
enough.

Use `-gpu swangle_indirect`. Both `swiftshader_indirect` and `guest` segfault
the headless emulator under steady redraw -- the download screen, the progress
notification, or a DevTools `Page.captureScreenshot` mid-animation -- each time
leaving a ~1 GB core with systemd-coredump. For screenshots, prefer
`adb exec-out screencap -p` at a resting screen.

## Release APK: signing and `check-apk.py`

Release builds are signed from the environment; the keystore never lives in
the repository:

```bash
export MEDIAGRAB_KEYSTORE=/path/outside/repo/mediagrab-release.jks
export MEDIAGRAB_KEYSTORE_PASSWORD=...      # MEDIAGRAB_KEY_PASSWORD defaults to this
export MEDIAGRAB_KEY_ALIAS=mediagrab        # the default
(cd android && ./gradlew assembleRelease)
python dev/check-apk.py android/app/build/outputs/apk/release/app-release.apk
```

Without the variables the build still succeeds but produces
`app-release-unsigned.apk`, which Android will not install and the check
refuses. The check also gates size (<= 120 MB), ABIs (arm64-v8a only),
the bundled `LICENSE`/`NOTICE`, the pinned yt-dlp, a non-debug signature and
debuggable being off.

## `sync-contract.py`

Maintains `openapi-contract.json`, a snapshot of the OpenAPI document FastAPI
serves at `/openapi.json`.

```bash
python dev/sync-contract.py           # regenerate and rewrite the file
python dev/sync-contract.py --check   # exit 1 if it is stale
```

Run it after any **intentional** change to a request or response model, and
commit the result. `tests/test_api_contract.py` asserts the live spec still
matches, so a contract change cannot land without the snapshot moving with it.

The snapshot exists because M4 re-implements these eleven endpoints in Kotlin
while `static/js/app.js` — the frontend both servers share — stays one file.
Without a pinned contract, drift between the two implementations produces no
error anywhere: the WebView renders nothing on a phone while desktop keeps
working. The specific failure is naming. Pydantic emits snake_case
(`job_id`, `download_url`); Kotlin's default is camelCase, so a `val jobId`
serialises to `{"jobId": …}`, the frontend reads `.job_id`, gets `undefined`,
and no stack trace is ever produced.

The Android build should consume the same file rather than restating the
schema in Kotlin.
