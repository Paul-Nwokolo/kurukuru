# SignPath Foundation application — draft

**Status: draft. Nothing has been submitted and nothing has been bought.**

This is the text to send, plus the two things that must be true first and the
one question to ask them up front rather than discover later.

---

## Before submitting

1. **The build must run in CI.** SignPath Foundation requires that "binary
   artifacts must be built from source code in a verifiable way", with
   provenance over the repository, branch and build agent. A laptop build does
   not qualify. `.github/workflows/release.yml` does this; it needs at least
   one green run on a tag before the application is credible.
2. **Ask about the bundled QEMU before relying on the answer.** Their terms
   permit including unsigned upstream OSS binaries in a signed package, and
   separately forbid signing binaries that are not yours. Kurukuru is the
   first of those and not the second — but it bundles 120 files it did not
   build, which is unusual enough to name in the application rather than have
   raised in review. The draft below does that in its own section.

---

## Why this is worth applying for

Kurukuru is blocked by **Smart App Control**, not merely warned about by
SmartScreen. On a clean Windows 11 machine, Windows refuses to load the
bundled QEMU and the product does not run at all. The status code is
`0xC0E90002`, `STATUS_SYSTEM_INTEGRITY_POLICY_VIOLATION`. The only workaround
available to a user today is turning Smart App Control off machine-wide, which
is a bad thing to ask of somebody installing a hobby tool.

Signing the installer is expected to fix this **without** signing QEMU:
Microsoft documents reputation inheriting from a trusted installer to the
files it writes, recorded as a `$KERNEL.SMARTLOCKER.ORIGINCLAIM` attribute. It
is not instant — reputation still has to accrue — but it is the mechanism, and
it is why "sign the installer, ship QEMU unsigned inside it" is the right
shape.

---

## Draft application text

> **Project:** Kurukuru — local cloud infrastructure
> **Repository:** https://github.com/Paul-Nwokolo/kurukuru
> **Licence:** Apache-2.0
> **Maintainer:** Paul Nwokolo (sole maintainer, owns the repository)
>
> **What it is.** Kurukuru launches and manages QEMU virtual machines on one
> Windows machine, through an HTTP API, a web dashboard and a CLI. It is for
> people who want EC2-shaped workflows locally without a cloud account. One
> machine, one signed-in user, no remote mode, no multi-tenancy.
>
> **What we would like signed.** The Windows installer
> (`Kurukuru-<version>-Setup.exe`) and the executables inside it that we
> build: `kurukuru.exe` (a PyInstaller freeze of our own Python source) and
> the uninstaller Inno Setup generates.
>
> **Builds.** GitHub Actions, `.github/workflows/release.yml`, on
> `windows-latest`, triggered by a version tag. Every input is pinned: the
> Python dependencies, PyInstaller, and QEMU — whose upstream installer is
> downloaded by URL and verified against a SHA-256 recorded in the repository
> *before* it is executed. Nothing about the build depends on the state of the
> machine that runs it.
>
> **Why we need it.** Windows 11's Smart App Control blocks Kurukuru outright
> on a clean install — not a SmartScreen warning, a refusal to load, with
> status `0xC0E90002`. The only workaround we can offer users today is to
> disable a machine-wide security feature, which we would rather not ask
> anyone to do.
>
> **One thing we want to raise up front.**
>
> Our installer bundles a trimmed QEMU: 2 executables and 118 DLLs we did not
> build, from the upstream Windows build at qemu.weilnetz.de. They are GPLv2,
> unmodified, and redistributed as-is. We **do not** propose to sign them, and
> we understand the rule that a project signs only its own binaries.
>
> We read your terms as permitting this — "you may include unsigned binaries
> of upstream OSS projects, e.g. DLL files, in your signed packages" — and we
> would like that confirmed before we depend on it, because it is the whole
> reason the application is worth making: signing only `kurukuru.exe` while
> the bundle stayed untrusted would not lift the block we are trying to lift.
>
> Every bundled file's SHA-256 is recorded in a manifest inside the build
> (`qemu-manifest.json`) and re-verified immediately before packaging, so we
> can say exactly what is in a given release. Licence texts (`COPYING`,
> `COPYING.LIB`) ship alongside, and `THIRD-PARTY-NOTICES.md` states what QEMU
> is and that it is a separate program.
>
> If bundling unsigned upstream binaries is not acceptable under the
> Foundation's terms, we would rather know now than after integrating — please
> say so and we will look at building QEMU from source in the same pipeline
> instead.
>
> **Eligibility checklist.**
>
> - OSI-approved licence with no commercial dual-licensing: Apache-2.0 for our
>   code, GPLv2 for the bundled QEMU. No proprietary components.
> - No malware, no PUP, no hacking or exploitation tooling. It is a hypervisor
>   control plane for the machine it runs on.
> - Actively maintained and already released: three releases, most recent
>   0.1.2. Public changelog and design-decision log.
> - Uninstallation: a standard Inno Setup uninstaller which removes the
>   program, its `PATH` entry and its scheduled task, and asks separately
>   before touching user data.
> - Privacy: no telemetry, no network calls except downloading the Ubuntu
>   cloud image the user asks for. Binds loopback by default.
> - MFA is enabled on the maintainer's GitHub account.

---

## After acceptance, in order

1. Add the SignPath GitHub Action to `release.yml`, between "Build the
   installer" and "Install it, and make it run" — so the smoke test exercises
   the *signed* artefact, not an unsigned one that is then signed blind.
2. Sign `kurukuru.exe` **before** Inno Setup compiles, then the installer
   afterwards. Signing the installer alone leaves the executable inside it
   unsigned, which is the file Smart App Control actually evaluates when the
   user runs the app.
3. Re-measure on a machine with Smart App Control enforcing, which this
   project does not currently have. The claim "signing fixes the block" is
   still an inference from Microsoft's documentation, not something we have
   observed — and given how much of 0.1.2 came from exactly that gap, it
   should be observed before it is written down as fact.
4. Update `docs/DECISIONS.md` #62 with what was actually measured, and remove
   the "Nothing is code-signed" known limitation only once it is untrue.
