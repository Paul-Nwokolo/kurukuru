# Installing Kurukuru

Windows 11 or Windows 10 (64-bit). Nothing else needs to be installed first —
not Python, not QEMU.

---

## Requirements

| | |
|---|---|
| **OS** | Windows 10 or 11, 64-bit |
| **Disk** | ~250 MB for the program, plus whatever your VMs use. A single Linux VM is about 1 GB; a Windows VM is 40 GB or more |
| **Memory** | 8 GB is comfortable. Kurukuru holds 2 GB back for Windows itself and will refuse a VM that would eat into it |
| **Virtualisation** | Enabled in firmware, and either Hyper-V or the Windows Hypervisor Platform feature turned on. Without it VMs still run, roughly 30× slower |
| **Rights** | None. It installs into your own profile and never asks for an administrator prompt |

To check virtualisation: open Task Manager → Performance → CPU. "Virtualisation:
Enabled" is what you want. After installing, `kurukuru doctor` reports what
Kurukuru itself found.

---

## Windows will warn you, and here is exactly what you will see

**This build is not code-signed.** When you run the installer, Windows
SmartScreen shows a blue dialog:

> **Windows protected your PC**
> Microsoft Defender SmartScreen prevented an unrecognised app from starting.
> Running this app might put your PC at risk.

There is no "Run" button visible. Click **More info**, which reveals the
publisher line and a **Run anyway** button.

That warning is not a sign that anything is wrong with this particular file —
it is what Windows shows for *any* executable without a paid signing
certificate and without an established download reputation. It would look
exactly the same for something that genuinely was malicious, which is why the
honest thing is to say so here rather than let you meet it unexplained.

If that trade is not one you want to make, you can build from source instead —
see [CONTRIBUTING.md](../CONTRIBUTING.md) — and the result is byte-for-byte the
same program.

---

## What the installer does

Everything below happens inside your own user profile.

| | |
|---|---|
| **Program** | `%LOCALAPPDATA%\Programs\Kurukuru` — the backend with its own Python runtime, the dashboard, and a pinned copy of QEMU |
| **Start Menu** | A "Kurukuru" entry that opens the dashboard, and a "Kurukuru console" entry that opens a prompt with `kurukuru` on `PATH` |
| **PATH** | `kurukuru` added to your user `PATH` (not the system one) |
| **Startup** | Optional: a scheduled task named `Kurukuru` that starts the backend when you sign in |
| **Your data** | `%USERPROFILE%\.kurukuru` — created on first run, never touched by the installer |

It does **not** install a Windows service, modify system settings, add anything
to the machine-wide `PATH`, or touch a QEMU you already have. The bundled QEMU
is found by configuration rather than by `PATH`, so it cannot be shadowed by
another install and cannot shadow one.

### The startup task

Ticked by default. It runs `kurukuru serve` as you, at your logon, at your
normal privilege level. You can see it in Task Scheduler under the name
`Kurukuru`, and disable it there without uninstalling anything.

It is a *task* rather than a *service* deliberately. A service would need
administrator rights to install and would run as a different account — which
would make `~/.kurukuru` mean a different directory from the one you see in a
terminal.

If you untick it, start Kurukuru yourself with `kurukuru serve`, or from the
Start Menu.

---

## First launch

The dashboard is at **<http://127.0.0.1:7842>** — the Start Menu entry opens it.

A fresh install has no account, so the first screen is a **create-account
form** rather than a login. Choose a username and a password of at least 12
characters. There are no character rules; length is what makes a password hard
to guess.

That form works only from this machine and only until an account exists. After
that it is gone, and the same screen becomes an ordinary login. If you would
rather do it in a terminal, `kurukuru auth init` does exactly the same thing.

Then:

```
kurukuru doctor                 # what Kurukuru found on this host
kurukuru launch web --wait      # a Linux VM, downloading the base image once
kurukuru ssh web                # a shell in it
```

The first launch downloads the Ubuntu cloud image, about 600 MB, once.

---

## Where your data lives

```
%USERPROFILE%\.kurukuru\
    kurukuru.db          the database: instances, images, volumes, accounts
    kurukuru.env         your settings, if you change any (see below)
    backups\             automatic copies, taken before any schema change
    cli-token            this machine's API token
    keys\                the keypair every VM trusts
    isos\                boot media you drop here yourself
    qemu\
        base-images\     downloaded and imported images
        instances\<name>\  each VM's disk, and the state to restart it
        volumes\         additional disks
```

To move all of it — onto a bigger drive, say — set `KURUKURU_STATE_DIR` as an
environment variable and restart. Everything follows it. Move the directory
first; nothing is copied for you.

---

## Changing a setting

Settings are read once when Kurukuru starts. On an installed build they live in

```
%USERPROFILE%\.kurukuru\kurukuru.env
```

which does not exist until you create it. One `NAME=value` per line, using the
variable name the **Settings** page shows against each value:

```
KURUKURU_PORT=7843
KURUKURU_QEMU_BOOT_TIMEOUT_SECONDS=900
```

Then restart Kurukuru — **Start Menu → Restart Kurukuru**, or:

```
kurukuru restart
```

An environment variable of the same name still wins over the file, which is how
`KURUKURU_STATE_DIR` has to be set: it decides *where this file is read from*,
so it cannot be set inside it.

**Do not set `KURUKURU_QEMU_SYSTEM_BINARY` or `KURUKURU_QEMU_IMG_BINARY` on an
installed build.** Kurukuru ships QEMU and finds it beside its own executable.
The workaround that named those two variables applies to **0.1.0 only**, where
nothing pointed at the bundle; on any later build it replaces a correct answer
with a hardcoded path. `kurukuru doctor` reports one that is still set.

---

## Upgrading

Run the new installer. It installs over the old one, keeps `%USERPROFILE%\.kurukuru`
untouched, re-registers the startup task against the new build, and re-runs any
database migrations on the next start.

**Stop your VMs first.** A running VM holds its disk open. Since 0.1.2 the
installer stops the startup task and ends any Kurukuru and QEMU processes
before it touches a file, so an upgrade over a running install works — but a
VM ended that way loses whatever was unsaved inside it, exactly as pulling its
power would. If a file still cannot be replaced, the installer says so and
changes nothing, rather than leaving a half-replaced install behind.

**Per-user only.** 0.1.2 removed the "install for me / for all users" choice.
The state directory, the token's file permissions and the logon task all assume
one signed-in user, so all-users was an untested shape — one a real user picked,
and got stranded on. An existing all-users install is detected, and the
installer asks you to remove it first.

Verified end to end: installing 0.1.1 over a running 0.1.0 preserved the
account, the running instance, every database row and the VM's disk.

### Upgrading from a pre-Kurukuru install

If you used this when it was called *Local IaaS*, your data is in
`~/.local-iaas`. The first start after upgrading moves it to `~/.kurukuru`,
rewrites the paths recorded inside it, and takes a database backup first.

It refuses to move anything while a VM is running, and says which. Stop them
and start again.

`IAAS_*` environment variables still work for one release, with a warning
naming the `KURUKURU_*` replacement.

---

## Uninstalling

Settings → Apps → Installed apps → Kurukuru → Uninstall. Or run
`unins000.exe` from the install directory.

**Your VMs are not deleted.** The uninstaller asks, once, whether to remove
`%USERPROFILE%\.kurukuru` as well, and defaults to **No**. Answer No and a
later reinstall picks up exactly where you left off.

In the next release that question counts what is there before it asks — VM disks,
images, volumes, ISOs, the database and **every backup of it**, each with its
size — and answering Yes moves the folder to the **Recycle Bin** rather than
destroying it, so you can put it back. The disk space returns when you empty
the bin. If the folder is too large for the Recycle Bin, nothing is removed
and the uninstaller says so rather than deleting it permanently.

An unattended uninstall (`/VERYSILENT /SUPPRESSMSGBOXES`) never asks and never
removes your data.

It also removes the startup task and the `PATH` entry.

Removing the `PATH` entry is new in 0.1.2 — every earlier uninstall left one
behind, pointing at a directory that no longer existed. If you have uninstalled
an older build, check your user `PATH` for a stale `...\Programs\Kurukuru`.

---

## If something is wrong

```
kurukuru doctor
```

reports the host: whether QEMU is usable, whether acceleration works, where
everything lives, and how much room is left.

**"Smart App Control is on and enforcing."** Windows is refusing to load
Kurukuru's bundled QEMU because this build is not code-signed. Nothing is wrong
with your install or your machine: Smart App Control is on by default on a
clean Windows 11 install and blocks any program it does not recognise. Older
builds reported this as `qemu-system-x86_64.exe --version failed (exit
3236495362)` with no other explanation — that number is `0xC0E90002`, an
application-control block.

To use Kurukuru, turn Smart App Control off in **Windows Security → App &
browser control → Smart App Control**. It is a machine-wide security setting
protecting everything else you run, so it is worth a moment's thought rather
than a reflex click.

**"Port 7842 is already in use."** Something else is on it. `kurukuru serve
--port 7843`, or set `KURUKURU_PORT`.

**"Port 7842 is reserved on this machine."** Different problem, and not one
elevation fixes: Windows reserves blocks of ports for Hyper-V and WSL, and they
move when you reboot. `netsh int ipv4 show excludedportrange protocol=tcp`
lists them; pick a port outside.

**The dashboard says it cannot reach the backend.** Check the startup task is
running (Task Scheduler → `Kurukuru`), or start it yourself with `kurukuru serve`.

**A VM's screen is black.** Under hardware acceleration, guests that stay in VGA
text mode render nothing. Relaunch with modern graphics, or software emulation.
See [WINDOWS.md](WINDOWS.md).

---

## Known limitations

- **Windows guests: 10 yes, Server not yet, 11 never.** A Windows 10 install has
  completed end to end and passed a validation scorecard — one install, not a
  body of evidence, and it needs two automatic restarts on the way through to
  work around an upstream QEMU defect. Windows Server has not completed an
  install here. Windows 11 cannot be installed at all: it requires TPM 2.0,
  which cannot be emulated on a Windows host. See
  [WINDOWS.md](WINDOWS.md) for the evidence and [DECISIONS.md](DECISIONS.md).
- **No TLS.** Plain HTTP on loopback. Anything beyond this machine needs a
  TLS-terminating proxy in front of it.
- **No roles.** Every account is a full administrator.
- **One host.** There is no clustering and no remote hypervisor.
- **Unsigned.** See the SmartScreen note above.
