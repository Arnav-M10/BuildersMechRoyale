# Codex work status

## Delivered

- Copied the latest `bot.py` and `geo.py` from `../BuildersMechRoyale/` into this worktree before review. I did not edit either file after that copy.
- Added the standard-library simulator in `dev/sim/engine.py` and the subprocess/multiprocessing runner in `dev/sim/run.py`.
- The simulator reads the recorded full config and reuses `geo.Geo.load()` for the map. The runner builds `cache.pkl` in the worktree when needed; `.gitignore` already excludes it.
- Main mode runs the supplied bot as a subprocess against 15 in-process squad bots. FFA mode runs 16 subprocess copies. Both use lockstep decisions, 20 Hz physics, a 50 ms response budget, and coast/idle handling.
- Generated observations are checked against `dev/mechfight.schema.json` by default. Invalid bot actions are ignored, and `--no-validate` disables generated-observation validation only.
- Added the ranked review in `dev/REVIEW_CODEX.md` and proposed fixes in the unapplied unified diff `dev/bot_codex_suggestions.patch`.

## Run it

From the repository root:

```sh
python3 dev/sim/run.py --matches 8 --seed 17 -- python3 -u bot.py
python3 dev/sim/run.py --matches 8 --seed 17 --ffa -- python3 -u bot.py
```

Use `--jobs N` to control multiprocessing. Match seeds increment from `--seed`; the bot seed stays fixed per slot. The first run builds the map cache, and later runs load it.

## Verification and results

- `python3 dev/check/check.py --map-dir arena -- python3 -u bot.py`: PASS, 300/300 actions, zero stale/wrong ticks, zero invalid lines, no debug cap violations. Answer time was p99 0.5 ms and max 3.3 ms on this machine.
- FFA smoke run: one match with 16 subprocess bots, schema validation on, zero timeouts and invalid actions. Each placement from 1 through 16 occurred once; the seat-averaged score was 7.500.
- Eight squad matches, seeds 17–24, four workers: mean `(16 - place)` **8.625**; zero timeouts and invalid actions.

| Place | Matches |
|---:|---:|
| 1 | 3 |
| 9 | 1 |
| 10 | 1 |
| 11 | 1 |
| 12 | 1 |
| 14 | 1 |

Per-match outcomes:

| Seed | Place | Kills | Death time | Cause |
|---:|---:|---:|---:|---|
| 17 | 12 | 0 | 208.5 s | zone |
| 18 | 10 | 5 | 110.9 s | gun by 6 |
| 19 | 1 | 6 | survived | winner |
| 20 | 1 | 6 | survived | winner |
| 21 | 1 | 10 | survived | winner |
| 22 | 11 | 3 | 106.4 s | gun by 12 |
| 23 | 9 | 5 | 167.3 s | gun by 1 |
| 24 | 14 | 0 | 167.3 s | gun by 14 |

## Fidelity limits

This is a useful fast comparison harness, not a replica of the real server. Movement uses sampled grid collisions and simplified roof/overhang handling; trees and canopy do not block simulator vision; projectile and splash collision use discrete samples; NPC and house behavior are deliberately simple. The squad's hunter count ramps up as mechs are eliminated, with the strongest tiers sent first.

The offline score of 8.625 is materially above the reported real-server score of 3.56. Treat the absolute score as uncalibrated and use repeated seed-matched comparisons between bot versions until the house behavior is tuned closer to the real practice bots.

