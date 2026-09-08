# Kurukuru Backend & System Review

Following up on the frontend review, I've conducted a deep-dive into the backend, architecture, CLI, and test suite. The analysis is framed by the constraints (single-host, QEMU-only, SQLite loopback API) and the specific failure modes you care about (false greens, abstraction leaks, swallowed failures).

## 1. Code Quality & Correctness

### A. Critical: Module-Level Settings Evaluation Bypasses Test Isolation
The most severe finding violates the `CONTRIBUTING.md` rule against module-level settings capture:
* **`backend/kurukuru/database.py:31`**: `settings = get_settings()` is called at the module level and immediately used to configure the SQLAlchemy `engine = create_engine(settings.resolved_database_url, ...)` at import time.
* **`backend/kurukuru/main.py:39`**: `settings = get_settings()` is called at the module level and subsequently used inside `list_flavors()` (line 186), which directly reads `settings.flavors` instead of using `Depends(get_settings)`.

**Why it matters:** The `isolated_state` fixture in `conftest.py` works by clearing the `get_settings` LRU cache and overriding the dependency injection framework. Because `database.py` binds the engine URL *at import time*, it evaluates against the real user settings before the fixture can intervene. The only reason this hasn't corrupted a developer's real database is because of the `_forbid_real_database_connections` guard in `conftest.py`, which is functioning exactly as intended to catch this.

### B. Tests Passing Vacuously (False Greens)
I audited the test suite for checks that report success without actually verifying the intended outcome.
* **`test_dashboard.py::test_a_traversal_over_http_falls_through_to_the_app`**:
  ```python
  for attempt in ("/../secret.txt", "/..%2fsecret.txt", "/assets/../../secret.txt"):
      assert "do not serve me" not in served.get(attempt).text
  ```
  **The false green:** This asserts the secret string isn't in the response. If the application crashes on path traversal and returns a `500 Internal Server Error`, or if the HTTP client truncates the request, the string won't be in the response and the test will pass vacuously. **Fix:** Assert `served.get(attempt).status_code == 200` and `<!doctype html>` in the response to prove the fallback actually engaged.

* **`test_process.py::test_terminating_a_dead_pid_does_nothing`**:
  Checks `terminate_pid(None) is False` and `terminate_pid(0) is False`. While true, it doesn't test the actual condition of "a valid PID that is already dead". It should test with a dummy high PID and mock `os.kill` to raise `ProcessLookupError`, ensuring the wrapper catches it and returns cleanly.

### C. Abstraction Leaks & Swallowed Errors
* **`backend/kurukuru/database.py` (Backup directory creation):** 
  In `backup_database_file()`, if `directory.mkdir(parents=True)` raises an `OSError`, it falls into the generic `except (OSError, sqlite3.Error)` block and logs: `Could not back up...`. The failure is swallowed, returning `None`. This is deliberate per the docstring ("refusing to proceed... would turn a precaution into an outage"), but it means if the filesystem becomes read-only, schema migrations will proceed without any safety net, irreversibly altering the state.
* **CLI Port Conflict Leak (`cli.py`)**: 
  When a port forward collides, the CLI tests assert it exits with an `invalid` status and a reason. However, if the underlying FastAPI throws a 500 due to a failed bind that wasn't caught as a domain error, the CLI prints a generic "Internal Server Error" rather than a domain-specific "Port 8080 already in use" message.

## 2. Features Worth Adding

Given the target audience (platform engineers, homelab users, testing infra code), here are three capabilities that provide concrete value without breaking the single-host, no-authz constraints.

### A. Cloud-Init Dry-Run & Validation (Platform Engineer)
**The Problem:** Debugging `user-data` YAML is a notoriously slow loop. You launch a VM, wait for it to boot, SSH in, and read `/var/log/cloud-init-output.log` to find out you had a typo in `write_files`.
**The Feature:** Add an endpoint (`POST /instances/dry-run` or `/cloud-init/validate`) that accepts `user-data`, merges it with the system defaults, and validates it against the cloud-init schema (via `cloud-init schema --system`).
**The Value:** Immediate feedback on syntax and structure before provisioning a disk or spending time waiting for QEMU to boot.

### B. Event Stream / Webhooks for CI/CD Automation
**The Problem:** Scripts provisioning instances for integration tests have to poll `GET /instances/{id}` in a `while True; sleep 5` loop to know when an instance transitions from `Provisioning` to `Running` and acquires an IP address.
**The Feature:** Expose a Server-Sent Events (SSE) or WebSocket endpoint (`/events/stream`) that broadcasts the events currently being logged to `record_event()` in `events.py`.
**The Value:** Allows infrastructure-as-code tools or bash scripts to block and `await` VM readiness without hammering the loopback API with polling requests, ensuring instant reaction times when a VM finishes booting.

### C. Linked Clones / Base Images (Homelab & Testing)
**The Problem:** The current snapshot architecture is stopped-only, and launching 5 test instances from an imported 2GB qcow2 image results in 10GB of disk usage and non-trivial IO overhead.
**The Feature:** Support "Linked Clones" via QEMU's backing files (`qemu-img create -f qcow2 -b base.qcow2 overlay.qcow2`). Users can mark an image or a stopped instance as a "Template".
**The Value:** Launching 10 VMs from a template becomes instantaneous and costs almost zero disk space. A platform engineer testing an Ansible playbook against a cluster can tear down and rebuild their 5-node environment in milliseconds rather than waiting for full disk copies.
