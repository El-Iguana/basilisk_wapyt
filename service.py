#!/usr/bin/env python3
"""
ASGI entrypoint for Basilisk.

    python service.py
    # or: uvicorn service:app --host 0.0.0.0 --port 8766

Everything server-side is wired here rather than in the application module, so
``appcode/basilisk.py`` stays pure browser code: pytincture serves that
module's source to Pyodide, and a server-only import in it is still visible to
the AST pass that resolves the widgetset and the entrypoint.

Most of this is Monguana's ``service.py`` with the names changed; the
reasons behind each piece are recorded there and in CLAUDE.md.
"""
from __future__ import annotations

import ipaddress
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
APPCODE = HERE / "appcode"

# The BFF modules import each other as `services.<name>`, and pytincture loads
# them by path from the modules folder, so that folder must be importable here.
sys.path.insert(0, str(APPCODE))


def load_dotenv_file() -> None:
    """Load ``.env`` from the project root. Real environment variables win."""
    env_path = HERE / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_dotenv_file()


def register_mime_types() -> None:
    """
    Pin the types the browser is strict about. On Windows, ``mimetypes``
    reads the registry, where other software often maps ``.js`` to
    ``text/plain``; StaticFiles and pytincture's assets would then serve
    scripts, and Pyodide's WebAssembly, with a type the browser refuses.
    """
    import mimetypes

    for suffix, kind in (
        (".js", "text/javascript"), (".mjs", "text/javascript"), (".css", "text/css"),
        (".json", "application/json"), (".wasm", "application/wasm"),
        (".webp", "image/webp"), (".svg", "image/svg+xml"), (".zip", "application/zip"),
    ):
        mimetypes.add_type(kind, suffix)


register_mime_types()

from pytincture import PytinctureConfig, create_app  # noqa: E402

from services.db import init_db, session_secret  # noqa: E402
from services.login_page import LoginPageMiddleware, check_at_startup  # noqa: E402
from services.engine_pool import pool  # noqa: E402
from services.transfer import router as transfer_router  # noqa: E402

APPLICATION = "basilisk"
PORT = int(os.getenv("PORT", "8768"))
# The address uvicorn listens on. The dev container runs with host networking
# (a database GUI has to reach servers on the host's loopback and LAN), where
# 0.0.0.0 would publish plain HTTP to the whole network: podman-run.sh sets
# 127.0.0.1.
BIND = os.getenv("BASILISK_BIND", "0.0.0.0")
# The loopback address Basilisk is reached on. pytincture serves authenticated
# plain HTTP only on a literal loopback IP, so this is an address, not
# "localhost".
HOST = os.getenv("BASILISK_HOST", "").strip() or "127.0.0.1"
# pytincture cookie_namespace (1.0.0rc13): session, CSRF and SAML cookies are
# named basilisk-* instead of pytincture-*, so a sign-in here does not replace
# another pytincture app's on the same host. Changing it signs everyone out once.
COOKIE_NAMESPACE = "basilisk"


def canonical_origin() -> str:
    """
    The single origin this service is reached on. The default is the literal
    IP, not "localhost": pytincture's loopback check parses it as an address.
    """
    return os.getenv("BASILISK_CANONICAL_ORIGIN", f"http://{HOST}:{PORT}").rstrip("/")


def _strip_port(value: str) -> str:
    head, _, tail = value.rpartition(":")
    return head if head and tail.isdigit() else value


def allowed_hosts() -> tuple[str, ...]:
    """
    Exact Host values this service answers to, **hostnames only** (Starlette
    compares with the port stripped). The canonical origin's host is always
    included, because pytincture refuses a configuration without it.
    """
    configured = os.getenv("BASILISK_ALLOWED_HOSTS", "").strip()
    if configured:
        hosts = [_strip_port(part.strip()) for part in configured.split(",") if part.strip()]
    else:
        hosts = [HOST]
    canonical = urlsplit(canonical_origin()).hostname
    if canonical and canonical not in hosts:
        hosts.append(canonical)
    return tuple(dict.fromkeys(hosts))


def is_loopback_deployment(origin: str) -> bool:
    parsed = urlsplit(origin)
    if parsed.scheme == "https":
        return False
    try:
        return ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        return False


def build_app():
    init_db()

    # Registered by dotted path, not by setter: create_app() loads its own
    # isolated backend module, which a setter called against the shared one
    # never reaches. AUTH_SESSION_CLAIM_KEYS is what keeps user_id in the
    # session; without it every BFF call is a silent 403.
    hooks = {
        "AUTH_USER_AUTHENTICATOR": "services.auth.authenticate",
        "BFF_POLICY_HOOK_PATH": "services.auth.policy_hook",
        "AUTH_SESSION_CLAIM_KEYS": "user_id,is_admin,username",
    }

    origin = canonical_origin()
    loopback = is_loopback_deployment(origin)
    if loopback:
        print(
            "  Loopback development mode: plain HTTP on localhost.\n"
            "  Set BASILISK_CANONICAL_ORIGIN to an https:// URL (and put TLS in\n"
            "  front) before exposing this to anything but your own machine.",
            flush=True,
        )

    application = create_app(
        PytinctureConfig(
            modules_path=str(APPCODE),
            default_application=APPLICATION,
            enable_user_login=True,
            # Only unlocks loopback plain HTTP. pytincture's password-less
            # branch is unreachable once AUTH_USER_AUTHENTICATOR is set.
            enable_dev_email_login=loopback,
            session_secret=session_secret(),
            allowed_hosts=allowed_hosts(),
            canonical_origin=origin,
            trusted_proxy_headers=not loopback,
            environment=hooks,
            # Own cookie names (basilisk-dev-session over loopback HTTP,
            # __Host-basilisk-session over HTTPS).
            cookie_namespace=COOKIE_NAMESPACE,
        )
    )

    check_at_startup()
    application.add_middleware(LoginPageMiddleware)
    application.include_router(transfer_router)

    # Same-origin artwork: pytincture's CSP would allow https: images, but
    # local keeps the branding working on an air-gapped deployment.
    from fastapi.staticfiles import StaticFiles

    application.mount(
        "/static", StaticFiles(directory=str(APPCODE / "static")), name="static"
    )

    # Third-party browser code, vendored because the CSP allows scripts from
    # 'self' only: the CodeMirror bundle (tools/codemirror/), loaded on demand.
    application.mount(
        "/vendor", StaticFiles(directory=str(APPCODE / "vendor")), name="vendor"
    )

    @application.on_event("shutdown")
    async def _close_pool() -> None:
        from services.sessions import close_all

        close_all()  # roll back open manual-commit transactions first
        pool.close_all()

    return application


app = build_app()


if __name__ == "__main__":
    import uvicorn

    print(
        f"""
  ____             _ _ _     _
 | __ )  __ _ ___(_) (_)___| | __
 |  _ \\ / _` / __| | | / __| |/ /
 | |_) | (_| \\__ \\ | | \\__ \\   <
 |____/ \\__,_|___/_|_|_|___/_|\\_\\
  Browser-based SQL database manager — pytincture/wapyt
  Developer : OldManGan <eliguana@protonmail.com>
  URL       : {canonical_origin()}/{APPLICATION}/
""",
        flush=True,
    )
    uvicorn.run(app, host=BIND, port=PORT, log_level="info")
