#!/usr/bin/env python3
"""Decode the BLE-listen ESPHome log into per-device sensor readings, and flag SUSPECT frames.

Pairs with esphome/ble-listen.yaml (which logs `ADV <mac> RSSI <r> MFR uuid=<company> data=<hex>`).
This is the "radio ground truth" half of the parser-diff loop: decode the raw advert/scan-response
bytes the ESP32 actually heard, so you can compare them to what Home Assistant parsed.

Field-tested lessons baked in (from a real Govee H5074-vs-H5075 investigation):
  * ESPHome injects ANSI color codes into log lines — strip them before bytes.fromhex().
  * Govee H5074 AND H5075 share company id 0xEC88 and both lead with 0x00, but have DIFFERENT
    payload layouts — you MUST decode by known-model-per-MAC, not by guessing from the bytes.
  * A manufacturer frame on a known device's MAC whose company id ISN'T the expected one (e.g. an
    Apple 0x004C iBeacon, or an INTELLI_ROCKS name) is a SUSPECT — the leading over-the-air
    MAC-collision hypothesis for "occasional garbage readings."

Usage:
    esphome logs ble-listen.yaml | python3 decode_ble_log.py --map AA:BB:CC:DD:EE:F1=H5074,AA:BB:CC:DD:EE:F2=H5075
    python3 decode_ble_log.py --map ... < captured.log
    # add --csv out.csv to log every decode, --suspect-only to print only anomalies.

Extend MODELS for other devices. Confirm a new model's shape with a one-variable diff against the
device's own screen / the vendor app before trusting it.
"""

from __future__ import annotations

import argparse
import re
import sys

ANSI = re.compile(r"\x1b\[[0-9;]*m")
LINE = re.compile(
    r"ADV\s+(?P<mac>[0-9A-Fa-f:]{17})\s+RSSI\s+(?P<rssi>-?\d+)\s+"
    r"MFR\s+uuid=(?P<uuid>\S+)\s+data=(?P<hex>[0-9A-Fa-f]+)"
)


def _i16le(b: bytes) -> int:
    return int.from_bytes(b, "little", signed=True)


def decode_h5074(d: bytes) -> dict:
    # 00 + temp(int16 LE)/100 + hum(int16 LE)/100 + batt + 1 trailing  (7 bytes)
    if len(d) < 6:
        raise ValueError("short H5074 frame")
    return {"temp_c": _i16le(d[1:3]) / 100, "humidity": _i16le(d[3:5]) / 100, "battery": d[5]}


def decode_h5075(d: bytes) -> dict:
    # 00 + 3-byte BE packed + batt.  temp = val/10000, hum = (val%1000)/10  (top bit = sign)
    if len(d) < 5:
        raise ValueError("short H5075 frame")
    packed = int.from_bytes(d[1:4], "big")
    negative = packed & 0x800000
    packed &= 0x7FFFFF
    temp = (packed / 10000) * (-1 if negative else 1)
    return {"temp_c": round(temp, 2), "humidity": (packed % 1000) / 10, "battery": d[4]}


MODELS = {
    "H5074": {"company": 0xEC88, "decode": decode_h5074},
    "H5075": {"company": 0xEC88, "decode": decode_h5075},
}


def _company(uuid: str) -> int | None:
    try:
        return int(uuid, 16)
    except ValueError:
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", required=True,
                    help="comma-separated MAC=MODEL pairs, e.g. AA:..F1=H5074,AA:..F2=H5075")
    ap.add_argument("--csv", help="append every decode to this CSV file")
    ap.add_argument("--suspect-only", action="store_true", help="print only SUSPECT frames")
    args = ap.parse_args()

    mac_model = {}
    for pair in args.map.split(","):
        mac, _, model = pair.partition("=")
        mac = mac.strip().upper()
        model = model.strip().upper()
        if model not in MODELS:
            sys.exit(f"unknown model {model!r}; known: {', '.join(MODELS)}")
        mac_model[mac] = model

    csv = open(args.csv, "a") if args.csv else None
    if csv:
        csv.write("mac,model,temp_c,humidity,battery,rssi,suspect,note\n")

    for raw in sys.stdin:
        m = LINE.search(ANSI.sub("", raw))
        if not m:
            continue
        mac = m["mac"].upper()
        if mac not in mac_model:
            continue
        model = mac_model[mac]
        spec = MODELS[model]
        company = _company(m["uuid"])
        try:
            data = bytes.fromhex(m["hex"])
        except ValueError:
            continue

        suspect, note, decoded = False, "", {}
        if company != spec["company"]:
            suspect, note = True, f"company 0x{company:04X} != expected 0x{spec['company']:04X} (collision?)"
        else:
            try:
                decoded = spec["decode"](data)
            except ValueError as e:
                suspect, note = True, str(e)

        if args.suspect_only and not suspect:
            continue
        flag = "SUSPECT" if suspect else "ok"
        vals = (f"temp={decoded['temp_c']}C hum={decoded['humidity']}% batt={decoded['battery']}%"
                if decoded else "(undecoded)")
        print(f"[{flag}] {mac} {model} {vals} rssi={m['rssi']} {note}".rstrip())
        if csv:
            csv.write(f"{mac},{model},{decoded.get('temp_c','')},{decoded.get('humidity','')},"
                      f"{decoded.get('battery','')},{m['rssi']},{int(suspect)},{note}\n")
            csv.flush()  # per-write flush so a long unattended run never loses data


if __name__ == "__main__":
    main()
