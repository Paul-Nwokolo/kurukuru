# tools

Standalone diagnostics and the release build, none of them imported by the
backend. The diagnostics exist because a specific mistake was made without
them; the build script exists because a release has to be reproducible by
somebody who is not its author. All of them live here rather than in a scratch
directory so they cannot drift or be lost.

## build_installer.py

Produces the shipped artefacts from a clean checkout: the built dashboard, the
frozen backend, and a trimmed QEMU bundle with a SHA-256 recorded for every
file.

```
python tools/build_installer.py --check-only          # pre-flight, fast, safe
python tools/build_installer.py --build-root C:/kk-build
```

Two of its checks are worth knowing about, because both guard failures that
have already happened here:

- **The build root is length-checked before anything is built.** Windows' 260
  character limit has broken this project three times, and the symptom is
  always an error naming a file in a dependency nobody was thinking about. The
  refusal carries the arithmetic — how long, what the budget is, how far over —
  because "use a shorter path" is not actionable on its own.
- **Every bundled QEMU file is hashed at collection and re-verified before
  packaging.** Upstream's Windows installer is signed with an expired
  certificate, so the signature is treated as absent; the manifest pins *what
  was tested* instead. It catches a changed byte, a truncation, a missing
  licence text, and a file that appeared without being recorded.

Run them with the backend virtualenv, which already has the one third-party
dependency they use (`pycdlib`, declared in `backend/requirements.txt`):

```
backend/.venv/Scripts/python.exe tools/<tool>.py --help     # Windows
backend/.venv/bin/python tools/<tool>.py --help             # Linux/macOS
```

Tests for both:

```
backend/.venv/Scripts/python.exe -m pytest tools/ -q
```

## `verify_media.py` — is this install ISO sound?

Checks a Windows ISO **along the boot path that will actually execute**, rather
than along the list of files that happen to carry a signature.

An earlier version passed a Windows 10 ISO whose installer could not boot. It
checked a volume label and Authenticode on four PE files — and on the legacy BIOS
path the engine actually uses, three of those four are UEFI-only and never
execute, while the fourth runs after WinPE is already up. The checked set and the
executing set did not intersect at all, so a green result carried no information.
See DECISIONS #34.

So this version labels every component with whether it **executes** on the
selected firmware path, computes its verdict only from those, hashes the
components that cannot be signed (`etfsboot.com` is raw real-mode code with no PE
header) instead of skipping them, and verifies `boot.wim`'s integrity table.

```
python tools/verify_media.py                              # every ISO in ~/.kurukuru/isos
python tools/verify_media.py <iso> --no-hash              # one, skip the slow whole-ISO SHA256
python tools/verify_media.py <iso> --firmware uefi        # judge the UEFI path instead
```

**It still cannot prove an ISO boots**, and it will still pass the known-bad
media, because nothing checkable about that ISO is wrong. What it gives you is
the `boot.wim` hash on the executed path — which is what actually differed
between the media that works and the media that does not.

Note: every piece of genuine Microsoft media tested here ships **without** a WIM
integrity table, so that check reports "not present" rather than passing. The
verification code is exercised by synthetic WIMs in the tests instead.

## `ab_measure.py` — did that change actually do anything?

An alternating-runs harness for guest-boot measurements.

The Windows host used for Phase 13 is **bimodal**: the same QEMU command line
either reaches the Windows Setup language screen in about 21 seconds or pegs both
vCPUs and reaches nothing at all. There is no middle outcome. Three separate
hypotheses were "confirmed" by single runs and later retracted; one configuration
failed three consecutive times and then two single-variable variants of it both
passed on the first try. See DECISIONS #40.

So this tool runs arms **alternating** rather than batched, records host free
memory per run, reports a per-run table in execution order plus a per-arm
distribution, and **refuses to offer a comparison** from fewer than three runs per
arm. It never emits a verdict — a human reads the distribution.

```
python tools/ab_measure.py --runs 3 --cap 240 \
    --iso ~/.kurukuru/isos/Windows10.iso \
    --arm "novnc:" --arm "vnc:-vnc;127.0.0.1:30"
```

Arm syntax is `name:arg;arg;arg` — a semicolon-separated argv fragment appended
to the base command, empty for the control arm. Semicolons rather than commas
because QEMU's own flags use commas *inside* a single argv token (`-device
qemu-xhci,id=xhci` is one token) — a comma-delimited arm spec cannot express
that without shredding it into two bogus tokens, which QEMU then rejects. The
milestone is "the framebuffer shows a rich GUI screen" (more than `--colours`
distinct colours, not predominantly black), which separates a Setup screen from
a boot logo without reading the screen; adjust `--colours` for other guests.

## `reset_measure.py` / `qmp_driver.py` — the upstream `system_reset` hang

Reproduces the QEMU/WHPX defect filed at <UPSTREAM ISSUE URL — fill in once
filed>: an in-process QMP `system_reset` leaves a Windows guest stuck at
SeaBIOS's boot prompt forever, while a fresh QEMU process against the same
disk always boots. See DECISIONS #55 and docs/WINDOWS.md for the full
measurement history (36/36 across every configuration tried, on two separate
QEMU builds) and `kurukuru/reboot_watchdog.py` for the workaround this defect
forced.

These two files exist here, not in host-local scratch like this project's
other one-off diagnostics, because a filed upstream report should not depend
on the exact reproduction still existing on whichever machine happened to find
it. **Delete both, and this entry, once the linked upstream issue is fixed**
and this project's minimum QEMU version moves past it — there is nothing else
to clean up.

```
python tools/reset_measure.py --iso <path to a Windows install ISO> --trials 5
```

No completed Windows install is needed — the hang lives at the firmware level,
not in guest OS state. Each trial boots a throwaway disk overlay against the
ISO, waits for a stable graphical frame (Setup's language screen), sends
`system_reset` directly over QMP, and reports whether a new graphical frame
ever appears. On a hang, it also launches a fresh process against the same
disk file to confirm the disk itself is fine — the same control that
separated this defect from a corrupted-disk theory in the original
investigation.

`qmp_driver.py` is the minimal QMP client and screendump/colour-count helpers
`reset_measure.py` needs; it duplicates rather than imports `ab_measure.py`'s
private `Qmp` class, in keeping with the rest of this directory being
independent, standalone scripts.
