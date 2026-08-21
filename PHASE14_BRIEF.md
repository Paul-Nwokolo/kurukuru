# PHASE 14 BRIEF — Carried Items & Hardening

No new user-facing features. This phase closes the backlog that accumulated
across phases 10–13 and hardens what exists, so that the packaging phase
starts from a clean base rather than a list of asterisks.

Read docs/DECISIONS.md first — several items here are open questions
recorded there, not tasks with obvious answers. Where an item needs a
decision rather than code, bring it back rather than choosing silently.

Each part is independent. Do them in order, verify each, and commit per
part rather than in one lump — this is exactly the kind of phase where a
single large commit hides regressions.

---

## PART A — Migration safety

Recorded as review item #6 and deferred pending the state-dir change,
which has since landed.

- Take an automatic backup before any additive migration runs: the .db
  plus its -wal and -shm as a consistent set. Prefer SQLite's online
  backup API over a file copy — a live backend may be mid-write, and
  Phase 13's manual backup demonstrated the difference.
- Retention: keep the last N (setting, default 5), prune older ones.
  Backups live under state_dir, not beside the database.
- Skip the backup when the migration is a no-op (nothing to add or
  redefine) so ordinary startups don't accumulate identical copies.
- Restore is a documented manual procedure, not an API. Write it in
  CONTRIBUTING: which file to use, why the online-backup snapshot is the
  one to prefer, and how to verify row counts after.
- Test: a migration on a database with rows produces a backup that opens
  and reports identical counts; a no-op startup produces none; retention
  prunes correctly.

## PART B — UEFI: decide, then act

Decision 35 logged the full wiring; `_uefi_capability` currently reports
"detected, not wired into launches". Phase 13 measured that NVRAM
persists correctly with a per-instance varstore, so the mechanism works.

The open design question, unanswered: **a UEFI instance's NVRAM is not
covered by a qcow2 snapshot, so restoring a snapshot desyncs the boot
entries from the disk.** Three options were stated and none chosen:
snapshot the varstore alongside, refuse snapshots on UEFI instances, or
allow the desync and warn.

Bring me a recommendation with reasoning BEFORE implementing. My prior is
that snapshotting the varstore alongside the overlay is the only option
that doesn't either cripple a feature or ship a known footgun — but it
means snapshot becomes a two-file operation with its own partial-failure
mode, which is exactly the sort of thing that needs thinking through
rather than assuming.

Once decided, the wiring itself is as scoped in decision 35: firmware +
nvram_path on InstanceRuntime, firmware on LaunchOptions, per-instance
OVMF_VARS.fd copy (never a shared template — it would leak one VM's boot
entries into another), the pflash pair in build_launch_command,
boot_cloned_instance carrying it through, DB column and migration,
InstanceCreate field, API refusal when unavailable, CLI flag and UI
control. Legacy BIOS stays the default.

## PART C — Volume snapshots

Deliberately not built in Phase 11: a volume is a plain qcow2 and
qemu-img would work on it, but it needs its own lifecycle and confirm
semantics rather than being folded into instance snapshots.

- Snapshot/restore/delete for volumes, stopped-and-detached only (or
  stopped-instance if attached — decide which is safer and say why).
- The existing instance-snapshot warning names attached volumes as NOT
  covered. That text must stay accurate: an instance snapshot still does
  not cover volumes, and a volume snapshot does not cover the instance.
  Two independent things, said plainly in both dialogs.
- Reuse the existing Snapshot table if it fits cleanly; add a separate
  one if the shared shape would force nullable columns that only make
  sense for one kind. Recommend rather than assuming.
- CLI: `iaas volumes snapshot create|ls|restore|rm`.

## PART D — Reconcile affordance

Review item #12: the reconcile button is honest afterwards, but during
the ~5s wait there is little affordance, and the 3s list poll keeps
running underneath so the table may update for unrelated reasons
mid-operation.

- Make the in-progress state unmistakable without a spinner that implies
  more than it knows.
- Consider suppressing or coalescing the background poll while a manual
  reconcile is in flight, so the user can attribute what they see.
- Small, but it is the control people press when something looks wrong,
  which is exactly when ambiguity is most expensive.

## PART E — QMP robustness follow-through

Phase 13 established the three-state liveness (pid alive + QMP answers /
pid alive + QMP unreachable / pid dead) and made the unreachable case a
logged warning rather than a silent flap. Finish the job:

- Surface "QMP unreachable but process alive" in the UI as a
  distinguishable state, not as healthy. This is the condition that
  produced 40 minutes of Running↔Stopped flapping; a user seeing it
  should know what it means.
- Document in ARCHITECTURE that QEMU's QMP chardev accepts one client, so
  any second consumer (tooling, a second backend process, a debugging
  session) will make the reconciler see an unreachable socket. That is a
  real operational constraint and it is currently only in a commit
  message.

## PART F — Housekeeping

- The roadmap note describing the reconcile result as "a sentence that
  disappears" is now only true of success, since errors persist. One-line
  fix, flagged in Phase 12 and never made.
- Sweep DECISIONS for any remaining citation that points at the wrong
  entry after the Phase 13 renumbering.
- README "not built" list: re-read it against what now exists. It has
  been wrong twice.
- Confirm no TODO/FIXME markers have crept back into backend/app,
  frontend/src, or tools/.

---

## Cross-cutting
- Migrations additive via init_db; the real database converges with all
  rows intact, tested as test_migrations.py does — and now with Part A's
  backup taken automatically.
- Commit per part, on a branch, pushed.
- All suites green; exit codes read directly, never through a pipe.
- Live verification for anything touching the hypervisor or the database.

## Report
Per part: what shipped, live evidence, and — for Parts B and C — the
recommendation and its reasoning before the code. Flag anything you found
that belongs in a later phase rather than fixing it here.

## Out of scope
Auth and multi-user (next phase), Windows guest work (blocked on
hardware), packaging, the product name, anything requiring a
non-bimodal host.
