# Installing Kurukuru on Linux

> **Linux is not a supported platform yet.** This is a packaging milestone:
> Kurukuru installs on Linux as a Python package, runs its backend as a
> systemd user service, and serves the dashboard. **No VM has been booted under
> KVM from this install path.** Until a host with `/dev/kvm` has run the full
> scorecard — launch, boot, console, SSH, stop and start, reboot — treat
> everything below as the way to install a program whose central feature is
> unverified on this platform. [What has and has not been verified](#what-has-and-has-not-been-verified)
> is at the end, item by item.

## Requirements

| | Needed for | Notes |
|---|---|---|
| Python 3.11+ and [pipx](https://pipx.pypa.io/) | installing | `sudo apt install pipx` on Debian/Ubuntu, then `pipx ensurepath` |
| QEMU (`qemu-system-x86_64`, `qemu-img`) | running VMs | From your distribution — see below. **Kurukuru does not ship QEMU on Linux** |
| OpenSSH client (`ssh`, `ssh-keygen`) | SSH into VMs; the orchestrator key | Usually installed already |
| `/dev/kvm`, and membership of its group | fast VMs | Optional: without it VMs run under software emulation, roughly 30x slower |
| systemd with a user manager | the background service | Optional: without it, run `kurukuru serve` yourself |

### QEMU, from your distribution

Kurukuru uses the QEMU your distribution ships — patched by them, built against
KVM, and upgraded with the rest of the system. (Why it is not bundled the way
the Windows installer bundles it: [DECISIONS.md](DECISIONS.md) #70.)

| Distribution | Command |
|---|---|
| Debian, Ubuntu, Mint | `sudo apt install qemu-system-x86 qemu-utils` |
| Fedora | `sudo dnf install qemu-system-x86-core qemu-img` |
| Arch | `sudo pacman -S qemu-base` |
| openSUSE | `sudo zypper install qemu-x86 qemu-tools` |
| RHEL, Rocky, Alma | Their `qemu-kvm` installs `/usr/libexec/qemu-kvm` and no `qemu-system-x86_64`. Kurukuru has not been tested with it; you need an upstream build, pointed at with `KURUKURU_QEMU_SYSTEM_BINARY` and `KURUKURU_QEMU_IMG_BINARY` |

Only the Ubuntu line has actually been installed from (Ubuntu 24.04, QEMU
8.2.2). The others are each distribution's documented package names, not
tested installs. `kurukuru doctor` prints the right line for the machine it
runs on.

## Install

**No wheel is published yet, and Kurukuru is not on PyPI.** Do not run
`pipx install kurukuru`: the name is unregistered there, so today that command
fails — and if anyone ever registers it, it would install their package, not
this one. Build a wheel from a checkout instead (needs Node.js for the
dashboard) and install it by path:

```bash
git clone https://github.com/Paul-Nwokolo/kurukuru.git && cd kurukuru
(cd frontend && npm ci)
python3 tools/build_wheel.py --out dist
pipx install dist/kurukuru-*.whl
```

`build_wheel.py` puts the built dashboard inside the package and then checks the
wheel it produced, refusing one without it. pipx gives Kurukuru its own
virtualenv and puts the `kurukuru` command in `~/.local/bin`.

## First run

```bash
kurukuru auth init            # create your account; prompts for a password
kurukuru service install      # start the backend now, and at each login
kurukuru doctor               # what works, what does not, and what to do
```

Then open <http://127.0.0.1:7842>. The backend listens on loopback only.

`kurukuru service install` writes `~/.config/systemd/user/kurukuru.service`,
enables it and starts it. Re-run it after an upgrade or after moving the
install: it rewrites the unit for the `kurukuru` you ran it with and restarts
the service. Logs: `journalctl --user -u kurukuru.service -f`.

Without systemd (a container, WSL without systemd enabled) run
`kurukuru serve` in a terminal instead.

## The kvm group

`/dev/kvm` is normally owned by the `kvm` group. If you are not in it, QEMU
gets `Permission denied` and VMs fall back to software emulation — which looks
like Kurukuru being broken. `kurukuru doctor` says which case you are in.

```bash
sudo usermod -aG kvm $USER
```

**Then log out completely and back in** (or reboot). Group membership is fixed
when a login session starts; `usermod` changes the group file, not anything
already running. If the service runs with lingering on (below), its user
manager survives your logout and keeps the old groups — restart it too:

```bash
sudo systemctl restart user@$(id -u).service
```

`doctor` tells "not in the group" apart from "in the group, but this process
started before that", because running `usermod` again is the wrong fix for the
second.

No `/dev/kvm` at all means virtualization is off in the firmware, the module is
not loaded (`sudo modprobe kvm_intel` or `kvm_amd`), or this machine is itself
a VM without nested virtualization — which WSL2 usually is.

## Does it keep running after I log out?

**Not by default.** A systemd user service belongs to your user manager, and
systemd stops that manager — and the backend with it — when your last session
ends. To keep it running after logout:

```bash
sudo loginctl enable-linger $USER
```

`kurukuru service install`, `service status` and `doctor` all say whether
lingering is on. If you only use Kurukuru while logged in, you do not need it.

**Under WSL, lingering does not help.** WSL stops the whole distribution when
its last window closes — measured with `Linger=yes`, the instance was
`Stopped` a minute after the last terminal closed — so the service runs only
while some WSL window is open. It starts again by itself the next time the
distribution starts. `doctor` and `service install` say this when they detect
WSL.

What happens to *running VMs* at that moment has not been verified on Linux;
see the open questions at the end.

## Where your data lives

| | New install | If `~/.kurukuru` already exists |
|---|---|---|
| VMs, images, keys, database, backups | `~/.local/share/kurukuru` (`$XDG_DATA_HOME/kurukuru`) | `~/.kurukuru` |
| `kurukuru.env`, `cli.toml` | `~/.config/kurukuru` (`$XDG_CONFIG_HOME/kurukuru`) | `~/.kurukuru` |
| The service unit | `~/.config/systemd/user/kurukuru.service` | same |

Kurukuru follows the XDG Base Directory spec on Linux. If you ran a source
checkout before 0.1.5, your VMs are in `~/.kurukuru`, and **they stay there** —
Kurukuru keeps using that tree as long as it exists, and never moves it. To
adopt the XDG layout yourself:

```bash
systemctl --user stop kurukuru.service
mv ~/.kurukuru ~/.local/share/kurukuru
mkdir -p ~/.config/kurukuru
mv ~/.local/share/kurukuru/{kurukuru.env,cli.toml} ~/.config/kurukuru/ 2>/dev/null
systemctl --user start kurukuru.service
```

`KURUKURU_STATE_DIR` overrides all of this. (Why: [DECISIONS.md](DECISIONS.md) #69.)

## Upgrade

Build the new wheel as above, then:

```bash
pipx install --force dist/kurukuru-*.whl
kurukuru service install      # rewrites the unit if needed, and restarts
```

pipx replaces the whole virtualenv, so nothing from the previous version is
left behind to be imported by mistake — the problem the Windows installer had
until 0.1.4 ([DECISIONS.md](DECISIONS.md) #66). Your data is not touched.

The service unit uses `KillMode=process`, so restarting the backend is
*designed* to leave running VMs alone, as it does on Windows. That is not yet
verified with real VMs on Linux.

## Uninstall

Three steps, and only the second touches your data — and only if you say so:

```bash
kurukuru service uninstall    # stop and remove the service; data untouched
kurukuru data remove          # optional: see below
pipx uninstall kurukuru       # remove the program
```

`kurukuru data remove` refuses while the backend is running, lists what is in
the state directory with a size against each kind of thing, and asks. If you
say yes it moves the directory to your desktop's trash (`gio trash`, or
`trash-put` from trash-cli) so you can restore it. If there is no trash it
**renames** the directory aside, to `kurukuru.removed-<timestamp>` next to it,
and prints the `rm -rf` for you to run when you are sure. Nothing is deleted.
Database backups live inside that directory, so removing it removes them too.

## What has and has not been verified

| | Where | Result |
|---|---|---|
| The wheel builds, with the dashboard inside, and is inspected | Windows dev machine; GitHub Ubuntu runner | Yes |
| `pipx install` of the wheel; `kurukuru` on PATH | GitHub Ubuntu runner | See CI: `wheel installs with pipx (Linux)` |
| The installed backend serves the API and the packaged dashboard | GitHub Ubuntu runner | See CI |
| State under `~/.local/share/kurukuru`, not `~/.kurukuru`; key directory 0700, private key 0600 | GitHub Ubuntu runner | See CI |
| `doctor` names the distribution's QEMU package and no Windows-only advice | GitHub Ubuntu runner; tests on every platform | See CI |
| The backend and tools suites on Linux, with every skip listed | GitHub Ubuntu runner | See CI: `backend + tools (Linux, no KVM)` |
| pipx install of the wheel; `kurukuru version` reports the loaded framework | WSL2, Ubuntu 24.04.5, pipx 1.4.3, Python 3.12.3, on ext4 under `~` | Yes |
| State under `~/.local/share/kurukuru` only; state dir, `keys/`, private key, database and CLI token all owner-only (0700/0600); public key 0644 | WSL2, ext4 | Yes — measured on ext4, never on `/mnt/c` |
| `service install`: unit written with an absolute `ExecStart` and `KillMode=process`, enabled, started; dashboard served from the wheel | WSL2, systemd 255 | Yes |
| `service status`, `journalctl --user -u kurukuru.service`, `kurukuru restart` (new MainPID, waits for the backend) | WSL2 | Yes |
| An enabled service starts by itself when the user manager starts | WSL2: distribution stopped, then booted | Yes — up 4 s after boot, health 200, untouched |
| `KillMode=process` leaves a child of the service running across a restart; the default kills it | WSL2, with a plain `sleep` standing in for QEMU | Yes for the stand-in; systemd logged the "left-over process". **Not verified with real VMs** |
| `doctor` live: distro QEMU command, not-in-`kvm`-group diagnosis with `usermod` and the re-login trap, XDG state, lingering; no Windows-only wording | WSL2 | Yes |
| `data remove` refuses while the backend runs; `gio trash`, `trash-put`, and the rename aside each keep the tree byte-identical | WSL2, real trees on ext4 | Yes |
| `service uninstall` and `pipx uninstall` leave the data untouched | WSL2 | Yes — fingerprint identical |
| `loginctl enable-linger` needs root | WSL2 | Yes — without sudo it fails ("No such device or address") |
| Lingering keeps the service running after the last session ends | WSL2 | **Not establishable under WSL**: WSL stops the whole distribution when the last window closes, with or without lingering. Needs a real machine |
| **Any VM launched under KVM from this install** | needs bare metal | **Never** |

### Open questions — need a host with `/dev/kvm`

WSL2 cannot answer these honestly (nested virtualization under Hyper-V is
unreliable, its networking is NAT'd, and it has no display path), so they are
recorded, not guessed:

1. **Do VMs launch, boot and run under KVM from a pipx install?** The whole
   scorecard: launch, boot time, SSH, console, stop, start, terminate.
2. **`-cpu host`** — chosen in Phase 9 on reasoning, never executed.
3. **The display default.** `std` VGA is the default because of a WHPX
   limitation; whether the Ubuntu cloud image renders on `std` under KVM
   decides whether the default should differ per platform. Settled the way the
   WHPX question was: boot it, inject a keystroke, diff the framebuffer.
4. **Is the reboot hang (QEMU #4410) a Windows-host problem only?** If it never
   happens under KVM, the reboot watchdog should not run there.
5. **VMs across a service restart and across logout.** `KillMode=process` is
   meant to leave them running through a restart; what logind does to them at
   logout without lingering depends on its `KillUserProcesses` setting.
6. **Console behaviour and port forwards** under KVM, on a real network stack.
