"""UI-BRIEF-16 visual-answer panels: hub panel state, show_* tools, select taps.

Options/list panels are pushed as ordinary DisplayCommands; what is NEW is the
server-side panel state that maps a device ``select {index}`` back to a label
the model understands, and the rules for when that mapping lives (active
session -> queued choice; idle -> session opens with on-screen context).

These run the real aiohttp app over a real socket (like test_display_api) but
swap in the StubSession, so the hub dispatch is what is under test -- the Live
leg's own select handling lives in test_session.py.
"""

from __future__ import annotations

import pytest

import protocol as P
from main import DISPLAY_SECRET_HEADER, HUB_KEY
from protocol import UiState
from test_display_api import FakeDevice, SECRET, client, handled, tap_device  # noqa: F401


def _hub(client):
    return client.app[HUB_KEY]


async def _push_options(client, question="Which is a planet?", labels=None):
    """Push an options panel through the real tool entry point."""
    labels = labels or ["Mercury", "Venus", "Earth", "Mars"]
    device = await FakeDevice.connect(client)
    result = _hub(client).show_options(question=question, options=labels)
    assert result["ok"] is True
    panel = await device.expect(P.Display)
    return device, panel


# --- tool -> display --------------------------------------------------------


async def test_show_options_pushes_a_tappable_panel_and_remembers_labels(
    client,
) -> None:
    device = await FakeDevice.connect(client)
    hub = _hub(client)
    result = hub.show_options(question="Which is a planet?", options=["Mercury", "Venus"])
    assert result["ok"] is True and result["displayed"] == 2

    msg = await device.expect(P.Display)
    cmd = msg.command
    assert cmd.type is P.DisplayType.OPTIONS
    assert cmd.duration == 0.0, "panels stay until resolved or cleared"
    assert cmd.priority == 1, "panels outrank ordinary cards"
    assert cmd.payload["question"] == "Which is a planet?"
    assert [o["label"] for o in cmd.payload["options"]] == ["Mercury", "Venus"]

    assert hub.panel["kind"] == "options"
    assert hub.panel["labels"] == ["Mercury", "Venus"]
    assert hub.panel["question"] == "Which is a planet?"


async def test_show_options_rejects_more_than_six(client) -> None:
    await FakeDevice.connect(client)
    hub = _hub(client)
    result = hub.show_options(question="Pick", options=[f"Option {i}" for i in range(7)])
    assert "error" in result, "> 6 options must come back as an error to the model"
    assert hub.panel is None, "and nothing is pushed or remembered"


async def test_show_options_requires_at_least_two(client) -> None:
    hub = _hub(client)
    result = hub.show_options(question="Pick", options=["only one"])
    assert "error" in result


async def test_show_options_clips_screen_labels_but_keeps_originals(client) -> None:
    device = await FakeDevice.connect(client)
    hub = _hub(client)
    long = "A label far longer than the twenty-eight character screen budget"
    result = hub.show_options(question="Q", options=[long, "Short"])
    assert result["ok"] is True
    cmd = (await device.expect(P.Display)).command
    shown = cmd.payload["options"][0]["label"]
    assert len(shown) == P.OPTION_LABEL_MAX_CHARS and shown.endswith("\u2026")
    # The mapping must hand the model back exactly what it wrote, not the
    # clipped screen copy.
    assert hub.panel["labels"] == [long, "Short"]


async def test_show_list_pushes_a_read_only_panel(client) -> None:
    device = await FakeDevice.connect(client)
    hub = _hub(client)
    result = hub.show_list(title="How to brew", items=["Boil water", "Add grounds", "Steep"])
    assert result["ok"] is True
    cmd = (await device.expect(P.Display)).command
    assert cmd.type is P.DisplayType.LIST
    assert cmd.payload["items"][0] == "Boil water"
    assert hub.panel["kind"] == "list"
    assert hub.panel["labels"] is None, "list rows carry no select meaning"


async def test_show_list_rejects_over_fifteen_items(client) -> None:
    await FakeDevice.connect(client)
    hub = _hub(client)
    result = hub.show_list(title="Too much", items=[f"item {i}" for i in range(16)])
    assert "error" in result
    assert hub.panel is None


# --- select -> label mapping ------------------------------------------------


async def test_select_during_an_active_session_queues_the_label(client) -> None:
    device, session = await tap_device(client, session_active=True)
    hub = _hub(client)
    hub.show_options(question="Which is a planet?", options=["Mercury", "Venus", "Earth"])
    await device.expect(P.Display)

    await device.ws.send_str(P.Select(index=2).encode())
    # The select handler clears the panel before anything else can read the
    # socket, so the DisplayClear IS the completion barrier here.
    await device.expect(P.DisplayClear)

    assert session.choices == ["the user chose Earth"], \
        "the model hears the LABEL, never a bare index"
    assert hub.panel is None, "the tap resolves the panel"


async def test_select_while_idle_opens_a_session_with_context(client) -> None:
    device, session = await tap_device(client, session_active=False)
    hub = _hub(client)
    hub.show_options(question="Which is a planet?", options=["Mercury", "Venus", "Earth"])
    await device.expect(P.Display)

    await device.ws.send_str(P.Select(index=0).encode())
    await device.expect(P.DisplayClear)

    assert session.starts == ["select"], "an idle select opens a session"
    assert len(session.choices) == 1
    assert "Mercury" in session.choices[0]
    assert "Which is a planet?" in session.choices[0]
    assert hub.panel is None


async def test_select_while_idle_uses_plain_text_when_no_question(client) -> None:
    device, session = await tap_device(client, session_active=False)
    hub = _hub(client)
    hub.show_options(options=["Alpha", "Beta"])
    await device.expect(P.Display)
    await device.ws.send_str(P.Select(index=1).encode())
    await device.expect(P.DisplayClear)
    assert session.starts == ["select"]
    assert session.choices == ["The user tapped Beta on the on-screen options."]


async def test_select_with_no_panel_is_ignored(client) -> None:
    device, session = await tap_device(client, session_active=True)
    await device.ws.send_str(P.Select(index=0).encode())
    await handled(device)
    assert session.choices == []
    assert session.starts == []
    assert _hub(client).panel is None  # and nothing appeared to clear


async def test_select_out_of_range_is_ignored_with_an_error(client) -> None:
    device, session = await tap_device(client, session_active=True)
    hub = _hub(client)
    hub.show_options(options=["Alpha", "Beta"])
    await device.expect(P.Display)
    await device.ws.send_str(P.Select(index=9).encode())
    err = await device.expect(P.ErrorMsg)
    assert err.code == "bad_select"
    assert session.choices == []
    assert hub.panel is not None, "a mis-tap must not kill the panel"


async def test_select_on_a_list_panel_is_ignored(client) -> None:
    device, session = await tap_device(client, session_active=True)
    hub = _hub(client)
    hub.show_list(title="Steps", items=["First", "Second"])
    await device.expect(P.Display)
    await device.ws.send_str(P.Select(index=0).encode())
    await handled(device)
    assert session.choices == []
    assert hub.panel is not None


# --- panel lifecycle --------------------------------------------------------


async def test_panel_replaced_by_the_next_panel(client) -> None:
    device = await FakeDevice.connect(client)
    hub = _hub(client)
    hub.show_options(question="Q1", options=["A", "B"])
    await device.expect(P.Display)
    hub.show_options(question="Q2", options=["C", "D", "E"])
    await device.expect(P.Display)
    assert hub.panel["question"] == "Q2"
    assert hub.panel["labels"] == ["C", "D", "E"]


async def test_panel_cleared_when_the_session_goes_idle(client) -> None:
    """Voice cancel and the idle timeout both end with state -> idle; a panel
    that outlives its conversation must not sit on the screen over the clock."""
    device, session = await tap_device(client, session_active=True)
    hub = _hub(client)
    hub.show_options(question="Q", options=["A", "B"])
    await device.expect(P.Display)
    await hub.set_state(UiState.IDLE)  # what the Live leg does when it closes
    assert hub.panel is None
    await device.expect(P.DisplayClear)


async def test_http_pushed_panel_is_selectable(client) -> None:
    """POST /display options behaves like a tool push: it remembers the panel
    so a later select can map back to a label."""
    device, session = await tap_device(client, session_active=True)
    hub = _hub(client)
    resp = await client.post(
        "/display",
        json={
            "type": "options",
            "payload": {
                "question": "Dinner?",
                "options": [{"label": "Pizza"}, {"label": "Sushi"}],
            },
        },
        headers={DISPLAY_SECRET_HEADER: SECRET},
    )
    assert resp.status == 200
    await device.expect(P.Display)
    assert hub.panel["labels"] == ["Pizza", "Sushi"]

    await device.ws.send_str(P.Select(index=1).encode())
    await handled(device)
    assert session.choices == ["the user chose Sushi"]


async def test_http_display_clear_drops_the_panel(client) -> None:
    device = await FakeDevice.connect(client)
    hub = _hub(client)
    hub.show_options(question="Q", options=["A", "B"])
    await device.expect(P.Display)
    resp = await client.post("/display/clear", headers={DISPLAY_SECRET_HEADER: SECRET})
    assert resp.status == 200
    await device.expect(P.DisplayClear)
    assert hub.panel is None


# --- fake-device end-to-end (UI-BRIEF-16 deliverable) -----------------------
# A REAL device socket + a REAL LiveSessionManager with a fake Gemini
# connector: question -> options on screen -> tap index -> the model receives
# the chosen label and the conversation continues.


async def _install_live(client):
    """Swap the hub's no-key manager for one with a fake Gemini connector."""
    from types import SimpleNamespace

    import asyncio

    from gemini_live import LiveSessionManager
    from test_session import FakeConnector, FakeLiveSession, make_config, settle

    hub = _hub(client)
    cfg = make_config(
        gemini_api_key="fake-key-for-tests",  # live_enabled follows the key
    )
    model = FakeLiveSession()
    manager = LiveSessionManager(cfg, hub, connector=FakeConnector(model))
    hub.session = manager
    return hub, manager, model, asyncio, SimpleNamespace, settle


async def test_e2e_tool_pushes_options_onto_the_device_then_a_tap_reaches_the_model(
    client,
) -> None:
    """Trivia flow with the session open: the model's show_options call lands on
    the screen, and tapping row 2 feeds 'the user chose Mercury' back to it."""
    hub, manager, model, asyncio, S, settle = await _install_live(client)
    device = await FakeDevice.connect(client)
    try:
        assert await manager.start("trivia") is True
        await settle()

        # The model asks its question via the tool (this is what the receive
        # loop would deliver mid-turn).
        model.emit(
            S(
                data=None,
                server_content=None,
                tool_call=S(
                    function_calls=[
                        S(
                            name="show_options",
                            id="call-trivia",
                            args={
                                "question": "Which planet is closest to the sun?",
                                "options": ["Mars", "Venus", "Mercury", "Earth"],
                            },
                        )
                    ]
                ),
                go_away=None,
            )
        )
        msg = await device.expect(P.Display)
        cmd = msg.command
        assert cmd.type is P.DisplayType.OPTIONS
        assert cmd.payload["question"] == "Which planet is closest to the sun?"
        assert [o["label"] for o in cmd.payload["options"]] == [
            "Mars", "Venus", "Mercury", "Earth",
        ]
        assert hub.panel["labels"] == ["Mars", "Venus", "Mercury", "Earth"]

        # The model speaks the question and finishes its turn.
        model.emit_audio(b"\xaa" * 40)
        await settle()
        model.emit_content(turn_complete=True)
        await settle()

        # The user taps row 2 (0-based) on the device screen.
        await device.ws.send_str(P.Select(index=2).encode())
        await device.expect(P.DisplayClear)  # the tap resolves the panel
        assert hub.panel is None

        # The choice reaches the model as the label, once the turn is over.
        await asyncio.sleep(0.5)
        assert model.text_in == ["the user chose Mercury"]
    finally:
        await manager.stop("e2e teardown")


async def test_e2e_an_idle_tap_on_an_options_panel_opens_a_session_from_context(
    client,
) -> None:
    """No session is open; the options panel is up from an earlier exchange.
    Tapping row 0 opens a session whose first input names the on-screen
    question and the tapped label."""
    hub, manager, model, asyncio, S, settle = await _install_live(client)
    device = await FakeDevice.connect(client)
    try:
        hub.show_options(question="Beach or mountains?", options=["Beach", "Mountains"])
        await device.expect(P.Display)
        assert manager.active is False

        await device.ws.send_str(P.Select(index=1).encode())
        await device.expect(P.DisplayClear)
        assert manager.active, "an idle select must open a session"
        assert hub.panel is None

        await asyncio.sleep(0.6)
        assert model.text_in == [
            "On-screen question: Beach or mountains? The user tapped Mountains."
        ]
    finally:
        await manager.stop("e2e teardown")
