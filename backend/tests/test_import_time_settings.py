"""CONTRIBUTING rule 5, enforced instead of remembered.

A module-level value captured at import cannot be redirected afterwards. The
rule exists because the pattern has produced four separate bugs here, and it
kept producing them *after* it was written down — `database.py` still built its
engine from an import-time snapshot, and `main.py`'s `/flavors` still answered
from one, one route below the `/ssh-key` that DECISIONS #59 had already fixed
for exactly this reason.

Prose in CONTRIBUTING did not stop that. These tests do, cheaply: they read the
source rather than the behaviour, because the behaviour is indistinguishable
from correct until something tries to redirect settings — which in production is
never, and in tests is masked by fixtures patching around it.

Deliberately narrow. This does not ban the *name* `settings` at module level;
it bans reading configuration at import in the two modules where doing so has
actually cost something, and requires every FastAPI route that wants settings
to take the dependency.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import kurukuru.database
import kurukuru.main

#: Modules that must not read settings at import.
#:
#: `main.py` is deliberately absent. It reads settings at module scope to *wire
#: the application*: the CORS origins, the trusted-host list and the dashboard
#: directory are all needed while the app object is being constructed, and
#: there is no later moment to do it in. That is import-time construction, not
#: an import-time snapshot answering a request — the distinction rule 5 draws.
#: What main.py is held to instead is the route check at the bottom of this
#: file, which is where its actual bug was.
WATCHED = ("kurukuru/database.py",)

#: Settings fields that can differ between one install and another, or between
#: a snapshot and a fresh read. These are the ones a frozen value gets wrong.
#:
#: `app_name` and `app_version` are excluded on purpose. They are re-exports of
#: constants in `kurukuru.product`, so a snapshot of them cannot disagree with a
#: fresh read — no environment variable moves them, and `/health` reading them
#: from the module-level `settings` is not the bug this is looking for. Adding
#: them would make the check fire on four routes that are correct, which is how
#: a guard gets deleted.
INSTALL_VARYING = frozenset(
    {
        "state_dir", "database_url", "resolved_database_url", "database_path",
        "qemu_dir", "iso_dir", "ssh_key_dir", "cloud_init_dir", "dashboard_dir",
        "flavors", "host", "port", "debug", "cors_origins", "auth_token_file",
    }
)

BACKEND = Path(__file__).resolve().parent.parent


def _module_level_calls(source: str, func: str) -> list[int]:
    """Line numbers where ``func()`` is called at module scope.

    "Module scope" means statements that run on import. A `def` at module level
    is such a statement, but its *body* is not — it runs when called. Walking
    into function bodies is the mistake the first version of this made, and it
    reported `database.py` as broken for reading settings inside the very
    function that was added to stop reading them at import.
    """
    tree = ast.parse(source)
    found: list[int] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue  # its body runs when called, not at import
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name)
                and sub.func.id == func
            ):
                found.append(sub.lineno)
    return found


@pytest.mark.parametrize("relative", WATCHED)
def test_settings_are_not_read_at_import(relative: str) -> None:
    """No ``get_settings()`` at module scope in the files that got this wrong.

    `main.py` keeps a module-level `settings` name for the middleware wiring it
    genuinely needs at startup, so this checks the *call*, not the name — an
    import-time call is what freezes a value; a name assigned from one already
    frozen is the same bug one step later, and there is nowhere left to hide it
    once the call is gone.
    """
    source = (BACKEND / relative).read_text(encoding="utf-8")
    at = _module_level_calls(source, "get_settings")
    assert not at, (
        f"{relative} calls get_settings() at import, on line(s) {at}. "
        "Read it at call time, or take Settings as a dependency — see "
        "CONTRIBUTING 'Verification integrity' rule 5 and DECISIONS #59."
    )


def test_the_database_engine_is_not_built_at_import() -> None:
    """`create_engine` must not run at module scope in database.py.

    The engine is the specific value that mattered: built at import, it binds
    whatever `resolved_database_url` said at the time, and every module doing
    `from kurukuru.database import engine` copies that binding. Correctness then
    depends on a fixture overwriting all of them, which is the hand-maintained
    arrangement DECISIONS #59 exists to stop relying on.
    """
    source = (BACKEND / "kurukuru/database.py").read_text(encoding="utf-8")
    at = _module_level_calls(source, "create_engine")
    assert not at, (
        f"database.py calls create_engine() at import, on line(s) {at}. "
        "Build it lazily so the URL comes from settings at first use."
    )


def test_the_engine_is_still_reachable_as_a_module_attribute() -> None:
    """The lazy engine must not have broken the thing everything imports.

    Ten modules do `from kurukuru.database import engine as db_engine`, and
    `conftest.isolated_state` patches `kurukuru.database.engine`. Making the
    engine lazy is only safe while both still work, so both are asserted here
    rather than assumed — this is the test that fails if PEP 562's
    `__getattr__` is removed or renamed.
    """
    engine = kurukuru.database.engine  # noqa: B018 - the access *is* the test
    assert engine is not None
    # Second access must come from globals, not __getattr__, and be the same
    # object — a fresh engine per access would give every caller its own pool.
    assert kurukuru.database.engine is engine


def test_every_route_taking_settings_takes_it_as_a_dependency() -> None:
    """No FastAPI route may close over the module-level `settings` in main.py.

    `/flavors` did, and answered from the configuration the process started
    with. Checked by source rather than by calling the routes, because calling
    them cannot tell the difference: the frozen value and the live one are
    equal in production and only diverge where something redirects settings.
    """
    source = inspect.getsource(kurukuru.main)
    tree = ast.parse(source)

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorators = ast.unparse(ast.Module(body=list(node.decorator_list), type_ignores=[]))
        is_route = any(
            f".{verb}(" in decorators for verb in ("get", "post", "put", "patch", "delete")
        )
        if not is_route:
            continue
        params = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
        if "settings" in params:
            continue  # shadowed by its own parameter; fine

        # `settings.<anything>` where `settings` is the bare module-level name.
        # Matched on the AST rather than on text: `"settings.flavors" in body`
        # also matches `request_settings.flavors`, which is the *correct* form —
        # the first version of this test flagged the route it had just been
        # written to bless.
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Attribute)
                and isinstance(sub.value, ast.Name)
                and sub.value.id == "settings"
                and sub.attr in INSTALL_VARYING
            ):
                offenders.append(f"{node.name} (settings.{sub.attr})")
                break

    assert not offenders, (
        f"These routes read the module-level settings instead of "
        f"Depends(get_settings): {offenders}. See DECISIONS #59."
    )
