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
| [PHASE8_CLI_BRIEF.md](PHASE8_CLI_BRIEF.md) | Command-line interface | Implemented |
| [PHASE9_LINUX_BRIEF.md](PHASE9_LINUX_BRIEF.md) | Linux support | Implemented; the Linux host it was validated on no longer exists |
| [PHASE10_BRIEF.md](PHASE10_BRIEF.md) | Key pairs, user-data, snapshots, instance detail | Implemented |
| [PHASE11_BRIEF.md](PHASE11_BRIEF.md) | Event log, projects, volumes, networks | Implemented |
| [PHASE12_BRIEF.md](PHASE12_BRIEF.md) | Design system, themes, structure | Implemented; its naming and wordmark were superseded by Phase 16 (see below) |
| [PHASE13_WINDOWS_BRIEF.md](PHASE13_WINDOWS_BRIEF.md) | Windows guest support | **Partially implemented.** Its completion criterion was never met (see below) |
| [PHASE14_BRIEF.md](PHASE14_BRIEF.md) | Carried items and hardening | Implemented |
| [PHASE15_AUTH_BRIEF.md](PHASE15_AUTH_BRIEF.md) | Authentication | Implemented |
| [PHASE16_PACKAGING_BRIEF.md](PHASE16_PACKAGING_BRIEF.md) | Rebrand and packaging, Windows first | Implemented; produced the name, the mark and the installer |
| [DOCS_BRIEF.md](DOCS_BRIEF.md) | Documentation and consolidation pass | Implemented; this directory is its output |
| [frontend_review.md](frontend_review.md) | External review, dashboard | Every finding addressed before 0.1.0 (see below) |
| [backend_review.md](backend_review.md) | External review, backend | Every finding addressed before 0.1.0 (see below) |
| [ROADMAP_v2.md](ROADMAP_v2.md) | Strategic direction | Current as of the Multipass retirement |

## External reviews

Two point-in-time reviews from the run-up to 0.1.0. They are kept for the same
reason the briefs are — they record what was found and why it mattered — and
they are historical for the same reason. **Every defect in both was fixed before
release.** Read them as a record of what was corrected, not as a list of what is
wrong with the code today.

- **[frontend_review.md](frontend_review.md)** — keyboard access, responsive
  layout and ARIA gaps. Fixed in `cb32cbe` (merged `39b0058`) and `17560ab`
  (merged `91f06db`). Two of its recommendations were deliberately *not* taken,
  and the reasoning is written down so they are not "fixed" later: a destructive
  dialog keeps Cancel as the initial focus, and the host capacity bar is a
  `meter` rather than a `progressbar`. See decisions 60 and 61 in
  [`../DECISIONS.md`](../DECISIONS.md).
- **[backend_review.md](backend_review.md)** — settings captured at import time,
  a route reading a module-level snapshot, and a test that passed while
  asserting nothing. Fixed in `f352a35` (merged `22ae8d4`), with the sweep that
  looked for the same false-green shape elsewhere merged in `764486d`. The
  underlying pattern became rule 5 of the "Verification integrity" section of
  [`../../CONTRIBUTING.md`](../../CONTRIBUTING.md).

  This review also made three feature suggestions. Those were **not** built:
  they are recorded under "Considered, not scheduled" in
  [`../ROADMAP.md`](../ROADMAP.md) with the reasoning, which is a different
  outcome from the defects above and is worth not conflating.

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
- **Phase 12 deliberately kept the placeholder name.** It said the product name
  was not decided and told the reader not to invent one: "Keep 'Local IaaS
  Orchestrator' and make the lockup easy to change." Phase 16 named it Kurukuru
  and replaced the placeholder wordmark with the current mark. Everything in
  Phase 12 about naming, the lockup or the wordmark is superseded; its colour,
  theme and spacing work is still what the dashboard uses.
- **Phase 13 set a completion criterion that was never met.** It asked for a
  Windows guest that boots the ISO, has the installer see the disk, completes,
  and reboots into the installed system. No Windows install has ever completed
  on this host. It also asked for an honest assessment of whether Windows 11
  was reachable; the answer turned out to be no, permanently, on a Windows
  host — QEMU excludes TPM 2.0 there at build time. Read the brief as the
  question, not the answer; the answer is in the README's "Honest limitations".
