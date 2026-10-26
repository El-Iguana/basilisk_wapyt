"""SSH tunnel settings (services/tunnels.py) and host-key fingerprints (services/ssh.py)."""
from __future__ import annotations

import base64
import hashlib

from services import ssh, tunnels

STORED = {"enabled": True, "host": "bastion", "port": 22, "username": "me", "auth": "password",
          "password": "s3cret", "private_key": "", "passphrase": "",
          "host_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"}


def test_merge_keeps_secrets_unless_sent():
    assert tunnels.merge_settings(STORED, None) == STORED
    edited = tunnels.merge_settings(STORED, {"enabled": True, "host": "bastion", "port": "22",
                                             "username": " me ", "auth": "password"})
    assert edited["password"] == "s3cret" and edited["username"] == "me"
    assert edited["host_key"] == STORED["host_key"]
    cleared = tunnels.merge_settings(STORED, {**edited, "password": ""})
    assert cleared["password"] == ""


def test_merge_forgets_the_pin_when_the_host_changes_or_when_asked():
    sent = {key: value for key, value in STORED.items() if key != "host_key"}
    moved = tunnels.merge_settings(STORED, {**sent, "host": "elsewhere"})
    assert moved["host_key"] == ""
    port = tunnels.merge_settings(STORED, {**sent, "port": 2222})
    assert port["host_key"] == ""
    forgot = tunnels.merge_settings(STORED, {**STORED, "forget_host_key": True})
    assert forgot["host_key"] == ""
    assert forgot["host_key"] == ""
    # A key from a Test pins only when none is pinned.
    offered = tunnels.merge_settings(STORED, {**STORED, "host_key": "ssh-rsa AAAA"})
    assert offered["host_key"] == STORED["host_key"]
    fresh = tunnels.merge_settings({}, {**STORED, "host_key": "ssh-rsa AAAA"})
    assert fresh["host_key"] == "ssh-rsa AAAA"


def test_merge_drops_the_other_way_of_signing_in():
    by_key = tunnels.merge_settings(STORED, {**STORED, "auth": "key", "private_key": "PEM"})
    assert by_key["password"] == "" and by_key["private_key"] == "PEM"
    back = tunnels.merge_settings(by_key, {**by_key, "auth": "password", "password": "x"})
    assert back["private_key"] == "" and back["passphrase"] == ""
    assert tunnels.merge_settings({}, {"auth": "telepathy"})["auth"] == "password"


def test_validate():
    assert tunnels.validate({"enabled": False}) == {}
    errors = tunnels.validate(tunnels.merge_settings({}, {"enabled": True, "port": 70000,
                                                          "auth": "key"}))
    assert set(errors) == {"ssh_host", "ssh_port", "ssh_username", "ssh_private_key"}
    bad = tunnels.merge_settings({}, {"enabled": True, "host": "h", "username": "u",
                                      "auth": "key", "private_key": "not a key"})
    assert "Unrecognised" in tunnels.validate(bad)["ssh_private_key"]


def test_public_settings_hide_secrets():
    public = tunnels.public_settings(STORED)
    assert not {"password", "private_key", "passphrase"} & set(public)
    assert public["has_password"] and not public["has_key"]
    blob = base64.b64decode(STORED["host_key"].split()[1])
    expected = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    assert public["host_key"] == expected == ssh.fingerprint_of(STORED["host_key"])
    assert ssh.fingerprint_of("garbage") == "" and tunnels.public_settings(None)["port"] == 22
