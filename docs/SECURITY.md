# Security

What this system protects, what it does not, and how to recover when something
goes wrong. Read the second section before you bind it to anything but
localhost.

---

## The threat model, in one paragraph

This is a control plane for virtual machines on one machine. Anything that can
authenticate to it can create, destroy and take a console into every VM it
manages. **Authentication answers "who are you". There is no authorization** —
every account is equal, and projects organise resources without isolating them.
The threat this phase addresses is *an unauthenticated caller reaching the API*:
another process on the host, a page open in your browser, or anything on the
network if you have bound it beyond loopback.

### The clearest statement of the boundary

The first account is created by `kurukuru auth init`, on the host, writing directly
to the database — not through the API. That choice is the threat model in
miniature, so it is worth stating plainly:

> Creating the first account over HTTP would need a public, state-changing route
> — one that anybody who can reach the port may call, exactly once, to become
> the owner of every VM on the machine. Requiring filesystem access to the state
> directory instead is **strictly stronger**, because that directory already
> holds the orchestrator's SSH private key and every VM disk. Anyone who can
> read it has already won; anyone who cannot is not helped by an HTTP route.

The same reasoning governs `kurukuru auth reset-password`, which additionally cannot
require a credential because it is the recovery path for someone who has lost
one.

**The general rule this expresses:** a control plane cannot protect the thing it
is built on top of. Nothing here defends against an attacker who can read the
state directory, run code as your user, or elevate. What it defends against is
everything that can talk to the port without those.

---

## What is protected

**Every route is closed by default.** Three are public, each for a stated
reason: `/health` (liveness, and it deliberately reports nothing about
instances), `/auth/login`, and `/auth/first-run` (whether any account exists,
which an install with none cannot hide anyway, plus the name of the CLI so the
login screen can name the command to run without spelling it). The guard is applied to the
application, not to each router, and `tests/test_auth_coverage.py` enumerates
the routes the app actually registered and fails if any answers anything but
401 to an anonymous caller. That test found four open routes on its first run —
FastAPI's own `/docs`, `/redoc`, `/openapi.json` and `/docs/oauth2-redirect`,
which are registered where application dependencies do not reach.

**Passwords** are hashed with argon2id. There is no default credential and no
way to create one.

**Sessions** are server-side rows; the cookie is an opaque secret, `httpOnly`
and `SameSite=Strict`. Logging out deletes the row rather than only clearing the
cookie.

**API tokens** are shown once and stored hashed. Revocation takes effect on the
next request.

**Changing the password invalidates everything** — every session, every API
token, every outstanding console ticket, including the session that made the
change. That is deliberate: you change a password when you believe something
leaked, and a flow that leaves the current session alive cannot tell you from
whoever else had it.

**The console** is authorised by a single-use ticket, minted over an
authenticated request and redeemed once on connect. It is bound to the instance,
to the session that minted it, and to the password version — so a ticket cannot
open a different VM, cannot be replayed, and dies on logout or password change.

**Login is rate-limited**, failures are generic (an unknown username and a wrong
password are indistinguishable, in message and in timing), and comparisons are
constant-time.

### CSRF, and why `SameSite` is not enough

`SameSite` is computed from scheme and registrable domain. **Port is not part of
a site.** So `http://localhost:9999` is the *same site* as `http://localhost:7842`,
and a cookie set by this backend is sent with requests originating from a page on
any other local port — a stale dev server, a docs preview, a package's build
tool.

Measured in a browser rather than assumed: a page on one localhost port logged
in, a page on another submitted a form POST, and the API answered **403, not
401**. 401 would have meant the cookie was never sent. 403 means it was sent and
the CSRF token refused it.

So every state-changing request authenticated by **cookie** must carry the
`X-Kurukuru-CSRF` header. Requests authenticated by **Bearer token** are exempt, and
that is not a gap: a browser cannot be induced to attach an `Authorization`
header to a cross-origin request, so the header's presence is itself evidence of
intent.

CORS allows only exact origins. There is deliberately no origin-regex escape
hatch — with credentials enabled it would make any page on any local port a
fully authenticated client.

---

## The browser is a client you did not choose

Binding `127.0.0.1` keeps other machines out. It does not keep *pages* out. A
browser will send a request to `127.0.0.1:7842` from any site the user happens
to have open, and it will attach the session cookie while doing it — so in
practice every page on the internet is a client of this server, and that, not
the network, is where the interesting attacks are.

Four things address it.

**`X-Frame-Options: DENY`.** Without it a hostile page can load the dashboard in
an invisible iframe, position it under something the user is about to click, and
have that click land on a real, authenticated control. There is no legitimate
reason to frame this application.

**A Content-Security-Policy**, with `frame-ancestors 'none'`, `object-src
'none'`, `base-uri 'none'`, and script limited to same-origin plus one hash. The
hash is computed at startup from the `index.html` actually being served, not
baked into a constant: `index.html` carries one deliberate inline script — the
theme bootstrap, which must be inline and blocking so the page does not paint
the wrong theme and repaint — and a policy that forgot to hash it would
reintroduce exactly the flash that script exists to prevent. Deriving it from
the file means the two cannot drift.

**`X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`.** The
second is stricter than the usual `strict-origin-when-cross-origin` on purpose:
dashboard URLs carry instance UUIDs and no outbound navigation has any business
carrying one.

**Host header validation.** This is the one that is easy to miss. Session
cookies plus per-session CSRF tokens stop an ordinary cross-site request — the
attacker's page cannot read the token. *DNS rebinding* is the version that
defeats that: the attacker publishes a domain that resolves to `127.0.0.1`, at
which point their page is **same-origin** with this server and can simply read
the token before using it. The `Host` header is the one field that still records
where the browser thought it was going, and a rebound request carries the
attacker's name in it. Requests whose `Host` is not the bound address or a
loopback name are refused with a 400. The allowed list is derived from the bind
address, so binding a LAN address deliberately still works.

### Known, deferred

**A quadratic `Range`-header parse in Starlette's `FileResponse`**
(PYSEC-2026-1942). Reachable unauthenticated through the dashboard's asset
route; the worst case is CPU burn on the machine already running the server, by
a page the user visited. It is not fixed because it cannot be: FastAPI
0.115.12 requires `starlette<0.47.0` and the fix landed in 0.49.1, so it is on
the far side of a FastAPI major upgrade. That upgrade is the first task of
0.2.0 — see the README's deferred gaps.

Two other Starlette advisories were assessed and do not apply: the `HTTPEndpoint`
method-lookup issue (FastAPI does not use `HTTPEndpoint`) and the `StaticFiles`
UNC SSRF on Windows (the dashboard is served by this project's own handler).
That second one is worth reading twice, because the same mistake *was* present
here — in the dashboard's asset resolver and in the ISO resolver, both of which
resolved a joined path before checking it was inside the root. On Windows that
turns a request path naming a UNC share into an outbound SMB connection to a
host the caller chose, from an unauthenticated route. Both now use
`kurukuru.safe_paths.resolve_within`, which decides containment before touching
the filesystem.

## What is *not* protected

Be direct with yourself about these before exposing the port.

**No transport encryption.** Everything is plain HTTP. Passwords, session
cookies, API tokens and every VM's framebuffer cross the wire in the clear. On
loopback that is acceptable. On anything else it is not, and the fix is a
reverse proxy terminating TLS in front of it — this product does not do it for
you.

**No authorization, no isolation.** Every account can do everything. Projects
are labels, not boundaries. A second account is a second person with full
control, not a restricted user.

**No protection from your own machine's processes.** A process running as you
can read the CLI token file, the SSH private key and the database. This is the
same boundary POSIX `0600` gives, and it is not a flaw in the file permissions —
it is what "same user" means.

**No audit of who did what.** The event log records what happened, not which
account did it. The `actor` field distinguishes API from reconciler, not person
from person.

**No account lockout policy beyond a rate limit**, no password expiry, no MFA,
no SSO.

### The CLI token file

`kurukuru auth login` stores an API token at `~/.kurukuru/cli-token`
(`KURUKURU_AUTH_TOKEN_FILE`), locked to your OS user. What that is worth, measured
rather than assumed:

| | |
|---|---|
| Other OS users, including non-elevated administrators | **cannot** read it |
| Processes running as you | **can** — and could re-grant themselves anyway, since owners hold `WRITE_DAC` |
| SYSTEM, or an elevated administrator | **can**, by taking ownership |

That is exactly the POSIX `0600` boundary. Note that `os.chmod` does **not**
provide it on Windows — on NTFS it leaves the file's access control list
byte-identical — so the file is locked with an explicit ACL instead.

**Access control lists are an NTFS/ReFS feature.** If you point `KURUKURU_STATE_DIR`
at FAT32 or exFAT — a USB stick, an SD card — there is no ACL to set and the
token is readable by anyone with the disk. The CLI checks the filesystem before
believing the operation worked and warns loudly rather than reporting success.

The token in that file is an ordinary API token. If it leaks, revoke it:
`kurukuru auth token ls`, then `kurukuru auth token rm <name>`.

---

## Binding to something other than localhost

The default binds to `127.0.0.1`, and guest-facing sockets (VNC, QMP, the SSH
port forward) are bound to loopback too, with a test guarding it.

If you bind the API elsewhere, understand that you are exposing plain HTTP
carrying credentials and VM framebuffers. At minimum:

1. Put a reverse proxy in front of it and terminate TLS there.
2. Restrict who can reach the port at the network layer.
3. Set a long, unique password. Rate limiting slows an attacker; it does not
   stop one who has time.
4. Remember there is no authorization: every account you create is a full
   administrator of every VM.

If you only need the dashboard from another machine occasionally, an SSH tunnel
to loopback is a better answer than binding wider.

---

## Recovering

### A forgotten password

On the machine hosting the API:

```
kurukuru auth reset-password
```

It prompts for a new password, and invalidates every session, API token and
console ticket — including this machine's stored token. Run `kurukuru auth login`
afterwards.

This requires filesystem access to the state directory, which is the point: see
the threat model above.

### A leaked API token

```
kurukuru auth token ls
kurukuru auth token rm <name-or-prefix>
```

Revocation takes effect on the next request. If you do not know which token
leaked, change the password — that invalidates all of them at once.

### A leaked password

Change it: Settings → Account in the dashboard, or `kurukuru auth reset-password` on
the host. Everything issued under the old password stops working immediately.

### Locked out because no account exists

A backend with no account answers 401 to everything except `/health` and the
login routes, and logs the command to run at startup. Create the owner account:

```
kurukuru auth init
```

The password is prompted for, never taken as a flag — a flag lands in shell
history, which is the leak
[CONTRIBUTING](../CONTRIBUTING.md#credentials-never-go-in-a-tool-config-file-ignored-or-not)
documents.

---

## Reporting a problem

This is a personal project with no security contact and no disclosure process.
If you find something, open an issue — and if it is exploitable, say so without
a working exploit in the text.
