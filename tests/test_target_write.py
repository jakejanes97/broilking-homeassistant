"""Offline tests for Broil King target writes.

The grill is not contacted. Hardware acceptance of SendGenericCommand on a
GetAllValues firmware is not established by these tests; they lock conversion,
the published frame bytes, response handling, and the installed read mapping.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
import types
import unittest
from pathlib import Path

logging.disable(logging.CRITICAL)

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
PKG = "custom_components.broilking"
SECRET = "test-pass-not-a-credential"

# Frozen SendGenericCommand bodies. Actions 0 and 4-7 are the power, unit,
# mode, timer, and brightness frames and must stay byte-for-byte.
GOLDEN_COMMANDS = {
    (0, 0, 0): "225 225 000 009 000 048 002 000",
    (1, 175, 0): "225 225 000 010 000 048 005 000 175",
    (1, 300, 0): "225 225 000 010 000 048 005 001 044",
    (2, 175, 0): "225 225 000 010 000 048 006 000 175",
    (2, 200, 0): "225 225 000 010 000 048 006 000 200",
    (3, 175, 0): "225 225 000 010 000 048 009 000 175",
    (4, 1, 0): "225 225 000 009 000 048 003 001",
    (5, 0, 0): "225 225 000 009 000 048 007 000",
    (5, 2, 0): "225 225 000 009 000 048 007 002",
    (6, 1, 30): "225 225 000 010 000 048 010 001 030",
    (7, 3, 0): "225 225 000 009 000 048 011 003",
}


class HomeAssistantError(Exception):
    """Stand-in for homeassistant.exceptions.HomeAssistantError."""


class ServiceValidationError(HomeAssistantError):
    """Stand-in for homeassistant.exceptions.ServiceValidationError."""


def ensure_ha_exceptions() -> None:
    """Register exception stand-ins before api.py is imported."""
    if "homeassistant.exceptions" in sys.modules:
        return
    if "homeassistant" not in sys.modules:
        sys.modules["homeassistant"] = types.ModuleType("homeassistant")
    module = types.ModuleType("homeassistant.exceptions")
    module.HomeAssistantError = HomeAssistantError
    module.ServiceValidationError = ServiceValidationError
    sys.modules["homeassistant.exceptions"] = module


def load(name: str):
    """Load an integration module without executing the HA package init."""
    ensure_ha_exceptions()
    for mod_name, path in (
        ("custom_components", ROOT / "custom_components"),
        (PKG, ROOT / "custom_components" / "broilking"),
    ):
        if mod_name not in sys.modules:
            module = types.ModuleType(mod_name)
            module.__path__ = [str(path)]
            module.__package__ = mod_name
            sys.modules[mod_name] = module
    full = f"{PKG}.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(
        full, ROOT / "custom_components" / "broilking" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = PKG
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


def stub_homeassistant() -> None:
    if "homeassistant.helpers.update_coordinator" in sys.modules:
        return

    def add(name: str, **attrs):
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module
        return module

    class CoordinatorEntity:
        def __init__(self, coordinator):
            self.coordinator = coordinator

        def __class_getitem__(cls, _item):
            return cls

    class DataUpdateCoordinator:
        def __init__(self, *args, **kwargs):
            pass

        def __class_getitem__(cls, _item):
            return cls

    add("homeassistant")
    add("homeassistant.core", HomeAssistant=object)
    add("homeassistant.helpers")
    add("homeassistant.helpers.device_registry", DeviceInfo=dict)
    add(
        "homeassistant.helpers.update_coordinator",
        CoordinatorEntity=CoordinatorEntity,
        DataUpdateCoordinator=DataUpdateCoordinator,
    )


def rpc_ok(message) -> dict:
    if not isinstance(message, str):
        message = json.dumps(message)
    return {"error": {"code": 0, "message": message}}


def rpc_err(code: int, message) -> dict:
    if not isinstance(message, str):
        message = json.dumps(message)
    return {"error": {"code": code, "message": message}}


def sample_state(**overrides) -> dict:
    """Generic GetAllValues body. Not a capture from a grill."""
    state = {
        "grillTemp": 90,
        "grillSetTemp": 225,
        "grillOn": 0,
        "probe01Temp": 43,
        "probe02Temp": 65535,
        "probe01SetTemp": 200,
        "probe02SetTemp": 200,
        "timer": 0,
        "devicePass": SECRET,
        "ssid": "not-a-network",
    }
    state.update(overrides)
    return state


class _Msg:
    def __init__(self, data: str, msg_type):
        self.data = data
        self.type = msg_type


class FakeWS:
    def __init__(self, session, reply):
        self.session = session
        self.reply = reply
        self._done = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def send_str(self, data: str) -> None:
        self.session.sent.append(json.loads(data))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._done:
            raise StopAsyncIteration
        self._done = True
        frame = self.session.sent[-1]
        body = self.reply(frame) if callable(self.reply) else dict(self.reply)
        body.setdefault("id", frame["id"])
        return _Msg(json.dumps(body), aiohttp.WSMsgType.TEXT)


class FakeSession:
    def __init__(self, replies):
        self._replies = list(replies)
        self.sent = []
        self.timeouts = []

    def ws_connect(self, url, timeout=10):
        self.timeouts.append(timeout)
        if not self._replies:
            raise AssertionError(f"no fake reply left for {url}")
        return FakeWS(self, self._replies.pop(0))


class ClosedSession(FakeSession):
    def ws_connect(self, url, timeout=10):
        return _ClosedWS(self)


class _ClosedWS(FakeWS):
    def __init__(self, session):
        super().__init__(session, {})

    async def __anext__(self):
        raise StopAsyncIteration


class DownSession:
    sent = []

    def ws_connect(self, url, timeout=10):
        raise aiohttp.ClientConnectionError("unreachable")


class TargetWriteTests(unittest.IsolatedAsyncioTestCase):
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

    def test_command_table_matches_published_frames(self):
        for (action, value, value2), expected in GOLDEN_COMMANDS.items():
            got = self.api.BroilKingClient._generic_command_string(
                action, value, value2
            )
            self.assertEqual(got, expected)
        with self.assertRaises(ValueError):
            self.api.BroilKingClient._generic_command_string(9, 1, 0)

    def test_source_keeps_getallvalues_and_omits_custom_commands(self):
        source = (ROOT / "custom_components" / "broilking" / "api.py").read_text()
        body = source.split("async def async_get_temperatures", 1)[1].split(
            "\n    async def ", 1
        )[0]
        code = "\n".join(
            line for line in body.splitlines() if not line.strip().startswith("#")
        )
        self.assertIn('_rpc("GetAllValues")', code)
        self.assertNotIn("GetCurrentTemperatures", code)
        self.assertNotIn("SendCustomCommand", source)
        self.assertNotIn("commandID", source)

    def test_entities_keep_celsius_ranges_and_other_controls(self):
        const = load("const")
        number = (ROOT / "custom_components" / "broilking" / "number.py").read_text()
        climate = (ROOT / "custom_components" / "broilking" / "climate.py").read_text()
        probe = number.split("class BroilKingProbeTarget", 1)[1]
        timer = number.split("class BroilKingProbeTarget", 1)[0]
        self.assertNotIn("_pending", probe)
        self.assertIn("async_refresh", probe)
        self.assertIn("_pending", timer)
        self.assertIn("async_set_timer", timer)
        self.assertIn("BOARD_ACK_DELAY", timer)
        preset = climate.split("async def async_set_preset_mode", 1)[1]
        self.assertIn("async_set_mode", preset)
        self.assertIn("async_request_refresh", preset)
        power = climate.split("async def async_set_hvac_mode", 1)[1]
        self.assertIn("async_set_mode", power)
        self.assertEqual(const.MIN_TEMP_C, 82)
        self.assertEqual(const.MAX_TEMP_C, 316)
        self.assertEqual(const.PROBE_TARGET_MIN_C, 30)
        self.assertEqual(const.PROBE_TARGET_MAX_C, 175)
        self.assertEqual(const.MODES, {0: "Off", 1: "Smoke", 2: "Cook", 3: "High"})
        manifest = json.loads(
            (ROOT / "custom_components" / "broilking" / "manifest.json").read_text()
        )
        self.assertEqual(manifest["version"], "0.1.5")

    def test_conversion_and_display_round_trip(self):
        stub_homeassistant()
        entity = load("entity")
        # 175 F expressed as Celsius still encodes back to 175 F on the wire.
        as_c = (175 - 32) * 5.0 / 9.0
        self.assertEqual(entity.c_to_f(as_c), 175)
        const = load("const")
        self.assertLessEqual(const.PROBE_TARGET_MIN_C, as_c)
        self.assertGreaterEqual(const.PROBE_TARGET_MAX_C, as_c)
        # A stored 200 F becomes 93.3 C. Showing that Celsius value in
        # Fahrenheit with one decimal is 199.9, which is the display artifact.
        native_c = entity.f_to_c(200)
        self.assertEqual(native_c, 93.3)
        displayed_f = round(native_c * 9.0 / 5.0 + 32.0, 1)
        self.assertEqual(displayed_f, 199.9)
        self.assertEqual(entity.c_to_f(native_c), 200)
        self.assertIsNone(entity.f_to_c(None))

    def test_confirm_helper(self):
        self.api.ensure_target_confirmed(175, 175, "probe 1 target")
        self.api.ensure_target_confirmed(175.0, 175, "probe 1 target")
        with self.assertRaises(self.api.BroilKingError) as ctx:
            self.api.ensure_target_confirmed(200, 175, "probe 1 target")
        self.assertIn("175", str(ctx.exception))
        self.assertIn("200", str(ctx.exception))
        with self.assertRaises(self.api.BroilKingError):
            self.api.ensure_target_confirmed(None, 175, "grill target")

    async def test_getallvalues_mapping_drops_secrets(self):
        client = self.client([rpc_ok(sample_state())])
        data = await client.async_get_temperatures()
        self.assertEqual(
            data,
            {
                "grillTemp": 90,
                "grillSetTemp": 225,
                "moduleIsOn": 0,
                "probe_1_Temp": 43,
                "probe_2_Temp": 0,
                "probe_1_Set_Temp": 200,
                "probe_2_Set_Temp": 200,
                "alarm_Hour": 0,
                "alarm_Minute": 0,
            },
        )
        self.assertNotIn(SECRET, json.dumps(data))
        frame = client._session.sent[0]
        self.assertEqual(frame["method"], "GetAllValues")
        self.assertNotIn("args", frame)

    async def test_unusable_read_raises(self):
        client = self.client([rpc_err(404, "missing")])
        with self.assertRaises(self.api.BroilKingError):
            await client.async_get_temperatures()

    def test_device_error_is_home_assistant_error(self):
        self.assertTrue(issubclass(self.api.BroilKingError, HomeAssistantError))
        self.assertFalse(issubclass(self.api.BroilKingError, ServiceValidationError))

    async def test_missing_send_generic_handler_is_one_call(self):
        """Mongoose OS reports an unregistered method as code 404.

        The message is a plain string, the call does not wait or retry, and
        the exception Home Assistant handles is HomeAssistantError. A valid
        175 F request is not a ServiceValidationError.
        """
        client = self.client(
            [
                {
                    "error": {
                        "code": 404,
                        "message": "No handler for SendGenericCommand",
                    }
                }
            ]
        )
        with self.assertRaises(self.api.BroilKingError) as ctx:
            await client.async_set_probe_target_f(1, 175)
        err = ctx.exception
        self.assertIsInstance(err, HomeAssistantError)
        self.assertNotIsInstance(err, ServiceValidationError)
        text = str(err)
        self.assertEqual(
            text,
            "probe 1 target 175 F rejected by SendGenericCommand"
            ": No handler for SendGenericCommand",
        )
        self.assertEqual(self.sleeps, [])
        self.assertEqual(len(client._session.sent), 1)
        self.assertEqual(client._session.sent[0]["method"], "SendGenericCommand")
        self.assertEqual(
            client._session.sent[0]["args"]["command"],
            GOLDEN_COMMANDS[(2, 175, 0)],
        )

    async def test_probe_rejected_frame_raises_without_waiting(self):
        client = self.client(
            [rpc_err(3, {"reason": "ignored", "pass": SECRET})]
        )
        with self.assertRaises(self.api.BroilKingError) as ctx:
            await client.async_set_probe_target_f(1, 175)
        text = str(ctx.exception)
        self.assertIn("rejected", text)
        self.assertNotIn(SECRET, text)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(len(client._session.sent), 1)
        frame = client._session.sent[0]
        self.assertEqual(frame["method"], "SendGenericCommand")
        self.assertEqual(
            frame["args"]["command"], GOLDEN_COMMANDS[(2, 175, 0)]
        )
        self.assertNotEqual(
            frame["args"]["command"], GOLDEN_COMMANDS[(2, 200, 0)]
        )

    async def test_probe_ack_without_echo_is_not_acceptance(self):
        """A code-0 reply that leaves the old setpoint is still a failure.

        This does not show whether a physical grill accepts the frame.
        """
        client = self.client([rpc_ok("ok"), rpc_ok(sample_state())])
        with self.assertRaises(self.api.BroilKingError) as ctx:
            await client.async_set_probe_target_f(1, 175)
        self.assertIn("not confirmed", str(ctx.exception))
        self.assertIn("200", str(ctx.exception))
        self.assertEqual(self.sleeps, [self.api.TARGET_ACK_DELAY])
        methods = [frame["method"] for frame in client._session.sent]
        self.assertEqual(methods, ["SendGenericCommand", "GetAllValues"])

    async def test_probe_echo_returns_success(self):
        echoed = sample_state(probe01SetTemp=175)
        client = self.client([rpc_ok({"status": "ok"}), rpc_ok(echoed)])
        ok, payload = await client.async_set_probe_target_f(1, 175)
        self.assertTrue(ok)
        self.assertEqual(payload, {"status": "ok"})
        self.assertEqual(
            client._session.sent[0]["args"]["command"],
            GOLDEN_COMMANDS[(2, 175, 0)],
        )

    async def test_probe_2_and_grill_frames(self):
        echoed = sample_state(probe02SetTemp=175, grillSetTemp=250)
        probe = self.client([{"result": "ok"}, rpc_ok(echoed)])
        await probe.async_set_probe_target_f(2, 175)
        self.assertEqual(
            probe._session.sent[0]["args"]["command"],
            GOLDEN_COMMANDS[(3, 175, 0)],
        )
        grill = self.client(
            [rpc_ok("ok"), rpc_ok(sample_state(grillSetTemp=250))]
        )
        await grill.async_set_grill_target_f(250)
        self.assertEqual(
            grill._session.sent[0]["method"], "SendGenericCommand"
        )
        self.assertEqual(
            grill._session.sent[0]["args"]["command"],
            "225 225 000 010 000 048 005 000 250",
        )

    async def test_grill_keeps_previous_setpoint(self):
        client = self.client(
            [rpc_ok("ok"), rpc_ok(sample_state(grillSetTemp=225))]
        )
        with self.assertRaises(self.api.BroilKingError) as ctx:
            await client.async_set_grill_target_f(250)
        self.assertIn("grill target 250 F", str(ctx.exception))
        self.assertIn("225", str(ctx.exception))

    async def test_read_failure_after_ack_is_not_success(self):
        client = self.client([rpc_ok("ok"), rpc_err(1, "later")])
        with self.assertRaises(self.api.BroilKingError) as ctx:
            await client.async_set_probe_target_f(1, 175)
        self.assertIn("not confirmed", str(ctx.exception))

    async def test_mode_timer_and_power_keep_previous_behavior(self):
        """Other controls keep the previous return-the-tuple behavior."""
        mode = self.client([rpc_err(9, "no")])
        ok, payload = await mode.async_set_mode(2)
        self.assertFalse(ok)
        self.assertEqual(payload, "no")
        self.assertEqual(self.sleeps, [])
        self.assertEqual(
            mode._session.sent[0]["args"]["command"],
            GOLDEN_COMMANDS[(5, 2, 0)],
        )

        power = self.client([rpc_ok("ok")])
        ok, _payload = await power.async_set_mode(0)
        self.assertTrue(ok)
        self.assertEqual(
            power._session.sent[0]["args"]["command"],
            GOLDEN_COMMANDS[(5, 0, 0)],
        )

        timer = self.client([rpc_ok({"left": 90})])
        ok, payload = await timer.async_set_timer(1, 30)
        self.assertTrue(ok)
        self.assertEqual(payload, {"left": 90})
        self.assertEqual(
            timer._session.sent[0]["args"]["command"],
            GOLDEN_COMMANDS[(6, 1, 30)],
        )
        self.assertEqual(len(timer._session.sent), 1)

    async def test_transport_and_empty_reply_raise(self):
        down = self.api.BroilKingClient("grill.invalid", DownSession(), "")
        with self.assertRaises(self.api.BroilKingError):
            await down.async_set_grill_target_f(225)
        silent = self.api.BroilKingClient("grill.invalid", ClosedSession([]), "")
        with self.assertRaises(self.api.BroilKingError):
            await silent.async_set_probe_target_f(1, 175)

    def test_payload_suffix_redacts_pass_and_truncates(self):
        text = self.api._payload_suffix({"pass": SECRET, "reason": "busy"})
        self.assertNotIn(SECRET, text)
        self.assertIn("busy", text)
        long_text = self.api._payload_suffix("x" * 400)
        self.assertLess(len(long_text), 200)
        self.assertTrue(long_text.endswith("..."))


if __name__ == "__main__":
    unittest.main()
