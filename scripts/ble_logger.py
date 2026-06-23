#!/usr/bin/env python3
"""Self-reconnecting BLE logger for the untether ESP32 `ble-listen` mode.

Built for long, unattended parser-diff captures (e.g. a 13h shower-window run): it connects to the
ESP32 over the ESPHome API, streams its logs, decodes each Govee advert (via bledecode), writes CSV,
flags SUSPECT frames, prints a periodic heartbeat, and AUTO-RECONNECTS so a Wi-Fi blip or a device
reboot doesn't end your capture. It also reconnects if the log stream goes silent past --stale
(a quiet stream usually means the link dropped, not that the device went quiet).

Run --self-test FIRST to verify the decoder before committing to a long run — it catches payload-math
typos before you wait hours (lesson learned the hard way).

Deps:  pip install aioesphomeapi   (only needed to actually connect; --self-test needs nothing)

Usage:
  python3 ble_logger.py --self-test --map AA:..F1=H5074,AA:..F2=H5075
  python3 ble_logger.py --host 192.168.1.50 --map AA:..F1=H5074,AA:..F2=H5075 \
      --csv capture.csv --heartbeat 300 [--suspect-only] [--password PW] [--encryption-key NOISE_PSK]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bledecode


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


async def run(args, mac_model) -> None:
    from aioesphomeapi import APIClient
    try:
        from aioesphomeapi import LogLevel
        log_level = LogLevel.LOG_LEVEL_VERY_VERBOSE
    except Exception:
        log_level = None

    csv = open(args.csv, "a") if args.csv else None
    if csv and os.stat(args.csv).st_size == 0:
        csv.write("ts,mac,model,temp_c,humidity,battery,rssi,suspect,note\n")
        csv.flush()

    stats = {"frames": 0, "suspects": 0, "last_log": time.monotonic(), "last": {}}

    def on_log(msg) -> None:
        raw = msg.message
        raw = raw.decode(errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        stats["last_log"] = time.monotonic()
        r = bledecode.decode_line(raw, mac_model)
        if r is None:
            return
        stats["frames"] += 1
        if r["suspect"]:
            stats["suspects"] += 1
        d = r["decoded"]
        if d:
            stats["last"][r["mac"]] = d
        if r["suspect"] or not args.suspect_only:
            vals = (f"temp={d['temp_c']}C hum={d['humidity']}% batt={d['battery']}%"
                    if d else "(undecoded)")
            print(f"[{_ts()}] [{'SUSPECT' if r['suspect'] else 'ok'}] {r['mac']} {r['model']} "
                  f"{vals} rssi={r['rssi']} {r['note']}".rstrip(), flush=True)
        if csv:
            csv.write(f"{_ts()},{r['mac']},{r['model']},{d.get('temp_c','')},{d.get('humidity','')},"
                      f"{d.get('battery','')},{r['rssi']},{int(r['suspect'])},{r['note']}\n")
            csv.flush()  # per-write flush so a crash never loses captured data

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(args.heartbeat)
            last = "; ".join(f"{m} {v['temp_c']}C/{v['humidity']}%" for m, v in stats["last"].items())
            print(f"[{_ts()}] heartbeat: {stats['frames']} frames, {stats['suspects']} suspect | "
                  f"{last or 'no readings yet'}", flush=True)

    hb = asyncio.ensure_future(heartbeat())
    backoff = 2
    try:
        while True:
            cli = APIClient(args.host, 6053, args.password or "",
                            noise_psk=(args.encryption_key or None))
            try:
                try:
                    await cli.connect(login=True)
                except Exception:
                    await cli.connect(login=False)
                print(f"[{_ts()}] connected to {args.host}", flush=True)
                backoff = 2
                stats["last_log"] = time.monotonic()
                if log_level is not None:
                    cli.subscribe_logs(on_log, log_level=log_level)
                else:
                    cli.subscribe_logs(on_log)
                while True:  # hold the link; bail to reconnect if the stream goes stale
                    await asyncio.sleep(5)
                    if time.monotonic() - stats["last_log"] > args.stale:
                        print(f"[{_ts()}] no logs for {args.stale}s — reconnecting", flush=True)
                        break
            except Exception as e:
                print(f"[{_ts()}] connection error: {e!r} — retrying in {backoff}s", flush=True)
            finally:
                try:
                    await cli.disconnect()
                except Exception:
                    pass
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
    finally:
        hb.cancel()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", required=True, help="MAC=MODEL pairs, e.g. AA:..F1=H5074,AA:..F2=H5075")
    ap.add_argument("--host", help="ESP32 IP or hostname (required unless --self-test)")
    ap.add_argument("--csv", help="append every decode to this CSV (flushed per write)")
    ap.add_argument("--suspect-only", action="store_true", help="only print SUSPECT frames")
    ap.add_argument("--heartbeat", type=int, default=300, help="status line every N seconds")
    ap.add_argument("--stale", type=int, default=120, help="reconnect if no logs for N seconds")
    ap.add_argument("--password", default="", help="ESPHome API password, if set")
    ap.add_argument("--encryption-key", default="", help="ESPHome API noise PSK, if set")
    ap.add_argument("--self-test", action="store_true", help="verify the decoder, then exit")
    args = ap.parse_args()

    try:
        mac_model = bledecode.parse_map(args.map)
    except ValueError as e:
        sys.exit(str(e))
    if args.self_test:
        sys.exit(0 if bledecode.self_test() else 1)
    if not args.host:
        sys.exit("--host is required (unless --self-test)")
    try:
        asyncio.run(run(args, mac_model))
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
