"""UI-BRIEF-16: the Live-session leg of visual panels -- selection injection.

Panel push/select mapping at the hub is covered in test_panels.py. What lives
here is the session machinery: ``choose()`` hands a selection to the model as
text, gated so it never barges the model's own turn, plus the tool dispatch
that turns show_options/show_list/clear_panel calls into hub actions.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from gemini_live import (
    CLEAR_PANEL_TOOL,
    SHOW_LIST_TOOL,
    SHOW_OPTIONS_TOOL,
    LiveSessionManager,
)
from test_session import (  # shared fakes from the session suite
    FakeConnector,
    FakeLiveSession,
    RecordingSink,
    make_config,
    settle,
)

# --- choose(): feeding a selection to the model -----------------------------


async def test_choose_returns_false_with_no_session_running() -> None:
    manager = LiveSessionManager(make_config(), RecordingSink())
    assert manager.choose("the user chose Paris") is False


async def test_choose_in_an_active_session_delivers_the_text() -> None:
    """A tap after the model finished its turn goes out immediately -- the
    quiet window is exactly the moment the user is expected to answer."""
    session = FakeLiveSession()
    sink = RecordingSink()
    manager = LiveSessionManager(
        make_config(), sink, connector=FakeConnector(session)
    )
    await manager.start("trivia")
    await settle()

    # Model asked the question and finished (turn_complete arms the window).
    session.emit_audio(b"\xaa" * 40)
    await settle()
    session.emit_content(turn_complete=True)
    await settle()
    assert session.text_in == []

    assert manager.choose("the user chose Paris") is True
    await asyncio.sleep(0.5)  # real time: the choose pump polls on 0.25 s ticks
    assert session.text_in == ["the user chose Paris"]


async def test_choose_waits_until_the_model_finishes_speaking() -> None:
    """A tap WHILE the model is still talking is queued, never barged in on;
    it is delivered the moment the current turn ends (UI-BRIEF-16 spec)."""
    session = FakeLiveSession()
    manager = LiveSessionManager(
        make_config(), RecordingSink(), connector=FakeConnector(session)
    )
    await manager.start("trivia")
    await settle()

    # Model mid-answer: audio flowing, turn NOT complete yet.
    session.emit_audio(b"\xbb" * 40)
    await settle()
    assert manager.choose("the user chose Rome") is True

    # Real time passes while the model is still speaking: nothing may go out --
    # the pump's poll tick (0.25 s) is far shorter than this, so an ungated
    # delivery would have happened by now. Sending would cut the model off.
    await asyncio.sleep(0.6)
    assert session.text_in == [], "must not barge an in-flight model answer"

    # Turn ends -> the queued selection is delivered. The choose pump polls on
    # 0.25 s ticks and its barge gate sleeps 0.05 s, so give it real time.
    session.emit_content(turn_complete=True)
    await asyncio.sleep(0.5)
    assert session.text_in == ["the user chose Rome"]


# --- tool dispatch: show_options / show_list / clear_panel ------------------


class PanelSink(RecordingSink):
    """A sink that records the panel-tool calls like the real Hub does."""

    def __init__(self) -> None:
        super().__init__()
        self.panels: list[tuple[str, dict]] = []
        self.clears = 0

    def show_options(self, question, options):
        self.panels.append(("show_options", {"question": question, "options": options}))
        return {"ok": True, "displayed": len(options) if options else 0}

    def show_list(self, title, items):
        self.panels.append(("show_list", {"title": title, "items": items}))
        return {"ok": True, "displayed": len(items) if items else 0}

    def clear_panel(self):
        self.clears += 1
        return {"ok": True}


async def _call(manager: LiveSessionManager, name: str, args: dict):
    """Run one function call through the manager's tool runner."""
    call = SimpleNamespace(name=name, id="call-p", args=args)
    return await manager._run_tool(call)


async def test_show_options_routes_to_the_sink() -> None:
    sink = PanelSink()
    manager = LiveSessionManager(make_config(), sink)
    result = await _call(
        manager,
        SHOW_OPTIONS_TOOL,
        {"question": "Which is a planet?", "options": ["Mercury", "Venus", "Earth"]},
    )
    assert result["ok"] is True
    assert sink.panels[0][0] == "show_options"
    assert sink.panels[0][1]["question"] == "Which is a planet?"


async def test_show_list_routes_to_the_sink() -> None:
    sink = PanelSink()
    manager = LiveSessionManager(make_config(), sink)
    result = await _call(
        manager, SHOW_LIST_TOOL, {"title": "Steps", "items": ["A", "B", "C"]}
    )
    assert result["ok"] is True
    assert sink.panels[0][0] == "show_list"


async def test_clear_panel_routes_to_the_sink() -> None:
    sink = PanelSink()
    manager = LiveSessionManager(make_config(), sink)
    result = await _call(manager, CLEAR_PANEL_TOOL, {})
    assert result["ok"] is True
    assert sink.clears == 1


async def test_panel_tools_missing_from_the_sink_become_errors() -> None:
    """A sink (hub) predating UI-BRIEF-16 must degrade to a model-readable
    error, not crash the session."""
    manager = LiveSessionManager(make_config(), RecordingSink())
    result = await _call(
        manager, SHOW_OPTIONS_TOOL, {"question": "q", "options": ["a", "b"]}
    )
    assert "error" in result
    result = await _call(manager, SHOW_LIST_TOOL, {"items": ["a"]})
    assert "error" in result
    result = await _call(manager, CLEAR_PANEL_TOOL, {})
    assert "error" in result


async def test_unknown_panel_tool_is_still_an_error_not_a_crash() -> None:
    manager = LiveSessionManager(make_config(), RecordingSink())
    result = await _call(manager, "show_widgets", {})
    assert "error" in result
