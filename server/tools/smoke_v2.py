"""Run the real fake-device harness and HTTP display path without keys or hardware.

    .venv/Scripts/python.exe server/tools/smoke_v2.py

Uses an ephemeral loopback port and an in-memory random shared secret. Nothing
is written to .env and no Gemini session or paid API is opened.
"""
from __future__ import annotations

import asyncio
import secrets
import sys
from pathlib import Path

import aiohttp
from aiohttp.test_utils import TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import Config
from main import HUB_KEY, build_app
from fake_device import FakeDevice, parse_args
from test_display import CASES, REJECTED


async def smoke() -> None:
    secret = secrets.token_urlsafe(32)
    app = build_app(Config(shared_secret=secret, gemini_api_key="", wake_enabled=False,
                           wake_download_models=False, heartbeat_interval_s=1))
    async with TestServer(app) as server:
        args = parse_args(["--no-audio", "--hold", "5", "--timer", "1"])
        args.url = str(server.make_url("/ws")).replace("http://", "ws://")
        args.secret = secret
        device = FakeDevice(args)
        task = asyncio.create_task(device.run())
        try:
            async with asyncio.timeout(10):
                while app[HUB_KEY].alarms.last_state is None:
                    await asyncio.sleep(0.01)
                state = await app[HUB_KEY].alarms.set_timer("Socket smoke", 0.5)
                assert any(t.label == "Socket smoke" for t in state.timers)
                async with aiohttp.ClientSession(headers={"X-Shared-Secret": secret}) as client:
                    for _, body in CASES:
                        async with client.post(server.make_url("/display"), json=body) as response:
                            assert response.status == 200
                    for _, body in REJECTED:
                        async with client.post(server.make_url("/display"), json=body) as response:
                            assert response.status == 400
                    async with client.post(server.make_url("/display/clear")) as response:
                        assert response.status == 200
                assert await task == 0
                assert device.displays == len(CASES)
                assert device.chimes == 2
                print(f"PASS: {len(CASES)} display cards, {len(REJECTED)} rejected payloads, "
                      "correlated timer ack, 2 simulated chimes, clean disconnect; Live disabled")
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(smoke())
