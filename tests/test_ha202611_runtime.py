"""In-memory Home Assistant 2026.11 service registration.

Run with Python >=3.14.2 and homeassistant==2026.11.0. Device I/O is a local
stand-in bound to 127.0.0.1. Schema validation and ServiceRegistry are the
ones Home Assistant ships. This does not connect to an external Home
Assistant or to a grill.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import MappingProxyType

from aiohttp import WSMsgType, web

import homeassistant
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigEntries,
)
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.bootstrap import async_load_base_functionality
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration, async_setup as loader_setup
from homeassistant.setup import async_setup_component
import probatio
import voluptuous as vol

from custom_components.broilking.const import DOMAIN

HOST_SECRET = "not-a-device-password"


class FakeGrill:
    """Local RPC stand-in. Counts methods and can leave one call unanswered."""

    def __init__(self) -> None:
        self.methods: list[str] = []
        self.described: list[str] = []
        self.list_names = [
            "GetAllValues",
            "Sys.GetInfo",
            "FS.Get",
            "Config.Get",
            "SendCustomCommand",
        ]
        self.stall_list = False
        self._runner: web.AppRunner | None = None
        self.port = 0

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/rpc/Sys.GetInfo", self._info)
        app.router.add_get("/rpc", self._ws)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        sockets = site._server.sockets
        assert sockets
        self.port = sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    async def _info(self, _request: web.Request) -> web.Response:
        return web.json_response({"fw_version": "test-firmware", "uptime": 3})

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        async for msg in socket:
            if msg.type != WSMsgType.TEXT:
                continue
            frame = json.loads(msg.data)
            method = str(frame.get("method"))
            self.methods.append(method)
            if method == "RPC.List" and self.stall_list:
                for _ in range(100):
                    if socket.closed:
                        return socket
                    await asyncio.sleep(0.1)
                return socket
            await socket.send_str(json.dumps(self._reply(frame)))
            break
        return socket

    def _reply(self, frame: dict) -> dict:
        method = frame.get("method")
        rid = frame.get("id")
        if method == "GetAllValues":
            state = {
                "grillTemp": 90,
                "grillSetTemp": 225,
                "grillOn": 0,
                "probe01Temp": 43,
                "probe02Temp": 65535,
                "probe01SetTemp": 200,
                "probe02SetTemp": 200,
                "timer": 0,
            }
            return {"id": rid, "error": {"code": 0, "message": json.dumps(state)}}
        if method == "RPC.List":
            assert "args" not in frame or frame["args"] in (None, {})
            return {"id": rid, "result": list(self.list_names)}
        if method == "RPC.Describe":
            name = frame["args"]["name"]
            self.described.append(name)
            return {"id": rid, "result": {"name": name, "args_fmt": "{command: %Q}"}}
        return {"id": rid, "error": {"code": 404, "message": f"No handler for {method}"}}


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.grill = FakeGrill()
        await self.grill.start()
        self.config_dir = Path(tempfile.mkdtemp(prefix="ha202611-broilking-"))
        link = self.config_dir / "custom_components"
        target = Path("/workspace/custom_components")
        if not link.exists():
            link.symlink_to(target, target_is_directory=True)
        self.hass = HomeAssistant(str(self.config_dir))
        self.hass.config_entries = ConfigEntries(self.hass, {})
        loader_setup(self.hass)
        await self.hass.async_start()
        assert await async_load_base_functionality(self.hass)
        assert await async_setup_component(self.hass, "network", {})
        integration = await async_get_integration(self.hass, DOMAIN)
        assert integration.domain == DOMAIN
        self.entry = ConfigEntry(
            data={"host": self.grill.host, "password": HOST_SECRET},
            discovery_keys=MappingProxyType({}),
            domain=DOMAIN,
            minor_version=1,
            options={},
            pref_disable_polling=True,
            source="user",
            subentries_data=None,
            title="Broil King test",
            unique_id="runtime-test",
            version=1,
        )
        await self.hass.config_entries.async_add(self.entry)

    async def asyncTearDown(self) -> None:
        await self.hass.async_stop()
        await self.grill.stop()

    async def test_loaded_service_registry(self) -> None:
        self.assertEqual(HA_VERSION, "2026.11.0")
        self.assertEqual(probatio.__version__, "0.13.0")
        self.assertEqual(vol.__name__, "probatio._vol_shim")
        self.assertFalse(hasattr(vol, "PREVENT"))
        with self.assertRaises(AttributeError):
            getattr(vol, "PREVENT")
        self.assertTrue(hasattr(vol, "PREVENT_EXTRA"))

        self.assertEqual(self.entry.state, ConfigEntryState.LOADED)
        self.assertTrue(self.hass.services.has_service(DOMAIN, "discover_commands"))
        handler = self.hass.services.async_services()[DOMAIN]["discover_commands"]
        self.assertEqual(handler.supports_response.value, "optional")

        before = list(self.grill.methods)
        response = await self.hass.services.async_call(
            DOMAIN,
            "discover_commands",
            {},
            blocking=True,
            return_response=True,
        )
        self.assertIsInstance(response, dict)
        assert response is not None
        self.assertTrue(response["ok"])
        self.assertEqual(response["described"], [])
        self.assertIn("GetAllValues", response["methods"])
        self.assertIn("SendCustomCommand", response["methods"])
        self.assertNotIn("FS.Get", response["methods"])
        self.assertNotIn("Config.Get", response["methods"])
        blob = json.dumps(response)
        self.assertNotIn(HOST_SECRET, blob)
        self.assertNotIn(self.grill.host, blob)
        listed = [name for name in self.grill.methods[len(before) :] if name == "RPC.List"]
        self.assertEqual(listed, ["RPC.List"])
        self.assertNotIn("FS.Get", self.grill.methods)
        self.assertNotIn("Config.Get", self.grill.methods)
        self.assertNotIn("SendGenericCommand", self.grill.methods)

        with self.assertRaises(probatio.Invalid):
            await self.hass.services.async_call(
                DOMAIN,
                "discover_commands",
                {"method": "FS.Get"},
                blocking=True,
                return_response=True,
            )
        self.assertEqual(
            [name for name in self.grill.methods if name == "RPC.List"],
            ["RPC.List"],
        )

        silent = await self.hass.services.async_call(
            DOMAIN,
            "discover_commands",
            {},
            blocking=True,
            return_response=False,
        )
        self.assertIsNone(silent)

        self.grill.list_names = ["GetAllValues", "SendGenericCommand"]
        described = await self.hass.services.async_call(
            DOMAIN,
            "discover_commands",
            {},
            blocking=True,
            return_response=True,
        )
        assert described is not None
        self.assertEqual(
            described["described"],
            [{"name": "SendGenericCommand", "args_fmt": "{command: %Q}"}],
        )
        self.assertEqual(self.grill.described, ["SendGenericCommand"])

        self.grill.stall_list = True
        started = time.monotonic()
        stalled = await asyncio.wait_for(
            self.hass.services.async_call(
                DOMAIN,
                "discover_commands",
                {},
                blocking=True,
                return_response=True,
            ),
            timeout=12,
        )
        elapsed = time.monotonic() - started
        self.assertFalse(stalled["ok"])
        self.assertGreaterEqual(elapsed, 7)
        self.assertLess(elapsed, 12)
        # Answered lists plus one silent list, with no retry of the silence.
        self.assertEqual(self.grill.methods.count("RPC.List"), 4)

        unloaded = await self.hass.config_entries.async_unload(self.entry.entry_id)
        self.assertTrue(unloaded)
        self.assertFalse(self.hass.services.has_service(DOMAIN, "discover_commands"))


if __name__ == "__main__":
    unittest.main()
