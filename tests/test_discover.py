"""Offline tests for the read-only RPC.List diagnostic.

No socket is opened. The service cannot name an arbitrary RPC method, and
RPC.Describe is limited to SendGenericCommand when that name is in the list.
"""
from __future__ import annotations

import hashlib
import json
import unittest

from tests.test_target_write import (
    ROOT,
    FakeSession,
    HomeAssistantError,
    load,
    rpc_ok,
)

BASELINE_012 = {
    "climate.py": "a6751420b71c4e980d53614db130991e93d6dbed7ea80ab0b4aad85bafcb51c3",
    "const.py": "09061152869755f704ae5309f09ddd4863f8ff26b0fa65a9ad7b01d121381199",
    "coordinator.py": "d3c434a0e14d726f6cbed41c98e66547a3f8a9ba7698b20b2b4174fe80104ac5",
    "entity.py": "970dc1ebd63a483a340a30e5835673ae0b2f21252378bee4ccedbd4be5e7cb06",
    "number.py": "aa36759a2f2eb2f58dd7b8b914339462a1903d129001ab519ae7c2b78b04b67a",
    "sensor.py": "b10b91a002d6926c5ecd6fdc638078d0cf4bb5b8aee0d88aa07d3f13f5bc4036",
    "binary_sensor.py": "64c0566b70cf5845b4a957e1c87cb168821b78a5b867dc6687f7ef1c898f1ff5",
    "strings.json": "53bfb89bf649d06b103122104e0d8112a6e9bcdaa6084777ce6b41b5b2baa434",
}

HOST = "192.0.2.8"
SECRET = "secret-token"


def _listed(names):
    return {"result": names}


class DiscoverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = load("api")
        self.sleeps = []

        async def no_wait(delay):
            self.sleeps.append(delay)

        self._sleep = self.api.asyncio.sleep
        self.api.asyncio.sleep = no_wait

    async def asyncTearDown(self):
        self.api.asyncio.sleep = self._sleep

    def client(self, replies, password=SECRET):
        return self.api.BroilKingClient("grill.invalid", FakeSession(replies), password)
    def test_name_filter_drops_secrets_and_duplicates(self):
        api = self.api
        names, omitted = api.sanitize_method_names(
            [
                "GetAllValues",
                "FS.Get",
                "Config.Get",
                "Config.Set",
                "wifiPassword",
                "not a name",
                {"name": "Sys.GetInfo", "value": SECRET},
                "GetAllValues",
                "SendGenericCommand",
                "sendgenericcommand",
                12,
                "x" * 80,
            ]
        )
        self.assertEqual(
            names, ["GetAllValues", "SendGenericCommand", "Sys.GetInfo"]
        )
        self.assertGreaterEqual(omitted, 8)
        self.assertNotIn(SECRET, json.dumps(names))
        self.assertEqual(
            api.describe_targets(names), ["SendGenericCommand"]
        )
        self.assertEqual(
            api.describe_targets(
                ["SendCustomCommand", "SetGrillTemp", "ProbeTarget8", "GetAllValues"]
            ),
            [],
        )
        self.assertLessEqual(len(api.describe_targets(["SendGenericCommand"] * 5)), 3)

    def test_args_format_redacts_pass_and_drops_extra_fields(self):
        described = self.api._sanitize_describe(
            "SendGenericCommand",
            {
                "name": "SendGenericCommand",
                "args_fmt": "{command: %Q, pass: %Q}",
                "password": SECRET,
                "host": HOST,
            },
        )
        self.assertEqual(described["name"], "SendGenericCommand")
        self.assertEqual(described["args_fmt"], "[redacted]")
        self.assertNotIn("password", described)
        self.assertNotIn(SECRET, json.dumps(described))
        plain = self.api._sanitize_describe(
            "SendGenericCommand", {"args_fmt": "{command: %Q}"}
        )
        self.assertEqual(plain, {"name": "SendGenericCommand", "args_fmt": "{command: %Q}"})

    def test_service_source_is_not_a_passthrough(self):
        init = (ROOT / "custom_components" / "broilking" / "__init__.py").read_text()
        services = (ROOT / "custom_components" / "broilking" / "services.yaml").read_text()
        coordinator = (
            ROOT / "custom_components" / "broilking" / "coordinator.py"
        ).read_text()
        setup = init.split("async def async_setup_entry", 1)[1].split(
            "def _async_ensure_discover_service", 1
        )[0]
        self.assertIn("async_config_entry_first_refresh", setup)
        self.assertNotIn("async_discover_commands", setup)
        self.assertNotIn("RPC.List", setup)
        self.assertNotIn("RPC.List", coordinator)
        self.assertIn('SERVICE_DISCOVER = "discover_commands"', init)
        self.assertIn("extra=vol.PREVENT", init)
        self.assertNotIn('Optional("method")', init)
        self.assertNotIn('Optional("command")', init)
        self.assertNotIn('Optional("host")', init)
        self.assertNotIn("FS.Get", services)
        self.assertNotIn("Config.Get", services)
        discover = self.api.BroilKingClient.async_discover_commands
        source = discover.__doc__ + "\n" + _function_body(discover)
        self.assertNotIn('_rpc("FS.Get"', source)
        self.assertNotIn('_rpc("Config.Get"', source)
        self.assertNotIn('_rpc("SendGenericCommand"', source)
        self.assertNotIn('_rpc("SendCustomCommand"', source)
        self.assertEqual(source.count("await self._rpc("), 2)

    def test_unchanged_entity_files_match_012_baseline(self):
        directory = ROOT / "custom_components" / "broilking"
        for name, expected in BASELINE_012.items():
            digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()
            self.assertEqual(digest, expected, name)

    async def test_list_only_when_no_documented_control_method(self):
        client = self.client(
            [
                _listed(
                    [
                        "GetAllValues",
                        "Sys.GetInfo",
                        "SendCustomCommand",
                        "FS.Get",
                        "Config.Get",
                    ]
                )
            ],
            password=SECRET,
        )
        client._host = HOST
        result = await client.async_discover_commands()
        self.assertTrue(result["ok"])
        self.assertEqual(result["error"], "")
        self.assertEqual(
            result["methods"],
            ["GetAllValues", "SendCustomCommand", "Sys.GetInfo"],
        )
        self.assertEqual(result["described"], [])
        self.assertEqual(result["omitted_count"], 2)
        self.assertEqual(len(client._session.sent), 1)
        frame = client._session.sent[0]
        self.assertEqual(frame["method"], "RPC.List")
        self.assertNotIn("args", frame)
        self.assertEqual(client._session.timeouts, [self.api.DISCOVER_TIMEOUT])
        blob = json.dumps(result)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn(HOST, blob)
        self.assertNotIn("FS.Get", blob)
        self.assertNotIn("Config.Get", blob)

    async def test_describe_only_documented_method_from_the_list(self):
        client = self.client(
            [
                _listed(
                    [
                        "SendCustomCommand",
                        "SendGenericCommand",
                        "FS.Get",
                        "SetGrillTemp",
                    ]
                ),
                rpc_ok({"name": "SendGenericCommand", "args_fmt": "{command: %Q}"}),
            ]
        )
        result = await client.async_discover_commands()
        self.assertEqual(
            [frame["method"] for frame in client._session.sent],
            ["RPC.List", "RPC.Describe"],
        )
        self.assertEqual(
            client._session.sent[1]["args"], {"name": "SendGenericCommand"}
        )
        self.assertNotIn("pass", client._session.sent[1]["args"])
        self.assertEqual(
            result["described"],
            [{"name": "SendGenericCommand", "args_fmt": "{command: %Q}"}],
        )
        self.assertNotIn("SendCustomCommand", json.dumps(result["described"]))

    async def test_missing_list_method_is_one_call(self):
        client = self.client(
            [{"error": {"code": 404, "message": "No handler for RPC.List"}}]
        )
        result = await client.async_discover_commands()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "No handler for RPC.List")
        self.assertEqual(result["methods"], [])
        self.assertEqual(result["described"], [])
        self.assertEqual(len(client._session.sent), 1)
        self.assertEqual(client._session.timeouts, [self.api.DISCOVER_TIMEOUT])

    async def test_timeout_does_not_retry_or_leak_host(self):
        session = _TimeoutSession()
        client = self.api.BroilKingClient(HOST, session, SECRET)
        result = await client.async_discover_commands()
        self.assertFalse(result["ok"])
        self.assertEqual(session.calls, [self.api.DISCOVER_TIMEOUT])
        blob = json.dumps(result)
        self.assertNotIn(HOST, blob)
        self.assertNotIn(SECRET, blob)
        self.assertIn("RPC.List", result["error"])
        self.assertIsInstance(self.api.BroilKingError("x"), HomeAssistantError)

    async def test_temperature_read_timeout_stays_ten_seconds(self):
        client = self.client([rpc_ok({"grillTemp": 1})])
        await client.async_get_temperatures()
        self.assertEqual(client._session.timeouts, [10])
        self.assertEqual(client._session.sent[0]["method"], "GetAllValues")


class _TimeoutSession:
    def __init__(self):
        self.calls = []

    def ws_connect(self, url, timeout=10):
        self.calls.append(timeout)
        raise TimeoutError(f"timed out talking to ws://{HOST}/rpc")


def _function_body(func) -> str:
    import inspect

    return inspect.getsource(func)
