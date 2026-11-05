"""Offline tests for the USSD state machine — no network, no Hermes import.

Run from the repo root: python -m unittest discover -s africastalking/tests -t . -v
"""

from __future__ import annotations

import unittest

from africastalking.at_client import UssdRequest
from africastalking.ussd import ACK, ABOUT, ASK_PROMPT, BYE, INVALID, MAIN_MENU, respond

PHONE = "+254712345678"


def req(text: str, session: str = "ATXid_1") -> UssdRequest:
    return UssdRequest(session, PHONE, "*384*23216#", text)


class RecordingDispatch:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, phone: str, message: str) -> None:
        self.calls.append((phone, message))


class TestUssdFlow(unittest.TestCase):
    def test_first_prompt_shows_menu(self):
        d = RecordingDispatch()
        out = respond(req(""), d)
        self.assertTrue(out.startswith("CON "))
        self.assertIn("Hermes Agent", out)
        self.assertEqual(out, "CON " + MAIN_MENU)
        self.assertEqual(d.calls, [])

    def test_choice_1_prompts_for_message(self):
        d = RecordingDispatch()
        self.assertEqual(respond(req("1"), d), "CON " + ASK_PROMPT)
        self.assertEqual(d.calls, [])

    def test_choice_1_dispatches_and_ends(self):
        d = RecordingDispatch()
        out = respond(req("1*What is the weather in Mombasa?"), d)
        self.assertEqual(out, "END " + ACK)
        self.assertEqual(d.calls, [(PHONE, "What is the weather in Mombasa?")])

    def test_choice_1_empty_message_reprompts(self):
        d = RecordingDispatch()
        self.assertEqual(respond(req("1*"), d), "CON " + ASK_PROMPT)
        self.assertEqual(d.calls, [])

    def test_star_inside_message_is_rejoined(self):
        d = RecordingDispatch()
        respond(req("1*2*3=6?"), d)
        self.assertEqual(d.calls, [(PHONE, "2*3=6?")])

    def test_choice_2_about(self):
        d = RecordingDispatch()
        self.assertEqual(respond(req("2"), d), "END " + ABOUT)

    def test_choice_0_exit(self):
        d = RecordingDispatch()
        self.assertEqual(respond(req("0"), d), "END " + BYE)

    def test_invalid_choice(self):
        d = RecordingDispatch()
        self.assertEqual(respond(req("9"), d), "END " + INVALID)
        self.assertEqual(d.calls, [])

    def test_dispatch_called_at_most_once(self):
        # AT retries duplicate callbacks with the same accumulated text; the
        # adapter dedups, but the state machine must also never double-call
        # within one respond().
        d = RecordingDispatch()
        respond(req("1*hello"), d)
        self.assertEqual(len(d.calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
