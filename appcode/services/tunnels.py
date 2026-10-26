"""
SSH tunnels for connection profiles (milestone 3). A plain module.

A :class:`Tunnel` listens on ``127.0.0.1:<ephemeral port>`` and forwards each
connection made to it through an SSH server (``direct-tcpip``) to the
database's host and port *as that server sees them*. The engine pool gives
every engine whose profile has SSH switched on its own tunnel and points the
driver at the local port (``engine_pool.build_engine``); disposing the
engine closes it.

* The SSH connection is made **when the tunnel is built**, so a wrong
  password, an unknown host or a changed host key is the error the person
  sees, not a driver's "connection closed".
* The host key is pinned on first use (``services/ssh.py``); the pool saves
  it on the profile (``db.pin_ssh_host_key``).
* If the SSH connection drops (the server restarts, a NAT forgets it), the
  next database connection through the tunnel dials it again; the pool also
  rebuilds an engine whose tunnel is not healthy (``healthy``).
* It listens on loopback only. Other processes on this machine could connect
  to it, as with ``ssh -L``, but the database still asks for its own
  credentials.
"""
from __future__ import annotations

import logging
import select
import socket
import threading
from typing import Callable, Optional

logger = logging.getLogger("basilisk.tunnels")

CONNECT_TIMEOUT_S = 15
_CHUNK = 32 * 1024


class Tunnel:
    def __init__(self, ssh_cfg: dict, target: tuple,
                 on_learn: Optional[Callable[[str], None]] = None) -> None:
        self._cfg = dict(ssh_cfg)
        self._target = (str(target[0]), int(target[1]))
        self._on_learn = on_learn
        self._lock = threading.Lock()
        self._client = None
        self._closed = False
        self.learned: Optional[str] = None
        self.last_error = ""
        self._dial()
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(32)
        self.port = int(self._server.getsockname()[1])
        threading.Thread(target=self._accept_loop, name=f"tunnel-{self.port}",
                         daemon=True).start()

    # -- the SSH side ---------------------------------------------------

    def _learn(self, serialized: str) -> None:
        self.learned = serialized
        # Pinned from now on, for this tunnel's own redials too.
        self._cfg["host_key"] = serialized
        if self._on_learn is not None:
            try:
                self._on_learn(serialized)
            except Exception:  # noqa: BLE001 - the tunnel works; the pin is saved next time
                logger.exception("could not save the pinned host key")

    def _dial(self) -> None:
        from services import ssh

        self._client = ssh.connect(self._cfg, timeout=CONNECT_TIMEOUT_S,
                                   on_learn_host_key=self._learn)

    def healthy(self) -> bool:
        transport = self._client.get_transport() if self._client else None
        return bool(not self._closed and transport is not None and transport.is_active())

    def _channel(self, peer: tuple):
        with self._lock:
            if not self.healthy() and not self._closed:
                try:
                    self._client.close()
                except Exception:  # noqa: BLE001
                    pass
                self._dial()
            return self._client.get_transport().open_channel(
                "direct-tcpip", self._target, peer, timeout=CONNECT_TIMEOUT_S)

    # -- the local side -------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._closed:
            try:
                sock, peer = self._server.accept()
            except OSError:
                return              # closed
            threading.Thread(target=self._serve, args=(sock, peer), daemon=True,
                             name=f"tunnel-{self.port}-{peer[1]}").start()

    def _serve(self, sock: socket.socket, peer: tuple) -> None:
        try:
            channel = self._channel(peer)
        except Exception as exc:  # noqa: BLE001 - the driver sees a closed socket
            from services.engine_pool import error_text

            self.last_error = error_text(exc)
            logger.warning("tunnel to %s:%s failed: %s", *self._target, self.last_error)
            sock.close()
            return
        try:
            while True:
                ready, _w, _x = select.select([sock, channel], [], [], 30)
                if sock in ready:
                    data = sock.recv(_CHUNK)
                    if not data:
                        break
                    channel.sendall(data)
                if channel in ready:
                    data = channel.recv(_CHUNK)
                    if not data:
                        break
                    sock.sendall(data)
                if self._closed:
                    break
        except (OSError, EOFError):
            pass
        finally:
            for end in (channel, sock):
                try:
                    end.close()
                except Exception:  # noqa: BLE001
                    pass

    def close(self) -> None:
        self._closed = True
        for thing in (self._server, self._client):
            try:
                if thing is not None:
                    thing.close()
            except Exception:  # noqa: BLE001
                pass


def ssh_enabled(profile: dict) -> bool:
    return bool((profile.get("ssh") or {}).get("enabled"))


# ---------------------------------------------------------------------------
# A profile's SSH settings: what the editor sends, what is stored, what the
# browser may see. Secrets (password, private_key, passphrase) never go back.
# ---------------------------------------------------------------------------

SECRETS = ("password", "private_key", "passphrase")
AUTH = ("password", "key")


def merge_settings(stored: Optional[dict], incoming: Optional[dict]) -> dict:
    """
    The settings to store. ``incoming`` None keeps the stored ones. A secret
    absent from ``incoming`` keeps its stored value; ``""`` clears it. The
    pinned host key is dropped when the SSH host or port changes, or when
    ``forget_host_key`` is set; ``host_key`` (from a Test) pins a key only
    when none is pinned.
    """
    stored = dict(stored or {})
    if incoming is None:
        return stored
    merged = {
        "enabled": bool(incoming.get("enabled")),
        "host": str(incoming.get("host") or "").strip(),
        "port": _port(incoming.get("port")),
        "username": str(incoming.get("username") or "").strip(),
        "auth": incoming.get("auth") if incoming.get("auth") in AUTH else "password",
    }
    for secret in SECRETS:
        merged[secret] = str(incoming[secret]) if secret in incoming else stored.get(secret, "")
    moved = (merged["host"], merged["port"]) != (stored.get("host"), _port(stored.get("port")))
    if incoming.get("forget_host_key") or moved:
        merged["host_key"] = ""
    else:
        merged["host_key"] = stored.get("host_key") or ""
    if not merged["host_key"] and incoming.get("host_key") and not incoming.get("forget_host_key"):
        merged["host_key"] = str(incoming["host_key"]).strip()
    # The other way of signing in is not kept: one secret less at rest.
    if merged["auth"] == "password":
        merged["private_key"] = merged["passphrase"] = ""
    else:
        merged["password"] = ""
    return merged


def _port(value) -> int:
    try:
        return int(value or 22)
    except (TypeError, ValueError):
        return -1


def validate(settings: dict) -> dict:
    """Field errors (``ssh_host`` …) for settings that are switched on."""
    if not settings.get("enabled"):
        return {}
    errors: dict = {}
    if not settings.get("host"):
        errors["ssh_host"] = "The SSH host is required"
    if not 0 < settings.get("port", 0) < 65536:
        errors["ssh_port"] = "Port must be 1-65535"
    if not settings.get("username"):
        errors["ssh_username"] = "The SSH user is required"
    if settings.get("auth") == "key":
        if not (settings.get("private_key") or "").strip():
            errors["ssh_private_key"] = "Paste the private key"
        else:
            from services import ssh

            try:
                ssh.load_private_key(settings["private_key"].strip(),
                                     settings.get("passphrase") or "")
            except Exception as exc:  # noqa: BLE001 - paramiko's reason, for the person
                errors["ssh_private_key"] = str(exc)
    return errors


def public_settings(settings: Optional[dict]) -> dict:
    """What the editor may show: no secrets, the pinned key as a fingerprint."""
    from services import ssh

    settings = settings or {}
    return {
        "enabled": bool(settings.get("enabled")),
        "host": settings.get("host") or "", "port": settings.get("port") or 22,
        "username": settings.get("username") or "",
        "auth": settings.get("auth") or "password",
        "has_password": bool(settings.get("password")),
        "has_key": bool(settings.get("private_key")),
        "has_passphrase": bool(settings.get("passphrase")),
        "host_key": ssh.fingerprint_of(settings.get("host_key") or ""),
    }
