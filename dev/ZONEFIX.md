# Zone timeline diagnosis

## Root cause

`dev/check/stream.jsonl.gz` has an older, slower match contract than the live
Go recordings. The simulator was correctly honoring that file's explicit
configuration. The live `live14_GO1` and `live14_GO2` hello records all use a
240 s match and half-length zone holds/shrinks; the check fixture uses 480 s
and double-length holds/shrinks.

| Source | Match duration | Zone close | Radius at 60 / 110 / 150 / 200 / 250 s |
| --- | ---: | ---: | --- |
| Live hello (`live14_GO1/m1.jsonl`) | 240 s | 207.5 s | 380 / 240 / 128.333 / 15 / 0 m |
| `dev/check/stream.jsonl.gz` | 480 s | 415 s | 560 / 380 / 380 / 240 / 182.857 m |

At t=218 s, the live contract is in the closed 0 m circle at 260 HP/s. The
stream fixture is still in its 240 m phase at 60 HP/s. That is the major
timeline mismatch behind the FFA sim comparison.

## Engine and observations

`zone_state_at()` in `dev/sim/engine.py` now holds the phase interpolation in
one pure helper, used by `Match.zone_state()` and the checker. It uses the
configured `hold_s`/`shrink_s`, switches to the next phase at the prior
`shrink_end`, linearly interpolates the current center/radius, and reports the
following circle in `next_center`/`next_radius`. The generated `obs.zone` is
that current interpolated circle. The engine's `self.in_zone` and damage check
use the same horizontal center-to-circle test. At 20 Hz, zone damage is
`dps * 0.05` (1, 1.75, 3, 5, 8, or 13 HP per physics tick); `_damage()` skips
armor reduction for `weapon == "zone"`. This matches protocol §9 and its
zone-damage semantics.

I compared all 19,196 observations in the 16 `live14_GO1/GO2` files. Engine
radius from each recording's own hello config differs from recorded radius by
at most 0.004286 m (observation rounding); recorded `self.in_zone` disagrees
with the reported circle 0 times. There were 0 null-attacker `damaged` events
in these Go observations. At t=60, 110, and 150 in `GO1/m1`, the observed
radii are 380, 240, and 128.33 m, with the expected phase DPS and shrink
windows. That particular bot recording ends at t=185.3, so actual frames at
200 and 250 s are unavailable; the checker prints `n/a` for those samples.

The provided stream fixture is internally consistent with its own 480 s
contract and protocol defaults. I left phase values config-driven instead of
hardcoding a 0.5 multiplier: doing that would make valid 480 s configs wrong.
Use a current live hello as `--real-config` when calibrating against current
practice matches, or refresh the check fixture from a current hello.

## Checker

```sh
python3 dev/sim/zone_check.py /path/to/live14_GO1/m1.jsonl
python3 dev/sim/run.py --matches 48 --seed 6000 --jobs 4 \
  --real-config /path/to/live14_GO1/m1.jsonl --ffa -- python3 -u bot.py
```

The checker accepts a mechfight JSONL hello+observation recording or a replay
header carrying the zone contract. It compares engine radii with recorded
`obs.zone.radius` where that frame exists, and audits `self.in_zone` against
the recorded circle.
