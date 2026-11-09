"""Schema regression on probatio 0.13.0, the engine Home Assistant 2026.11.0 pins.

Home Assistant aliases voluptuous to probatio._vol_shim before integrations
load. This test does that, then builds the discover schema and calls it the
way ServiceRegistry.async_call does: handler.schema(service_data).

This interpreter cannot import the homeassistant 2026.11.0 package. The
schema object and the unknown-key failure are from the real pinned engine.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

from probatio.compat import install_as_voluptuous

install_as_voluptuous()

import probatio
import voluptuous as vol

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "custom_components" / "broilking" / "discover_schema.py"


def _load_schema_module():
    spec = importlib.util.spec_from_file_location(
        "broilking_discover_schema_under_test", SCHEMA_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate_like_core_2026_11(schema, service_data):
    """Match homeassistant/core.py ServiceRegistry.async_call at tag 2026.11.0.

    The core method calls ``handler.schema(service_data)`` and lets
    ``probatio.Invalid`` propagate.
    """
    if schema:
        return schema(service_data)
    return service_data


class HomeAssistant202611SchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema_mod = _load_schema_module()

    def test_engine_is_the_pinned_shim(self):
        self.assertEqual(probatio.__version__, "0.13.0")
        self.assertEqual(vol.__name__, "probatio._vol_shim")
        self.assertIs(vol.Schema, self.schema_mod.vol.Schema)
        self.assertFalse(hasattr(vol, "PREVENT"))
        with self.assertRaises(AttributeError) as caught:
            getattr(vol, "PREVENT")
        self.assertIn("PREVENT", str(caught.exception))
        self.assertTrue(hasattr(vol, "PREVENT_EXTRA"))
        self.assertIs(self.schema_mod.DISCOVER_SCHEMA.extra, vol.PREVENT_EXTRA)

    def test_registration_schema_rejects_unknown_fields(self):
        schema = self.schema_mod.DISCOVER_SCHEMA
        self.assertEqual(_validate_like_core_2026_11(schema, {}), {})
        self.assertEqual(
            _validate_like_core_2026_11(schema, {"entry_id": "abc"}),
            {"entry_id": "abc"},
        )
        for payload in (
            {"method": "FS.Get"},
            {"method": "Config.Get"},
            {"command": "SendGenericCommand"},
            {"host": "192.0.2.1"},
            {"entry_id": "abc", "method": "RPC.List"},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(probatio.Invalid):
                    _validate_like_core_2026_11(schema, payload)

    def test_entry_id_must_be_a_string(self):
        with self.assertRaises(probatio.Invalid):
            _validate_like_core_2026_11(
                self.schema_mod.DISCOVER_SCHEMA, {"entry_id": 1}
            )

    def test_source_does_not_use_the_missing_constant(self):
        init = (ROOT / "custom_components" / "broilking" / "__init__.py").read_text()
        schema = SCHEMA_PATH.read_text()
        self.assertIn("schema=DISCOVER_SCHEMA", init)
        self.assertNotRegex(init, r"vol\.PREVENT(?!_EXTRA)")
        self.assertIn("extra=vol.PREVENT_EXTRA", schema)
        self.assertNotRegex(schema, r"vol\.PREVENT(?!_EXTRA)")

    def test_homeassistant_import_matches_this_runtime(self):
        """On 3.14, require the real 2026.11 package. Older Pythons are not that runtime."""
        if sys.version_info < (3, 14):
            module = sys.modules.get("homeassistant")
            if module is not None and getattr(module, "__file__", None):
                self.fail("Home Assistant 2026.11 is not supported on this Python")
            return
        import homeassistant
        from homeassistant.const import REQUIRED_PYTHON_VER
        from homeassistant.const import __version__ as ha_version

        self.assertGreaterEqual(sys.version_info[:3], REQUIRED_PYTHON_VER)
        self.assertEqual(ha_version, "2026.11.0")
