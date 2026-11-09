# 0.1.7 discovery bounds and safe errors

Installed 0.1.4 still fails setup on `vol.PREVENT`. 0.1.6 fixed that and the silent-after-handshake case, and QA held it: a TCP accept with no HTTP stayed open past 15 seconds, a refused connection returned the address, a lowercase `sendgenericcommand` was described, and a token planted in a device error was copied into the service result. 0.1.7 is the tree checked against Home Assistant 2026.11.0. The setpoint is still not fixed.

## What changed

Discovery no longer uses aiohttp for `RPC.List` or `RPC.Describe`. aiohttp 3.14 does not end a websocket handshake when TCP is accepted and no HTTP arrives, and cancelling `ws_connect` did not return in a direct check. Each discovery call now opens its own asyncio socket. The 8 second deadline covers the TCP connect, the HTTP upgrade, the reply, and aborting the socket. There is no retry.

The service result uses fixed words: `timeout`, `connection_failed`, `rejected`, `no_reply`, `unusable_reply`, or the exact sentence `No handler for RPC.List` when the device text is that sentence and nothing more. Socket text and other device text are not copied into the result or this component's log.

`RPC.Describe` runs only when the list contains the exact spelling `SendGenericCommand`. `sendgenericcommand` is left in the method list and is not described.

`GetAllValues` still uses the existing aiohttp read, the same key mapping, and the same 10 second reply wait. Climate, number, coordinator, and `services.yaml` are unchanged. No target, power, mode, or timer write was added.

## Checks

Python 3.12.3, without the Home Assistant package:

`python3 -m unittest tests.test_discover_transport tests.test_discover tests.test_target_write tests.test_ha202611_schema`

35 tests, OK, 77.085s. This includes the localhost handshake stall, refused port, planted secret, mute peer, wrong-id flood, and cancellation.

Python 3.14.10, homeassistant 2026.11.0, probatio 0.13.0, aiohttp 3.14.5:

`/tmp/ha202611/bin/python -m unittest tests.test_discover_transport tests.test_discover tests.test_target_write tests.test_ha202611_schema tests.test_ha202611_runtime`

36 tests, OK, 87.269s. The runtime test loads the component through `ConfigEntries.async_add` on an in-memory Home Assistant. The stand-in is `127.0.0.1` only. The entry reached `loaded`, empty data returned the discovery dict, an unknown `method` raised `probatio.Invalid`, `return_response=False` returned `None`, describe ran only for exact `SendGenericCommand`, a silent reply timed out without a retry, and unload removed the service.

Commands used to build that environment:

```text
uv python install 3.14
uv venv /tmp/ha202611 --python 3.14
uv pip install --python /tmp/ha202611/bin/python 'homeassistant==2026.11.0'
```

No external Home Assistant and no grill were contacted. Nothing was deployed.

Home Assistant still logs that the unchanged climate entity uses `_attr_temperature_unit`, `current_temperature`, and `target_temperature`, which core says stop working in 2027.11. The entities were created.

`GetAllValues` still goes through the shared Home Assistant aiohttp session. A temperature poll against a peer that accepts TCP and never sends HTTP can still sit on that session's connect timeout. Discovery does not use that path.

## Install on the current 0.1.4 files

Apply only `repair/broilking-0.1.7-from-0.1.4.patch` with `patch -p1` from the component directory. Do not apply the 0.1.5 or 0.1.6 patch first. `backup_pre_0.1.4` is the 0.1.2 rollback.

Patch SHA-256: `a70001cc2b010423155cab7152ae802c6946d10811110284e7bfc1dfc882ddc1`

Hashes that must match before the patch:

```
aabc62de38f01c30887e1d520035b601b0a2562c2c114bbe1e7e873b3032f266  api.py
5e547cca5ab8ec5be318d52f8f07c4bff1e93e863e5ee5ca4b3e2458f3799d8d  __init__.py
16544543224b36f902d36b05dec1f2e205effdf10587c092df55e9755c173db0  manifest.json
4076550d4200e2377a25bc31e51032e8e855a66b1efa4ff36a10f795d1c3e6ca  services.yaml
```

`services.yaml`, `climate.py`, `number.py`, and `coordinator.py` stay as they are. After the patch:

```
73fa0f7b79b59d8c70107417408867ea4568225b1573bb6d5ccfff1904796d0b  api.py
b779f3516b4404fc5db33c5e2403d95ac52b1454e34240a0c0eb077ac49d3079  __init__.py
fcad92c02410c8ab877ce63b4355c1eaaf810a9148aa0112c627f8788dc17d25  manifest.json
17d78bf847a47a0728746c4adfa11a2216ec89c11ad68a1d54e3f8120f8d3f98  discover_schema.py
```

Manifest version is `0.1.7`. The archive is `repair/broilking-target-repair.tar.gz`. From the extracted directory, `sha256sum -c repair/SHA256SUMS`.

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

`described` stays empty unless the list contains the exact name `SendGenericCommand`. A missing list method returns `error` `No handler for RPC.List`. A refused connection returns `connection_failed`. A silent or half-open connection returns `timeout`.
