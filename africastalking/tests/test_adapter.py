"""Offline tests for the adapter's security- and contract-critical paths.

Uses hermes_stubs to import africastalking.adapter without a Hermes install.
Covers: ?secret= guard (incl. the non-ASCII TypeError regression), SMS/USSD
duplicate-callback dedup, empty-message ack, burst send id/retry semantics,
and the standalone (cron) sender.

Run from the repo root: python -m unittest discover -s africastalking/tests -t . -v
"""

from __future__ import annotations

import asyncio
import types
import unittest
import urllib.parse
from unittest import mock

from africastalking.tests import hermes_stubs

hermes_stubs.install()

from africastalking import at_client
from africastalking import adapter as adapter_mod
from africastalking.adapter import AfricaTalkingAdapter, _standalone_send

PHONE = "+254712345678"


def make_adapter(**extra) -> AfricaTalkingAdapter:
    cfg = types.SimpleNamespace(extra={"username": "sandbox", "api_key": "test-key", **extra})
    return AfricaTalkingAdapter(cfg)


class FakeRequest:
    def __init__(self, body: str = "", query: dict | None = None):
        self._body = body
        self.query = query or {}

    async def text(self) -> str:
        return self._body


def sms_body(msg_id: str = "ATXid_1", text: str = "hello hermes") -> str:
    # Real AT callbacks are properly form-encoded — build them the same way
    # (urlencode escapes literal '+' as %2B; hand-rolled replace(' ', '+') lied).
    return urllib.parse.urlencode(
        {"id": msg_id, "text": text, "from": PHONE, "to": "12345", "linkId": "lk", "date": "now"}
    )


def ussd_body(text: str, session: str = "ATXid_s1") -> str:
    return urllib.parse.urlencode(
        {"sessionId": session, "phoneNumber": PHONE, "serviceCode": "*384", "text": text}
    )


async def drain(adapter: AfricaTalkingAdapter) -> None:
    if adapter._tasks:
        await asyncio.gather(*list(adapter._tasks), return_exceptions=True)


class TestSecretGuard(unittest.TestCase):
    def test_disabled_secret_allows_all(self):
        a = make_adapter()
        self.assertTrue(a._secret_ok(FakeRequest()))
        self.assertTrue(a._secret_ok(FakeRequest(query={"secret": "anything"})))

    def test_matching_and_wrong(self):
        a = make_adapter(webhook_secret="s3cr3t")
        self.assertTrue(a._secret_ok(FakeRequest(query={"secret": "s3cr3t"})))
        self.assertFalse(a._secret_ok(FakeRequest(query={"secret": "wrong"})))
        self.assertFalse(a._secret_ok(FakeRequest()))

    def test_non_ascii_param_returns_false_not_typeerror(self):
        # Regression: compare_digest(str, str) raised TypeError on non-ASCII,
        # turning every attacker-chosen ?secret=%C3%A9 into a 500.
        a = make_adapter(webhook_secret="s3cr3t")
        self.assertFalse(a._secret_ok(FakeRequest(query={"secret": "é"})))

    def test_non_ascii_configured_secret_matches(self):
        a = make_adapter(webhook_secret="pässwörd")
        self.assertTrue(a._secret_ok(FakeRequest(query={"secret": "pässwörd"})))
        self.assertFalse(a._secret_ok(FakeRequest(query={"secret": "password"})))

    def test_handler_403_without_secret(self):
        a = make_adapter(webhook_secret="s3cr3t")

        async def run():
            resp = await a._on_sms(FakeRequest(sms_body()))
            self.assertEqual(resp.status, 403)
            resp = await a._on_ussd(FakeRequest(ussd_body("1*hi")))
            self.assertEqual(resp.status, 403)
            self.assertEqual(a.handled, [])

        asyncio.run(run())


class TestSmsInbound(unittest.IsolatedAsyncioTestCase):
    async def test_inbound_sms_reaches_gateway(self):
        a = make_adapter()
        resp = await a._on_sms(FakeRequest(sms_body("ATXid_9", "what is 2+2")))
        self.assertEqual(resp.status, 200)
        await drain(a)
        self.assertEqual(len(a.handled), 1)
        ev = a.handled[0]
        self.assertEqual(ev.text, "what is 2+2")
        self.assertEqual(ev.message_id, "ATXid_9")
        self.assertEqual(ev.source["chat_id"], PHONE)
        self.assertEqual(ev.source["chat_type"], "dm")

    async def test_duplicate_id_dispatches_once(self):
        a = make_adapter()
        body = FakeRequest(sms_body("ATXid_dup", "hi"))
        await a._on_sms(body)
        await a._on_sms(FakeRequest(sms_body("ATXid_dup", "hi")))
        await drain(a)
        self.assertEqual(len(a.handled), 1)

    async def test_empty_text_acked_without_dispatch(self):
        a = make_adapter()
        resp = await a._on_sms(FakeRequest("id=ATXid_e&text=&from=%2B254712345678"))
        self.assertEqual(resp.status, 200)
        await drain(a)
        self.assertEqual(a.handled, [])

    async def test_bad_form_is_400(self):
        a = make_adapter()
        resp = await a._on_sms(FakeRequest("text=hi"))  # no sender
        self.assertEqual(resp.status, 400)


class TestUssdInbound(unittest.IsolatedAsyncioTestCase):
    async def test_menu_then_question_handoff(self):
        a = make_adapter()
        r1 = await a._on_ussd(FakeRequest(ussd_body("")))
        self.assertTrue(r1.text.startswith("CON "))
        r2 = await a._on_ussd(FakeRequest(ussd_body("1")))
        self.assertTrue(r2.text.startswith("CON "))
        r3 = await a._on_ussd(FakeRequest(ussd_body("1*weather in mombasa")))
        self.assertTrue(r3.text.startswith("END "))
        await drain(a)
        self.assertEqual(len(a.handled), 1)
        self.assertEqual(a.handled[0].text, "weather in mombasa")
        self.assertEqual(a.handled[0].source["chat_id"], PHONE)

    async def test_duplicate_callback_never_starts_second_turn(self):
        a = make_adapter()
        body = ussd_body("1*pay bills")
        ra = await a._on_ussd(FakeRequest(body))
        rb = await a._on_ussd(FakeRequest(body))
        await drain(a)
        self.assertEqual(ra.text, rb.text)  # deterministic re-answer
        self.assertEqual(len(a.handled), 1)  # ...but only ONE agent turn

    async def test_bad_ussd_form_ends_cleanly(self):
        a = make_adapter()
        resp = await a._on_ussd(FakeRequest("text=1"))  # missing sessionId/phone
        self.assertEqual(resp.status, 400)
        self.assertTrue(resp.text.startswith("END "))


def _payload(mid: str) -> dict:
    return {
        "SMSMessageData": {
            "Response": "Sent with success",
            "Recipients": [{"number": PHONE, "statusCode": 101, "messageId": mid}],
        }
    }


class TestSendBurst(unittest.IsolatedAsyncioTestCase):
    async def test_multi_part_ids_follow_base_contract(self):
        a = make_adapter()
        counter = iter(f"m{i}" for i in range(1, 10))
        with mock.patch.object(
            at_client, "send_sms", side_effect=lambda *args, **kw: _payload(next(counter))
        ) as m:
            long_text = " ".join(f"word{i}" for i in range(80))  # >160 chars → multiple parts
            result = await a.send(PHONE, long_text)
        self.assertTrue(result.success)
        self.assertGreater(m.call_count, 1)
        # message_id = LAST part; continuation = earlier ids in send order.
        self.assertEqual(result.message_id, f"m{m.call_count}")
        self.assertEqual(result.continuation_message_ids, tuple(f"m{i}" for i in range(1, m.call_count)))

    async def test_mid_burst_failure_is_not_retryable(self):
        # A base-class auto-retry would re-send earlier parts as duplicate paid SMS.
        a = make_adapter()
        with mock.patch.object(
            at_client,
            "send_sms",
            side_effect=[_payload("m1"), at_client.ATError("boom", status=500)],
        ):
            long_text = " ".join(f"word{i}" for i in range(80))
            result = await a.send(PHONE, long_text)
        self.assertFalse(result.success)
        self.assertFalse(result.retryable)

    async def test_first_part_failure_is_retryable(self):
        a = make_adapter()
        with mock.patch.object(at_client, "send_sms", side_effect=at_client.ATError("boom", status=503)):
            result = await a.send(PHONE, "short")
        self.assertFalse(result.success)
        self.assertTrue(result.retryable)

    async def test_empty_content_fails_cleanly(self):
        a = make_adapter()
        result = await a.send(PHONE, "   ")
        self.assertFalse(result.success)
        self.assertEqual(result.error, "empty message")


class TestStandaloneSender(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        hermes_stubs.ENV.clear()

    def tearDown(self):
        hermes_stubs.ENV.clear()

    async def test_sends_with_credentials(self):
        hermes_stubs.ENV.update({"AT_USERNAME": "sandbox", "AT_API_KEY": "k"})
        pconfig = types.SimpleNamespace(extra={})
        with mock.patch.object(at_client, "send_sms", return_value=_payload("cron1")):
            out = await _standalone_send(pconfig, PHONE, "daily brief")
        self.assertTrue(out["success"])
        self.assertEqual(out["message_id"], "cron1")

    async def test_missing_credentials_error(self):
        pconfig = types.SimpleNamespace(extra={})
        out = await _standalone_send(pconfig, PHONE, "hi")
        self.assertIn("error", out)

    async def test_api_rejection_surfaces_error(self):
        hermes_stubs.ENV.update({"AT_USERNAME": "sandbox", "AT_API_KEY": "k"})
        pconfig = types.SimpleNamespace(extra={})
        with mock.patch.object(at_client, "send_sms", side_effect=at_client.ATError("nope", status=401)):
            out = await _standalone_send(pconfig, PHONE, "hi")
        self.assertIn("error", out)


class TestEnvEnablement(unittest.TestCase):
    def setUp(self):
        hermes_stubs.ENV.clear()

    def tearDown(self):
        hermes_stubs.ENV.clear()

    def test_none_without_credentials(self):
        self.assertIsNone(adapter_mod._env_enablement())

    def test_seeds_extras_and_home_channel(self):
        hermes_stubs.ENV.update(
            {
                "AT_USERNAME": "sandbox",
                "AT_API_KEY": "k",
                "AT_CALLBACK_PORT": "9000",
                "AT_HOME_CHANNEL": PHONE,
            }
        )
        seed = adapter_mod._env_enablement()
        self.assertEqual(seed["username"], "sandbox")
        self.assertEqual(seed["callback_port"], 9000)
        self.assertEqual(seed["home_channel"]["chat_id"], PHONE)

    def test_check_requirements(self):
        self.assertFalse(adapter_mod.check_requirements())
        hermes_stubs.ENV.update({"AT_USERNAME": "u", "AT_API_KEY": "k"})
        self.assertTrue(adapter_mod.check_requirements())


if __name__ == "__main__":
    unittest.main(verbosity=2)
