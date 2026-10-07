# PHASE 17 BRIEF — The Starlette upgrade

Read docs/DECISIONS.md and CONTRIBUTING.md first — in particular the
verification-integrity section, which is the standard this phase is held
to like any other.

This is ROADMAP 0.2.0 §1, deferred twice: once at the 0.1.0 release and
again at 0.1.2. The reasoning each time was sound — doing a framework
major-version bump during release week is how a release breaks. That
reason has expired. Nine advisories have been shipping in every release
since the first one.

## What is actually being asked for

The audit found nine pip advisories, none reachable without breaking a
pin. The two that matter are fixed only in Starlette 1.x, which sits
behind a FastAPI major-version upgrade — FastAPI 0.115.12 requires
starlette<0.47.0, and the lowest fix is 0.47.2.

So this is not "bump a dependency". It is a framework upgrade with a
security motivation, and the honest sequence is:

1. Re-run the audit. The advisory set is months old. Some may have been
   resolved upstream, others added. Report the current list by severity
   and reachability before deciding what the upgrade has to achieve.
2. Read FastAPI's and Starlette's migration notes for every version
   crossed, not just the endpoints. Report what changes for us
   specifically — lifespan, dependency injection, WebSockets,
   TestClient, static files, middleware ordering.
3. Then upgrade.

If the re-run shows the reachable advisories are fixed below a major
bump, say so and take the smaller change. Do not do a major upgrade for
its own sake.

## Where this project will feel it

These are the surfaces most likely to break, named so they get tested
rather than discovered:

- **The WebSocket console.** `/instances/{id}/console` bridges a browser
  WebSocket to QEMU's VNC socket. Starlette owns that WebSocket. The
  console handshake regression test exists precisely because a change
  here once broke silently — run it, and prove it still fails against a
  deliberate break.
- **TestClient.** Hundreds of tests use it. A behaviour change here
  shows up as mass failures, which is the easy case, or as tests that
  still pass while asserting the wrong thing, which is not.
- **The Host header validation** added in Phase 15 (TrustedHostMiddleware)
  and the CORS configuration, both Starlette middleware.
- **Static file serving and the SPA fallback**, which the dashboard
  depends on in the packaged shape.
- **The CSRF defence**, which rides on cookie and header handling.
  Decision 45 established by browser measurement that SameSite does not
  protect a loopback server from other local ports. Re-run that
  measurement after the upgrade rather than assuming it holds — it is
  the one security property with no framework-level guarantee behind it.
- **The route enumeration test**, which asserts every registered route
  requires authentication. It found four open routes on its first run
  because FastAPI registers `/docs` and friends directly on the router.
  A framework upgrade can change exactly that, so re-prove it fires.

## Requirements

- Pin the new versions explicitly, the way the current ones are pinned.
  No floating ranges.
- `pip-audit` must be clean, or every remaining advisory must be
  recorded with why it is not reachable.
- The full suite green, plus the frontend verify chain.
- CI green on the clean Windows runner — not just locally. The last two
  releases were saved by that distinction.
- Record the upgrade in DECISIONS: what moved, what broke, and what the
  advisories were. If anything had to change in our code to accommodate
  it, say what and why.

## Verification beyond the suite

A passing suite is necessary and not sufficient here, for the reason
this project keeps relearning. Do these live:

1. Open the browser console on a running instance and confirm a live
   framebuffer and working keyboard.
2. Sign in through the dashboard, change the password, confirm
   invalidation still bounces you to login.
3. The cross-port CSRF measurement from decision 45, in a real browser.
4. `kurukuru launch web --wait && kurukuru ssh web whoami` end to end.
5. Install the built artefact and reach the dashboard — the packaged
   shape is where static serving and the SPA fallback actually matter.

## Release

If it lands clean, cut 0.1.4 from CI with the usual discipline: icon
check, checksum, anonymous download-and-hash, release notes leading with
the security motivation and naming the advisories closed.

If the upgrade turns out to be larger than a contained change — a
migration that touches many files or changes behaviour we depend on —
stop and report rather than pushing through. A half-migrated framework
is worse than nine unreachable advisories.

## Out of scope
Linux packaging, new features, signing, anything on the 0.2.0 list
beyond §1.
