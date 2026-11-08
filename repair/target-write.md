# Read-only command discovery, integration 0.1.4

The setpoint is still not fixed. Firmware 42.43 has no documented target-write method. This build adds a Home Assistant action that reads `RPC.List` once through the grill already configured for the integration. It does not set a temperature, mode, timer, or power state.

Apply it to the installed 0.1.2 files. Do not apply `repair/broilking-0.1.3.patch` first. Version 0.1.3 was not installed. This patch includes that Home Assistant error change and the diagnostic.

No grill and no Home Assistant instance was contacted while building it.

## What the action does

`broilking.discover_commands` uses the existing config entry. It does not take a hostname or an RPC method name. Extra service fields are rejected.

Each explicit run:

1. Calls `RPC.List` once, with no arguments, with an 8 second timeout, and does not retry.
2. Returns method names that are short identifiers. Names for `FS.*`, `Config.*`, and names that look like credentials are counted in `omitted_count` and are not returned.
3. Calls `RPC.Describe` only when the list actually contains `SendGenericCommand`, which is the one target-control method this integration already documents. The call is `{"name": "SendGenericCommand"}` and nothing else. At most one describe call is made. A format string that mentions a password field is replaced with `[redacted]`.
4. Does not call `FS.Get`, `Config.Get`, `SendGenericCommand` as a write, or any guessed setter.

`RPC.List` and `RPC.Describe` are the read-only introspection methods in the Mongoose OS documentation: https://mongoose-os.com/docs/mongoose-os/howtos/rpc-list.md

`GetAllValues`, Celsius units, timer, mode, power, and the dashboard entities are unchanged. A rejected target write is still a `HomeAssistantError`, so Home Assistant handles it instead of treating it as an unexpected exception.

The action is not called during startup or polling. The first refresh after restart is still `GetAllValues`.

## Response

Success, with no documented control method in the list:

```json
{
  "ok": true,
  "error": "",
  "methods": ["GetAllValues", "Sys.GetInfo"],
  "described": [],
  "omitted_count": 0
}
```

`methods` in a real reply are whatever the firmware listed, after the filter above. `described` stays empty unless `SendGenericCommand` was one of those names.

Missing `RPC.List` handler, one call, no describe:

```json
{
  "ok": false,
  "error": "No handler for RPC.List",
  "methods": [],
  "described": [],
  "omitted_count": 0
}
```

The response does not include the grill address or the device password.

## Check the installed files, then copy

From the Broil King component directory, the installed 0.1.2 hashes must match before the patch. `services.yaml` is not in 0.1.2; the patch creates it.

```
172ef7e24d7117615f7368843eed5c4b1bff8ec19f830cdd443fac2fec7afa4b  api.py
29c545de852511e85bb994209c8165a523a933f837463d05ddf11a36631a9dc8  __init__.py
a6751420b71c4e980d53614db130991e93d6dbed7ea80ab0b4aad85bafcb51c3  climate.py
d3c434a0e14d726f6cbed41c98e66547a3f8a9ba7698b20b2b4174fe80104ac5  coordinator.py
90a280069d24b4f09ab2402d8931a192dbd20526e35d28168db67d1ea0da3eea  manifest.json
aa36759a2f2eb2f58dd7b8b914339462a1903d129001ab519ae7c2b78b04b67a  number.py
```

`climate.py`, `coordinator.py`, and `number.py` stay on those hashes. After `patch -p1 < broilking-0.1.4-from-0.1.2.patch`:

```
aabc62de38f01c30887e1d520035b601b0a2562c2c114bbe1e7e873b3032f266  api.py
5e547cca5ab8ec5be318d52f8f07c4bff1e93e863e5ee5ca4b3e2458f3799d8d  __init__.py
16544543224b36f902d36b05dec1f2e205effdf10587c092df55e9755c173db0  manifest.json
4076550d4200e2377a25bc31e51032e8e855a66b1efa4ff36a10f795d1c3e6ca  services.yaml
```

Manifest version is `0.1.4`. Leave `translations/` and every other file in place.

The archive `repair/broilking-target-repair.tar.gz` contains the full component, this note, the patch, checksums, and the offline tests. From the extracted directory, `sha256sum -c repair/SHA256SUMS`.

## Restart once, then the action

Restart Home Assistant once so it loads the new files. Do not call `number.set_value`, `climate.set_temperature`, `climate.set_hvac_mode`, or `climate.set_preset_mode`.

In Developer Tools → Actions, choose `broilking.discover_commands` and run it with no action data. Read the response. If more than one Broil King config entry is loaded, pass that entry's id and no other field.

The same call over the REST API is `POST /api/services/broilking/discover_commands?return_response` with body `{}`.

## Tests

`python3 -m unittest tests.test_target_write tests.test_discover` — 27 tests, no socket. They cover the 0.1.2 entity-file hashes, `GetAllValues`, the `HomeAssistantError` subclass, one `RPC.List` call when no documented control method is listed, a single `RPC.Describe` only for `SendGenericCommand` when it is listed, omission of `FS.Get` and `Config.Get`, a code-404 `No handler for RPC.List` with no second call, an 8 second timeout with no retry, and a service schema that rejects a method name.
