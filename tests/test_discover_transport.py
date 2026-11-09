"""Localhost discovery transport. No external host is contacted.

These tests use the real client socket path: TCP accept with no HTTP,
a refused port, a secret planted in a device error, a mute peer, a
wrong-id flood, and cancellation.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
import unittest

from tests.test_target_write import load

TOKEN = "planted-secret-token"
_WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _server_frame(text: str) -> bytes:
    payload = text.encode()
    return bytes((0x81, len(payload))) + payload


def _accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1(key.encode() + _WS_GUID).digest()).decode()


async def _read_http(reader: asyncio.StreamReader) -> dict[str, str]:
    request = (await reader.readuntil(b"\r\n\r\n")).decode("ascii", "replace")
    headers = {}
    for line in request.split("\r\n")[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    return headers


def _client_json(data: bytes) -> dict:
    start = data.find(b"\x81")
    length = data[start + 1] & 0x7F
    offset = start + 2
    mask = data[offset : offset + 4]
    offset += 4
    payload = bytes(
        byte ^ mask[index % 4] for index, byte in enumerate(data[offset : offset + length])
    )
    return json.loads(payload)


async def _finish_handshake(reader, writer) -> None:
    headers = await _read_http(reader)
    accept = _accept_key(headers["sec-websocket-key"])
    writer.write(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        ).encode()
    )
    await writer.drain()


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.api = load("api")
        self.accepts = 0

    def client(self, port: int):
        return self.api.BroilKingClient(f"127.0.0.1:{port}", session=None, password=TOKEN)

    async def test_handshake_stall_returns_within_eight_seconds(self) -> None:
        async def handler(reader, writer):
            self.accepts += 1
            try:
                await asyncio.sleep(30)
            finally:
                writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        started = time.monotonic()
        result = await self.client(port).async_discover_commands()
        elapsed = time.monotonic() - started
        self.assertEqual(result["error"], "timeout")
        self.assertFalse(result["ok"])
        self.assertGreaterEqual(elapsed, 7)
        self.assertLess(elapsed, 9.5)
        self.assertEqual(self.accepts, 1)
        self.assertNotIn("127.0.0.1", json.dumps(result))
        again = await self.client(port).async_discover_commands()
        self.assertEqual(again["error"], "timeout")
        self.assertEqual(self.accepts, 2)
        server.close()
        await server.wait_closed()

    async def test_refused_connection_hides_the_address(self) -> None:
        result = await self.client(1).async_discover_commands()
        blob = json.dumps(result)
        self.assertEqual(result["error"], "connection_failed")
        self.assertNotIn("127.0.0.1", blob)
        self.assertNotIn(TOKEN, blob)

    async def test_secret_in_device_error_is_not_returned(self) -> None:
        async def handler(reader, writer):
            self.accepts += 1
            await _finish_handshake(reader, writer)
            request = _client_json(await reader.read(4096))
            body = {
                "id": request["id"],
                "error": {
                    "code": 404,
                    "message": f"No handler for RPC.List {TOKEN} ws://127.0.0.1/rpc",
                },
            }
            writer.write(_server_frame(json.dumps(body)))
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        result = await self.client(port).async_discover_commands()
        blob = json.dumps(result)
        self.assertEqual(result["error"], "rejected")
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn("127.0.0.1", blob)
        self.assertEqual(self.accepts, 1)
        server.close()
        await server.wait_closed()

    async def test_exact_no_handler_phrase_is_kept(self) -> None:
        async def handler(reader, writer):
            await _finish_handshake(reader, writer)
            request = _client_json(await reader.read(4096))
            body = {
                "id": request["id"],
                "error": {"code": 404, "message": "No handler for RPC.List"},
            }
            writer.write(_server_frame(json.dumps(body)))
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        result = await self.client(port).async_discover_commands()
        self.assertEqual(result["error"], "No handler for RPC.List")
        server.close()
        await server.wait_closed()

    async def test_lowercase_name_is_not_described(self) -> None:
        seen = []

        async def handler(reader, writer):
            self.accepts += 1
            await _finish_handshake(reader, writer)
            request = _client_json(await reader.read(4096))
            seen.append(request.get("method") == "RPC.Describe")
            if self.accepts == 1:
                body = {
                    "id": request["id"],
                    "result": ["sendgenericcommand", "GetAllValues"],
                }
            else:
                body = {"id": request["id"], "result": {"args_fmt": "{}"}}
            writer.write(_server_frame(json.dumps(body)))
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        result = await self.client(port).async_discover_commands()
        self.assertEqual(result["described"], [])
        self.assertIn("sendgenericcommand", result["methods"])
        self.assertEqual(self.accepts, 1)
        self.assertEqual(seen, [False])
        server.close()
        await server.wait_closed()

    async def test_mute_peer_and_wrong_id_do_not_retry(self) -> None:
        frames_out = []

        async def handler(reader, writer):
            self.accepts += 1
            await _finish_handshake(reader, writer)
            data = await reader.read(4096)
            frames_out.append(data)
            if self.accepts == 1:
                try:
                    await asyncio.sleep(30)
                finally:
                    writer.close()
                return
            for _ in range(40):
                writer.write(_server_frame(json.dumps({"id": 999, "result": []})))
            await writer.drain()
            try:
                await asyncio.sleep(30)
            finally:
                writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = self.client(port)
        started = time.monotonic()
        mute = await client.async_discover_commands()
        self.assertEqual(mute["error"], "timeout")
        self.assertLess(time.monotonic() - started, 9.5)
        started = time.monotonic()
        wrong = await client.async_discover_commands()
        self.assertEqual(wrong["error"], "timeout")
        self.assertLess(time.monotonic() - started, 9.5)
        self.assertEqual(self.accepts, 2)
        self.assertEqual(len(frames_out), 2)
        server.close()
        await server.wait_closed()

    async def test_cancellation_does_not_block_a_later_call(self) -> None:
        release = asyncio.Event()

        async def handler(reader, writer):
            self.accepts += 1
            await _finish_handshake(reader, writer)
            raw = await reader.read(4096)
            if self.accepts == 1:
                await release.wait()
                writer.close()
                return
            request = _client_json(raw)
            body = {"id": request["id"], "result": ["GetAllValues"]}
            writer.write(_server_frame(json.dumps(body)))
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = self.client(port)
        task = asyncio.create_task(client.async_discover_commands())
        await asyncio.sleep(1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        release.set()
        result = await client.async_discover_commands()
        self.assertTrue(result["ok"])
        self.assertEqual(result["methods"], ["GetAllValues"])
        self.assertEqual(self.accepts, 2)
        server.close()
        await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
