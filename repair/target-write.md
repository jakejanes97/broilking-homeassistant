# Setup fix 0.1.5 for Home Assistant 2026.11

Installed 0.1.4 fails while loading. `import voluptuous as vol` inside Home Assistant 2026.11 is `probatio._vol_shim` from probatio 0.13.0, and that module has no `PREVENT`. Setup raises `AttributeError` at the discover-service schema and the integration stays unloaded. No RPC call and no target write ran.

0.1.5 builds that schema with `extra=vol.PREVENT_EXTRA`, which is the constant Home Assistant core uses for the same “reject unknown keys” rule. The action is otherwise the 0.1.4 diagnostic. `GetAllValues`, climate, number, coordinator, and `services.yaml` are unchanged.

This environment did not boot Home Assistant 2026.11.0. That package requires Python >=3.14.2, and the tests ran on Python 3.12.3, so `import homeassistant` fails here. The regression did install probatio 0.13.0, the version pinned by Home Assistant 2026.11.0, called `install_as_voluptuous()` so `import voluptuous` is `probatio._vol_shim`, reproduced the missing `PREVENT` attribute, and called `schema(service_data)` the way `ServiceRegistry.async_call` does in core tag 2026.11.0. Unknown fields raise `probatio.Invalid`. The service was not registered on a running Home Assistant process, and the grill was not contacted.

## Check, then copy

Apply only `repair/broilking-0.1.5-from-0.1.4.patch` with `patch -p1` from the component directory that is currently 0.1.4. Do not go back through the 0.1.2 patch.

Hashes that must match before the patch:

```
aabc62de38f01c30887e1d520035b601b0a2562c2c114bbe1e7e873b3032f266  api.py
5e547cca5ab8ec5be318d52f8f07c4bff1e93e863e5ee5ca4b3e2458f3799d8d  __init__.py
16544543224b36f902d36b05dec1f2e205effdf10587c092df55e9755c173db0  manifest.json
4076550d4200e2377a25bc31e51032e8e855a66b1efa4ff36a10f795d1c3e6ca  services.yaml
```

`api.py` and `services.yaml` stay on those hashes. After the patch:

```
b779f3516b4404fc5db33c5e2403d95ac52b1454e34240a0c0eb077ac49d3079  __init__.py
423057142aaf8aaad5e824028a5ce35ef33c697a70e057ba5557828dfda452ae  manifest.json
17d78bf847a47a0728746c4adfa11a2216ec89c11ad68a1d54e3f8120f8d3f98  discover_schema.py
```

Manifest version is `0.1.5`. `discover_schema.py` is new. Leave every other file, including `translations/`, in place.

The archive `repair/broilking-target-repair.tar.gz` contains the full component, this note, the patch, checksums, and the tests. From the extracted directory, `sha256sum -c repair/SHA256SUMS`.

## Restart once, then the same action

Restart Home Assistant once. Do not call `number.set_value`, `climate.set_temperature`, `climate.set_hvac_mode`, or `climate.set_preset_mode`.

In Developer Tools → Actions, run `broilking.discover_commands` with no data. If more than one Broil King config entry is loaded, pass that entry id and no other field. A field such as `method` or `command` is rejected by the schema.

REST: `POST /api/services/broilking/discover_commands?return_response` with body `{}`.

Success shape:

```json
{
  "ok": true,
  "error": "",
  "methods": ["GetAllValues"],
  "described": [],
  "omitted_count": 0
}
```

`described` stays empty unless the list actually contains `SendGenericCommand`. A missing `RPC.List` handler returns `ok: false` and `error: "No handler for RPC.List"` after that one call. The response does not include the grill address or password.

`RPC.List` is one call, no arguments, 8 second timeout, no retry. `RPC.Describe` runs only for the documented name `SendGenericCommand` when that name is in the list. `FS.Get` and `Config.Get` are not called.

## Tests

`python3 -m unittest tests.test_ha202611_schema tests.test_target_write tests.test_discover` — 32 tests. `test_ha202611_schema` requires `probatio==0.13.0` and does not mock the schema engine. The other tests remain offline and do not open a socket.
