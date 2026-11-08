# Broil King target writes, integration 0.1.3

The setpoint is still not fixed. Firmware 42.43 answered the one authorized probe-1 write with Mongoose OS’s “no such method” error, and the stored targets did not change. Version 0.1.3 only makes that device error a Home Assistant service error. It does not add a write method.

No further grill call, scan, or restart was made from this change.

## What the authorized test showed

One `SendGenericCommand` for probe 1 at 175 F, with the grill off, returned:

```
probe 1 target 175 F rejected by SendGenericCommand: No handler for SendGenericCommand
```

Home Assistant reported HTTP 500 because `BroilKingError` was a plain `Exception`, so the service call was unhandled. There was no retry. The probe target stayed 200 F (displayed as 199.9 F). The grill target stayed 225 F. `GetAllValues` was still the read path, and the five installed 0.1.2 hashes matched before that test.

## Why that reply rules out this frame

Public Mongoose OS RPC (`mongoose-os-libs/rpc-common`, `src/mg_rpc.c`) sends that text when no registered handler matches the method name:

```c
mg_rpc_send_errorf(ri, 404, "No handler for %.*s", (int) frame->method.len, frame->method.p);
```

The dispatcher does this before a handler runs, with JSON-RPC error code 404. The Fahrenheit bytes inside the command string are not interpreted. A different temperature in the same `SendGenericCommand` method cannot succeed on this firmware.

The public Broil King integration documents `SendGenericCommand` for the `Broil_King` pellet firmware that serves `GetCurrentTemperatures` (Regal Pellet 500, around firmware 042.42). It does not document a write method for a firmware whose read path is `GetAllValues` and whose RPC table has no `SendGenericCommand` handler. Public search still shows no command map for `SendCustomCommand` ids. Those ids are not in this tree.

No supported target-write protocol for firmware 42.43 was found. That is the blocker.

## Safe read-only diagnostic, not run here

Mongoose OS documents two introspection calls. They are read-only in the project docs and were not sent to the grill:

- `RPC.List` with no arguments returns the registered method names. Docs: https://mongoose-os.com/docs/mongoose-os/howtos/rpc-list.md and https://mongoose-os.com/docs/mongoose-os/userguide/rpc.md
- `RPC.Describe` with `{"name": "<one name from that list>"}` returns that method’s argument format.

`RPC.List` has to be enabled on the device (`mg_rpc_add_list_handler`). If this firmware did not enable it, the same “No handler for RPC.List” error comes back and nothing is written.

Do not call `FS.Get` or `Config.Get` while looking. The upstream integration documents that `FS.Get` can return the Wi-Fi password and the cloud private key.

A later `RPC.List` result would be evidence of which write method exists. Until a documented method is known, the integration keeps sending `SendGenericCommand` and reports the rejection.

## What 0.1.3 changes

`BroilKingError` now subclasses `homeassistant.exceptions.HomeAssistantError`. Home Assistant’s service handler returns that message instead of treating it as an unexpected exception. Websocket calls use `ERR_HOME_ASSISTANT_ERROR`. The REST `/api/services` handler in current Home Assistant core still uses HTTP 500 for `HomeAssistantError`, and the JSON body is the firmware message. That 500 is the documented status for an expected device failure.

`ServiceValidationError` is not used. Developer docs reserve it for incorrect service use, and it is what turns the REST call into HTTP 400. 175 F is inside the probe range (30–175 C). The firmware rejected the method name.

`GetAllValues`, the command bytes, the 2 second echo check, Celsius units, timer, mode, and power are the 0.1.2 code. `number.py`, `climate.py`, and `coordinator.py` are unchanged.

## Box copy

Apply `repair/broilking-0.1.3.patch` on the installed 0.1.2 component (`patch -p1` from that directory). It touches `api.py` and `manifest.json` only. Do not replay `repair/broilking-target-write.patch`; that one is the earlier 0.1.2 delta and will not match files that are already 0.1.2.

0.1.2 baseline hashes this patch expects:

```
172ef7e24d7117615f7368843eed5c4b1bff8ec19f830cdd443fac2fec7afa4b  api.py
a6751420b71c4e980d53614db130991e93d6dbed7ea80ab0b4aad85bafcb51c3  climate.py
d3c434a0e14d726f6cbed41c98e66547a3f8a9ba7698b20b2b4174fe80104ac5  coordinator.py
90a280069d24b4f09ab2402d8931a192dbd20526e35d28168db67d1ea0da3eea  manifest.json
aa36759a2f2eb2f58dd7b8b914339462a1903d129001ab519ae7c2b78b04b67a  number.py
```

After the patch, `climate.py`, `coordinator.py`, and `number.py` keep those hashes. `api.py` and `manifest.json` become:

```
40811de53ab5953828279052dfb7c08638e838ea27f20cdb1f52d131c890f982  api.py
e616305ad864d81e251f56508ab238abb334fd23c81a4d7f8ee80437f2fedd79  manifest.json
```

The archive `repair/broilking-target-repair.tar.gz` contains the full 0.1.3 component, both patches, this note, the checksum list, and `tests/test_target_write.py`. Check it with `sha256sum -c repair/SHA256SUMS` from the extracted directory.

Restart Home Assistant only when you choose to. This change does not request a restart or another target write.

## Tests

`python3 -m unittest tests.test_target_write` — 18 tests, aiohttp only, no socket. The new case feeds a code-404 reply whose message is the plain string `No handler for SendGenericCommand`, checks the exception text, checks that it is a `HomeAssistantError` and not a `ServiceValidationError`, and checks that the client sent one `SendGenericCommand` frame and did not sleep.
