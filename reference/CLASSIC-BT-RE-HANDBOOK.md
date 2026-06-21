# The Classic Bluetooth RE Handbook

A reverse-engineer's map of **Bluetooth Classic (BR/EDR)** — the RFCOMM/SPP stack that the modern
BLE-only toolchain (bleak, Home Assistant, ESPHome) can't see, let alone speak. This is the spec
knowledge `untether` is built on, distilled to what you actually need at the wire to take a
Classic serial device from "vendor app only" to "I control it from my own code."

Every section maps a protocol layer to **what it looks like on the wire**, **where you capture it**,
and **which `untether-bt` primitive decodes it**. Hex values and formulas are from the Bluetooth Core
Specification ("RFCOMM with TS 07.10", "Logical Link Control and Adaptation Protocol", "Service
Discovery Protocol") and ETSI/3GPP **TS 07.10** (the GSM multiplexer RFCOMM is built on). See
[References](#references).

---

## 0 · Why Classic needs its own playbook

Host BLE stacks closed the door on Classic: `bleak` lists RFCOMM as **wontfix**, and the entire
`habluetooth → HA bluetooth → ESPHome bluetooth_proxy` chain is BLE-only by design. So a Classic
serial device (a label printer, a massage chair, a Divoom pixel display, an industrial meter) has
**no first-class path to a host or to Home Assistant**. Two consequences shape everything below:

- **You capture at HCI, not over the air.** You sidestep frequency hopping and link encryption by
  tapping the host↔controller boundary, where both BLE and Classic traffic is in the clear.
- **You bridge to reach it.** A host that can't open an RFCOMM socket needs a proxy. `untether`'s
  answer is the ESP32 `untether_spp` firmware: RFCOMM on one side, a TCP byte stream on the other.

---

## 1 · The Classic stack (layer map)

```
  Application  (the vendor protocol: your framed command bytes)
      │
  SPP          Serial Port Profile — "this is a serial cable" (SDP service class 0x1101)
      │
  RFCOMM       serial-port emulation over a multiplexer (ETSI TS 07.10 subset)
      │
  L2CAP        channels (CIDs) + segmentation; RFCOMM rides PSM 0x0003
      │
  HCI          host↔controller boundary  ← YOUR CAPTURE POINT (btsnoop / btsnooz)
      │
  LMP / Link Manager   pairing, link keys, role switch  (below HCI; not in snoop)
      │
  Baseband / Radio     2.4 GHz, 79×1 MHz channels, 1600 hops/s, piconet
```

The discovery side (SDP) and the security side (pairing/SSP) hang off this spine and are covered in
their own sections. Class of Device, carried in the inquiry response, tells you *what a device is*
before you connect (§8).

---

## 2 · The capture point: HCI, btsnoop, btsnooz

Everything observable lives in the **HCI** stream: command/event packets and **ACL** data (which
carries L2CAP, which carries RFCOMM). Android writes this to `btsnoop_hci.log`; `btmon -w` and
Wireshark read the same format.

- **btsnoop** — 16-byte header (`b"btsnoop\0"`, version 1, datalink) then records. The infamous
  gotcha: timestamps are **signed µs since midnight Jan 1, year 0** — subtract `0x00DCDDB30F2F8000`
  for Unix time. Datalink `1002` (H4) prefixes each record with the 1-byte packet-type indicator.
- **btsnooz** — what Android *bug reports* embed: a `<bQ` header (version, last-timestamp-ms) then a
  zlib stream of delta-encoded records. Not the same format; you must inflate it first.

```python
from untether_bt import load_btsnoop, hci_packets, l2cap_payloads
snoop = load_btsnoop(open("capture", "rb").read())   # auto-detects btsnoop vs btsnooz
for lp in l2cap_payloads(hci_packets(snoop)):
    print(hex(lp.cid), lp.sent, lp.payload.hex())     # RFCOMM rides a dynamic CID (≥0x0040)
```

| Need | Primitive |
|------|-----------|
| Parse either capture format | `load_btsnoop`, `is_btsnooz`, `decompress_btsnooz` |
| Classify HCI / pull ACL → L2CAP | `hci_packets`, `l2cap_payloads` |
| Decode GATT (the BLE cousin) | `att_pdus` |
| Capture + UI-action correlation | `Capture`, `correlate`, `Recorder` |

---

## 3 · L2CAP — channels under everything

L2CAP multiplexes the ACL link into **channels** identified by a **CID**. Fixed CIDs include the
signalling channel `0x0001` and ATT `0x0004` (BLE). Connection-oriented Classic channels (RFCOMM
among them) get **dynamically assigned CIDs ≥ 0x0040**, negotiated on the signalling channel by
**PSM** (Protocol/Service Multiplexer). The PSM that matters here:

> **RFCOMM = PSM `0x0003`.** (SDP itself = `0x0001`, BNEP = `0x000F`.)

So in a capture you won't see "RFCOMM" labelled — you see ACL → L2CAP on some CID like `0x0040`,
and the bytes on that CID are RFCOMM frames. `l2cap_payloads()` hands you `(cid, sent, payload)`;
the RFCOMM decode in §4 is what those payloads contain.

For finicky modules: keep **ERTM (Enhanced Retransmission Mode) off** and use basic L2CAP — many
cheap RFCOMM endpoints choke on ERTM, which is why a plain socket often connects where a "proper"
one hangs.

---

## 4 · RFCOMM (TS 07.10 subset) — the heart of it

RFCOMM emulates up to **60 serial ports** between two devices over a single TS 07.10 *multiplexer*
running on one L2CAP channel. Each direction of each emulated port is a **DLC** (Data Link
Connection) identified by a **DLCI**.

### 4.1 Frame structure (basic option)

TS 07.10's opening/closing flags are dropped in RFCOMM — L2CAP already frames the packet. What's on
the wire per frame:

```
 ┌─────────┬─────────┬──────────────────┬───────────────┬───────┐
 │ Address │ Control │ Length Indicator │  Information   │  FCS  │
 │ 1 octet │ 1 octet │   1 or 2 octets  │ (0..N octets) │ 1 oct │
 └─────────┴─────────┴──────────────────┴───────────────┴───────┘
```

- **Length Indicator**: 1 octet if it fits in 7 bits (EA=1, `len<<1 | 1`); else 2 octets (EA=0).
- **FCS**: CRC-8, polynomial `x⁸ + x² + x + 1`. Coverage differs by frame type:
  - **SABM, DISC, UA, DM** → FCS over **Address + Control + Length**.
  - **UIH** (the data/most frames) → FCS over **Address + Control only** (not the payload).

### 4.2 The Address field — where the DLCI hides

| Bit | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|-----|---|---|---|---|---|---|---|---|
| **TS 07.10** | EA | C/R | DLCI (6 bits) →→→→→→ |||||
| **RFCOMM** | EA | C/R | D | Server Channel (5 bits) →→→→ ||||

- **EA** = Extension bit (1 = last octet of the address).
- **C/R** = Command/Response.
- **D** = Direction bit. RFCOMM splits the 6-bit DLCI into a 1-bit direction + a 5-bit **server
  channel** (1–30, advertised in SDP). This is how both ends can host servers on one session
  without DLCI collisions.

> **DLCI math:** `DLCI = (server_channel << 1) | direction_bit`.
> Usable DLCI range is **2…61**. **DLCI 0** is the multiplexer **control channel**; DLCI 1 is
> unusable (server-channel 0 is reserved); 62–63 are reserved. The convention: the session
> *initiator* uses direction bit = 1.

### 4.3 Frame types (control field)

RFCOMM uses only the TS 07.10 frames below (UI and error-recovery frames are *not* supported). The
control-field octet is the base type below **OR-ed with the P/F bit `0x10`** when set — so a SABM
command with P/F goes out as `0x3F`. Identify a frame by masking: `type = control & 0xEF`.

| Frame | Role | Base control (P/F masked) |
|-------|------|---------------------------|
| **SABM** | command — open a DLC | `0x2F` |
| **UA**   | response — DLC accepted | `0x63` |
| **DM**   | response — DLC refused/closed | `0x0F` |
| **DISC** | command — close a DLC | `0x43` |
| **UIH**  | command & response — carries data + MUX control | `0xEF` |

The connection dance: initiator sends **SABM on DLCI 0** → peer replies **UA** (multiplexer up).
Then **SABM on the data DLCI** → **UA** (port open). Data flows in **UIH** frames. A refusal is a
**DM**. Teardown is **DISC** → **UA**; closing DLCI 0 (or the L2CAP channel) drops the session.

### 4.4 Multiplexer control (UIH frames on DLCI 0)

Out-of-band control rides UIH frames on DLCI 0. The commands RFCOMM supports:

- **PN** (Parameter Negotiation) — frame size (N1, default 127, range 23–32767), and the
  **convergence layer** field that turns on **credit-based flow control**.
- **MSC** (Modem Status Command) — conveys the RS-232 control signals (RTS/CTS/DTR/DSR/RI/DCD) and
  the break signal. RFCOMM uses convergence layer type 1, so MSC is *the* way flow/line state moves.
- **RPN** (Remote Port Negotiation), **RLS** (Remote Line Status), **FCon/FCoff** (aggregate flow),
  **Test**, and **NSC** (Non-Supported Command response).

### 4.5 Credit-based flow control

RFCOMM's addition to TS 07.10. Once negotiated (via PN's convergence-layer = credit-based), a UIH
frame on a data DLC with the **P/F bit set** carries a **one-octet credit count** inserted right
after the length indicator, before the information field. Credits are the number of frames the
sender may still transmit; running out stalls the DLC until the peer grants more. If you see data
abruptly stop with the link still up, suspect credit exhaustion, not a hang.

### 4.6 System parameters

| Parameter | Value |
|-----------|-------|
| Max frame size **N1** | default **127** (negotiable 23–32767) |
| Ack timer **T1** (SABM/DISC P/F response) | **60 s** |
| MUX control response **T2** (DLCI 0) | **60 s** |

There is **at most one RFCOMM session per device pair** (keyed by the two BD_ADDRs). New DLCs go on
the existing session. This is the protocol reason a Classic device that accepts the app *and* your
bridge at once misbehaves: contention over the single session/bond, not a flaky radio.

```python
from untether_bt import parse_rfcomm, iter_rfcomm, Capture
f = parse_rfcomm(bytes.fromhex("033f011c"))     # the canonical SABM on DLCI 0
f.frame_type, f.dlci, f.fcs_ok                   # ('SABM', 0, True)
for fr in Capture.from_btsnoop(cap).rfcomm_frames():   # decode RFCOMM straight from a capture
    print(fr.frame_type, fr.server_channel, fr.information.hex())
```

In live use the bytes usually arrive already de-multiplexed through the `untether_spp` bridge (it
terminates RFCOMM on the ESP32 and gives you the clean serial stream), so `rfcomm.py` is what you
reach for when reading **raw** RFCOMM in a btsnoop capture, building a bridge, or debugging a DLC
that won't open. The FCS table and check match the canonical TS 07.10 / Linux-kernel implementation.

---

## 5 · SDP — finding the RFCOMM channel (don't hardcode it)

The server-channel number (§4.2) is **assigned at runtime** and discovered over **SDP** (PSM
`0x0001`). You query the device's service record for the SPP service class and read the **Protocol
Descriptor List**: an L2CAP element, then an **RFCOMM** element (protocol UUID `0x0003`) whose
parameter is the **server channel**. That's the number you SABM against.

```python
from untether_bt import parse_ssa_response, spp_channel, Capture
spp_channel(parse_ssa_response(sdp_response_bytes))     # channel from a live SDP query
spp_channel(Capture.from_btsnoop(cap).sdp_records())    # …or recovered straight from a capture
```

On Linux you can browse live: `untether_bt.bluez.spp_channel(mac)` (via pybluez, `[bluez]` extra).
The key namespaces — service classes vs protocol identifiers — are distinct registries; resolve
them with `sdp_service_name(0x1101)` ("Serial Port (SPP)") and `protocol_name(0x0003)` ("RFCOMM").

| Need | Primitive |
|------|-----------|
| Parse SDP data elements / SSA response | `parse_data_element`, `parse_ssa_response` |
| Recover the RFCOMM channel | `spp_channel`, `rfcomm_channel`, `find_rfcomm_channels` |
| SDP records from a capture | `Capture.sdp_records()` |
| Live SDP browse (Linux) | `untether_bt.bluez.browse_services`, `…spp_channel` |

---

## 6 · SPP & security

**SPP** (Serial Port Profile) is the thin profile on top: service class `0x1101`, a Protocol
Descriptor List pointing at the RFCOMM channel, and a "this is a virtual serial cable" contract.
Nothing more — the *vendor* protocol (your framed command bytes) lives above it and is what you
reverse with the capture/correlate pipeline.

Security realities for cheap modules:

- Most use **Secure Simple Pairing "Just Works"** (no PIN), or legacy PIN `0000`/`1234`. Match what
  the phone did — check `dumpsys bluetooth_manager` for a bond, or its absence.
- **One bond per device.** While the vendor app holds the device, your bridge can't; disable the
  phone's BT (`adb shell svc bluetooth disable`) or unpair it first.
- Keep **L2CAP basic mode** (ERTM off) for flaky endpoints (§3).

---

## 7 · Class of Device — knowing what it is before you connect

The inquiry response carries a 24-bit **Class of Device**: a **major service class** bitfield (bits
13–23), a **major device class** (bits 8–12), and a **minor device class** (bits 2–7). Decode it to
triage a scan:

```python
from untether_bt import parse_class_of_device
parse_class_of_device(0x5A020C)
# {'major_service_classes': ['Networking …', 'Capturing …', 'Object Transfer …', 'Telephony …'],
#  'major_device_class': 'Phone …', 'minor_device_class': 'Smartphone', 'major': 2, 'minor': 3}
```

A device advertising the **Audio** major service with an A2DP service class is a speaker, not your
serial target — useful when one physical gadget exposes several endpoints (the classic "that MAC is
the A2DP sink, not the control channel" trap).

---

## 8 · The Assigned-Numbers resolver

Every numeric identifier above resolves through one module — the **full** SIG registries, bundled,
not a hand-picked subset (regenerate with `python/tools/gen_assigned_numbers.py`):

```python
from untether_bt import (company_name, gatt_name, sdp_service_name, protocol_name,
                         ad_type_name, appearance_name, parse_class_of_device, describe_uuid)

company_name(0x004C)         # 'Apple, Inc.'      (BLE manufacturer-data company ID)
protocol_name(0x0003)        # 'RFCOMM'           (SDP protocol identifier)
sdp_service_name(0x1101)     # 'Serial Port (SPP)'(SDP service class)
ad_type_name(0xFF)           # 'Manufacturer Specific Data'  (advertising/EIR type)
describe_uuid(0x180F)        # '0x180F (Battery Service)'    (GATT, the BLE side)
```

Namespaces are kept apart on purpose: `0x1101` is an SDP service class, not a GATT service; `0x0003`
is a protocol identifier, not either.

---

## 9 · The on-ramp: `untether_spp` ESP32 bridge

To actually *talk* to the device, terminate RFCOMM somewhere a host can reach. The companion
firmware (original ESP32 / WROOM-32 — S3/C3/C6 are BLE-only and **cannot** do Classic) RFCOMM-
connects to the device and re-exposes the byte stream as a TCP server. From there it's just a
socket:

```python
from untether_bt import SppBridge, SppConnection
with SppBridge("192.168.1.50", 8888) as dev:    # one-shot scripts
    dev.send(b"...")
# or a self-healing persistent link for a daemon / HA coordinator:
conn = SppConnection("192.168.1.50", 8888, on_chunk=handle, on_connect=send_handshake)
await conn.start()
```

The bridge handles the entire §4 multiplexer for you; your code sees only the §0 application bytes —
which is exactly the layer you reverse-engineer.

---

## References

- **Bluetooth Core Specification** — *RFCOMM with TS 07.10* (frame types, address field, DLCI
  allocation, FCS coverage, MUX control, system parameters); *L2CAP* (PSMs, dynamic CIDs, ERTM);
  *Service Discovery Protocol* (data elements, protocol descriptor lists). https://www.bluetooth.com/specifications/specs/
- **ETSI / 3GPP TS 07.10** (GSM 07.10) — the terminal-multiplexer protocol RFCOMM subsets.
- **Bluetooth Assigned Numbers** — company IDs, service classes, protocol identifiers, AD types,
  appearance, Class of Device. https://www.bluetooth.com/specifications/assigned-numbers/ (machine-
  readable source: https://bitbucket.org/bluetooth-SIG/public).
- **btsnoop format** — Fte/Wireshark `wiretap/btsnoop.c`; **btsnooz** — AOSP `btsnooz.py`.
- This handbook is part of [`untether`](https://github.com/dallanwagz/untether); the decoders it
  references ship in the `untether-bt` library (`pip install untether-bt`).
