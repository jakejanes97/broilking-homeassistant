# 0.1.6 after an in-memory Home Assistant 2026.11 run

The device is still on 0.1.4, which fails setup because `vol.PREVENT` does not exist. 0.1.6 is the tree that was loaded into Home Assistant 2026.11.0. It includes the `PREVENT_EXTRA` schema and a reply timeout that actually ends a silent websocket. The setpoint is still not fixed. `climate.py`, `number.py`, `coordinator.py`, and `services.yaml` are unchanged.

## What was installed for the check

```text
uv 0.13.7
uv python install 3.14
uv venv /tmp/ha202611 --python 3.14
uv pip install --python /tmp/ha202611/bin/python 'homeassistant==2026.11.0'
```

That environment resolved to Python 3.14.10, homeassistant 2026.11.0, probatio 0.13.0, and aiohttp 3.14.5. Importing `homeassistant` aliases `voluptuous` to `probatio._vol_shim`. That module has `PREVENT_EXTRA` and does not have `PREVENT`.

## What the in-memory instance did

`/tmp/ha202611/bin/python -m unittest tests.test_ha202611_runtime -v` loaded this component through Home Assistant's config-entry setup. Registries, the frame helper, and the network component were the real ones. The grill was a stand-in bound to `127.0.0.1` on a random port. Nothing connected to an external Home Assistant or to the grill.

Result: 1 test, OK, about 10 seconds.

- Config entry state was `loaded`.
- `broilking.discover_commands` was registered with `supports_response` `optional`.
- `{}` with `return_response=True` returned the discovery dict. `FS.Get` and `Config.Get` were omitted. The host and password were not in the response. The stand-in saw one `RPC.List` and no `SendGenericCommand`, `FS.Get`, or `Config.Get`.
- `{"method": "FS.Get"}` raised `probatio.Invalid` inside `ServiceRegistry.async_call` before another list call.
- The same action with `return_response=False` returned `None`.
- When the stand-in then listed `SendGenericCommand`, the next call described only that name.
- A stand-in that accepted `RPC.List` and then stayed silent made the action return `ok: false` with `RPC RPC.List timed out` in under 12 seconds, and that list was not retried.
- Unloading the config entry removed the service.
- Setup still reads with `GetAllValues`. The stand-in answered that read.

aiohttp 3.14 does not treat a float `timeout` as a reply deadline, and `ClientWSTimeout.ws_receive` also did not stop the silent peer in a direct check. 0.1.6 therefore wraps the read in `asyncio.wait_for` for the same 8 second discovery limit and the existing 10 second read limit. A prompt reply is unchanged.

Home Assistant logged that `BroilKingGrill` still uses `_attr_temperature_unit`, `current_temperature`, and `target_temperature`, which it says stop working in 2027.11. Those entities were still created. This build does not change them.

`python3 -m unittest tests.test_ha202611_schema tests.test_target_write tests.test_discover` on Python 3.12.3 passed 32 tests. That interpreter cannot import homeassistant 2026.11.0. The runtime test above is the one that used the real service registry.

## Install on the current 0.1.4 files

Apply only `repair/broilking-0.1.6-from-0.1.4.patch` with `patch -p1` from the component directory. Do not apply the 0.1.5 patch first. `backup_pre_0.1.4` remains the 0.1.2 rollback.

Hashes that must match before the patch:

```
aabc62de38f01c30887e1d520035b601b0a2562c2c114bbe1e7e873b3032f266  api.py
5e547cca5ab8ec5be318d52f8f07c4bff1e93e863e5ee5ca4b3e2458f3799d8d  __init__.py
16544543224b36f902d36b05dec1f2e205effdf10587c092df55e9755c173db0  manifest.json
4076550d4200e2377a25bc31e51032e8e855a66b1efa4ff36a10f795d1c3e6ca  services.yaml
```

`services.yaml`, `climate.py`, `number.py`, and `coordinator.py` stay as they are. After the patch:

```
77da1f2589b05ce0cdafe7ddb50880b71f28935af17b87b4f9d6e903fc2a812f  api.py
b779f3516b4404fc5db33c5e2403d95ac52b1454e34240a0c0eb077ac49d3079  __init__.py
e739172ac2e9a062ce56b4737e5e37555d57fa06757471c83465168111fef288  manifest.json
17d78bf847a47a0728746c4adfa11a2216ec89c11ad68a1d54e3f8120f8d3f98  discover_schema.py
```

Manifest version is `0.1.6`. The archive is `repair/broilking-target-repair.tar.gz`. From the extracted directory, `sha256sum -c repair/SHA256SUMS`.

Restart Home Assistant once. Then run `broilking.discover_commands` with no data. Do not call `number.set_value`, `climate.set_temperature`, `climate.set_hvac_mode`, or `climate.set_preset_mode`.

```json
{
  "ok": true,
  "error": "",
  "methods": ["GetAllValues"],
  "described": [],
  "omitted_count": 0
}
```

`described` stays empty unless the list contains `SendGenericCommand`. A missing `RPC.List` returns `ok: false` and `error: "No handler for RPC.List"` after one call.
