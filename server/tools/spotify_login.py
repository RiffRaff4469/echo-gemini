"""One-time Spotify login. Run it once; the server refreshes silently after.

    .venv\\Scripts\\python.exe server\\tools\\spotify_login.py

It prints a URL, waits for you to approve it in a browser signed in to the
Premium account, and writes ``server/spotify_creds/token.json``. That file holds
a refresh token which is, in practice, permanent -- it is the account, so it
lives in a gitignored directory and should be treated like the password.

Authorization Code with PKCE, against librespot's public client id: there is no
client secret to store, and the consent screen names librespot, which is the
truth about what is going to play the audio.

Deliberately a separate script rather than something the server does on demand.
A server that can block on a human at the console is a server that hangs at boot
on the morning nobody is watching; when there are no credentials, echo-server
logs this command and carries on without music.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

from config import ConfigError, load_config  # noqa: E402
from spotify_auth import (  # noqa: E402
    SCOPES,
    TOKEN_URL,
    SpotifyAuthError,
    TokenStore,
    authorize_url,
    exchange_form,
    make_verifier,
)

# Long enough to find the phone, unlock it, and sign in.
LOGIN_TIMEOUT_S = 300.0

_DONE_PAGE = b"""<!doctype html><html><head><meta charset="utf-8">
<title>Jarvis</title><style>body{font-family:sans-serif;background:#0b0d10;
color:#e6ebf2;display:flex;height:100vh;margin:0;align-items:center;
justify-content:center}</style></head>
<body><div><h1>Linked.</h1><p>You can close this tab.</p></div></body></html>"""


class _Callback(BaseHTTPRequestHandler):
    """Answers exactly one redirect and hands the code back to the main thread."""

    result: dict[str, str] = {}
    finished = threading.Event()

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's name
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _Callback.result = {k: v[0] for k, v in query.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_DONE_PAGE)))
        self.end_headers()
        self.wfile.write(_DONE_PAGE)
        _Callback.finished.set()

    def log_message(self, *_args) -> None:
        """Silence the default stderr access log; this script has its own voice."""


def _post_form(url: str, form: dict[str, str]) -> dict:
    body = urllib.parse.urlencode(form).encode("ascii")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SpotifyAuthError(f"Spotify rejected the token request: {detail}") from exc
    except OSError as exc:
        raise SpotifyAuthError(f"Could not reach Spotify: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", help="path to a .env (default: repo root .env)")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="only print the URL; do not try to open one (use over SSH)",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.env_file)
    except ConfigError as exc:
        print(f"\nconfiguration error:\n\n{exc}\n", file=sys.stderr)
        return 2

    redirect = urllib.parse.urlparse(cfg.spotify_redirect_uri)
    if redirect.scheme != "http" or redirect.hostname not in ("127.0.0.1", "localhost"):
        print(
            f"SPOTIFY_REDIRECT_URI must be a loopback http URL, got "
            f"{cfg.spotify_redirect_uri!r}",
            file=sys.stderr,
        )
        return 2

    verifier = make_verifier()
    state = secrets.token_urlsafe(16)
    url = authorize_url(
        client_id=cfg.spotify_client_id,
        redirect_uri=cfg.spotify_redirect_uri,
        verifier=verifier,
        state=state,
    )

    try:
        httpd = HTTPServer((redirect.hostname, redirect.port or 80), _Callback)
    except OSError as exc:
        print(
            f"Could not listen on {cfg.spotify_redirect_uri}: {exc}\n"
            "Something else is using that port -- stop it, or set "
            "SPOTIFY_REDIRECT_URI to a free one (and add the same URI to the "
            "Spotify app's redirect list).",
            file=sys.stderr,
        )
        return 1

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    print("\nOpen this in a browser signed in to the Spotify Premium account:\n")
    print(f"  {url}\n")
    print(f"Scopes requested: {' '.join(SCOPES)}\n")
    if not args.no_browser:
        # Best effort. On a headless box this silently does nothing, which is
        # why the URL is printed first and not instead.
        try:
            webbrowser.open(url)
        except Exception:
            pass
    print(f"Waiting up to {LOGIN_TIMEOUT_S / 60:.0f} minutes for the redirect...")

    got_it = _Callback.finished.wait(LOGIN_TIMEOUT_S)
    httpd.shutdown()
    if not got_it:
        print("\nTimed out. Nothing was written; run this again.", file=sys.stderr)
        return 1

    result = _Callback.result
    if result.get("error"):
        print(f"\nSpotify returned an error: {result['error']}", file=sys.stderr)
        return 1
    if result.get("state") != state:
        # A mismatched state means the response is not the one this run asked
        # for. Refuse rather than exchange a code that came from somewhere else.
        print("\nState mismatch; refusing to continue.", file=sys.stderr)
        return 1
    code = result.get("code")
    if not code:
        print("\nNo authorization code in the redirect.", file=sys.stderr)
        return 1

    try:
        data = _post_form(
            TOKEN_URL,
            exchange_form(
                code=code,
                verifier=verifier,
                redirect_uri=cfg.spotify_redirect_uri,
                client_id=cfg.spotify_client_id,
            ),
        )
        store = TokenStore(
            Path(cfg.spotify_creds_dir) / "token.json", client_id=cfg.spotify_client_id
        )
        store.apply_token_response(data)
    except SpotifyAuthError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1

    print(f"\nLinked. Credentials written to {store.path}")
    missing = store.missing_scopes()
    if missing:
        print(f"WARNING: Spotify did not grant {', '.join(missing)}", file=sys.stderr)
    if not cfg.spotify_enabled:
        print("Set SPOTIFY_ENABLED=true in .env and restart echo-server.")
    else:
        print("Restart echo-server to pick it up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
