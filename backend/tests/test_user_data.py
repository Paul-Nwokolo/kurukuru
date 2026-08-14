"""
Custom user-data: parsing, merging, and what the guest ends up with.

The merge is the interesting part. Our cloud-config is what puts a login on the
machine, so a user's document has to be combined with it rather than swapped
for it — and the tests below are mostly about the ways that could go wrong
quietly: a user losing their own setting, or losing their way in.
"""

from __future__ import annotations

import pytest
import yaml

from app.cloud_init import build_config, render_user_data
from app.user_data import (
    UserDataError,
    merge_cloud_config,
    parse_user_data,
    render_merged,
)

from tests.test_instances_api import client, iso_dir  # noqa: F401 - fixtures


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_valid_cloud_config_parses():
    parsed = parse_user_data("packages:\n  - htop\nruncmd:\n  - [echo, hi]\n")
    assert parsed["packages"] == ["htop"]


def test_invalid_yaml_carries_the_parsers_own_message():
    """"Invalid YAML" on a long document is not something anyone can act on."""
    with pytest.raises(UserDataError) as caught:
        parse_user_data("packages:\n  - htop\n   bad indent: [\n")

    message = str(caught.value)
    assert "not valid YAML" in message
    # YAML's exception names the position; that is the useful half.
    assert "line" in message


def test_a_top_level_list_is_refused_with_an_example():
    """cloud-config is a mapping. A list parses fine and means nothing."""
    with pytest.raises(UserDataError, match="mapping"):
        parse_user_data("- htop\n- curl\n")


def test_empty_user_data_is_refused():
    with pytest.raises(UserDataError):
        parse_user_data("   \n  ")


# --------------------------------------------------------------------------- #
# Merging
# --------------------------------------------------------------------------- #
def test_user_keys_are_added_alongside_ours():
    merged = merge_cloud_config(
        {"package_update": True, "packages": ["curl"]},
        {"timezone": "Europe/London"},
    )
    assert merged == {
        "package_update": True,
        "packages": ["curl"],
        "timezone": "Europe/London",
    }


def test_on_a_leaf_conflict_the_user_wins():
    """They asked for something definite; we are the ones adding to it."""
    merged = merge_cloud_config({"package_update": True}, {"package_update": False})
    assert merged["package_update"] is False


def test_mappings_merge_recursively():
    merged = merge_cloud_config(
        {"apt": {"primary": "ours", "keep": 1}},
        {"apt": {"primary": "theirs"}},
    )
    assert merged["apt"] == {"primary": "theirs", "keep": 1}


def test_lists_concatenate_rather_than_replace():
    """Every list cloud-config uses is additive in meaning."""
    merged = merge_cloud_config({"packages": ["curl", "htop"]}, {"packages": ["git"]})
    assert merged["packages"] == ["curl", "htop", "git"]


def test_duplicate_list_entries_are_dropped():
    merged = merge_cloud_config({"packages": ["curl"]}, {"packages": ["curl", "git"]})
    assert merged["packages"] == ["curl", "git"]


def test_a_user_supplied_users_list_does_not_remove_our_login():
    """The one merge rule that would otherwise lock people out.

    If lists were replaced rather than concatenated — the natural reading of
    "the user's value wins" — a cloud-config with its own `users:` entry would
    drop the account our SSH keys were installed on, and the instance would
    come up with no way in at all. Concatenating keeps both; cloud-init is
    happy to create two users.
    """
    ours = build_config("x", public_keys=["ssh-ed25519 AAAAkey ours"])
    theirs = parse_user_data(
        "users:\n"
        "  - name: deploy\n"
        "    shell: /bin/sh\n"
    )

    merged = merge_cloud_config(ours, theirs)
    names = [u["name"] for u in merged["users"]]

    assert "iaas" in names and "deploy" in names
    iaas = next(u for u in merged["users"] if u["name"] == "iaas")
    assert iaas["ssh_authorized_keys"] == ["ssh-ed25519 AAAAkey ours"]


def test_neither_input_is_mutated():
    ours = {"packages": ["curl"]}
    theirs = {"packages": ["git"]}
    merge_cloud_config(ours, theirs)

    assert ours == {"packages": ["curl"]}
    assert theirs == {"packages": ["git"]}


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def test_no_user_data_renders_exactly_what_it_always_did():
    """Nine phases of instances were built from this; it must not shift."""
    base = build_config("plain", public_keys=["ssh-ed25519 AAAA k"])

    assert render_merged(base, None) == render_user_data(
        "plain", public_keys=["ssh-ed25519 AAAA k"]
    )


def test_the_rendered_document_is_still_valid_cloud_config():
    document = render_user_data(
        "merged",
        public_keys=["ssh-ed25519 AAAA k"],
        custom_user_data="packages: [git]\nwrite_files:\n  - path: /tmp/x\n    content: hi\n",
    )

    assert document.startswith("#cloud-config\n")
    parsed = yaml.safe_load(document)
    assert "git" in parsed["packages"] and "curl" in parsed["packages"]
    assert parsed["write_files"][0]["path"] == "/tmp/x"
    assert parsed["users"][0]["ssh_authorized_keys"] == ["ssh-ed25519 AAAA k"]


def test_yaml_is_never_string_concatenated():
    """A structural merge, not text.

    The giveaway would be two '#cloud-config' headers or a duplicated key; both
    would make the document either invalid or silently wrong.
    """
    document = render_user_data(
        "structural",
        public_keys=["k"],
        custom_user_data="#cloud-config\npackages: [git]\n",
    )

    assert document.count("#cloud-config") == 1
    assert document.count("packages:") == 1


# --------------------------------------------------------------------------- #
# The API
# --------------------------------------------------------------------------- #
def test_user_data_reaches_the_guest_and_is_stored_on_the_row(client):
    body = client.post(
        "/instances",
        json={
            "name": "with-userdata",
            "flavor": "small",
            "user_data": "packages:\n  - git\ntimezone: Europe/London\n",
        },
    )
    assert body.status_code == 202

    row = client.get(f"/instances/{body.json()['id']}").json()
    # Stored as supplied, so the detail view can show what was asked for.
    assert "timezone: Europe/London" in row["user_data"]


def test_invalid_yaml_is_a_422_on_the_request(client):
    r = client.post(
        "/instances",
        json={"name": "bad-yaml", "flavor": "small", "user_data": "packages: [\n"},
    )

    assert r.status_code == 422
    assert "not valid YAML" in r.json()["detail"]


def test_user_data_is_refused_for_an_iso_instance(client, iso_dir):
    """An installer has no cloud-init; accepting it would silently discard it."""
    (iso_dir / "alpine.iso").write_bytes(b"\0" * 32)

    r = client.post(
        "/instances",
        json={
            "name": "iso-userdata",
            "flavor": "small",
            "iso": "alpine.iso",
            "user_data": "packages: [git]",
        },
    )

    assert r.status_code == 422
    assert "no cloud-init" in r.json()["detail"]


def test_omitting_user_data_leaves_the_row_null(client):
    body = client.post("/instances", json={"name": "no-userdata", "flavor": "small"})
    assert client.get(f"/instances/{body.json()['id']}").json()["user_data"] is None
