# Phase briefs (historical)

These are the specifications the project was built from, kept because they
record *intent* — what was being attempted and why — which the code cannot.

**They are not documentation.** Where a brief and the code disagree, the code is
correct. Several briefs describe designs that were changed during
implementation, and at least one records a conclusion later proved wrong. For
current behaviour see [`../ARCHITECTURE.md`](../ARCHITECTURE.md),
[`../API.md`](../API.md) and [`../DECISIONS.md`](../DECISIONS.md).

| File | Phase | Status |
|---|---|---|
| [PHASE2_BRIEF.md](PHASE2_BRIEF.md) | Compute engine abstraction, Multipass driver | Implemented; the Multipass driver was later removed |
| [PHASE3_BRIEF.md](PHASE3_BRIEF.md) | React dashboard | Implemented; the launch flow was reworked in Phase 7 |
| [PHASE4_BRIEF.md](PHASE4_BRIEF.md) | cloud-init and SSH keys | Implemented |
| [PHASE5_QEMU_BRIEF.md](PHASE5_QEMU_BRIEF.md) | QemuEngine proof of concept | Implemented, with deviations (see below) |
| [PHASE6_BRIEF.md](PHASE6_BRIEF.md) | Console, ISO boot, image import | Implemented, with a significant correction (see below) |
| [PHASE7_BRIEF.md](PHASE7_BRIEF.md) | Custom resources, intent-framed launch, cleanups | Implemented |
| [ROADMAP_v2.md](ROADMAP_v2.md) | Strategic direction | Current as of the Multipass retirement |

## Never implemented

- **PHASE5_BRIEF.md** — an earlier Phase 5 covering Multipass golden images. It
  was superseded by `PHASE5_QEMU_BRIEF.md` before any of it was built, and the
  file is not in this repository.
- **ROADMAP.md (v1)** — superseded by `ROADMAP_v2.md`; also not present here.

Both are referenced in older briefs. Those references are dead.

## Known divergences between the briefs and the code

Worth knowing before reading a brief as if it described the system:

- **Phase 5 specified `-cpu max`.** WHPX dies instantly with
  "Unexpected VP exit code 4" on `max` or `host`. The code selects `qemu64`
  under WHPX and `max` under software emulation.
- **Phase 5 specified two files in the NoCloud seed.** The code writes three;
  the extra `network-config` works around QEMU's user-mode DNS proxy, which
  answers NXDOMAIN for everything on this host.
- **Phase 6 concluded that WHPX renders no display at all**, and forced ISO
  instances onto software emulation as a result. That conclusion was wrong — it
  came from testing display devices with no guest OS booted. Only VGA *text
  mode* is affected. The rule was removed in Phase 7 and the reasoning is in
  [decision 5](../DECISIONS.md).
- **Phase 6's ISO sub-text said "Slower to run".** No longer true, and no longer
  in the UI.
- **Phase 7 asked for a nullable `flavor` column.** SQLite cannot drop `NOT
  NULL` without a table rebuild, so the column stays non-null and stores
  `"custom"` for hand-sized instances.
