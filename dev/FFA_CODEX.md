# FFA layer

## Changes

- Added `ffa_detect`, `ffa_engage`, and `ffa_hold` to `P`, all defaulting to `False`. The bot patch is in `dev/bot_codex_ffa.patch` against the current `../BuildersMechRoyale/bot.py`.
- FFA latches on two distinct observed laser/lightning beams ending within 7 m of another mech, or on a death gap of at least three before the first zone shrink. The gap is deliberately restricted to the pre-shrink period: practice runs showed aggregate deaths can include non-player/zone casualties, which would falsely latch after that point. Beam detection remains active for the whole match.
- With `ffa_engage` enabled, the bot only selects mechs that attacked it in the last 4 s, are its current target, are within 35 m, are below 45% HP within 120 m while our HP is above 40%, or when at most four mechs remain. Other visible mechs are avoided via cover; NPC/farm targeting is suppressed during an unengaged FFA. Qualifying fights keep the existing berserk behavior.
- With `ffa_hold` enabled, at six or fewer alive and no target, it navigates to cover inside the next circle and faces the freshest threat.
- Added `--ffa-mixed` to `dev/sim/run.py`; it runs eight enabled and eight disabled copies, alternating slot groups every match to reduce spawn-slot bias.

## Verification

- `python3 dev/fuzz.py 40`: 0 errors; p50 0.29 ms, p99 2.72 ms, max 3.90 ms.
- `python3 dev/check/check.py --map-dir arena -- python3 -u bot.py`: PASS, 300/300 observations answered, 0 misses, 0 wrong ticks or invalid lines.
- Paired practice squad run, seeds 17–64: knobs off and on both scored mean 6.104 with identical placement for all 48 seeds and identical histograms. No FFA latch occurred in any of the 48 enabled traces.
- Mixed FFA run, `--matches 8 --seed 4200 --jobs 2 --ffa-mixed --no-validate`: enabled mean 7.281 vs disabled 7.703 (16-place score, higher is better), 64 seats per group; 0 timeouts and 0 invalid actions. This small sample is directional only. The runner is using 16 copies of this bot, not the final tournament's exact opponent mix.

I concede the `wdist` point: the cited 96-seed result (-0.91 ± 0.35 for 70 m vs 110 m) and real-server history favor 110 m; I found no evidence to recommend enabling the 70 m variant.
