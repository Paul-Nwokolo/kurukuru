# Security policy

## Reporting a vulnerability

**Please do not open a public issue.**

Report it privately through GitHub:
[**Report a vulnerability**](https://github.com/Paul-Nwokolo/kurukuru/security/advisories/new)
(also reachable from the repository's **Security** tab). That opens a private
advisory only you and the maintainer can see.

This is a personal project maintained by one person, so treat these as
intentions rather than guarantees: I aim to acknowledge a report within a week,
and to agree a disclosure timeline with you once the issue is understood. If you
have not heard back in two weeks, please assume it was missed and comment on the
advisory.

Please include what you would want if you were fixing it: what an attacker can
do, the conditions required, and the smallest reproduction you have.

## What is in scope

Kurukuru is a control plane for virtual machines on a single machine. It binds
to `127.0.0.1` by default and is designed for one user on one desktop. Reports
are in scope when they show something reachable **within that design**:

- A way to reach the API without authenticating, or to bypass the CSRF or
  console-ticket mechanisms.
- A way for a web page you merely visit to reach the local API — DNS rebinding,
  a CSP or `Host` validation gap, an exploitable CORS setting.
- Path traversal or command injection through any input the API accepts,
  including ISO paths, instance names and cloud-init data.
- Credentials, tokens or private keys written where another OS user can read
  them, or reported as protected when they are not.
- A guest escaping the isolation the documentation claims for it.

## What is out of scope

Not because these do not matter, but because they are properties of the design
rather than defects in it. They are documented in
[docs/SECURITY.md](docs/SECURITY.md), which is worth reading before reporting.

- **Any authenticated user can do anything.** Authentication answers "who are
  you"; there is no authorization layer, and every account is equal. Projects
  organise resources without isolating them. Role separation is on the roadmap.
- **Binding beyond loopback exposes everything.** `KURUKURU_HOST=0.0.0.0` puts
  the full API on your network on purpose. That is a supported configuration and
  a documented risk, not a vulnerability.
- **The local OS user is inside the trust boundary.** Anything running as you
  can read your token, exactly as it can read your SSH keys. Same-user
  compromise is not a boundary this tool defends, and neither does `chmod 600`.
- **SYSTEM and elevated administrators can read anything.** They can take
  ownership of any file. This is the Windows equivalent of `root`.
- **Guests are not sandboxed from each other beyond what QEMU provides.** A QEMU
  escape is a QEMU vulnerability; please report those upstream.

## Supported versions

Only the latest release. This project is at `0.x`: there are no maintenance
branches, and fixes ship in the next version rather than as backports.
