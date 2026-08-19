# tools

Standalone diagnostics. Neither is imported by the backend, and neither is
needed to run the orchestrator — but both exist because a specific mistake was
made without them, and both are here rather than in a scratch directory so they
cannot drift or be lost.

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
python tools/verify_media.py                              # every ISO in ~/.local-iaas/isos
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
    --iso ~/.local-iaas/isos/Windows10.iso \
    --arm "novnc:" --arm "vnc:-vnc,127.0.0.1:30"
```

Arm syntax is `name:arg,arg,arg` — a comma-separated argv fragment appended to the
base command, empty for the control arm. The milestone is "the framebuffer shows
a rich GUI screen" (more than `--colours` distinct colours, not predominantly
black), which separates a Setup screen from a boot logo without reading the
screen; adjust `--colours` for other guests.
