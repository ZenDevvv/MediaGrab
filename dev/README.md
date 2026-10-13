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
