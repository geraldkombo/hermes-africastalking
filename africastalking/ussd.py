"""USSD session state machine — a pure function of the callback input.

Africa's Talking is stateless: every callback carries the user's accumulated
input (``text`` = ``"1"``, ``"1*2"``, ``"1*hello"``), so the whole menu flow
is derivable from the request alone. ``respond()`` never touches the network;
the single side effect is one ``dispatch(phone, message)`` call when the user
submits a question, which the adapter turns into an agent run answered by SMS
(USSD callbacks must return within seconds — agent turns take longer, so the
SMS hand-off is the industry-standard pattern).
"""

from __future__ import annotations

from typing import Callable

from .at_client import UssdRequest, ussd_continue, ussd_end

MAIN_MENU = "Hermes Agent\n1. Ask Hermes a question\n2. About this service\n0. Exit"
ASK_PROMPT = "Type your question below.\nHermes will answer by SMS shortly."
ABOUT = (
    "Hermes Agent over USSD + SMS.\n"
    "Ask anything - the answer arrives as an SMS.\n"
    "No internet, no app, any phone.\n"
    "Powered by Africa's Talking."
)
ACK = "Received! Hermes is thinking - watch for an SMS shortly."
BYE = "Goodbye!"
INVALID = "Invalid choice. Please dial again."


def respond(req: UssdRequest, dispatch: Callable[[str, str], None]) -> str:
    """Compute the USSD reply (``CON …`` / ``END …``) for one callback.

    ``dispatch(phone_number, message)`` is called at most once, when the user
    submits a question for the agent. Pure otherwise — safe to re-run for
    duplicate callbacks with a no-op dispatch.
    """
    if req.is_first_prompt:
        return ussd_continue(MAIN_MENU)

    choice = req.steps[0]
    if choice == "0":
        return ussd_end(BYE)

    if choice == "1":
        if len(req.steps) == 1:
            return ussd_continue(ASK_PROMPT)
        # "*" inside the user's message arrives as extra steps — rejoin them.
        message = "*".join(req.steps[1:]).strip()
        if not message:
            return ussd_continue(ASK_PROMPT)
        dispatch(req.phone_number, message)
        return ussd_end(ACK)

    if choice == "2":
        return ussd_end(ABOUT)

    return ussd_end(INVALID)
