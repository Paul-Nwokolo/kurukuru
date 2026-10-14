# Kurukuru — local cloud infrastructure

Launch virtual machines on your own computer the way you would in a cloud: pick
a size, get an SSH command back, and forget where it is running. An HTTP API, a
web dashboard and a CLI over the whole VM lifecycle, on top of QEMU, with an
image catalog, cloud-init provisioning, a browser console, and sizing bounded by
what your machine can actually spare.

One machine, one account: it manages VMs on the computer it runs on, for the
person signed in to it. There is no remote mode and nothing here is
multi-tenant.

Source, documentation and issues:
<https://github.com/Paul-Nwokolo/kurukuru>

## Where this package fits

**On Windows, you want the installer, not this package.** Download
`Kurukuru-<version>-Setup.exe` from
<https://github.com/Paul-Nwokolo/kurukuru/releases/latest>. It bundles a tested
QEMU and needs nothing else first, not Python and not QEMU. Windows 10 and 11
with hardware acceleration is the one platform Kurukuru supports.

This package is the same application without QEMU. It installs and runs on
Windows (CI installs it with pipx, serves the dashboard from it, and checks
what `kurukuru doctor` tells you), but you must supply QEMU yourself, and
`kurukuru doctor` says how.

**On Linux, the packaging is complete and unverified on real KVM hardware.**
`pipx install`, a systemd user service, XDG state directories and a
data-safe uninstall are built, checked in CI on Ubuntu, and were checked by
hand in WSL2. **No
virtual machine has ever been booted under KVM from this package.** Until one
has, on real hardware, Linux is not a supported platform. That is also why the
only operating-system classifier on this page is Windows: a classifier is a
claim, and this one cannot yet be evidenced for Linux. The open questions, and
what would settle each, are listed in
<https://github.com/Paul-Nwokolo/kurukuru/blob/main/docs/PORTABILITY.md>.
Linux instructions:
<https://github.com/Paul-Nwokolo/kurukuru/blob/main/docs/INSTALL-LINUX.md>

macOS has code paths and nothing else.

## Before you rely on it

- **Plain HTTP on loopback.** The API and dashboard listen on 127.0.0.1 by
  default, with no TLS. Anything beyond loopback needs a TLS-terminating proxy:
  <https://github.com/Paul-Nwokolo/kurukuru/blob/main/docs/SECURITY.md>
- **Authentication, no authorization.** Every account is a full administrator.
- **Known limitations** are kept current at the end of
  <https://github.com/Paul-Nwokolo/kurukuru/blob/main/docs/DECISIONS.md>.

## License

Apache-2.0. See
<https://github.com/Paul-Nwokolo/kurukuru/blob/main/LICENSE> and
<https://github.com/Paul-Nwokolo/kurukuru/blob/main/NOTICE>.
