# PHASE 4 BRIEF — Cloud-Init & SSH Access

## Context
Phases 1-3 are DONE. Backend on :8000, React dashboard on :5173.
`MultipassEngine.provision_instance` already accepts a reserved
`cloud_init_path: str | None = None` parameter (added in Phase 2 for exactly
this phase). `multipass launch` supports `--cloud-init <file>`.
The `.env` file already holds IAAS_MULTIPASS_BINARY; new settings introduced
here must follow the same pydantic-settings pattern in `backend/app/config.py`.

## Goal
Every instance launched via POST /instances boots with:
1. A default non-root user (sudo, bash) — name from settings, default "iaas"
2. The orchestrator's SSH public key installed for that user
3. Package index updated on first boot (package_update: true) and a couple
   of baseline packages (curl, htop)

So the user can immediately run:  ssh iaas@<ip-from-dashboard>

## Tasks

### 1. SSH key management (`backend/app/ssh_keys.py`, new)
- On first use, generate a dedicated ed25519 keypair for the orchestrator at
  a configurable location (setting `ssh_key_dir`, default
  `~/.local-iaas/keys/` -> files `id_ed25519` / `id_ed25519.pub`).
  Use `ssh-keygen -t ed25519 -N "" -f <path>` via subprocess (Windows 10+
  ships OpenSSH; C:\Windows\System32\OpenSSH is on the System PATH here —
  but still make the binary path a setting, `ssh_keygen_binary`, default
  "ssh-keygen", same pattern as multipass).
- Idempotent: if the keypair exists, reuse it. Never overwrite.
- Set restrictive permissions where the platform allows; on Windows, note
  the limitation in a comment rather than failing.
- Expose `get_public_key() -> str`.

### 2. Cloud-init generation (`backend/app/cloud_init.py`, new)
- `build_cloud_init(instance_name: str) -> Path`: render a YAML file into a
  per-instance temp/workdir location (setting `cloud_init_dir`, default
  `~/.local-iaas/cloud-init/`), named `<instance-name>.yaml`.
- Payload (#cloud-config):
  - users: one user from settings.default_vm_user (default "iaas"),
    sudo ALL=(ALL) NOPASSWD:ALL, shell /bin/bash,
    ssh_authorized_keys: [<orchestrator public key>]
  - package_update: true
  - packages: [curl, htop]
- Generate YAML with pyyaml (already installed via uvicorn[standard]) — do
  NOT string-template YAML; build a dict and yaml.safe_dump it.
- Clean up the file after launch completes (success or failure) — it has
  served its purpose once the VM boots.

### 3. Wire into provisioning (`backend/app/routers/instances.py`)
- The background provisioning job: build the cloud-init file, pass its path
  to `provision_instance(..., cloud_init_path=...)`, ensure cleanup in a
  finally block.
- If cloud-init generation fails (e.g. ssh-keygen missing), mark the row
  Error with a clear error_message — do NOT silently launch without it.

### 4. Surface SSH in the API + UI
- Add `ssh_user` to the InstanceRead response (from settings; simplest
  correct approach: add a computed/serialized field, do not store per-row).
- New endpoint `GET /ssh-key` returning {public_key, key_path, ssh_user} so
  the UI and user can inspect it.
- Dashboard: on Running rows, add a "Copy SSH" action that copies
  `ssh <ssh_user>@<ip_address>` to the clipboard (small icon button next to
  the IP; toast/feedback on copy).

### 5. Config additions (`backend/app/config.py`)
  default_vm_user: str = "iaas"
  ssh_key_dir, cloud_init_dir, ssh_keygen_binary — as above.
  Keep extra="ignore" and utf-8-sig exactly as they are.

## Testing (do this yourself before finishing)
1. Unit tests: cloud-init YAML structure (parse it back with yaml.safe_load
   and assert keys), key manager idempotency (mock subprocess).
2. Live test: launch "cloudinit-test" (small) via the API, wait for Running,
   then verify SSH works non-interactively:
   ssh -i <key> -o StrictHostKeyChecking=accept-new iaas@<ip> "whoami && which curl"
   Expect "iaas" and a curl path. Then terminate and confirm cleanup
   (multipass list empty, no stray cloud-init yaml left behind).
3. Existing test suites (backend pytest, frontend build) must still pass.
4. Show me the generated cloud-init YAML (with the key redacted to its
   first/last 8 chars) and the SSH test output.

## Out of scope
Per-instance custom cloud-init (user-supplied scripts), multiple keys,
Windows ACL hardening of the private key. Note them as future work.
