"""Spotify OAuth (Authorization Code + PKCE) and the token file on disk.

Two consumers need a token and they need the *same* one:

  * the Spotify Web API, which is how every control in this feature is actually
    performed -- librespot is only a speaker, it has no search, no transport
    controls and no "what is playing" of its own;
  * librespot itself, which is handed an access token on the command line so it
    can register "Jarvis" as a Connect device without ever seeing a password.

Hence PKCE with no client secret. There is no secret to keep: this runs on a
household PC, and a confidential-client flow would mean minting a Spotify app
and pasting its secret into ``.env`` for no security gained. The default client
id is librespot's own public one, so the consent screen says something the
owner recognises.

The token file lives in a gitignored directory and holds a refresh token that
is, in practice, permanent. Treat it exactly as you would the password.

The one-time browser login is ``server/tools/spotify_login.py``. Nothing here
opens a browser or blocks on a human: the server refreshes silently and, with
no token on disk, logs the command to run and carries on without Spotify.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("echo.spotify.auth")

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

# librespot's public client id. Using it means the consent screen reads
# "librespot", which is the truth about what is about to play the audio.
LIBRESPOT_CLIENT_ID = "65b708073fc0480ea92a077233ca87bd"

# librespot's own default loopback redirect. Kept identical so the same
# redirect URI is already whitelisted for this client id.
DEFAULT_REDIRECT_URI = "http://127.0.0.1:5588/login"

# Everything the feature does, and nothing it does not. No playlist writes, no
# library modification, no follows: this thing plays music and reads back what
# is playing.
SCOPES = (
    "streaming",
    "app-remote-control",
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
    "playlist-read-private",
    "user-library-read",
    "user-top-read",
)

# Refresh this far before the stated expiry. A token that expires mid-request
# turns into a 401 the user hears as "something went wrong with Spotify".
REFRESH_MARGIN_S = 120.0


class SpotifyAuthError(RuntimeError):
    """Anything that makes the stored credentials unusable."""


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


def make_verifier() -> str:
    """A high-entropy PKCE code verifier (RFC 7636 wants 43-128 characters)."""
    return secrets.token_urlsafe(64)[:128]


def challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    verifier: str,
    state: str,
    scopes: tuple[str, ...] = SCOPES,
) -> str:
    from urllib.parse import urlencode

    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "code_challenge_method": "S256",
            "code_challenge": challenge_for(verifier),
            "state": state,
            "scope": " ".join(scopes),
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def exchange_form(
    *, code: str, verifier: str, redirect_uri: str, client_id: str
) -> dict[str, str]:
    return {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    }


def refresh_form(*, refresh_token: str, client_id: str) -> dict[str, str]:
    return {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }


# ---------------------------------------------------------------------------
# The token file
# ---------------------------------------------------------------------------


class TokenStore:
    """The credentials on disk, plus the refresh that keeps them usable.

    Deliberately tolerant of a missing or unreadable file: no credentials is a
    supported state (Spotify is simply unavailable and says so), not a startup
    failure. A *corrupt* file is treated the same way, because the fix is the
    same -- run the login script again.
    """

    def __init__(self, path: str | Path, *, client_id: str = LIBRESPOT_CLIENT_ID) -> None:
        self.path = Path(path)
        self.client_id = client_id
        self.access_token = ""
        self.refresh_token = ""
        self.expires_at = 0.0
        self.scope = ""

    # --- persistence ------------------------------------------------------

    def load(self) -> bool:
        """Read the file. Returns False when there is nothing usable to read."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable Spotify token file %s: %s", self.path, exc)
            return False
        if not isinstance(raw, dict) or not raw.get("refresh_token"):
            log.warning("Spotify token file %s has no refresh token", self.path)
            return False

        self.access_token = str(raw.get("access_token") or "")
        self.refresh_token = str(raw["refresh_token"])
        self.expires_at = float(raw.get("expires_at") or 0.0)
        self.scope = str(raw.get("scope") or "")
        self.client_id = str(raw.get("client_id") or self.client_id)
        return True

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(
            {
                "client_id": self.client_id,
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at,
                "scope": self.scope,
            },
            indent=2,
        )
        # Write-then-replace: a crash mid-write must not leave a truncated file
        # that reads as "no credentials" and sends the owner back to a browser.
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(self.path)

    def apply_token_response(self, data: dict[str, Any]) -> None:
        """Absorb a token endpoint response and persist it.

        Spotify omits ``refresh_token`` from most refresh responses, which means
        "keep using the one you have" and not "you no longer have one" -- losing
        it here would silently require a new browser login an hour later.
        """
        access = str(data.get("access_token") or "")
        if not access:
            raise SpotifyAuthError(f"token response carried no access_token: {data}")
        self.access_token = access
        self.expires_at = time.time() + float(data.get("expires_in") or 3600)
        if data.get("refresh_token"):
            self.refresh_token = str(data["refresh_token"])
        if data.get("scope"):
            self.scope = str(data["scope"])
        self.save()

    # --- state ------------------------------------------------------------

    @property
    def linked(self) -> bool:
        return bool(self.refresh_token)

    @property
    def stale(self) -> bool:
        return not self.access_token or time.time() >= self.expires_at - REFRESH_MARGIN_S

    def missing_scopes(self) -> list[str]:
        """Scopes granted at login that this build now needs and does not have.

        Worth surfacing: adding a scope to ``SCOPES`` does not retroactively
        grant it, and the failure it produces otherwise is a 403 on one specific
        call, hours later, with nothing pointing at the cause.
        """
        if not self.scope:
            return []
        granted = set(self.scope.split())
        return [s for s in SCOPES if s not in granted]

    def how_to_link(self) -> str:
        return (
            "Spotify is enabled but not linked yet. Run:\n"
            "    .venv\\Scripts\\python.exe server\\tools\\spotify_login.py\n"
            "and open the URL it prints in a browser signed in to the Premium "
            f"account. It writes {self.path}."
        )
