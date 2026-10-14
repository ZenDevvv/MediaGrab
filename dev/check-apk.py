#!/usr/bin/env python3
"""Check a release APK before it ships.

    python dev/check-apk.py android/app/build/outputs/apk/release/app-release.apk

    python dev/check-apk.py --unsigned-ok app-release-unsigned.apk

Fails (exit 1) unless every check passes:

* size     -- at most 120 MB, the top of the plan's budget (§1.1 Decision #7)
* ABIs     -- arm64-v8a only; x86_64 belongs to debug builds
* licences -- LICENSE and NOTICE bundled and identical to the repository's
              (GPL-3.0 §4, plan §1.8 Decision #32)
* yt-dlp   -- the bundled copy matches android/ytdlp.lock
* signing  -- signed, verifiably, and not with a debug key
* debuggable -- not set

Each of these has a way to go wrong silently: an unsigned APK builds fine, a
debug-signed one installs fine on the developer's own phone, and a missing
licence produces no error anywhere.
"""

from __future__ import annotations

import glob
import hashlib
import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_BYTES = 120 * 1024 * 1024
# yt-dlp's release zipapp starts with this shebang, then the zip.
ZIPAPP_MAGIC = b"#!/usr/bin/env python3\nPK"


def build_tool(name: str) -> str:
    sdk = os.environ.get("ANDROID_HOME") or str(ROOT / ".toolchain" / "android-sdk")
    found = sorted(glob.glob(os.path.join(sdk, "build-tools", "*", name)))
    if not found:
        sys.exit(f"FAIL: {name} not found under {sdk}/build-tools; source .toolchain/env.sh")
    return found[-1]


def read_lock() -> dict[str, str]:
    lock = {}
    for line in (ROOT / "android" / "ytdlp.lock").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            lock[key.strip()] = value.strip()
    return lock


def main(apk_path: str, unsigned_ok: bool = False) -> int:
    apk = Path(apk_path)
    if not apk.is_file():
        print(f"FAIL: no such file: {apk}")
        return 1

    failures: list[str] = []

    def check(ok: bool, passed: str, failed: str) -> None:
        print(("ok    " if ok else "FAIL  ") + (passed if ok else failed))
        if not ok:
            failures.append(failed)

    size = apk.stat().st_size
    check(size <= MAX_BYTES,
          f"size {size / 2**20:.1f} MB (budget <= 120 MB)",
          f"size {size / 2**20:.1f} MB exceeds the 120 MB budget")

    with zipfile.ZipFile(apk) as z:
        names = z.namelist()
        abis = sorted({n.split("/")[1] for n in names if n.startswith("lib/") and n.count("/") >= 2})
        check(abis == ["arm64-v8a"], f"ABIs {abis}", f"ABIs {abis}, expected only arm64-v8a")

        for legal in ("LICENSE", "NOTICE"):
            entry = f"assets/licenses/{legal}"
            bundled = z.read(entry) if entry in names else None
            check(bundled == (ROOT / legal).read_bytes(),
                  f"{legal} bundled and current",
                  f"{legal} missing from the APK or stale ({entry})")

        # Found by content, not path: release builds shorten resource paths
        # (res/raw/ytdlp became res/Nc). Requiring exactly one also catches
        # the library's own copy shipping beside ours.
        lock = read_lock()
        zipapps = [z.read(n) for n in names
                   if n.startswith("res/") and z.open(n).read(len(ZIPAPP_MAGIC)) == ZIPAPP_MAGIC]
        digests = [hashlib.sha256(b).hexdigest()[:16] for b in zipapps]
        check(len(zipapps) == 1 and hashlib.sha256(zipapps[0]).hexdigest() == lock.get("sha256"),
              f"yt-dlp {lock.get('version')} matches android/ytdlp.lock",
              f"expected one yt-dlp matching android/ytdlp.lock, found {len(zipapps)}: {digests}")

    if unsigned_ok:
        # For manual CI runs without the signing secrets: everything else is
        # still worth checking. Never used for a tag build.
        print("skip  signing (--unsigned-ok)")
        verify = None
    else:
        verify = subprocess.run(
            [build_tool("apksigner"), "verify", "--print-certs", str(apk)],
            capture_output=True, text=True,
        )
    signed = verify is not None and verify.returncode == 0
    if verify is not None:
        reason = ((verify.stderr or verify.stdout).strip().splitlines() or ["no output"])[0]
        check(signed, "signature verifies",
              f"not signed, or the signature does not verify: {reason}")
    if signed:
        subject = next((l for l in verify.stdout.splitlines() if "certificate DN" in l), "")
        check("Android Debug" not in subject,
              f"release certificate ({subject.split(':', 1)[-1].strip()})",
              "signed with a debug key; it could not update a release install")

    badging = subprocess.run([build_tool("aapt2"), "dump", "badging", str(apk)],
                             capture_output=True, text=True).stdout
    check("application-debuggable" not in badging, "not debuggable", "APK is marked debuggable")

    print()
    if failures:
        print(f"{len(failures)} check(s) failed")
        return 1
    print("release APK OK")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    unsigned_ok = "--unsigned-ok" in args
    paths = [a for a in args if a != "--unsigned-ok"]
    if len(paths) != 1:
        sys.exit(__doc__)
    sys.exit(main(paths[0], unsigned_ok))
