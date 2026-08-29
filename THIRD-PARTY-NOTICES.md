# Third-party notices

Kurukuru itself is Apache-2.0 (see [LICENSE](LICENSE) and [NOTICE](NOTICE)).
The Windows installer additionally **bundles QEMU**, which is licensed
separately and is not covered by that grant. This file states the posture, and
the installer places a copy of it beside the binaries it describes.

---

## QEMU

| | |
|---|---|
| **Pinned version** | 11.1.0 (tagged release, 11 August 2026) |
| **Windows build** | `qemu-w64-setup-20260811.exe` from <https://qemu.weilnetz.de/w64/> |
| **Primary licence** | GNU General Public License, version 2 |
| **Also present** | LGPL 2.1, and BSD-family licences for the bundled EDK2 firmware |
| **Licence texts** | `qemu/COPYING`, `qemu/COPYING.LIB`, `qemu/share/edk2-licenses.txt` inside the install directory |
| **Upstream project** | <https://www.qemu.org/> |

### Why bundling QEMU does not make Kurukuru GPL

Kurukuru **invokes** `qemu-system-x86_64` and `qemu-img` as separate
processes. It does not link against QEMU, include its headers, or incorporate
any of its code. Communication is by command line, by exit status, and by a
JSON protocol (QMP) over a loopback TCP socket — the same interfaces any other
program would use, and interfaces QEMU publishes for that purpose.

Shipping two independent programs on one medium is *aggregation*, which GPLv2
section 2 addresses directly: the licence does not extend to independent works
distributed alongside the covered work. So QEMU remains GPLv2 and Kurukuru
remains Apache-2.0, and neither reaches the other.

What bundling **does** oblige is this: QEMU is distributed in binary form, so
its licence text must travel with it and its complete corresponding source must
be offered. Both are below.

### Written offer of source code

The bundled QEMU binaries were built from the unmodified upstream release
tarball:

- **Source:** <https://download.qemu.org/qemu-11.1.0.tar.xz>
- **Signature:** <https://download.qemu.org/qemu-11.1.0.tar.xz.sig>
- **Size:** 141,831,772 bytes
- **Published:** 11 August 2026

Kurukuru applies **no patches** to QEMU. The binaries are taken as published by
the Windows build linked above; the installer selects a subset of the files
(the x86_64 emulator, `qemu-img`, their DLLs, and x86 firmware) and changes
none of them.

Should the URLs above ever cease to resolve, the same source is available for
at least three years from the date of distribution by opening an issue at
<https://github.com/Paul-Nwokolo/local-iaas>, and a copy will be provided on a
physical medium for no more than the cost of that medium.

### On the upstream signature

**The published Windows installer is signed with an expired certificate**, as
its own download page states. Kurukuru therefore treats that signature as
absent rather than as assurance.

Instead, `tools/build_installer.py` records the SHA-256 of every bundled QEMU
file at the moment they are collected, writes them to a manifest that ships
inside the installer, and re-verifies them before packaging. That pins *what was
tested* rather than *what was signed* — which is the property that actually
matters here, and the one Phase 13 established when a shadowed `qemu-img` from
an unrelated install produced results nobody could reproduce.

---

## Frontend dependencies

The dashboard is built with React, Vite, Tailwind CSS, TanStack Query, axios,
lucide-react and noVNC, all MIT- or BSD-licensed, plus the SIL Open Font
License for the bundled Space Grotesk and JetBrains Mono faces. The build
inlines them into the bundle it ships, and `frontend/package-lock.json` records
the exact version of every one.

## Backend dependencies

FastAPI, uvicorn, SQLModel, SQLAlchemy, pydantic, Typer, Rich, httpx, psutil,
PyYAML, pycdlib and argon2-cffi, all MIT-, BSD- or LGPL-licensed. Pinned in
`backend/pyproject.toml` and `backend/requirements.txt`.

`argon2-cffi` bundles the reference Argon2 implementation, which is dual
licensed CC0 / Apache-2.0.
