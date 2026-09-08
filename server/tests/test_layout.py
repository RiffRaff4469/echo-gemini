"""UI-BRIEF-14 layout focus: protocol round trip + the arbitration rules."""

from __future__ import annotations

import protocol as P


def test_layout_msg_round_trips() -> None:
    for wire in ("home", "music", "chat"):
        msg = P.decode(f'{{"v":1,"t":"layout","focus":"{wire}"}}')
        assert isinstance(msg, P.LayoutMsg)
        assert msg.focus.value == wire


def test_unknown_focus_falls_back_to_home() -> None:
    msg = P.decode('{"v":1,"t":"layout","focus":"hologram"}')
    assert isinstance(msg, P.LayoutMsg)
    assert msg.focus is P.LayoutFocus.HOME


def test_layout_encodes_the_full_focus() -> None:
    out = P.LayoutMsg(focus=P.LayoutFocus.MUSIC).fields()
    assert out == {"focus": "music"}
