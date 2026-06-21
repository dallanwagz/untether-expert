# Methodology — app → protocol → Home Assistant

The phased method untether uses to take a vendor-app-controlled device to a local, decoded protocol
(and into Home Assistant). Works for BLE GATT and Bluetooth Classic SPP.

## The one principle: do BOTH static and dynamic

- **Static** (decompile the app, e.g. jadx) = the *spec*: exact frame format, checksum, the full
  command catalog (every messageId), command-gating logic, and the status-struct field list.
- **Dynamic** (drive the app over ADB + capture HCI, or diff the device's live status) = the *truth
  on your hardware*: which feature maps to which packet, which transport actually works.
- **Neither alone is enough.** The decompilable app may target a different model — or may not even be
  the working transport (real case: an Android app spoke Classic SPP and never worked; the device was
  driven over BLE — only iOS worked). Confirm transport + command→feature mapping with dynamic
  evidence before building.

## Phase 0 — Transport determination (do FIRST)

The fork that dictates everything (HA's stack is **BLE-only**):
- **Classic SPP**: `BluetoothSocket` / `createRfcommSocketToServiceRecord` + UUID `0x1101`; a MAC in
  the iOS Bluetooth picker. → needs an **ESP32 bridge** (see `esphome/spp-bridge.yaml`).
- **BLE GATT**: `BluetoothGatt` / `writeCharacteristic`, vendor service `0xFFE0`/`0xFFF0`; a
  CoreBluetooth UUID (not a MAC) in iOS. → native to HA via proxies or a local adapter.
- **Passive advertisement**: the device just broadcasts (no connection) — decode the AD/manufacturer
  data; this is the BTHome/sensor case.

## Phase 1 — Static analysis

`adb shell pm path <pkg>` → `adb pull` → `jadx -d out app.apk`. Find: the **write path** + framing
(the wire frame ≠ the payload — there are start/end markers + a checksum), the **command catalog**
(grep constants + the frame builder; produce name → messageId → bytes + the checksum formula), the
**gating logic** (which commands are dropped in which states), and the **status-struct** field list.
`untether-bt`'s `apk.analyze_tree` automates much of this.

## Phase 2 — Dynamic analysis

Drive the UI by the accessibility hierarchy (not pixel taps): `uiautomator dump` → find node by
text/id → `input tap`. Capture what each press emits, best→worst: **HCI snoop log** (gold standard —
Developer Options → "Enable Bluetooth HCI snoop log", reproduce, pull `btsnoop_hci.log`), **logcat**
(the app's own send/connect logs), **live status diffing**. `untether-bt`'s `AndroidDriver` +
`tap_and_mark` + `correlate()` turn this into "I tapped Power → these bytes went out."

## Phase 3 — Find the channel

- **BLE**: enumerate GATT (`GattClient`/bleak/nRF Connect) → the **write** char (`0xFFF1`/`0xFFE1`)
  and the **notify** char; ignore OTA/DFU services. Watch for rotating RPAs — connect by name/bond.
- **SPP**: SDP discovery → the RFCOMM **server channel** (`untether-bt` `spp_channel(...)`, or
  `bluez.spp_channel(mac)` on Linux, or `Capture.sdp_records()` from a capture). Don't hardcode it.

## Phase 4 — Validate control + decode the status frame

1. Send a known command (framed exactly: markers + checksum) and confirm a **physical effect AND a
   status-stream change**. If nothing: check framing → gating precondition → security mode → channel.
2. Decode the status frame **one variable at a time**, diffing, cross-checked against the device's
   own screen. Watch for: flag bits inflating a value, one byte packing several settings, bit-shifted
   indices, live-value-vs-setting.
3. Document the gaps honestly (command-only, set-only, indistinguishable presets) — protocol limits.

## Phase 5 — Build the integration

- `protocol.py` — **pure** logic (build frames + parse status + command catalog), unit-tested against
  captured golden frames. The reusable, PR-able artifact.
- `coordinator.py` — owns the connection (use `untether-bt`'s `SppConnection` for SPP), capped-backoff
  reconnect + a staleness watchdog.
- `config_flow.py`, entities (a button per command, sensors per field, a generic send-command
  service), `manifest.json`, tests.

## Phase 6 — Reliability & the parser-diff

- **Single bond per device** — keep the vendor app/other hosts off the box HA holds.
- When HA shows wrong values, run the **parser diff** (untether's signature move): decode the raw
  radio bytes yourself (ESP32 `ble-listen` + `untether-bt`) and compare field-by-field to what HA
  parsed (via HA-MCP). Same bytes, different values ⇒ the bug is in HA's/upstream's parser.

## Hard-won principles (TL;DR)

1. Static = the spec; dynamic = the truth. Do both.
2. Verify the transport before building — the decompiled app may not be the working path.
3. The wire frame ≠ the payload (markers + checksum).
4. Decode status by one-variable diffs + screen cross-checks.
5. Document the gaps — they're protocol limits, not your bug.
6. A pure, unit-tested `protocol.py` is your durable, PR-able artifact.
