"""
Merging user-supplied cloud-config with the orchestrator's own.

A user who wants to install packages or write a file should not have to choose
between their cloud-config and ours — ours is what puts a login on the machine.
So the two are merged rather than swapped, and the merge is done on *parsed
structures*, never on text. String-concatenating two YAML documents produces
something that is either invalid or, worse, silently valid and wrong.

The merge rule, and the one judgement call in it:

* Mappings are merged key by key, recursively.
* For any leaf conflict, **the user's value wins**. They asked for something
  specific and we are the ones adding to it.
* Lists are **concatenated**, not replaced, with duplicates dropped. This is
  the deliberate exception to "the user wins", and it is the only place the two
  could reasonably disagree — see :func:`_merge_lists`.
"""

from __future__ import annotations

from typing import Any

import yaml


class UserDataError(ValueError):
    """The supplied user-data is not usable cloud-config."""


def parse_user_data(text: str) -> dict[str, Any]:
    """Parse and sanity-check a user's cloud-config document.

    ``safe_load`` throughout: this is untrusted input, and the full loader can
    construct arbitrary Python objects. The error message carries YAML's own
    description of the problem, including the line and column, because "invalid
    YAML" on a fifty-line document is not something anyone can act on.
    """
    stripped = (text or "").strip()
    if not stripped:
        raise UserDataError("The user-data is empty.")

    try:
        parsed = yaml.safe_load(stripped)
    except yaml.YAMLError as exc:
        raise UserDataError(f"user-data is not valid YAML: {exc}") from exc

    if parsed is None:
        raise UserDataError("The user-data parsed to nothing.")
    if not isinstance(parsed, dict):
        raise UserDataError(
            "cloud-config must be a mapping of keys at the top level, not a "
            f"{type(parsed).__name__}. A '#cloud-config' document looks like "
            "'packages:\\n  - htop'."
        )
    return parsed


def merge_cloud_config(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge ``overlay`` (the user's) onto ``base`` (ours).

    Returns a new dict; neither input is modified.
    """
    merged = dict(base)
    for key, value in overlay.items():
        if key not in merged:
            merged[key] = value
        elif isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = merge_cloud_config(merged[key], value)
        elif isinstance(merged[key], list) and isinstance(value, list):
            merged[key] = _merge_lists(key, merged[key], value)
        else:
            # A leaf conflict: the user asked for something definite.
            merged[key] = value
    return merged


def _merge_lists(key: str, ours: list[Any], theirs: list[Any]) -> list[Any]:
    """Concatenate two lists, dropping duplicates, ours first.

    Concatenation rather than replacement, because every list cloud-config uses
    is additive in meaning: ``packages`` is things to install, ``runcmd`` is
    commands to run, ``write_files`` is files to write. A user adding ``htop``
    means "also htop", not "instead of everything you were going to do".

    ``users`` is the one that would hurt if we got it wrong, and it is why this
    is not simply "the user's list wins": replacing our ``users`` entry with
    theirs would remove the account their SSH key was installed on, and the
    instance would come up with no way in. Concatenating leaves ours intact and
    adds theirs alongside — cloud-init is happy to create both.

    Ordering is stable and ours comes first so that our defaults are applied
    before their additions; for ``runcmd`` in particular, that is the order
    people expect.
    """
    del key  # named for the docstring's sake; the rule is the same for all
    merged: list[Any] = []
    for item in [*ours, *theirs]:
        if item in merged:  # exact duplicates only — no attempt at similarity
            continue
        merged.append(item)
    return merged


def render_merged(base: dict[str, Any], user_text: str | None) -> str:
    """Render the final ``#cloud-config`` document for a guest.

    With no user-data this is just our own config, byte-identical to what the
    previous nine phases produced.
    """
    config = base if not user_text else merge_cloud_config(base, parse_user_data(user_text))
    body = yaml.safe_dump(config, default_flow_style=False, sort_keys=False, width=4096)
    return f"#cloud-config\n{body}"
