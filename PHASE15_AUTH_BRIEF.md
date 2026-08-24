# PHASE 15 BRIEF — Authentication

Read docs/ARCHITECTURE.md and docs/DECISIONS.md first.

Today the API has no authentication. Anything that can reach port 8000 can
launch VMs, read the SSH key path, open a console, and delete instances.
That is defensible for a localhost-only development tool and indefensible
for anything else — and the README already says so under known
limitations. This phase fixes it.

## Scope: authentication, not authorization

Build **who are you**. Do NOT build **what are you allowed to do**.

- One owner account, created on first run. Optionally additional accounts,
  but every account is equal — no roles, no permissions matrix, no teams.
- Projects stay organisational. They do NOT become an isolation boundary
  in this phase, and nothing in the UI or docs may imply they have.

Roles, teams, per-project permissions and audit-by-actor are deliberately
out of scope: they are a coherent later body of work, and half-building
them now produces a permissions system that enforces nothing. Design so
they can be added without rework — an `actor` concept threaded through
events, a user id on resources — but do not build the enforcement.

## The hard design question — bring me a recommendation first

A local-first tool that demands a login every time is worse for its
primary user; a tool that ships unauthenticated is dangerous the moment
someone binds it to 0.0.0.0. Assess and recommend, with reasoning:

- **Always require auth**, including on localhost. Simplest to reason
  about, one code path, no "am I safe here?" logic. Costs the
  single-user-on-a-laptop case some friction.
- **Bind-dependent**: localhost-only listeners trust the local user;
  requiring auth as soon as the bind address is non-loopback. This is
  roughly what several local dev tools do. It has a real hole — any
  process or browser page on the same machine is "local".
- **Always require auth, but make the local case frictionless**: a token
  written to a file readable only by the owning OS user, which the CLI
  reads automatically and the dashboard bootstraps from. Auth is always
  on; the local user simply already has the credential.

My prior is the third. It has one code path, no trust-by-topology, and
the local experience stays close to what it is today. But it depends on
file permissions meaning something — and this codebase has already
documented that `os.chmod` is a no-op for real access control on NTFS.
That caveat needs answering, not noting: if the token file cannot be
protected on Windows, the option is weaker than it looks. Measure what
Windows ACLs actually give you here before recommending.

## Requirements once the model is decided

### Accounts and credentials
- Password hashing with a modern KDF (argon2id preferred, bcrypt
  acceptable). Never a bare hash, never a homemade scheme.
- First-run: create the owner account. Decide and justify how — an
  interactive `<cli> init`, a generated password printed once to the
  console on first start, or a setup screen reachable only from
  localhost. Whichever you choose, a default credential baked into the
  product is not an option.
- Password change. Password reset is a documented manual procedure
  (a CLI command run on the host), not an email flow — there is no mail
  infrastructure and inventing one is out of scope.

### Sessions and tokens
- Browser: httpOnly, SameSite cookies. If the API and dashboard are
  same-origin in production (they will be after packaging) say so and
  keep it simple.
- CLI and scripts: long-lived API tokens, listable and revocable, shown
  once at creation and stored hashed. `<cli> auth login`, `auth logout`,
  `auth token create|ls|rm`, `auth whoami`.
- Token in `Authorization: Bearer`. Never in a query string — the
  existing rule about not putting secrets on command lines applies to
  URLs too.

### Every entry point, without exception
Enumerate them from the code rather than from memory, and cover:
- All REST routes except health and whatever the login flow needs.
- **The WebSocket console.** This is the sharpest one: it is currently an
  unauthenticated path to a VM's framebuffer and keyboard. Browsers
  cannot set headers on a WebSocket handshake, so this needs a deliberate
  design — a short-lived single-use ticket issued over authenticated
  HTTP and redeemed on connect is the usual answer. Recommend and
  justify.
- The CLI, including `serve`, `doctor` and `capacity`.
- Anything that serves the built frontend after packaging.

### Security properties to get right
- Rate-limit login attempts. A local tool is not exempt: the threat is a
  malicious page or process on the same machine, not a botnet.
- Constant-time credential comparison.
- Generic failure messages — never distinguish "no such user" from "wrong
  password".
- Session invalidation on password change.
- CSRF: cookies plus a state-changing API needs either SameSite=Strict
  and same-origin, or a token. State which you rely on and why it holds.
- The `cors_origin_regex` escape hatch from Phase 10 interacts with this.
  Re-read that decision and confirm it is still safe, or narrow it.

### Migration and the existing install
- An existing install must not become unreachable. On upgrade, detect no
  accounts and run the first-run flow rather than locking the user out.
- Existing data has no owner. Decide what that means and keep it simple —
  attributing everything to the owner account is probably right.

## Frontend
- Login screen consistent with the design system, both themes.
- Session expiry handled gracefully — redirect to login, return to where
  they were, never a blank page or a silent failure.
- Account section in Settings: change password, manage API tokens.
- The health/backend-status indicator must distinguish "backend down"
  from "not authenticated". They look identical to a naive client and
  mean completely different things.

## Documentation
- README: authentication as a feature, and the known-limitations entry
  about having none must go.
- `docs/SECURITY.md`: the threat model this actually addresses, what it
  does not (no transport encryption unless the user terminates TLS
  themselves; no per-project isolation; no roles), how to reset a
  password, and honest guidance on binding to a non-loopback address.
- DECISIONS entries for the auth model, the console ticket mechanism, and
  the first-run flow.

## Testing
- Every route rejects unauthenticated access — test this by enumerating
  the app's routes programmatically and asserting each is covered, so a
  route added later fails the test rather than shipping open. This is the
  single most valuable test in the phase.
- The console WebSocket rejects an unauthenticated connect, a ticket for
  a different instance, and a reused ticket.
- Rate limiting fires; lockout releases.
- Token creation, use, revocation, and that a revoked token stops working
  immediately.
- Session invalidation on password change.
- Full existing suite green — every test that hits the API now needs
  authenticating, which is a large mechanical change; do it via a fixture
  rather than per-test.
- Live: log in through the browser, use the CLI with a token, open a
  console, and confirm an unauthenticated curl gets 401 on a
  representative sample of routes.

## Report
The auth-model recommendation and its evidence — including what Windows
ACLs actually provide for the token file — before implementing. Then per
area: what shipped, live evidence, what you deliberately left out.

## Out of scope
Roles, teams, per-project permissions, SSO/OAuth, TLS termination,
multi-host, packaging.
