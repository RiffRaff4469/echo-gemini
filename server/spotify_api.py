"""The Spotify Web API calls this feature makes, and nothing else.

librespot is a speaker. It receives a Connect stream and turns it into PCM; it
cannot search, cannot skip, cannot tell you what is playing and has no way to be
told to start something. Every one of those is a Web API call against the same
account, aimed at the Connect device librespot registered.

So the split is: **Web API decides, librespot makes the noise.** "Hey Jarvis,
play some lofi" is a search plus a play-on-device call from this file; the audio
that results arrives on librespot's stdout and goes down the device link.

Errors are normalised into ``SpotifyApiError`` carrying a sentence a voice model
can read out. That is the whole reason this wrapper exists rather than raw
``aiohttp`` calls at the tool layer: "Spotify says that needs Premium" is a
useful thing for the display to say, and a bare 403 traceback is not.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from spotify_auth import TokenStore, refresh_form, TOKEN_URL

log = logging.getLogger("echo.spotify.api")

API_ROOT = "https://api.spotify.com/v1"

# Short on purpose. These calls sit inside a Gemini tool call with a person
# standing in front of the display; "Spotify did not answer" beats ten seconds
# of dead air, exactly as for the alarm acks.
REQUEST_TIMEOUT_S = 6.0

SEARCH_TYPES = ("track", "album", "artist", "playlist")


class SpotifyApiError(RuntimeError):
    """A failure the model should say out loud, phrased for a person.

    ``status`` is carried alongside the sentence because one caller needs to
    tell the failures apart rather than just say them: the now-playing poller
    backs off on a 429 and only on a 429. Zero means "not an HTTP status" --
    a timeout, a DNS failure, a refresh that never got a response.
    """

    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class SpotifyNotLinked(SpotifyApiError):
    pass


class WebApi:
    """Thin async client over the endpoints this feature uses.

    Owns no session: one is created lazily and closed by ``aclose``, so the
    whole thing can be constructed at import time on a machine that never
    enables Spotify without opening a socket.
    """

    def __init__(self, tokens: TokenStore, *, session: aiohttp.ClientSession | None = None):
        self._tokens = tokens
        self._session = session
        self._owns_session = session is None
        self._refresh_lock = asyncio.Lock()

    async def http(self) -> aiohttp.ClientSession:
        """The shared client session, created on first use.

        Public because album art is fetched through it too (``ArtCache``): one
        session means one connection pool and one timeout policy for everything
        this feature does over the network.
        """
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
            )
            self._owns_session = True
        return self._session

    async def aclose(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # --- auth -------------------------------------------------------------

    async def access_token(self, *, force: bool = False) -> str:
        """A currently-valid access token, refreshing if it is close to expiry.

        The lock matters: a tool call and the now-playing poller can both hit a
        stale token in the same tick, and two simultaneous refreshes race to
        write the token file.
        """
        if not self._tokens.linked:
            raise SpotifyNotLinked(
                "Spotify is not linked to an account on this server yet."
            )
        async with self._refresh_lock:
            if not force and not self._tokens.stale:
                return self._tokens.access_token

            http = await self.http()
            form = refresh_form(
                refresh_token=self._tokens.refresh_token,
                client_id=self._tokens.client_id,
            )
            try:
                async with http.post(TOKEN_URL, data=form) as resp:
                    body = await resp.json(content_type=None)
                    if resp.status != 200:
                        raise SpotifyApiError(
                            "Spotify refused to refresh the saved login "
                            f"({body.get('error_description') or resp.status}). "
                            "It may need linking again."
                        )
            except aiohttp.ClientError as exc:
                raise SpotifyApiError(f"Could not reach Spotify: {exc}") from exc
            self._tokens.apply_token_response(body)
            log.info("refreshed the Spotify access token")
            return self._tokens.access_token

    # --- request plumbing -------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        _retried: bool = False,
    ) -> Any:
        """One API call. Returns parsed JSON, or None for an empty 2xx body.

        A 401 is retried exactly once with a forced refresh. Spotify can expire
        a token early (a password change, a revoked app), and the difference
        between recovering silently and telling the user to go and log in again
        is this one retry.
        """
        token = await self.access_token(force=_retried)
        http = await self.http()
        url = f"{API_ROOT}{path}"
        headers = {"Authorization": f"Bearer {token}"}

        try:
            async with http.request(
                method, url, params=params, json=json, headers=headers
            ) as resp:
                if resp.status == 401 and not _retried:
                    log.info("Spotify returned 401; refreshing and retrying once")
                    return await self.request(
                        method, path, params=params, json=json, _retried=True
                    )
                # 204 is the normal answer to every transport control, and to
                # "what is playing" when nothing is.
                if resp.status == 204 or not (await resp.read()):
                    if resp.status >= 400:
                        raise SpotifyApiError(
                            _describe(resp.status, {}), status=resp.status
                        )
                    return None
                body = await resp.json(content_type=None)
                if resp.status >= 400:
                    raise SpotifyApiError(
                        _describe(resp.status, body), status=resp.status
                    )
                return body
        except asyncio.TimeoutError as exc:
            raise SpotifyApiError("Spotify did not answer in time.") from exc
        except aiohttp.ClientError as exc:
            raise SpotifyApiError(f"Could not reach Spotify: {exc}") from exc

    # --- devices ----------------------------------------------------------

    async def devices(self) -> list[dict[str, Any]]:
        body = await self.request("GET", "/me/player/devices")
        return list((body or {}).get("devices") or [])

    async def find_device(self, name: str) -> dict[str, Any] | None:
        """The Connect device librespot registered, by its ``--name``.

        Matched case-insensitively on the name rather than held as an id: the
        id changes every time librespot restarts, and librespot restarts.
        """
        wanted = name.strip().lower()
        for device in await self.devices():
            if str(device.get("name", "")).strip().lower() == wanted:
                return device
        return None

    # --- search -----------------------------------------------------------

    async def search(
        self, query: str, *, kind: str = "track", limit: int = 5
    ) -> list[dict[str, Any]]:
        if kind not in SEARCH_TYPES:
            kind = "track"
        body = await self.request(
            "GET",
            "/search",
            # market=from_token would need the user-read-private scope, which
            # the personal-app login does not request; omitting it returns
            # market-default results and keeps the login scope-light.
            params={"q": query, "type": kind, "limit": limit},
        )
        items = ((body or {}).get(f"{kind}s") or {}).get("items") or []
        # Spotify can return nulls in a playlist search result list.
        return [item for item in items if isinstance(item, dict) and item.get("uri")]

    # --- transport --------------------------------------------------------

    async def play(
        self,
        *,
        device_id: str,
        uris: list[str] | None = None,
        context_uri: str = "",
    ) -> None:
        """Start playback on a specific device, activating it if it is idle.

        ``device_id`` on the query string is what makes this work from cold: it
        transfers to that device and starts, so the owner never has to pick
        "Jarvis" in the phone app first.
        """
        payload: dict[str, Any] = {}
        if context_uri:
            payload["context_uri"] = context_uri
        elif uris:
            payload["uris"] = uris
        await self.request(
            "PUT", "/me/player/play", params={"device_id": device_id}, json=payload
        )

    async def resume(self, *, device_id: str) -> None:
        # An empty body means "carry on from where you were" rather than
        # "restart the queue", which is the difference between resuming after a
        # conversation and starting the album again.
        await self.request("PUT", "/me/player/play", params={"device_id": device_id})

    async def pause(self, *, device_id: str = "") -> None:
        params = {"device_id": device_id} if device_id else None
        await self.request("PUT", "/me/player/pause", params=params)

    async def next_track(self, *, device_id: str = "") -> None:
        params = {"device_id": device_id} if device_id else None
        await self.request("POST", "/me/player/next", params=params)

    async def previous_track(self, *, device_id: str = "") -> None:
        params = {"device_id": device_id} if device_id else None
        await self.request("POST", "/me/player/previous", params=params)

    async def set_volume(self, percent: int, *, device_id: str = "") -> None:
        params: dict[str, Any] = {"volume_percent": max(0, min(100, int(percent)))}
        if device_id:
            params["device_id"] = device_id
        await self.request("PUT", "/me/player/volume", params=params)

    async def playback_state(self) -> dict[str, Any] | None:
        """The full player state, or None when nothing is playing anywhere."""
        return await self.request("GET", "/me/player", params={"market": "from_token"})


def _describe(status: int, body: Any) -> str:
    """Turn an API error into one sentence worth saying out loud."""
    message = ""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "")
        elif isinstance(error, str):
            message = str(body.get("error_description") or error)

    if status == 403:
        # By far the most likely 403 here, and the one with an actionable
        # answer, is a non-Premium account: Connect playback is Premium-only.
        return message or "Spotify refused that -- playback control needs Premium."
    if status == 404:
        return message or "Spotify has no active player to control right now."
    if status == 429:
        return "Spotify is rate-limiting this server; try again in a moment."
    if status >= 500:
        return "Spotify is having trouble at their end."
    return message or f"Spotify returned an error ({status})."
