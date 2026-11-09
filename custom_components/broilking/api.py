"""Async client for the Broil King smoker's local JSON-RPC-over-WebSocket API.

The firmware (Mongoose OS) answers on ws://<host>/rpc with no authentication on
the LAN. Custom methods return their result inside response.error.message as a
double-encoded JSON string, and error.code == 0 means success. Control commands
go through SendGenericCommand using the exact byte strings the official iQue app
builds (BleCommandMaker.buildGenericCommandSequence); the firmware appends the
UART checksum itself. Raw temperatures are always Fahrenheit.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time

import aiohttp

from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)


class BroilKingError(HomeAssistantError):
    """Raised when the smoker cannot be reached or returns an error.

    ``public_code`` is safe to show. It is not the device's own text.
    """

    def __init__(self, message: str, *, public_code: str = "failed") -> None:
        super().__init__(message)
        self.public_code = public_code


# Same pause the cook-timer number uses before trusting a read-back.
# The control board needs about 1.5 s to echo a write.
TARGET_ACK_DELAY = 2.0

# One attempt, no retry. Mongoose OS documents RPC.List with no arguments.
DISCOVER_TIMEOUT = 8
_MAX_METHOD_NAMES = 64
# Documented target-control method in this integration. RPC.Describe is used
# only when RPC.List actually returned this name. Other listed names are
# reported and not called, so discovery does not pick an ambiguous setter.
_DOCUMENTED_CONTROL_METHOD = "SendGenericCommand"
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.]{0,63}$")
_SECRET_RE = re.compile(
    r"(?i)(?:^fs\.|^config\.|password|secret|credential|token|ssid|wifi|"
    r"\bauth\b|certificate|private.?key|\bpass\b)"
)
_ARGS_FMT_RE = re.compile(r"^[A-Za-z0-9 {}:%._,\-]{0,80}$")
_URL_RE = re.compile(r"(?i)\b(?:ws|http)s?://\S+")


def ensure_target_confirmed(reported_f, requested_f, label: str) -> None:
    """Raise unless a later read shows the Fahrenheit target that was sent."""
    requested = int(requested_f)
    if reported_f is None or int(reported_f) != requested:
        raise BroilKingError(
            f"{label} {requested} F was not confirmed; grill reports {reported_f} F"
        )


def sanitize_method_names(raw) -> tuple[list[str], int]:
    """Keep short RPC names and count everything else as omitted.

    Names that identify filesystem, configuration, or credential methods are
    omitted rather than returned. The caller must not invent names.
    """
    if not isinstance(raw, list):
        return [], 0
    kept: list[str] = []
    omitted = 0
    seen: set[str] = set()
    for item in raw:
        name = item.get("name") if isinstance(item, dict) else item
        if not isinstance(name, str):
            omitted += 1
            continue
        if (
            not _NAME_RE.fullmatch(name)
            or _SECRET_RE.search(name)
            or name in seen
        ):
            omitted += 1
            continue
        seen.add(name)
        if len(kept) >= _MAX_METHOD_NAMES:
            omitted += 1
            continue
        kept.append(name)
    kept.sort(key=str.lower)
    return kept, omitted


def describe_targets(names: list[str]) -> list[str]:
    """The documented control name, only with that exact spelling.

    ``sendgenericcommand`` is not described. A listed name is never invented.
    """
    if _DOCUMENTED_CONTROL_METHOD in names:
        return [_DOCUMENTED_CONTROL_METHOD]
    return []


def public_rpc_text(payload) -> str:
    """Short error text with URLs removed. Dict payloads are not echoed."""
    if isinstance(payload, dict):
        message = payload.get("message")
        text = message if isinstance(message, str) else "RPC rejected"
    elif isinstance(payload, str):
        text = payload
    else:
        text = "RPC rejected"
    text = _URL_RE.sub("ws://grill", text)
    text = " ".join(text.split())
    if len(text) > 160:
        text = text[:160]
    return text


def _sanitize_describe(requested: str, payload) -> dict:
    """Keep only the requested name and a short args format."""
    if not isinstance(payload, dict):
        return {"name": requested, "args_fmt": None, "error": "unusable describe reply"}
    args = payload.get("args_fmt")
    if args is None:
        return {"name": requested, "args_fmt": None}
    if (
        not isinstance(args, str)
        or _ARGS_FMT_RE.fullmatch(args) is None
        or _SECRET_RE.search(args)
    ):
        return {"name": requested, "args_fmt": "[redacted]"}
    return {"name": requested, "args_fmt": args}


def _payload_suffix(payload) -> str:
    """Short rejection detail with any pass field removed."""
    safe = payload
    if isinstance(payload, dict):
        safe = {k: v for k, v in payload.items() if str(k).lower() != "pass"}
    if safe in ("", None, {}, []):
        return ""
    text = str(safe)
    if len(text) > 180:
        text = text[:180] + "..."
    return f": {text}"


_WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_WS_PAYLOAD = 65536


def _split_host(host: str) -> tuple[str, int]:
    if host.startswith("[") and "]" in host:
        name, _, rest = host[1:].partition("]")
        if rest.startswith(":") and rest[1:].isdigit():
            return name, int(rest[1:])
        return name, 80
    if host.count(":") == 1:
        name, port_text = host.split(":", 1)
        if port_text.isdigit():
            return name, int(port_text)
    return host, 80


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError
    return left


def _public_device_error(method: str, payload) -> str:
    """A fixed phrase. Device text is not copied unless it is that phrase."""
    expected = f"No handler for {method}"
    if payload == expected:
        return expected
    return "rejected"


def _ws_client_frame(opcode: int, payload: bytes) -> bytes:
    mask = os.urandom(4)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    length = len(payload)
    if length < 126:
        header = bytes((0x80 | opcode, 0x80 | length))
    elif length <= _MAX_WS_PAYLOAD:
        header = bytes((0x80 | opcode, 0x80 | 126)) + length.to_bytes(2, "big")
    else:
        raise BroilKingError("unusable_reply", public_code="unusable_reply")
    return header + mask + masked


async def _read_exact(reader: asyncio.StreamReader, size: int, deadline: float) -> bytes:
    return await asyncio.wait_for(reader.readexactly(size), _remaining(deadline))


async def _read_ws_message(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    deadline: float,
) -> bytes:
    chunks: list[bytes] = []
    while True:
        first, second = await _read_exact(reader, 2, deadline)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = int.from_bytes(await _read_exact(reader, 2, deadline), "big")
        elif length == 127:
            length = int.from_bytes(await _read_exact(reader, 8, deadline), "big")
        if length > _MAX_WS_PAYLOAD:
            raise BroilKingError("unusable_reply", public_code="unusable_reply")
        mask = await _read_exact(reader, 4, deadline) if masked else b""
        data = await _read_exact(reader, length, deadline) if length else b""
        if masked:
            data = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        if opcode == 0x8:
            raise BroilKingError("no_reply", public_code="no_reply")
        if opcode == 0x9:
            writer.write(_ws_client_frame(0xA, data))
            await asyncio.wait_for(writer.drain(), _remaining(deadline))
            continue
        if opcode in (0x0, 0x1):
            chunks.append(data)
            if first & 0x80:
                return b"".join(chunks)
            continue


def _abort_writer(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    transport = writer.transport
    writer.close()
    if transport is not None:
        transport.abort()


class BroilKingClient:
    """Stateless-per-call RPC client (one short WebSocket per request)."""

    # Action codes, verbatim from the iQue app's BleCommandMaker.
    ACT_POWER_OFF = 0
    ACT_GRILL_TEMP = 1   # value = degrees Fahrenheit (16-bit)
    ACT_PROBE1 = 2       # value = probe 1 target, Fahrenheit
    ACT_PROBE2 = 3       # value = probe 2 target, Fahrenheit
    ACT_TEMP_UNIT = 4
    ACT_COOKING_MODE = 5  # 0 Off / 1 Smoke / 2 Cook / 3 High
    ACT_TIMER = 6        # value = hours, value2 = minutes
    ACT_BRIGHTNESS = 7

    def __init__(self, host: str, session: aiohttp.ClientSession, password: str = ""):
        self._host = host
        self._session = session
        self._password = password or ""
        self._id = 0
        self._last_code = None
        self._last_warn = 0.0

    # ---- low level -----------------------------------------------------
    async def _rpc(self, method: str, args: dict | None = None, timeout: float = 10):
        self._id += 1
        rid = self._id
        frame: dict = {"method": method, "id": rid}
        if args is not None:
            frame["args"] = args
        url = f"ws://{self._host}/rpc"
        # aiohttp 3.14 applies a float only to websocket close, and
        # ClientWSTimeout.ws_receive did not stop a peer that accepted the
        # handshake and then stayed silent. wait_for is the bound that does.
        ws_timeout = aiohttp.ClientWSTimeout(ws_close=timeout, ws_receive=timeout)
        try:
            async with self._session.ws_connect(url, timeout=ws_timeout) as ws:
                await ws.send_str(json.dumps(frame))

                async def _read_reply():
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            resp = json.loads(msg.data)
                            if resp.get("id") != rid:
                                continue
                            err = resp.get("error")
                            self._last_code = err.get("code") if isinstance(err, dict) else None
                            if err is None:
                                return True, resp.get("result")
                            raw = err.get("message", "")
                            try:
                                payload = json.loads(raw)
                            except (ValueError, TypeError):
                                payload = raw
                            return err.get("code", -1) == 0, payload
                        if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                    raise BroilKingError(f"no reply to {method}")

                return await asyncio.wait_for(_read_reply(), timeout)
        except BroilKingError:
            raise
        except TimeoutError as exc:
            raise BroilKingError(f"RPC {method} timed out") from exc
        except (aiohttp.ClientError, OSError) as exc:
            raise BroilKingError(f"RPC {method} failed: {exc}") from exc

    # ---- reads ---------------------------------------------------------
    async def async_get_info(self) -> dict:
        """Sys.GetInfo over plain HTTP (used to identify the device)."""
        url = f"http://{self._host}/rpc/Sys.GetInfo"
        try:
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                return await r.json(content_type=None)
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            raise BroilKingError(f"Sys.GetInfo failed: {exc}") from exc

    async def async_get_temperatures(self) -> dict:
        # Newer firmware dropped GetCurrentTemperatures; GetAllValues (read-only,
        # no args) returns the state under different key names. Map them back to
        # the keys the entities already use.
        self._last_code = None
        ok, p = await self._rpc("GetAllValues")
        if ok and isinstance(p, dict) and "grillTemp" in p:
            def probe(v):
                # 65535 (or 0) == probe unplugged -> 0, which the entities report as unknown
                return 0 if v is None or v >= 65535 else v
            return {
                "grillTemp": p.get("grillTemp"),
                "grillSetTemp": p.get("grillSetTemp"),
                "moduleIsOn": p.get("grillOn"),
                "probe_1_Temp": probe(p.get("probe01Temp")),
                "probe_2_Temp": probe(p.get("probe02Temp")),
                "probe_1_Set_Temp": p.get("probe01SetTemp"),
                "probe_2_Set_Temp": p.get("probe02SetTemp"),
                "alarm_Hour": 0,
                "alarm_Minute": p.get("timer") or 0,
            }
        now = time.monotonic()
        if now - self._last_warn >= 300:
            self._last_warn = now
            _LOGGER.warning("GetAllValues unusable (code=%s)", self._last_code)
        raise BroilKingError("unusable GetAllValues reply")

    def _discover_failure(self, code: str) -> dict:
        return {
            "ok": False,
            "error": code,
            "methods": [],
            "described": [],
            "omitted_count": 0,
        }

    async def _bounded_rpc(self, method: str, args: dict | None, deadline: float):
        """One RPC over asyncio streams. The deadline covers connect and close."""
        self._id += 1
        rid = self._id
        frame = {"method": method, "id": rid}
        if args is not None:
            frame["args"] = args
        host, port = _split_host(self._host)
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                _remaining(deadline),
            )
            key = base64.b64encode(os.urandom(16)).decode()
            request = (
                "GET /rpc HTTP/1.1\r\n"
                f"Host: {host}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "\r\n"
            )
            writer.write(request.encode())
            await asyncio.wait_for(writer.drain(), _remaining(deadline))
            status = await asyncio.wait_for(reader.readline(), _remaining(deadline))
            if not status.startswith(b"HTTP/1.1 101") and not status.startswith(b"HTTP/1.0 101"):
                raise BroilKingError("connection_failed", public_code="connection_failed")
            accept = ""
            for _ in range(40):
                line = await asyncio.wait_for(reader.readline(), _remaining(deadline))
                if line in (b"\r\n", b"\n", b""):
                    break
                if line.lower().startswith(b"sec-websocket-accept:"):
                    accept = line.split(b":", 1)[1].strip().decode("ascii", "replace")
            else:
                raise BroilKingError("connection_failed", public_code="connection_failed")
            expected = base64.b64encode(
                hashlib.sha1(key.encode() + _WS_GUID).digest()
            ).decode()
            if accept != expected:
                raise BroilKingError("connection_failed", public_code="connection_failed")
            writer.write(_ws_client_frame(0x1, json.dumps(frame).encode()))
            await asyncio.wait_for(writer.drain(), _remaining(deadline))
            while True:
                message = await _read_ws_message(reader, writer, deadline)
                try:
                    resp = json.loads(message)
                except (ValueError, TypeError):
                    raise BroilKingError(
                        "unusable_reply", public_code="unusable_reply"
                    ) from None
                if not isinstance(resp, dict) or resp.get("id") != rid:
                    continue
                err = resp.get("error")
                self._last_code = err.get("code") if isinstance(err, dict) else None
                if err is None:
                    return True, resp.get("result")
                raw = err.get("message", "") if isinstance(err, dict) else ""
                try:
                    payload = json.loads(raw)
                except (ValueError, TypeError):
                    payload = raw
                return err.get("code", -1) == 0, payload
        except BroilKingError:
            raise
        except TimeoutError:
            raise BroilKingError("timeout", public_code="timeout") from None
        except asyncio.IncompleteReadError:
            raise BroilKingError("no_reply", public_code="no_reply") from None
        except OSError:
            raise BroilKingError(
                "connection_failed", public_code="connection_failed"
            ) from None
        finally:
            _abort_writer(writer)

    async def async_discover_commands(self) -> dict:
        """Read RPC.List once, then describe only exact SendGenericCommand.

        The 8 second deadline includes the TCP connection, the HTTP upgrade,
        the reply, and closing the socket. There is no retry. The service
        result uses fixed error words, not text from the device or the socket.
        """
        deadline = time.monotonic() + DISCOVER_TIMEOUT
        try:
            ok, payload = await self._bounded_rpc("RPC.List", None, deadline)
        except BroilKingError as exc:
            return self._discover_failure(exc.public_code)
        if not ok or not isinstance(payload, list):
            return self._discover_failure(_public_device_error("RPC.List", payload))
        methods, omitted = sanitize_method_names(payload)
        described = []
        for name in describe_targets(methods):
            try:
                described_ok, body = await self._bounded_rpc(
                    "RPC.Describe", {"name": name}, deadline
                )
            except BroilKingError as exc:
                described.append(
                    {"name": name, "args_fmt": None, "error": exc.public_code}
                )
                break
            if not described_ok:
                described.append(
                    {
                        "name": name,
                        "args_fmt": None,
                        "error": _public_device_error("RPC.Describe", body),
                    }
                )
                continue
            described.append(_sanitize_describe(name, body))
        return {
            "ok": True,
            "error": "",
            "methods": methods,
            "described": described,
            "omitted_count": omitted,
        }

    # ---- control -------------------------------------------------------
    # Setpoints stay on the published SendGenericCommand frames (actions 1/2/3,
    # degrees F). No public source substantiates a different write method for
    # firmware that answers GetAllValues, so this client does not send one.
    # A frame the firmware rejects, or a read-back that still shows the previous
    # setpoint, is a failure. Acceptance by a given grill is established only
    # when that grill echoes the new value.
    @staticmethod
    def _generic_command_string(action: int, value: int = 0, value2: int = 0) -> str:
        lo = value % 256
        hi = (value - lo) // 256
        p = lambda n: "%03d" % (n & 0xFF)
        table = {
            0: "225 225 000 009 000 048 002 " + p(value),
            1: "225 225 000 010 000 048 005 " + p(hi) + " " + p(lo),
            2: "225 225 000 010 000 048 006 " + p(hi) + " " + p(lo),
            3: "225 225 000 010 000 048 009 " + p(hi) + " " + p(lo),
            4: "225 225 000 009 000 048 003 " + p(value),
            5: "225 225 000 009 000 048 007 " + p(value),
            6: "225 225 000 010 000 048 010 " + p(value) + " " + p(value2),
            7: "225 225 000 009 000 048 011 " + p(value),
        }
        if action not in table:
            raise ValueError(f"bad action {action!r}")
        return table[action]

    async def _send_generic(self, action: int, value: int = 0, value2: int = 0):
        args = {
            "command": self._generic_command_string(action, value, value2),
            "pass": self._password,
        }
        return await self._rpc("SendGenericCommand", args)

    async def _require_target(self, ok: bool, payload, label: str, temp_f: int):
        if not ok:
            if payload == "No handler for SendGenericCommand":
                detail = payload
            else:
                detail = "rejected"
            raise BroilKingError(
                f"{label} {int(temp_f)} F rejected by SendGenericCommand: {detail}"
            )
        return ok, payload

    async def _confirm_setpoint(self, label: str, temp_f: int, field: str):
        await asyncio.sleep(TARGET_ACK_DELAY)
        try:
            data = await self.async_get_temperatures()
        except BroilKingError as exc:
            raise BroilKingError(
                f"{label} {int(temp_f)} F was not confirmed: {exc}"
            ) from exc
        ensure_target_confirmed(data.get(field), temp_f, label)
        return data

    async def async_set_grill_target_f(self, temp_f: int):
        temp_f = int(temp_f)
        ok, payload = await self._send_generic(self.ACT_GRILL_TEMP, temp_f)
        await self._require_target(ok, payload, "grill target", temp_f)
        await self._confirm_setpoint("grill target", temp_f, "grillSetTemp")
        return ok, payload

    async def async_set_mode(self, mode_id: int):
        return await self._send_generic(self.ACT_COOKING_MODE, int(mode_id) & 0xFF)

    async def async_set_probe_target_f(self, probe: int, temp_f: int):
        temp_f = int(temp_f)
        act = self.ACT_PROBE1 if probe == 1 else self.ACT_PROBE2
        label = f"probe {probe} target"
        ok, payload = await self._send_generic(act, temp_f)
        await self._require_target(ok, payload, label, temp_f)
        field = "probe_1_Set_Temp" if probe == 1 else "probe_2_Set_Temp"
        await self._confirm_setpoint(label, temp_f, field)
        return ok, payload

    async def async_set_timer(self, hours: int, minutes: int):
        return await self._send_generic(self.ACT_TIMER, int(hours), int(minutes))
