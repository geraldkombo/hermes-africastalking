"""Africa's Talking REST client + callback parsers — pure stdlib, no dependencies.

Outbound: SMS sending via https://api.africastalking.com/version1/messaging.
Inbound:  parsers for the two callback shapes AT posts to your webhook
          (USSD: sessionId/phoneNumber/serviceCode/text — SMS: id/text/from/to/linkId/date).

All functions are synchronous and side-effect free except ``send_sms`` (one HTTP
POST). The Hermes adapter runs ``send_sms`` via ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Iterable, Optional

API_BASE = "https://api.africastalking.com"
MESSAGING_PATH = "/version1/messaging"

# AT posts callbacks as application/x-www-form-urlencoded.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class ATError(Exception):
    """Any Africa's Talking API failure, with the upstream detail when present."""

    def __init__(self, message: str, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass(frozen=True)
class UssdRequest:
    """One USSD callback: the user's accumulated menu input for this session."""

    session_id: str
    phone_number: str
    service_code: str
    text: str  # "" on first prompt, then "1", "1*2", "1*2*hello" as the user navigates

    @property
    def is_first_prompt(self) -> bool:
        return self.text == ""

    @property
    def steps(self) -> list[str]:
        return self.text.split("*") if self.text else []

    @property
    def last_input(self) -> str:
        s = self.steps
        return s[-1] if s else ""


@dataclass(frozen=True)
class InboundSms:
    """One inbound-SMS callback posted by AT."""

    id: str
    text: str
    from_: str
    to: str
    link_id: str = ""
    date: str = ""


def ussd_continue(message: str) -> str:
    """Response that keeps the USSD session open and shows ``message``."""
    return ("CON " + _clean(message)).rstrip()


def ussd_end(message: str) -> str:
    """Response that closes the USSD session and shows ``message``."""
    return ("END " + _clean(message)).rstrip()


def _clean(text: str) -> str:
    return _CONTROL_CHARS.sub(" ", str(text or "")).strip()


def parse_form_body(body: bytes | str) -> dict[str, str]:
    """Parse an x-www-form-urlencoded callback body into single-valued keys."""
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    parsed = urllib.parse.parse_qs(body, keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


def parse_ussd_callback(form: dict) -> UssdRequest:
    """Build a UssdRequest from the callback form; raises ATError when unusable."""
    session_id = str(form.get("sessionId") or "").strip()
    phone = str(form.get("phoneNumber") or "").strip()
    if not session_id or not phone:
        raise ATError("USSD callback missing sessionId/phoneNumber")
    return UssdRequest(
        session_id=session_id,
        phone_number=phone,
        service_code=str(form.get("serviceCode") or "").strip(),
        text=str(form.get("text") or "").strip(),
    )


def parse_sms_callback(form: dict) -> InboundSms:
    """Build an InboundSms from the callback form; raises ATError when unusable."""
    text = str(form.get("text") or "")
    sender = str(form.get("from") or form.get("phoneNumber") or "").strip()
    if not sender:
        raise ATError("SMS callback missing sender")
    return InboundSms(
        id=str(form.get("id") or "").strip(),
        text=text,
        from_=sender,
        to=str(form.get("to") or "").strip(),
        link_id=str(form.get("linkId") or "").strip(),
        date=str(form.get("date") or "").strip(),
    )


def chunk_sms(text: str, limit: int = 160, max_parts: int = 8) -> list[str]:
    """Split a long reply into SMS-sized parts on word boundaries.

    Parts beyond ``max_parts`` are replaced by a single trailing ellipsis part,
    so an agent essay can never turn into an unbounded SMS bill.
    """
    text = _clean(text)
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    words = text.split(" ")
    parts: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else current + " " + word
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        # A single word longer than the limit gets hard-split.
        while len(word) > limit:
            parts.append(word[:limit])
            word = word[limit:]
        current = word
    if current:
        parts.append(current)
    if len(parts) > max_parts:
        parts = parts[:max_parts]
        parts.append("... (reply truncated)")
    return parts


def send_sms(
    username: str,
    api_key: str,
    recipients: Iterable[str],
    message: str,
    sender_id: Optional[str] = None,
    base_url: str = API_BASE,
    timeout: float = 30.0,
) -> dict:
    """Send one SMS to one or more recipients. Returns AT's parsed JSON response.

    Raises ATError on network failure, non-2xx status, or an AT-level error
    (``SMSMessageData.Response`` other than a success phrase).
    """
    to = ",".join(r.strip() for r in recipients if r and r.strip())
    if not to:
        raise ATError("send_sms needs at least one recipient")
    if not _clean(message):
        raise ATError("send_sms needs a non-empty message")
    if not username or not api_key:
        raise ATError("Africa's Talking username/API key not configured")

    fields = {"username": username, "to": to, "message": _clean(message)}
    if sender_id:
        fields["from"] = sender_id
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        base_url + MESSAGING_PATH,
        data=data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "apiKey": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https URL, fixed path)
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise ATError(f"Africa's Talking HTTP {exc.code}: {body[:200]}", status=exc.code, body=body) from exc
    except urllib.error.URLError as exc:
        raise ATError(f"Could not reach Africa's Talking: {exc.reason}") from exc

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise ATError(f"Unexpected non-JSON response (HTTP {status}): {raw[:200]}", status=status, body=raw)

    if status >= 300:
        raise ATError(f"Africa's Talking error (HTTP {status}): {raw[:200]}", status=status, body=raw)
    return parsed


def sms_response_summary(parsed: dict) -> tuple[bool, str]:
    """(ok, human summary) from AT's messaging response JSON."""
    try:
        data = parsed["SMSMessageData"]
        response = str(data.get("Response") or "")
        recipients = data.get("Recipients") or []
        ok = response.lower().startswith(("sent", "success")) or bool(
            recipients and all(int(r.get("statusCode", 0)) == 101 for r in recipients)
        )
        detail = response or json.dumps(data)[:160]
        return ok, detail
    except (KeyError, TypeError, ValueError):
        return False, json.dumps(parsed)[:200]
