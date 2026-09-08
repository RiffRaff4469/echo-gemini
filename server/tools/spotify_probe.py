"""Spotify e2e probe: search -> play on Jarvis -> verify state (real creds)."""

import asyncio
import sys
import time

sys.path.insert(0, "server")

from config import load_config  # noqa: E402
from spotify_auth import TokenStore  # noqa: E402
from spotify_api import WebApi  # noqa: E402


async def main() -> None:
    cfg = load_config()
    # The Web API rides the personal app (spotify_api_client_id / creds_web).
    api_tokens = TokenStore(
        __import__("pathlib").Path(cfg.spotify_api_creds_dir) / "token.json",
        client_id=cfg.spotify_api_client_id or cfg.spotify_client_id,
    )
    api_tokens.load()
    if not api_tokens.linked:
        print("WEB API NOT LINKED")
        return
    api = WebApi(api_tokens)

    device = await api.find_device(cfg.spotify_device_name)
    print("device:", device.get("name") if device else None,
          "| id:", (device.get("id") or "")[:12] if device else None)
    if not device:
        print("NO DEVICE NAMED", cfg.spotify_device_name)
        return

    hits = await api.search("lofi beats to relax", kind="playlist", limit=3)
    print("search hits:", len(hits))
    if not hits:
        return
    target = hits[0]
    print("playing:", target.get("name"), target.get("uri", "")[:20])
    await api.play(device_id=device["id"], context_uri=target["uri"])

    for i in range(6):
        await asyncio.sleep(2)
        state = await api.playback_state()
        if state:
            item = state.get("item") or {}
            print(
                f"  poll {i}: playing={state.get('is_playing')} "
                f"device={state.get('device', {}).get('name')} "
                f"track={item.get('name')}"
            )
            if state.get("is_playing"):
                print("PLAYBACK CONFIRMED")
                return
    print("NO PLAYBACK STATE AFTER 12s")


asyncio.run(main())
