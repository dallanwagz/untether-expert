#!/usr/bin/env python3
"""Decode a piped BLE-listen ESPHome log into per-device readings; flag SUSPECT frames (one-shot).

Pairs with esphome/ble-listen.yaml. The "radio ground truth" half of the parser-diff loop: decode
the raw advert/scan-response bytes the ESP32 actually heard, to compare against what HA parsed.
All decode logic lives in bledecode.py (shared with ble_logger.py so they can't drift).

Usage:
    esphome logs ble-listen.yaml | python3 decode_ble_log.py --map AA:..F1=H5074,AA:..F2=H5075
    python3 decode_ble_log.py --map ... < captured.log
    python3 decode_ble_log.py --self-test          # verify the decoder against golden frames

Options: --csv out.csv (log every decode, flushed), --suspect-only (print only anomalies).
For a long, self-reconnecting capture instead of a pipe, use ble_logger.py.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bledecode


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", help="comma-separated MAC=MODEL pairs, e.g. AA:..F1=H5074,AA:..F2=H5075")
    ap.add_argument("--csv", help="append every decode to this CSV file")
    ap.add_argument("--suspect-only", action="store_true", help="print only SUSPECT frames")
    ap.add_argument("--self-test", action="store_true", help="verify the decoder, then exit")
    args = ap.parse_args()

    if args.self_test:
        sys.exit(0 if bledecode.self_test() else 1)
    if not args.map:
        sys.exit("--map is required (unless --self-test)")
    try:
        mac_model = bledecode.parse_map(args.map)
    except ValueError as e:
        sys.exit(str(e))

    csv = open(args.csv, "a") if args.csv else None
    if csv and os.stat(args.csv).st_size == 0:
        csv.write("mac,model,temp_c,humidity,battery,rssi,suspect,note\n")

    for raw in sys.stdin:
        r = bledecode.decode_line(raw, mac_model)
        if r is None:
            continue
        if args.suspect_only and not r["suspect"]:
            continue
        d = r["decoded"]
        vals = (f"temp={d['temp_c']}C hum={d['humidity']}% batt={d['battery']}%"
                if d else "(undecoded)")
        print(f"[{'SUSPECT' if r['suspect'] else 'ok'}] {r['mac']} {r['model']} {vals} "
              f"rssi={r['rssi']} {r['note']}".rstrip())
        if csv:
            csv.write(f"{r['mac']},{r['model']},{d.get('temp_c','')},{d.get('humidity','')},"
                      f"{d.get('battery','')},{r['rssi']},{int(r['suspect'])},{r['note']}\n")
            csv.flush()


if __name__ == "__main__":
    main()
