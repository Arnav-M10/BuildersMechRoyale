#!/usr/bin/env python3
"""Compare engine zone radii with the observations in a mechfight recording.

Usage: python3 dev/sim/zone_check.py /path/to/match.jsonl[.gz]
The first record must be a hello (or replay header) containing ``config``.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dev.sim.engine import zone_state_at


CHECK_TIMES = (60.0, 110.0, 150.0, 200.0, 250.0)


def read_records(path):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        text = f.read()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
        if not records:
            raise ValueError("recording is empty")
        return records
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("frames"), list):
        return [value] + value["frames"]
    return [value]


def find_config(records):
    for row in records:
        if isinstance(row, dict) and isinstance(row.get("config"), dict):
            return row["config"]
        # Spectator mechreplay/1 headers can carry the zone contract directly
        # instead of wrapping it in mechfight/1's `config` object.
        if isinstance(row, dict):
            zone = row.get("zone")
            if isinstance(zone, dict) and isinstance(zone.get("phases"), list):
                return {"zone": zone, "match": row.get("match", {})}
    raise ValueError("no hello/header config found")


def find_zone_frames(records):
    frames = []
    for row in records:
        if not isinstance(row, dict):
            continue
        obs = row.get("obs") if isinstance(row.get("obs"), dict) else row
        zone = obs.get("zone")
        t = obs.get("t", row.get("t"))
        if isinstance(zone, dict) and isinstance(t, (int, float)):
            frames.append((float(t), zone))
    return frames


def recorded_zone_checks(records):
    checked = mismatches = zone_damage = 0
    for row in records:
        if not isinstance(row, dict):
            continue
        obs = row.get("obs") if isinstance(row.get("obs"), dict) else row
        zone = obs.get("zone")
        own = obs.get("self")
        if isinstance(zone, dict) and isinstance(own, dict):
            pos = own.get("pos")
            if isinstance(pos, list) and len(pos) >= 2:
                dx = float(pos[0]) - float(zone["center"][0])
                dy = float(pos[1]) - float(zone["center"][1])
                computed = math.hypot(dx, dy) <= float(zone["radius"])
                checked += 1
                mismatches += computed != bool(own.get("in_zone"))
        zone_damage += sum(1 for e in obs.get("events", [])
                           if e.get("type") == "damaged" and e.get("by") is None)
    return checked, mismatches, zone_damage


def state_from_config(config, t):
    z = config["zone"]
    center = tuple(float(v) for v in z["start_center"])
    circles = [(0.0, center, float(z["start_radius"]))]
    for phase in z["phases"]:
        circles.append((0.0, center, float(phase["radius"])))
    return zone_state_at(z["phases"], circles, t,
                         float(config.get("match", {}).get("duration_s", 0)))[0]


def nearest_frame(frames, target):
    if not frames:
        return None
    frame = min(frames, key=lambda pair: abs(pair[0] - target))
    return frame if abs(frame[0] - target) <= 0.051 else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("recording", help="JSON/JSONL recording with a hello config")
    ap.add_argument("--times", nargs="+", type=float, default=CHECK_TIMES,
                    help="times in seconds to compare (default: 60 110 150 200 250)")
    args = ap.parse_args(argv)

    rows = read_records(args.recording)
    config = find_config(rows)
    zcfg = config["zone"]
    total = sum(float(p["hold_s"]) + float(p["shrink_s"])
                for p in zcfg["phases"])
    frames = find_zone_frames(rows)
    checked, mismatches, zone_damage = recorded_zone_checks(rows)
    print("recording:", args.recording)
    print("match duration: %s s; configured zone closes at %.1f s" %
          (config.get("match", {}).get("duration_s", "unknown"), total))
    if frames:
        print("recorded zone frames: %d, t=%.1f..%.1f s" %
              (len(frames), frames[0][0], frames[-1][0]))
    else:
        print("recorded zone frames: none; showing the configured engine timeline only")
    if checked:
        print("recorded self.in_zone checks: %d, mismatches: %d; null-attacker damage events: %d" %
              (checked, mismatches, zone_damage))
    print("t(s)  engine radius  recorded radius  delta   recorded frame t  dps  shrink window")
    for t in args.times:
        sim = state_from_config(config, t)
        real = nearest_frame(frames, t)
        if real:
            rt, rz = real
            rr = float(rz["radius"])
            print("%5.1f %13.3f %16.3f %+7.3f %17.1f %5g  %.1f..%.1f" %
                  (t, sim["radius"], rr, sim["radius"] - rr, rt, sim["dps"],
                   sim["shrink_start"], sim["shrink_end"]))
        else:
            print("%5.1f %13.3f %16s %7s %17s %5g  %.1f..%.1f" %
                  (t, sim["radius"], "n/a", "n/a", "n/a", sim["dps"],
                   sim["shrink_start"], sim["shrink_end"]))


if __name__ == "__main__":
    main()
