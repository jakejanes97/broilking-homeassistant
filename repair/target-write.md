# Broil King target-write repair (fork, integration 0.1.2)

Firmware 42.43 still has to echo a new setpoint before that setpoint can be treated as stored. This patch does not establish that. It keeps the installed `GetAllValues` read path and the published `SendGenericCommand` frames, and it fails the Home Assistant call when the frame is rejected or the next read still shows the previous Fahrenheit value.

No grill, Home Assistant, or network device was contacted. Tests are offline mocks. Hardware acceptance of these frames on firmware 42.43 is unverified.

## What was happening

Probe and grill targets are stored by the grill in Fahrenheit. Home Assistant entities for those targets stay in Celsius. A stored 200 F is 93.3 C (`round((200 - 32) * 5 / 9, 1)`). Showing 93.3 C in Fahrenheit to one decimal is 199.9 F. That 199.9 figure is the display round-trip of 200 F.

The probe number published the typed value for `BOARD_ACK_DELAY` (2 seconds), then refreshed. A 175 F request is a legal probe target (about 79.4 C, inside 30–175 C) and the frame for probe 1 is:

```
225 225 000 010 000 048 006 000 175
```

That is the action-2 frame with value 175, which is distinct from the 200 F frame (`... 006 000 200`). After the refresh the grill's `probe01SetTemp` was still 200 F, so the entity returned to the 199.9 F display. The grill target path had the same unanswered write: `async_set_temperature` ignored the RPC result, and `grillSetTemp` stayed at the previous value.

## Protocol

The public integration (bbirke/broilking-homeassistant, through `ef56c51`) documents `SendGenericCommand` for the Broil_King pellet firmware, with `error.code == 0` meaning success, and the action table already in `api.py`. This installed tree uses that write path. Its working read path is `GetAllValues` with no arguments. That read function is unchanged.

A local draft suggested `SendCustomCommand` ids 7, 8, and 9 (and 0, 6, and 17) from an iQue app screen. That draft was never deployed. Public repository search did not turn up a Broil King command map for those ids, and this work did not decompile the app or call a private app API. Those ids are not in the patch. Sending them would be an untested command.

Until a public, checkable description of the write this firmware actually applies is available, the bytes on the wire stay the published generic frames. A grill that ignores those frames will keep the previous target. That is the blocker. The setpoint write is not fixed.

## What the patch changes

Files, relative to the installed component: `api.py`, `number.py`, `climate.py`, `coordinator.py` (docstring only), `manifest.json` (version 0.1.2).

- Target writes raise immediately when `SendGenericCommand` returns a non-zero code. The exception text omits any `pass` field.
- After a zero code, the client waits 2 seconds, reads `GetAllValues`, and raises when the echoed field is not the integer Fahrenheit value that was sent (`grillSetTemp`, `probe_1_Set_Temp`, `probe_2_Set_Temp`).
- The probe number no longer publishes the typed value during that wait. A following poll still shows a target that was set on the grill itself.
- Timer pending behavior, mode and power (`async_set_mode`, including Off as mode 0 on action 5), command bytes, Celsius native units, and temperature ranges are unchanged.
- `async_set_mode` and `async_set_timer` still return the `(ok, payload)` tuple and do not raise on a non-zero code.

A rejected frame no longer looks successful. An acknowledged frame that leaves the old setpoint also fails the service call. Neither result means the grill stored 175 F.

## Box copy

Download `repair/broilking-target-repair.tar.gz` from this branch. It contains:

- `custom_components/broilking/` — full component with the installed read path and this patch
- `repair/broilking-target-write.patch` — minimal diff against the installed files (`patch -p1` from the component directory)
- `repair/SHA256SUMS`
- `repair/target-write.md` — this note
- `tests/test_target_write.py`

Before copying anything, hash the five files already on the Home Assistant host. Apply the patch only when they match the installed hashes below. Copying just those five patched files is the same change. Leave every other file in the component directory in place, including `translations/`.

Installed hashes (pre-patch):

```
4f963140c2dcd8583643d889f6721ee5db77508320e52f1315b132d111c83f9c  api.py
b6a398ce48d3571c0e7e7fe6564d77cdff9b7dab125f6c603abc0cf12def307b  climate.py
90326cd431153c594dd600af0343013169b23819ec87ff3e1cc12124be1cb3b0  coordinator.py
814b52b09c56ebee31e301503b8f15014a9efc934f11576956d24d54be411235  manifest.json
be4121520a2d61b81a93d248161323e1fff64fd43706b347cbc54d5c43b6c90d  number.py
```

Patched hashes:

```
172ef7e24d7117615f7368843eed5c4b1bff8ec19f830cdd443fac2fec7afa4b  api.py
a6751420b71c4e980d53614db130991e93d6dbed7ea80ab0b4aad85bafcb51c3  climate.py
d3c434a0e14d726f6cbed41c98e66547a3f8a9ba7698b20b2b4174fe80104ac5  coordinator.py
90a280069d24b4f09ab2402d8931a192dbd20526e35d28168db67d1ea0da3eea  manifest.json
aa36759a2f2eb2f58dd7b8b914339462a1903d129001ab519ae7c2b78b04b67a  number.py
```

From the component directory, after the installed hashes match:

```
patch -p1 < broilking-target-write.patch
```

Restart Home Assistant after the files are in place. Manifest version becomes 0.1.2. Do not send a target write as a test until the intended temperature is chosen. If the grill does not echo that temperature, the service call fails and the previous setpoint remains.

`number.broil_king_grill_target_setpoint_f` is outside this component. This patch does not change it. A call that reaches `climate.set_temperature` uses the same fail-closed grill write.

## Tests

`python3 -m unittest tests.test_target_write` (16 tests, aiohttp only, no Home Assistant and no socket to a grill). They cover Fahrenheit/Celsius conversion, the 200 F → 93.3 C → 199.9 F display round-trip, the frozen command table (including power, mode, timer, and brightness), `GetAllValues` mapping, a rejected frame, a zero code whose read-back is still the old setpoint, a matching echo, transport failure, and mode/timer/power staying on the old return path.
