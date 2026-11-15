"""The stub's reply for the status line probe: every request gets one line of text and ends the turn."""


def reply(body):
    return "main", [{"type": "text", "text": "STUB-REPLY"}], "end_turn"
