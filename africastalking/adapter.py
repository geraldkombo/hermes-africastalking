"""Africa's Talking platform adapter for Hermes Agent — SMS two-way + USSD menus.

Inbound: a small aiohttp webhook server (AT posts form-encoded callbacks to
``/sms``, ``/ussd`` and ``/delivery``). Callbacks are acknowledged immediately
and agent replies go out proactively over the AT REST API — the documented
pattern for platforms with tight response windows.

Outbound: ``at_client.send_sms`` (pure stdlib) run via ``asyncio.to_thread``;
long replies are split on word boundaries into 160-char SMS parts, capped so
an agent essay can never become an unbounded phone bill.

config.yaml ``gateway.platforms.africastalking.extra`` keys: username, api_key,
sender_id, callback_host (127.0.0.1), callback_port (8055), webhook_secret.
Env vars override config.yaml: AT_USERNAME, AT_API_KEY, AT_SENDER_ID,
AT_CALLBACK_HOST, AT_CALLBACK_PORT, AT_WEBHOOK_SECRET.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime
import hmac
import logging
import uuid
from typing import Any, Optional

from aiohttp import web

from gateway.config import Platform
from gateway.platforms._shared import (
    coerce_port,
    extra_or_secret,
    get_scoped_secret,
    seed_extra_from_env,
    send_error,
)
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import MessageDeduplicator

from . import at_client, ussd
from .adapter_hint import PLATFORM_HINT

logger = logging.getLogger(__name__)

DEFAULT_CALLBACK_HOST = "127.0.0.1"
DEFAULT_CALLBACK_PORT = 8055


def _mask_phone(phone: str) -> str:
    """Redact the middle of a phone number for logs (PII hygiene)."""
    return phone[:4] + "•" * max(0, len(phone) - 7) + phone[-3:] if len(phone) > 7 else "•" * len(phone)


def _recipient_message_id(parsed: dict) -> Optional[str]:
    with contextlib.suppress(KeyError, IndexError, TypeError):
        return str(parsed["SMSMessageData"]["Recipients"][0]["messageId"]) or None
    return None


class AfricaTalkingAdapter(BasePlatformAdapter):
    """Webhook-driven adapter: AT cloud ⇄ local callback server ⇄ gateway."""

    # 3 concatenated SMS parts. The gateway's smart chunker splits at this
    # length; chunk_sms() then re-splits on word boundaries per 160-char part.
    MAX_MESSAGE_LENGTH = 480

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform("africastalking"))
        extra = getattr(config, "extra", {}) or {}
        self.username = extra_or_secret(extra, "username", "AT_USERNAME")
        self.api_key = extra_or_secret(extra, "api_key", "AT_API_KEY")
        self.sender_id = extra_or_secret(extra, "sender_id", "AT_SENDER_ID") or None
        self.callback_host = (
            extra_or_secret(extra, "callback_host", "AT_CALLBACK_HOST") or DEFAULT_CALLBACK_HOST
        )
        self.callback_port = coerce_port(
            extra_or_secret(extra, "callback_port", "AT_CALLBACK_PORT"), DEFAULT_CALLBACK_PORT
        )
        self.webhook_secret = extra_or_secret(extra, "webhook_secret", "AT_WEBHOOK_SECRET") or ""
        self._dedup = MessageDeduplicator(ttl_seconds=600)
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._tasks: set[asyncio.Task] = set()  # strong refs so GC can't eat live dispatches

    @property
    def name(self) -> str:
        return "Africa's Talking"

    def _fail(self, code: str, message: str, *, retryable: bool) -> bool:
        self._set_fatal_error(code, message, retryable=retryable)
        return False

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not (self.username and self.api_key):
            logger.error("Africa's Talking: AT_USERNAME and AT_API_KEY must be configured")
            return self._fail(
                "config_missing", "AT_USERNAME and AT_API_KEY must be set", retryable=False
            )
        # Two profiles must never bind the same callback port.
        if not self._acquire_platform_lock(
            "africastalking",
            f"{self.callback_host}:{self.callback_port}",
            f"callback port {self.callback_port}",
        ):
            return False
        app = web.Application()
        app.router.add_post("/sms", self._on_sms)
        app.router.add_post("/ussd", self._on_ussd)
        app.router.add_post("/delivery", self._on_delivery)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.callback_host, self.callback_port)
        try:
            await self._site.start()
        except OSError as exc:
            logger.error(
                "Africa's Talking: could not listen on %s:%s — %s",
                self.callback_host, self.callback_port, exc,
            )
            with contextlib.suppress(Exception):
                await self._runner.cleanup()
            self._runner = self._site = None
            self._release_platform_lock()
            return self._fail("port_in_use", f"Callback port {self.callback_port}: {exc}", retryable=True)
        self._mark_connected(listener_base=f"http://{self.callback_host}:{self.callback_port}")
        logger.info(
            "Africa's Talking: callback server on %s:%s (POST /sms /ussd /delivery)",
            self.callback_host, self.callback_port,
        )
        logger.info(
            "Africa's Talking: point your app's SMS + USSD callback URLs at this server "
            "(tunnel it first — see the africastalking-setup skill)"
        )
        self._wire_plugin_handlers(None)
        return True

    async def disconnect(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        if self._site is not None:
            with contextlib.suppress(Exception):
                await self._site.stop()
        if self._runner is not None:
            with contextlib.suppress(Exception):
                await self._runner.cleanup()
        self._runner = self._site = None
        with contextlib.suppress(Exception):
            self._release_platform_lock()
        self._mark_disconnected()

    # ── outbound ─────────────────────────────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        return await self._send_parts(chat_id, content)

    async def send_typing(self, chat_id: str, metadata: Optional[dict[str, Any]] = None) -> None:
        """SMS has no typing indicator — no-op."""

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": chat_id, "type": "dm", "chat_id": chat_id}

    async def _send_parts(self, phone: str, content: str) -> SendResult:
        parts = at_client.chunk_sms(content)
        if not parts:
            return SendResult(success=False, error="empty message")
        if not (self.username and self.api_key):
            return SendResult(success=False, error="Africa's Talking credentials not configured")
        last_id: Optional[str] = None
        ids: list[str] = []
        for part in parts:
            try:
                parsed = await asyncio.to_thread(
                    at_client.send_sms, self.username, self.api_key, [phone], part, self.sender_id
                )
            except at_client.ATError as exc:
                logger.warning("Africa's Talking: send to %s failed — %s", _mask_phone(phone), exc)
                # Only offer the base class's automatic retry when nothing went out yet:
                # a mid-burst retry would re-send earlier parts as duplicate paid SMS
                # (and on read-timeouts AT may already have accepted the part).
                retryable = not ids and (exc.status is None or exc.status >= 500 or exc.status == 429)
                return SendResult(success=False, error=str(exc), retryable=retryable)
            ok, detail = at_client.sms_response_summary(parsed)
            if not ok:
                logger.warning("Africa's Talking: send to %s rejected — %s", _mask_phone(phone), detail)
                return SendResult(success=False, error=f"Africa's Talking rejected: {detail}")
            mid = _recipient_message_id(parsed)
            if mid:
                ids.append(mid)
        # Base contract: message_id is the LAST chunk's id (later edits target the
        # newest chunk); continuation_message_ids holds the earlier ids in send order.
        last_id = ids[-1] if ids else None
        return SendResult(
            success=True, message_id=last_id or uuid.uuid4().hex, continuation_message_ids=tuple(ids[:-1])
        )

    # ── inbound: webhook handlers ────────────────────────────────────────────

    def _secret_ok(self, request: web.Request) -> bool:
        """Constant-time check of the optional ?secret= query param.

        Without a secret, anyone who learns the (tunnelled) callback URL could
        inject messages into the agent — so setting AT_WEBHOOK_SECRET is
        strongly recommended and documented in the setup skill.
        """
        if not self.webhook_secret:
            return True
        provided = request.query.get("secret", "")
        # compare_digest(str, str) raises TypeError on non-ASCII — and the query
        # param is attacker-controlled percent-decoded UTF-8. Compare bytes.
        return hmac.compare_digest(
            provided.encode("utf-8", "surrogateescape"),
            self.webhook_secret.encode("utf-8", "surrogateescape"),
        )

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _on_sms(self, request: web.Request) -> web.Response:
        if not self._secret_ok(request):
            return web.Response(status=403, text="forbidden")
        try:
            form = at_client.parse_form_body(await request.text())
            sms = at_client.parse_sms_callback(form)
        except at_client.ATError as exc:
            logger.debug("Africa's Talking: bad SMS callback — %s", exc)
            return web.Response(status=400, text="bad request")
        # Ack immediately; the agent turn (seconds to minutes) runs in background
        # and its reply goes out via send(). AT retries non-2xx callbacks.
        if not sms.text.strip():
            return web.Response(text="OK")
        if sms.id and self._dedup.is_duplicate(sms.id):
            return web.Response(text="OK")
        self._spawn(self._dispatch(text=sms.text, phone=sms.from_, message_id=sms.id or uuid.uuid4().hex))
        return web.Response(text="OK")

    async def _on_ussd(self, request: web.Request) -> web.Response:
        if not self._secret_ok(request):
            return web.Response(status=403, text="forbidden")
        try:
            form = at_client.parse_form_body(await request.text())
            req = at_client.parse_ussd_callback(form)
        except at_client.ATError as exc:
            logger.debug("Africa's Talking: bad USSD callback — %s", exc)
            return web.Response(status=400, text="END Bad request. Please dial again.")
        # AT retries USSD callbacks on timeout. respond() is deterministic, so a
        # duplicate is answered identically but with a no-op dispatch — the agent
        # turn must never start twice for one keypress.
        key = f"ussd:{req.session_id}:{req.text}"
        duplicate = self._dedup.is_duplicate(key)

        def dispatch(phone: str, message: str) -> None:
            if duplicate:
                return
            self._spawn(self._dispatch(text=message, phone=phone, message_id=uuid.uuid4().hex))

        reply = ussd.respond(req, dispatch)
        return web.Response(text=reply, content_type="text/plain")

    async def _on_delivery(self, request: web.Request) -> web.Response:
        """Delivery-report callbacks (opt-in on AT's side) — ack and ignore."""
        if not self._secret_ok(request):
            return web.Response(status=403, text="forbidden")
        return web.Response(text="OK")

    async def _dispatch(self, text: str, phone: str, message_id: str) -> None:
        """Build a MessageEvent and hand it to the base-class gateway router."""
        if not self._message_handler:
            return
        source = self.build_source(
            chat_id=phone, chat_name=phone, chat_type="dm", user_id=phone, user_name=phone
        )
        await self.handle_message(
            MessageEvent(
                text=text,
                message_type=MessageType.TEXT,
                source=source,
                message_id=message_id,
                timestamp=datetime.datetime.now(),
            )
        )


# ── plugin registration ──────────────────────────────────────────────────────


def check_requirements() -> bool:
    """PASSIVE probe: are credentials present right now? (never installs anything)"""
    return bool(get_scoped_secret("AT_USERNAME", "") and get_scoped_secret("AT_API_KEY", ""))


def validate_config(config) -> bool:
    extra = getattr(config, "extra", None)
    return bool(
        extra_or_secret(extra, "username", "AT_USERNAME")
        and extra_or_secret(extra, "api_key", "AT_API_KEY")
    )


def is_connected(config) -> bool:
    return validate_config(config)


def _env_enablement() -> Optional[dict]:
    """Seed ``PlatformConfig.extra`` from env BEFORE adapter construction, so
    env-only setups surface in ``hermes gateway status`` and cron delivery."""
    username = get_scoped_secret("AT_USERNAME", "").strip()
    api_key = get_scoped_secret("AT_API_KEY", "").strip()
    if not (username and api_key):
        return None
    return {
        "username": username,
        "api_key": api_key,
        **seed_extra_from_env(
            (
                ("AT_SENDER_ID", "sender_id", None),
                ("AT_CALLBACK_HOST", "callback_host", None),
                ("AT_CALLBACK_PORT", "callback_port", int),
                ("AT_WEBHOOK_SECRET", "webhook_secret", None),
            ),
            home_env="AT_HOME_CHANNEL",
        ),
    }


async def _send_sms_parts(username: str, api_key: str, sender_id: Optional[str], phone: str, message: str) -> dict[str, Any]:
    """Shared send loop for the standalone (out-of-process cron) path."""
    parts = at_client.chunk_sms(message)
    if not parts:
        return send_error("Africa's Talking standalone send: empty message")
    if not phone:
        return send_error("Africa's Talking standalone send: no destination phone number")
    last_id: Optional[str] = None
    for part in parts:
        try:
            parsed = await asyncio.to_thread(
                at_client.send_sms, username, api_key, [phone], part, sender_id
            )
        except at_client.ATError as exc:
            return send_error(f"Africa's Talking standalone send: {exc}")
        ok, detail = at_client.sms_response_summary(parsed)
        if not ok:
            return send_error(f"Africa's Talking standalone send rejected: {detail}")
        last_id = _recipient_message_id(parsed) or last_id
    return {"success": True, "message_id": last_id or uuid.uuid4().hex}


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[list[str]] = None,
    force_document: bool = False,
) -> dict[str, Any]:
    """Out-of-process cron delivery (``deliver=africastalking``): send one SMS
    burst via the REST API — no connection to hold open. ``thread_id`` /
    ``media_files`` are accepted for signature parity only (SMS is text-only)."""
    extra = getattr(pconfig, "extra", {}) or {}
    username = extra_or_secret(extra, "username", "AT_USERNAME")
    api_key = extra_or_secret(extra, "api_key", "AT_API_KEY")
    sender_id = extra_or_secret(extra, "sender_id", "AT_SENDER_ID") or None
    if not (username and api_key):
        return send_error("Africa's Talking standalone send: AT_USERNAME and AT_API_KEY must be configured")
    return await _send_sms_parts(username, api_key, sender_id, chat_id, message)


def interactive_setup() -> None:
    """``hermes gateway setup`` flow. Lazy hermes_cli imports keep this module
    importable outside the CLI; these are the documented PUBLIC setup modules
    from the platform-plugin guide (gateway_setup_wizard / setup / cli_output /
    config) — no private core imports."""
    from hermes_cli.cli_output import print_info, print_success, print_warning
    from hermes_cli.config import get_env_value, save_env_value
    from hermes_cli.gateway_setup_wizard import confirm_reconfigure, print_setup_header, prompt_csv
    from hermes_cli.setup import prompt

    print_setup_header("Africa's Talking")
    if not confirm_reconfigure("Africa's Talking", "AT_USERNAME", "AT_API_KEY"):
        return
    print_info("Chat with Hermes via SMS + USSD on any phone — feature phones included.")
    print_info("The free sandbox works for testing: https://account.africastalking.com/auth/register")
    print()
    username = prompt("App username (sandbox app: 'sandbox')", default=get_env_value("AT_USERNAME") or "")
    if not username:
        print_warning("Username is required — skipping Africa's Talking setup")
        return
    api_key = prompt("API key (AT dashboard → your app → Settings → API Key)", password=True)
    if not api_key:
        print_warning("API key is required — skipping Africa's Talking setup")
        return
    save_env_value("AT_USERNAME", username.strip())
    save_env_value("AT_API_KEY", api_key.strip())
    sender_id = prompt("Sender ID / shortcode (optional — production only)", default=get_env_value("AT_SENDER_ID") or "")
    save_env_value("AT_SENDER_ID", sender_id.strip())
    port = prompt(f"Callback port (default {DEFAULT_CALLBACK_PORT})", default=get_env_value("AT_CALLBACK_PORT") or "")
    if port:
        try:
            save_env_value("AT_CALLBACK_PORT", str(int(port)))
        except ValueError:
            print_warning(f"Invalid port — using default {DEFAULT_CALLBACK_PORT}")
    secret = prompt("Webhook secret (recommended — appended to callback URLs as ?secret=…)", password=True)
    if secret:
        save_env_value("AT_WEBHOOK_SECRET", secret.strip())
    allowed = prompt_csv("Phone numbers allowed to use the agent (comma-separated, empty = nobody until pairing)",
                         get_env_value("AT_ALLOWED_USERS") or "")
    save_env_value("AT_ALLOWED_USERS", allowed)
    print()
    print_info("Next steps:")
    print_info("  1. Expose the callback port, e.g.:  cloudflared tunnel --url http://127.0.0.1:8055")
    print_info("  2. In the AT dashboard set your app's callback URLs to")
    print_info("       https://<your-tunnel>/sms?secret=…   and   https://<your-tunnel>/ussd?secret=…")
    print_success("Configuration saved to ~/.hermes/.env")
    print_info("Restart the gateway for changes to take effect: hermes gateway restart")


def register(ctx):
    """Plugin entry point: called by the Hermes plugin system."""
    kwargs: dict[str, Any] = dict(
        name="africastalking",
        label="Africa's Talking",
        dashboard_description="Chat with Hermes via SMS and USSD on any phone through Africa's Talking.",
        docs_url="https://github.com/geraldkombo/hermes-africastalking#readme",
        adapter_factory=AfricaTalkingAdapter,
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["AT_USERNAME", "AT_API_KEY"],
        install_hint="No extra packages needed (aiohttp ships with Hermes core; the AT client is stdlib)",
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="AT_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="AT_ALLOWED_USERS",
        allow_all_env="AT_ALLOW_ALL_USERS",
        max_message_length=480,  # 3 SMS parts — the gateway chunker's budget
        emoji="📱",
        pii_safe=True,  # phone numbers on every event
        allow_update_command=True,
        platform_hint=PLATFORM_HINT,
    )
    # Feature-detect newer PlatformEntry fields so the plugin still loads on
    # older Hermes cores (per the platform-plugin guide).
    try:
        from gateway.platform_registry import PlatformEntry

        known = {f.name for f in dataclasses.fields(PlatformEntry)}
    except Exception:  # pragma: no cover — very old cores
        known = set()
    if "allowlist_suppresses_pairing" in known:
        # An owner who set AT_ALLOWED_USERS already restricted access — don't
        # send pairing codes to every other texter on a public shortcode.
        kwargs["allowlist_suppresses_pairing"] = True
    ctx.register_platform(**kwargs)
