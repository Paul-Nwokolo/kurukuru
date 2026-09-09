## What this changes

<!-- The behaviour that differs, not the files touched. -->

## Why

<!-- The problem. Link an issue if there is one. -->

## How it was verified

<!--
CONTRIBUTING.md has a section called "Verification integrity" that is the
main thing asked of a change here. In short: say what you actually ran and
what it printed, not what you expect it to do.

If the change adds a guard, say how you proved it fires — the usual way is to
break the thing on purpose and watch it fail.
-->

## Checklist

- [ ] Tests pass locally (`pytest backend/tests` and, for dashboard changes, `npm run verify`)
- [ ] New behaviour has a test that fails without the change
- [ ] Docs updated if behaviour or configuration changed
- [ ] A decision record added to `docs/DECISIONS.md` if this closes off an alternative
