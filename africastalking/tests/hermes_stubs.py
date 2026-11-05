"""Minimal offline stubs for the Hermes gateway surface our adapter touches.

``install()`` injects stub modules into ``sys.modules`` so ``africastalking.adapter``
can be imported and unit-tested without a Hermes installation or network. The
stubs mirror the real signatures verified against hermes-agent v0.22.7
(``gateway/platforms/base.py``, ``_shared.py``, ``event.py``, ``helpers.py``) —
they are test doubles, not a reimplementation.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from typing import Any, Optional

# Test-controllable "environment" behind get_scoped_secret/extra_or_secret.
ENV: dict[str, str] = {}


def _mod(name: str) -> types.ModuleType:
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m


def install() -> None:
    """Idempotently install the stubs. Call BEFORE importing africastalking.adapter."""
    if getattr(sys.modules.get("gateway"), "__hermes_stub__", False):
        return

    # ── aiohttp.web (only the names adapter.py uses) ────────────────────────
    aiohttp = _mod("aiohttp")
    web = _mod("aiohttp.web")
    aiohttp.web = web

    class _Router:
        def __init__(self):
            self.routes: list[tuple[str, Any]] = []

        def add_post(self, path: str, handler):
            self.routes.append((path, handler))

    class Application:
        def __init__(self, **kwargs):
            self.router = _Router()

    class AppRunner:
        def __init__(self, app, **kwargs):
            self.app = app

        async def setup(self):
            pass

        async def cleanup(self):
            pass

    class TCPSite:
        def __init__(self, runner, host, port):
            self.host, self.port = host, port

        async def start(self):
            pass

        async def stop(self):
            pass

    @dataclass
    class Response:
        text: str = ""
        status: int = 200
        content_type: Optional[str] = None

    web.Application = Application
    web.AppRunner = AppRunner
    web.TCPSite = TCPSite
    web.Response = Response
    web.Request = object  # type hint only in adapter.py

    # ── gateway.config ──────────────────────────────────────────────────────
    gateway = _mod("gateway")
    gateway.__hermes_stub__ = True
    config = _mod("gateway.config")
    gateway.config = config

    class Platform:
        """Mirrors the dynamic-member behaviour plugins rely on (Platform._missing_)."""

        def __init__(self, value: str):
            self.value = value

        def __repr__(self):
            return f"Platform({self.value!r})"

    config.Platform = Platform

    # ── gateway.platforms._shared ───────────────────────────────────────────
    platforms = _mod("gateway.platforms")
    gateway.platforms = platforms
    shared = _mod("gateway.platforms._shared")
    platforms._shared = shared

    def get_scoped_secret(name: str, default: str = "") -> str:
        return ENV.get(name, default)

    def extra_or_secret(extra: Optional[dict], key: str, env: str) -> Any:
        if extra and extra.get(key):
            return extra[key]
        return ENV.get(env, "")

    def coerce_port(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def seed_extra_from_env(spec, home_env: Optional[str] = None, home_default: str = "") -> dict:
        seeded: dict[str, Any] = {}
        for env_var, key, conv in spec:
            raw = ENV.get(env_var, "")
            if not raw:
                continue
            try:
                seeded[key] = conv(raw) if conv else raw
            except ValueError:
                continue
        if home_env:
            home = ENV.get(home_env, "") or home_default
            if home:
                seeded["home_channel"] = {"chat_id": home, "name": "Home"}
        return seeded

    def send_error(message: str) -> dict:
        return {"error": message}

    shared.get_scoped_secret = get_scoped_secret
    shared.extra_or_secret = extra_or_secret
    shared.coerce_port = coerce_port
    shared.seed_extra_from_env = seed_extra_from_env
    shared.send_error = send_error

    # ── gateway.platforms.event ─────────────────────────────────────────────
    event = _mod("gateway.platforms.event")
    platforms.event = event

    @dataclass
    class MessageEvent:
        text: str
        message_type: Any
        source: Any
        message_id: str
        timestamp: Any = None

    class MessageType:
        TEXT = "text"

    event.MessageEvent = MessageEvent
    event.MessageType = MessageType

    # ── gateway.platforms.helpers ───────────────────────────────────────────
    helpers = _mod("gateway.platforms.helpers")
    platforms.helpers = helpers

    class MessageDeduplicator:
        def __init__(self, ttl_seconds: int = 600):
            self.ttl_seconds = ttl_seconds
            self._seen: set[str] = set()

        def is_duplicate(self, message_id: str) -> bool:
            if message_id in self._seen:
                return True
            self._seen.add(message_id)
            return False

    helpers.MessageDeduplicator = MessageDeduplicator

    # ── gateway.platforms.base ──────────────────────────────────────────────
    base = _mod("gateway.platforms.base")
    platforms.base = base

    @dataclass
    class SendResult:
        success: bool
        message_id: Optional[str] = None
        error: Optional[str] = None
        raw_response: Any = None
        retryable: bool = False
        retry_after: Optional[float] = None
        continuation_message_ids: tuple = ()
        error_kind: Optional[str] = None

    class BasePlatformAdapter:
        """Recording double: every base-class touchpoint our adapter uses."""

        def __init__(self, config=None, platform=None, **kwargs):
            self.config = config
            self.platform = platform
            self.handled: list = []
            self.fatal_errors: list[tuple] = []
            self.locks: list[str] = []
            self.connected = False
            self.listener_base: Optional[str] = None
            self.wired: list = []
            self._message_handler = True  # adapter checks truthiness

        def _set_fatal_error(self, code: str, message: str, *, retryable: bool) -> None:
            self.fatal_errors.append((code, message, retryable))

        def _acquire_platform_lock(self, scope: str, identity: str, resource_desc: str) -> bool:
            self.locks.append(f"{scope}:{identity}")
            return True

        def _release_platform_lock(self) -> None:
            self.locks.clear()

        def _mark_connected(self, *, listener_base: Optional[str] = None) -> None:
            self.connected = True
            self.listener_base = listener_base

        def _mark_disconnected(self) -> None:
            self.connected = False

        def _wire_plugin_handlers(self, native: Any = None) -> None:
            self.wired.append(native)

        def build_source(self, **kwargs) -> dict:
            return dict(kwargs)

        async def handle_message(self, ev) -> None:
            self.handled.append(ev)

    base.BasePlatformAdapter = BasePlatformAdapter
    base.SendResult = SendResult
