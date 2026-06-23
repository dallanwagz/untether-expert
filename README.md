# untether — a Bluetooth expert for Claude Code

Clone this repo, say hello, and you're talking to **untether**: a Bluetooth subject-matter expert
that doesn't just answer questions — it has a toolkit and uses it.

> Hi — I'm untether, your Bluetooth expert. I can reverse-engineer a device's protocol, troubleshoot
> a flaky link, decode BLE or Classic on the wire, bridge a Classic-SPP device into Home Assistant,
> or dig into why HA's parser disagrees with what the radio actually sees. What are you working on?

It's a Claude Code **plugin** that packages everything learned from real, hands-on Bluetooth
reverse-engineering and troubleshooting into one persona:

- 🧠 **Spec-grounded knowledge** — the Bluetooth stack down to the wire, with a full Classic / RFCOMM
  / SPP handbook ([`reference/CLASSIC-BT-RE-HANDBOOK.md`](reference/CLASSIC-BT-RE-HANDBOOK.md)) and a
  battle-tested RE methodology ([`reference/METHODOLOGY.md`](reference/METHODOLOGY.md)).
- 🛠️ **A real library** — [`untether-bt`](https://pypi.org/project/untether-bt/) (`pip install
  untether-bt`): btsnoop/btsnooz/HCI/L2CAP/ATT + **RFCOMM/TS 07.10** decoders, BLE advert parsing,
  the full SIG Assigned-Numbers registries, a self-healing SPP transport, and app-RE helpers.
- 📡 **A radio it can flash** — ESP32 (a Wyze plug is ideal) ESPHome configs in [`esphome/`](esphome/)
  to put the radio into **SPP-bridge**, **BLE-listen**, or **Classic-probe** mode — to *talk to*
  Classic devices, *observe* raw adverts, or *check* a device's reachability/channel.
- 🏠 **Home Assistant via MCP** — read HA's Bluetooth state, advertisements, proxy slots, and parsed
  entity values, so it can run its signature **parser-diff loop** (below).

## Install (clone and go)

```bash
git clone https://github.com/dallanwagz/untether-expert.git
cd untether-expert
```

Then in Claude Code:

```
/plugin marketplace add /path/to/untether-expert   # the repo dir you just cloned
/plugin install untether-expert@untether
```

(Once it's on GitHub you can skip the clone: `/plugin marketplace add dallanwagz/untether-expert`
then `/plugin install untether-expert@untether`.)

Now start it any time with:

```
/untether-expert:bluetooth
```

…or just say *"bluetooth help"* / ask any Bluetooth question — the skill auto-engages.

### Optional: wire up Home Assistant

The plugin bundles an HA MCP client named **`untether-ha`** (named so it can't collide with a
`home-assistant` MCP you may already run). On install (or via `/plugin`), set the two user-config
values:

- **`ha_mcp_url`** — your HA Model Context Protocol Server SSE endpoint, e.g.
  `http://homeassistant.local:8123/mcp_server/sse` (enable the *Model Context Protocol Server*
  integration in Home Assistant first).
- **`ha_token`** — a long-lived access token from your HA profile.

**Already run your own HA MCP** (e.g. a local `uvx`/stdio server)? Just use it — leave these blank
and `untether-ha` stays harmlessly disconnected (disable it in `/plugin` if you like). Leave them
blank to skip HA entirely; everything else still works.

## The signature move: the parser-diff loop

Home Assistant (and the upstream `bluetooth-devices` / BTHome parsers) turn raw adverts into entity
values, and you normally can't see *inside* that step. untether can, by diffing the two ends:

1. **Radio ground truth** — flash the ESP32 with `esphome/ble-listen.yaml`, capture the device's raw
   advertisement bytes, decode them with `untether-bt`.
2. **HA's interpretation** — via HA-MCP, read what HA parsed for the same device + the raw advert HA
   received.
3. **Diff** — same bytes, different decoded values ⇒ the bug is in HA's/upstream's **parser** (wrong
   offset/scale/matcher); different bytes ⇒ a capture/proxy problem. Pinpoint the byte, propose the
   upstream fix.

`scripts/decode_ble_log.py` is the radio-truth half: it decodes the `ble-listen` log into per-device
readings (ANSI-stripped, decode-by-known-model-per-MAC) and flags SUSPECT frames (e.g. a foreign
company id on a known MAC — the MAC-collision hypothesis). Field-tested on Govee H5074/H5075; see
`reference/devices/`.

> ⚠️ **Active scanning matters.** Many sensors (Govee included) put their data in the BLE *scan
> response*, which a passive scan never solicits — you'd capture the device but zero values.
> `esphome/ble-listen.yaml` defaults to `active: true` for this reason.

## What's inside

```
untether-expert/
├── .claude-plugin/
│   ├── plugin.json          # plugin manifest (+ HA user config)
│   └── marketplace.json     # so the repo installs as its own marketplace
├── skills/untether-expert/SKILL.md   # the persona + SME knowledge (the brain)
├── commands/bluetooth.md             # /untether-expert:bluetooth entrypoint
├── agents/bt-protocol-analyst.md     # deep capture/protocol analysis subagent
├── .mcp.json                         # Home Assistant MCP client
├── esphome/                          # ESP32 radio-mode configs (spp-bridge / ble-listen / classic-probe)
└── reference/                        # the Classic-BT handbook + the RE methodology
```

## Hardware

A classic **ESP32 (WROOM-32)** for Bluetooth Classic work — S3/C3/C6/H2 are BLE-only. A **Wyze plug**
flashed with ESPHome is perfect: it's the radio *and* gives you relays to power-cycle stuck devices.
Plain BLE listening works on any ESP32.

## License

MIT. Built from real reverse-engineering work; contributions of device profiles, parser fixes, and
techniques are welcome.
