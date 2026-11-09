"""Brew-Client: WebSocket-Verbindung, Digest-Auth, Registrierung, Frames."""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import time as _time
import uuid as _uuid
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from . import protocol as p
from .digest import build_authorization, parse_challenge

log = logging.getLogger("brew.client")


class BrewHandler:
    """Callbacks. Alles optional, Default ist nichts tun."""

    async def on_connected(self, client: "BrewClient") -> None: ...
    async def on_disconnected(self, client: "BrewClient") -> None: ...
    async def on_group_tx(self, msg: p.CallMessage) -> None: ...
    async def on_group_idle(self, msg: p.CallMessage) -> None: ...
    async def on_call(self, msg: p.CallMessage) -> None: ...
    async def on_traffic(self, frame: p.FrameMessage) -> None: ...
    async def on_sds(self, frame: p.FrameMessage) -> None: ...
    async def on_subscriber(self, msg: p.SubscriberMessage) -> None: ...
    async def on_service(self, msg: p.ServiceMessage) -> None: ...
    async def on_error_message(self, msg: p.ErrorMessage) -> None: ...


class BrewClient:
    def __init__(self, cfg, handler: Optional[BrewHandler] = None):
        self.cfg = cfg
        self.handler = handler or BrewHandler()
        self.issi = cfg.issi
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        # Zeitstempel je Warnungsart, damit eine Stoerung im Audiotakt
        # das Log nicht flutet.
        self._warnungen: dict[str, float] = {}
        self._send_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=512)
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self._nonce_count = 0
        self.connected = False
        self.affiliated_groups: set[int] = set()
        self.last_error: Optional[str] = None
        self.connect_count = 0

    # ------------------------------------------------------------ Lifecycle

    async def run(self) -> None:
        """Laeuft bis stop(); verbindet sich bei Abbruch immer wieder neu."""
        backoff = self.cfg.reconnect_min_seconds
        while not self._stop.is_set():
            try:
                await self._connect_once()
                backoff = self.cfg.reconnect_min_seconds
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                log.warning("Brew-Verbindung fehlgeschlagen: %s", exc)
            finally:
                await self._teardown()

            if self._stop.is_set():
                break
            log.info("Neuer Verbindungsversuch in %.0f s", backoff)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, self.cfg.reconnect_max_seconds)

    async def flush(self, timeout: float = 2.0) -> bool:
        """Wartet, bis die Sendequeue leer ist.

        `stop()` schiesst den Sender sofort ab. Ohne dieses Warten ginge ein
        GROUP_IDLE, das kurz vorher eingereiht wurde, verloren - und der
        Gruppenruf bliebe auf dem Server ewig offen stehen.
        """
        ende = _time.monotonic() + timeout
        while not self._send_queue.empty() and _time.monotonic() < ende:
            await asyncio.sleep(0.02)
        # Der Sender braucht noch einen Moment fuer das letzte Paket.
        await asyncio.sleep(0.05)
        return self._send_queue.empty()

    async def stop(self) -> None:
        self._stop.set()
        await self._teardown()

    async def _teardown(self) -> None:
        self.connected = False
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        if self._ws is not None and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------ Verbindung

    async def _connect_once(self) -> None:
        ssl_ctx = None
        if self.cfg.url.startswith("https") and not self.cfg.verify_tls:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

        connector = aiohttp.TCPConnector(ssl=ssl_ctx) if ssl_ctx else aiohttp.TCPConnector()
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)
        self._session = aiohttp.ClientSession(connector=connector, timeout=timeout)

        session_path = await self._discover()
        ws_url = self._to_ws_url(session_path)
        log.info("WebSocket-Upgrade auf %s", ws_url)

        self._ws = await self._session.ws_connect(
            ws_url,
            protocols=("brew",),
            headers=self._base_headers(),
            heartbeat=None,
            autoping=True,
            max_msg_size=0,
        )
        self._set_nodelay()
        self.connected = True
        self.connect_count += 1
        self.last_error = None
        log.info("Brew verbunden (ISSI %s, Mode %s)", self.issi, self.cfg.mode)

        self._tasks = [
            asyncio.create_task(self._writer_loop(), name="brew-writer"),
            asyncio.create_task(self._ping_loop(), name="brew-ping"),
        ]

        await self.register()
        await self.handler.on_connected(self)

        try:
            await self._reader_loop()
        finally:
            self.connected = False
            await self.handler.on_disconnected(self)

    def _base_headers(self) -> dict:
        return {
            "User-Agent": self.cfg.user_agent,  # laut Spec Pflicht!
            "X-Brew-Version": str(self.cfg.brew_version),
            "X-Brew-Mode": self.cfg.mode,
        }

    async def _discover(self) -> str:
        """GET /brew/ mit Digest-Auth, liefert den Session-Pfad zurueck."""
        url = self.cfg.url.rstrip("/") + self.cfg.path
        path = urlsplit(url).path

        async with self._session.get(url, headers=self._base_headers()) as resp:
            if resp.status == 200:
                return (await resp.text()).strip()
            if resp.status != 401:
                raise RuntimeError(f"Discovery lieferte HTTP {resp.status}")
            challenge = parse_challenge(resp.headers.get("WWW-Authenticate", ""))
            await resp.read()

        if not challenge:
            raise RuntimeError("401 ohne brauchbaren Digest-Challenge")

        self._nonce_count += 1
        headers = self._base_headers()
        headers["Authorization"] = build_authorization(
            self.cfg.username, self.cfg.password, "GET", path,
            challenge, self._nonce_count,
        )
        async with self._session.get(url, headers=headers) as resp:
            if resp.status == 401:
                raise RuntimeError("Digest-Auth abgelehnt - User/Passwort pruefen")
            if resp.status != 200:
                raise RuntimeError(f"Auth lieferte HTTP {resp.status}")
            return (await resp.text()).strip()

    def _to_ws_url(self, session_path: str) -> str:
        if session_path.startswith(("ws://", "wss://")):
            return session_path
        base = urlsplit(self.cfg.url)
        scheme = "wss" if base.scheme == "https" else "ws"
        if session_path.startswith(("http://", "https://")):
            parsed = urlsplit(session_path)
            return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, ""))
        if not session_path.startswith("/"):
            session_path = "/" + session_path
        return urlunsplit((scheme, base.netloc, session_path, "", ""))

    def _set_nodelay(self) -> None:
        """Die Spec empfiehlt TCP_NODELAY - best effort, ohne Drama."""
        try:
            sock = self._ws._writer.transport.get_extra_info("socket")
            if sock is not None:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception as exc:  # pragma: no cover
            log.debug("TCP_NODELAY nicht setzbar: %s", exc)

    # ------------------------------------------------------------- Schleifen

    async def _reader_loop(self) -> None:
        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                await self._dispatch(msg.data)
            elif msg.type == aiohttp.WSMsgType.ERROR:
                raise RuntimeError(f"WebSocket-Fehler: {self._ws.exception()}")
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                              aiohttp.WSMsgType.CLOSING):
                break
        log.info("WebSocket geschlossen")

    async def _writer_loop(self) -> None:
        while True:
            payload = await self._send_queue.get()
            try:
                if self._ws is not None and not self._ws.closed:
                    await self._ws.send_bytes(payload)
            except Exception as exc:
                log.warning("Senden fehlgeschlagen: %s", exc)
            finally:
                self._send_queue.task_done()

    async def _ping_loop(self) -> None:
        interval = self.cfg.ping_interval_seconds
        while interval > 0:
            await asyncio.sleep(interval)
            try:
                if self._ws is not None and not self._ws.closed:
                    await self._ws.ping()
            except Exception:
                return

    def _selten(self, schluessel: str, sekunden: float = 10.0) -> bool:
        """Bremse fuer Warnungen, die im Takt des Audios kommen koennten."""
        jetzt = _time.monotonic()
        if jetzt - self._warnungen.get(schluessel, 0.0) < sekunden:
            return False
        self._warnungen[schluessel] = jetzt
        return True

    async def _dispatch(self, data: bytes) -> None:
        try:
            msg = p.parse(data)
        except p.BrewProtocolError as exc:
            # Nicht mehr nur DEBUG: als eine SDS aus einer fremden App nicht
            # ankam, war genau hier die Stille, die die Suche verzoegert hat.
            if self._selten("parse"):
                log.warning("Nachricht nicht parsbar (%s), %d Byte: %s",
                            exc, len(data), data[:32].hex(" "))
            return

        try:
            if isinstance(msg, p.CallMessage):
                if msg.type == p.CALL_GROUP_TX:
                    await self.handler.on_group_tx(msg)
                elif msg.type == p.CALL_GROUP_IDLE:
                    await self.handler.on_group_idle(msg)
                else:
                    await self.handler.on_call(msg)
            elif isinstance(msg, p.FrameMessage):
                if msg.type == p.FRAME_TRAFFIC_CHANNEL:
                    await self.handler.on_traffic(msg)
                elif msg.type in (p.FRAME_SDS_TRANSFER, p.FRAME_SDS_REPORT):
                    await self.handler.on_sds(msg)
                elif self._selten(f"frame{msg.type}"):
                    log.warning("Frame-Typ %s (%s) wird nicht behandelt, "
                                "%d Byte: %s", msg.type,
                                p.FRAME_NAMES.get(msg.type, "?"),
                                len(msg.data), msg.data[:32].hex(" "))
            elif isinstance(msg, p.SubscriberMessage):
                await self.handler.on_subscriber(msg)
            elif isinstance(msg, p.ServiceMessage):
                await self.handler.on_service(msg)
            elif isinstance(msg, p.ErrorMessage):
                log.warning("Brew-Fehlermeldung Typ %s: %s",
                            msg.type, msg.data[:64].hex())
                await self.handler.on_error_message(msg)
            elif self._selten(f"klasse{type(msg).__name__}"):
                log.warning("Nachrichtenklasse %s wird nicht behandelt",
                            type(msg).__name__)
        except Exception:
            log.exception("Handler-Fehler")

    # --------------------------------------------------------------- Senden

    def send(self, payload: bytes) -> None:
        """Nicht blockierend einreihen. Volle Queue = Frame faellt raus."""
        try:
            self._send_queue.put_nowait(payload)
        except asyncio.QueueFull:
            log.warning("Sendequeue voll, Nachricht verworfen")

    async def register(self) -> None:
        self.send(p.build_subscriber(p.SUB_REGISTER, self.issi))
        log.info("REGISTER gesendet (ISSI %s)", self.issi)

    async def affiliate(self, groups: list[int]) -> None:
        groups = sorted(set(int(g) for g in groups))
        if not groups:
            log.warning("Keine GSSIs zum Affiliieren - Tabelle leer oder alles aus")
            return
        self.send(p.build_subscriber(p.SUB_AFFILIATE, self.issi, groups))
        self.affiliated_groups = set(groups)
        log.info("AFFILIATE auf GSSI %s", ", ".join(str(g) for g in groups))

    async def deaffiliate(self, groups: list[int]) -> None:
        groups = sorted(set(int(g) for g in groups))
        if not groups:
            return
        self.send(p.build_subscriber(p.SUB_DEAFFILIATE, self.issi, groups))
        self.affiliated_groups -= set(groups)

    async def deregister(self) -> None:
        self.send(p.build_subscriber(p.SUB_DEREGISTER, self.issi))

    def group_tx(
        self,
        gssi: int,
        source: Optional[int] = None,
        callsign: str = "",
        priority: int = 0,
        identifier: Optional[_uuid.UUID] = None,
    ) -> _uuid.UUID:
        """Startet einen Gruppenruf Richtung TETRA. Gibt die Call-UUID zurueck."""
        identifier = identifier or _uuid.uuid4()
        mnemonic = p.encode_mnemonic(callsign) if callsign else b""
        self.send(
            p.build_group_tx(
                identifier,
                source=source if source is not None else self.issi,
                destination=gssi,
                priority=priority,
                mnemonic=mnemonic,
            )
        )
        return identifier

    def group_idle(self, identifier: _uuid.UUID, cause: int = 0) -> None:
        self.send(p.build_group_idle(identifier, cause))

    def traffic(self, identifier: _uuid.UUID, data: bytes) -> None:
        self.send(p.build_frame(p.FRAME_TRAFFIC_CHANNEL, identifier, data))

    def send_sds(self, destination: int, pdu: bytes,
                 source: Optional[int] = None) -> _uuid.UUID:
        """SDS = zwei Nachrichten mit derselben UUID (SHORT_TRANSFER + Frame)."""
        identifier = _uuid.uuid4()
        self.send(
            p.build_short_transfer(
                identifier,
                source=source if source is not None else self.issi,
                destination=destination,
            )
        )
        self.send(p.build_frame(p.FRAME_SDS_TRANSFER, identifier, pdu))
        return identifier

    def query_subscribers(self, issis: list[int]) -> None:
        if issis:
            self.send(p.build_query_subscribers(list(issis)))
