"""Shared BLE-advert decode for the untether ESP32 `ble-listen` logs.

Single source of truth used by BOTH `decode_ble_log.py` (one-shot, reads piped logs) and
`ble_logger.py` (long-running, self-reconnecting). Keeping the decode here means the two can never
drift apart — which is exactly the failure mode that bites you when the same payload math is copied
into two places (one gets a `/1000` vs `/10000` typo and the captures silently disagree).

Field-tested lessons baked in (from a real Govee H5074-vs-H5075 investigation):
  * ESPHome injects ANSI color codes into log lines — strip them before bytes.fromhex().
  * Govee H5074 AND H5075 share company id 0xEC88 and both lead with 0x00 but have DIFFERENT
    payload layouts — decode by known-model-per-MAC, never by guessing from the bytes.
  * A manufacturer frame on a known device's MAC whose company id ISN'T the expected one (e.g. an
    Apple 0x004C iBeacon) is a SUSPECT — the leading over-the-air MAC-collision hypothesis.
"""

from __future__ import annotations

import re

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


def parse_map(s: str) -> dict[str, str]:
    """'AA:..=H5074,BB:..=H5075' -> {MAC: MODEL}. Raises ValueError on an unknown model."""
    out: dict[str, str] = {}
    for pair in s.split(","):
        mac, _, model = pair.partition("=")
        mac, model = mac.strip().upper(), model.strip().upper()
        if model not in MODELS:
            raise ValueError(f"unknown model {model!r}; known: {', '.join(MODELS)}")
        out[mac] = model
    return out


def _company(uuid: str) -> int | None:
    try:
        return int(uuid, 16)
    except ValueError:
        return None


def decode_line(raw: str, mac_model: dict[str, str]) -> dict | None:
    """Decode one ESPHome log line. Returns a result dict, or None if it's not a mapped ADV line.

    Result: {mac, model, rssi, company, decoded:{temp_c,humidity,battery}|{}, suspect:bool, note:str}
    """
    m = LINE.search(ANSI.sub("", raw))
    if not m:
        return None
    mac = m["mac"].upper()
    if mac not in mac_model:
        return None
    model = mac_model[mac]
    spec = MODELS[model]
    company = _company(m["uuid"])
    try:
        data = bytes.fromhex(m["hex"])
    except ValueError:
        return None
    res = {"mac": mac, "model": model, "rssi": int(m["rssi"]), "company": company,
           "decoded": {}, "suspect": False, "note": ""}
    if company != spec["company"]:
        res["suspect"] = True
        res["note"] = f"company 0x{company:04X} != expected 0x{spec['company']:04X} (collision?)"
    else:
        try:
            res["decoded"] = spec["decode"](data)
        except ValueError as e:
            res["suspect"], res["note"] = True, str(e)
    return res


# Golden vectors for --self-test (verify the decoder BEFORE a long unattended run).
GOLDEN = [
    ("ADV AA:BB:CC:DD:EE:F1 RSSI -50 MFR uuid=0xEC88 data=00ca08f10d6400",
     {"AA:BB:CC:DD:EE:F1": "H5074"}, {"temp_c": 22.5, "humidity": 35.69, "battery": 100}, False),
    ("ADV AA:BB:CC:DD:EE:F2 RSSI -55 MFR uuid=0xEC88 data=00036fc464",
     {"AA:BB:CC:DD:EE:F2": "H5075"}, {"temp_c": 22.52, "humidity": 22.0, "battery": 100}, False),
    ("\x1b[0;36mADV AA:BB:CC:DD:EE:F1 RSSI -60 MFR uuid=0x004C data=0215aabbccdd\x1b[0m",
     {"AA:BB:CC:DD:EE:F1": "H5074"}, {}, True),
]


def _close(a: dict, b: dict) -> bool:
    return set(a) == set(b) and all(abs(a[k] - b[k]) < 0.011 for k in a)


def self_test() -> bool:
    ok = True
    for line, mm, expected, want_suspect in GOLDEN:
        r = decode_line(line, mm)
        passed = bool(r) and r["suspect"] == want_suspect and _close(r["decoded"], expected)
        print(("PASS" if passed else "FAIL"), "->", r)
        ok &= passed
    return ok
