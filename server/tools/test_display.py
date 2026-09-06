"""Drive POST /display against a running server, once per content type.

The automated assertion that each type actually reaches the device socket lives
in ``server/tests/test_display_api.py`` and runs under pytest with no server and
no hardware. THIS script is the live-fire version: point it at a real
echo-server with a real (or fake) device attached and watch the panel change.

    # terminal 1
    python server/main.py
    # terminal 2
    python server/tools/fake_device.py --no-audio
    # terminal 3
    python server/tools/test_display.py

Each request prints the equivalent curl, so the display API is documented by
its own test.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))

from fake_device import load_secret  # noqa: E402

CASES: list[tuple[str, dict]] = [
    (
        "now_playing -- metadata plumbing only, no playback",
        {
            "type": "now_playing",
            "payload": {
                "title": "Example track",
                "artist": "Example artist",
                "album": "Example album",
                "art_url": "https://example.com/album.jpg",
                "progress_s": 42,
                "duration_s": 210,
                "is_playing": True,
            },
            "duration": 15,
        },
    ),
    (
        "text -- the everyday case",
        {
            "type": "text",
            "payload": {"text": "Bus 62 in 4 min", "subtitle": "Westcott & Euclid"},
            "duration": 15,
            "priority": 1,
        },
    ),
    (
        "html -- why the push surface is a WebView",
        {
            "type": "html",
            "payload": {
                "html": (
                    "<div style='font-family:sans-serif;color:#eee;"
                    "background:#111;height:100%;display:flex;flex-direction:column;"
                    "align-items:center;justify-content:center'>"
                    "<div style='font-size:64px'>18&deg;C</div>"
                    "<div style='font-size:28px;opacity:.7'>Overcast, rain by 6pm</div>"
                    "</div>"
                )
            },
            "duration": 15,
        },
    ),
    (
        "image -- remote URL",
        {
            "type": "image",
            "payload": {"url": "https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png"},
            "duration": 10,
        },
    ),
    (
        "timer -- counts down on the device",
        {
            "type": "timer",
            "payload": {"label": "Pasta", "seconds": 480},
            "duration": 0,
            "priority": 5,
        },
    ),
]

REJECTED: list[tuple[str, dict]] = [
    ("unknown type", {"type": "video", "payload": {}}),
    ("missing payload field", {"type": "text", "payload": {}}),
    ("timer without seconds", {"type": "timer", "payload": {"label": "x"}}),
    ("javascript: image url", {"type": "image", "payload": {"url": "javascript:alert(1)"}}),
    ("negative duration", {"type": "text", "payload": {"text": "x"}, "duration": -1}),
]


def curl_for(base: str, body: dict) -> str:
    return (
        f"curl -X POST {base}/display "
        f"-H 'X-Shared-Secret: $ECHO_SHARED_SECRET' "
        f"-H 'Content-Type: application/json' "
        f"-d '{json.dumps(body)}'"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--base",
        default=os.environ.get("ECHO_HTTP", "http://127.0.0.1:8765"),
        help="server base URL (default: %(default)s)",
    )
    parser.add_argument("--secret", help="shared secret (default: .env / environment)")
    parser.add_argument("--pause", type=float, default=3.0, help="seconds between pushes")
    parser.add_argument(
        "--skip-rejects", action="store_true", help="only send the valid commands"
    )
    args = parser.parse_args(argv)
    secret = load_secret(args.secret)
    headers = {"X-Shared-Secret": secret}

    failures = 0
    with httpx.Client(timeout=10.0) as client:
        try:
            health = client.get(f"{args.base}/health").json()
        except httpx.HTTPError as exc:
            print(f"cannot reach {args.base}: {exc}")
            print("is `python server/main.py` running?")
            return 1

        print(f"server up: {json.dumps(health, indent=2)}\n")
        if not health["device_connected"]:
            print(
                "NO DEVICE CONNECTED -- every push below will correctly return 503.\n"
                "Start one:  python server/tools/fake_device.py --no-audio\n"
            )

        for label, body in CASES:
            print(f"--- {label}")
            print(f"    {curl_for(args.base, body)}")
            resp = client.post(f"{args.base}/display", json=body, headers=headers)
            print(f"    -> {resp.status_code} {resp.text.strip()}")
            expected = 200 if health["device_connected"] else 503
            if resp.status_code != expected:
                print(f"    !! expected {expected}")
                failures += 1
            print()
            time.sleep(args.pause)

        print("--- clear (back to the ambient clock)")
        resp = client.post(f"{args.base}/display/clear", headers=headers)
        print(f"    -> {resp.status_code} {resp.text.strip()}\n")

        if not args.skip_rejects:
            print("=== these must all be rejected with 400 ===")
            for label, body in REJECTED:
                resp = client.post(f"{args.base}/display", json=body, headers=headers)
                ok = "ok " if resp.status_code == 400 else "!! "
                if resp.status_code != 400:
                    failures += 1
                print(f"  {ok}{label}: {resp.status_code} {resp.text.strip()}")

            print("\n=== and this must be rejected with 401 ===")
            resp = client.post(
                f"{args.base}/display", json={"type": "text", "payload": {"text": "x"}}
            )
            ok = "ok " if resp.status_code == 401 else "!! "
            if resp.status_code != 401:
                failures += 1
            print(f"  {ok}no secret header: {resp.status_code} {resp.text.strip()}")

    print()
    print("FAILURES: " + str(failures) if failures else "all display API checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
