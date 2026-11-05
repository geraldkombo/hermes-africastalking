"""Offline tests for at_client — pure stdlib, no network, no Hermes import.

Run from the repo root: python -m unittest discover -s africastalking/tests -t . -v
"""

from __future__ import annotations

import json
import unittest
import urllib.error
from unittest import mock

from africastalking import at_client
from africastalking.at_client import (
    ATError,
    InboundSms,
    UssdRequest,
    chunk_sms,
    parse_form_body,
    parse_sms_callback,
    parse_ussd_callback,
    send_sms,
    sms_response_summary,
    ussd_continue,
    ussd_end,
)


class TestUssdRequest(unittest.TestCase):
    def test_first_prompt(self):
        req = UssdRequest("s1", "+254712345678", "*384*23216#", "")
        self.assertTrue(req.is_first_prompt)
        self.assertEqual(req.steps, [])
        self.assertEqual(req.last_input, "")

    def test_navigation_steps(self):
        req = UssdRequest("s1", "+254712345678", "*384*23216#", "1*2*hello world")
        self.assertFalse(req.is_first_prompt)
        self.assertEqual(req.steps, ["1", "2", "hello world"])
        self.assertEqual(req.last_input, "hello world")


class TestFormatters(unittest.TestCase):
    def test_continue_and_end(self):
        self.assertEqual(ussd_continue("Pick one"), "CON Pick one")
        self.assertEqual(ussd_end("Bye"), "END Bye")

    def test_control_chars_stripped(self):
        # CON/END responses must never contain raw control characters.
        self.assertEqual(ussd_continue("a\x00b\x1bc"), "CON a b c")

    def test_none_safe(self):
        self.assertEqual(ussd_end(None), "END")


class TestParsers(unittest.TestCase):
    def test_parse_form_body(self):
        form = parse_form_body(b"sessionId=abc&phoneNumber=%2B254712345678&text=1*2")
        self.assertEqual(
            form, {"sessionId": "abc", "phoneNumber": "+254712345678", "text": "1*2"}
        )

    def test_parse_form_body_str_and_blank(self):
        form = parse_form_body("text=&sessionId=x")
        self.assertEqual(form["text"], "")

    def test_parse_ussd_callback_ok(self):
        req = parse_ussd_callback(
            {
                "sessionId": "ATXid_123",
                "phoneNumber": "+254712345678",
                "serviceCode": "*384*23216#",
                "text": "1",
            }
        )
        self.assertEqual(req.session_id, "ATXid_123")
        self.assertEqual(req.steps, ["1"])

    def test_parse_ussd_callback_missing_fields(self):
        with self.assertRaises(ATError):
            parse_ussd_callback({"phoneNumber": "+254712345678"})
        with self.assertRaises(ATError):
            parse_ussd_callback({"sessionId": "x"})

    def test_parse_sms_callback_ok(self):
        sms = parse_sms_callback(
            {
                "id": "ATXid_999",
                "text": "hello hermes",
                "from": "+254712345678",
                "to": "12345",
                "linkId": "lk1",
                "date": "2026-11-05 10:00:00",
            }
        )
        self.assertIsInstance(sms, InboundSms)
        self.assertEqual(sms.from_, "+254712345678")
        self.assertEqual(sms.text, "hello hermes")

    def test_parse_sms_callback_missing_sender(self):
        with self.assertRaises(ATError):
            parse_sms_callback({"text": "hi"})


class TestChunkSms(unittest.TestCase):
    def test_short_message_single_part(self):
        self.assertEqual(chunk_sms("hello"), ["hello"])

    def test_empty(self):
        self.assertEqual(chunk_sms(""), [])
        self.assertEqual(chunk_sms("   "), [])

    def test_word_boundary_split(self):
        text = " ".join(f"word{i}" for i in range(60))  # ~354 chars
        parts = chunk_sms(text, limit=160)
        self.assertGreaterEqual(len(parts), 3)
        for p in parts:
            self.assertLessEqual(len(p), 160)
        # Reassembly preserves words (single spaces at joins).
        self.assertEqual(" ".join(parts), text)

    def test_long_word_hard_split(self):
        parts = chunk_sms("x" * 400, limit=160)
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(len(p) <= 160 for p in parts))

    def test_max_parts_cap(self):
        text = " ".join(f"word{i}" for i in range(500))
        parts = chunk_sms(text, limit=160, max_parts=2)
        self.assertEqual(len(parts), 3)  # 2 content parts + truncation notice
        self.assertEqual(parts[-1], "... (reply truncated)")


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestSendSms(unittest.TestCase):
    def test_validation_errors_are_offline(self):
        with self.assertRaises(ATError):
            send_sms("u", "k", [], "hi")
        with self.assertRaises(ATError):
            send_sms("u", "k", ["+254712345678"], "")
        with self.assertRaises(ATError):
            send_sms("", "", ["+254712345678"], "hi")

    def test_success_response(self):
        payload = {
            "SMSMessageData": {
                "Recipients": [
                    {"number": "+254712345678", "statusCode": 101, "status": "Success"}
                ],
                "Response": "Sent with success",
            }
        }
        with mock.patch(
            "urllib.request.urlopen",
            return_value=FakeResponse(200, payload),
        ) as m:
            parsed = send_sms("sandbox", "key", ["+254712345678"], "hello")
        self.assertEqual(parsed, payload)
        # Verify the request shape AT expects.
        req = m.call_args[0][0]
        self.assertEqual(req.full_url, at_client.API_BASE + at_client.MESSAGING_PATH)
        self.assertEqual(req.get_header("Apikey"), "key")
        body = req.data.decode()
        self.assertIn("username=sandbox", body)
        self.assertIn("message=hello", body)
        ok, detail = sms_response_summary(parsed)
        self.assertTrue(ok)
        self.assertEqual(detail, "Sent with success")

    def test_http_error_maps_to_atererror(self):
        err = urllib.error.HTTPError(
            at_client.API_BASE, 401, "Unauthorized", {}, None  # type: ignore[arg-type]
        )
        err.read = lambda: b'{"error":"invalid api key"}'  # type: ignore[method-assign]
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(ATError) as ctx:
                send_sms("u", "badkey", ["+254712345678"], "hi")
        self.assertEqual(ctx.exception.status, 401)
        self.assertIn("invalid api key", ctx.exception.body)

    def test_network_error_maps_to_atererror(self):
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("dns failure"),
        ):
            with self.assertRaises(ATError) as ctx:
                send_sms("u", "k", ["+254712345678"], "hi")
        self.assertIn("Could not reach", str(ctx.exception))

    def test_non_json_response(self):
        class Html(FakeResponse):
            def read(self):
                return b"<html>502 bad gateway</html>"

        with mock.patch(
            "urllib.request.urlopen", return_value=Html(200, {})
        ):
            with self.assertRaises(ATError):
                send_sms("u", "k", ["+254712345678"], "hi")

    def test_summary_failure_shapes(self):
        ok, _ = sms_response_summary({"SMSMessageData": {"Response": "Invalid sender"}})
        self.assertFalse(ok)
        ok, _ = sms_response_summary({"nonsense": 1})
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
