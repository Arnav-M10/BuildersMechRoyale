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

This is a fast comparison harness, not a replica of the real server. Movement uses sampled grid collisions and simplified roof/overhang handling; trees and canopy do not block simulator vision; projectile and splash collision use discrete samples. The initial house behavior was under-calibrated; see the updated model and measured results below.

The initial offline score of 8.625 was materially above the reported real-server score of 3.56. The calibration update below supersedes the initial house-ordering and score notes.

## Calibration

Updated `dev/sim/engine.py` to send hunters weakest-first (easy, medium, hard, expert). Hunter count starts at one and rises with both match time and house deaths, reaching the full roster by 65% of match time or after four house deaths. Non-hunters roam/loot and defend themselves, but do not attack other house bots. House weapon preference is laser, lightning, mortar; the gun is retained. Offer choice follows the replay's early upgrade order: plating, gun damage, gun magazine, armor, gun rate, nanites, then laser damage.

House movement now includes tier-specific reaction time, aim error/lead, strafing, boost use, roof goals, weapon looting, and retreat behavior. Hunter routes use `Geo.nav` when the direct route is blocked. Jet movement applies vertical acceleration and gravity, respects `max_rise_speed` and jet drain, and permits airborne traversal over steep grid slopes and walls.

### Replay anchors and calibration

I mined the 11 complete records in `dev/live_matches.json` (the final entry is a one-frame stub). Their house builds averaged 28.0% gun hits (`3028/10797` mech hits from house gunfire); per-build rates ranged from 18.1% for `probe-astra-skirmish` to 48.8% for `probe-red-best`, with `house:expert` at 48.0%. This supports distinct tiers rather than one shared aim profile. The supplied replay measurements also anchor movement (median 11.7 m/s, boosting 17% of frames, <6% below 1 m/s), roof occupancy (8–28%), weapon ranges, loadouts, and upgrade order.

### Main-bot benchmark

Ran 48 matches, seeds 17–64, with schema validation enabled, four worker processes, and the unchanged runner CLI. The tested bot was a byte-identical snapshot of `../BuildersMechRoyale/bot.py` and `geo.py` copied into the worktree for the run; the temporary snapshot has been removed. Mean score was **2.958** (`16 - place`), within the requested 3.5 +/- 0.7 target. There were no bot timeouts or invalid actions.

| Place | Matches |
|---:|---:|
| 8 | 2 |
| 9 | 2 |
| 10 | 2 |
| 12 | 7 |
| 13 | 13 |
| 14 | 13 |
| 15 | 7 |
| 16 | 2 |

Contestant deaths had median time **52.3 s** and all occurred before 240 s. Causes: 33 gun, 11 laser, 3 lightning, 1 NPC. Thus 44/48 deaths were due to gun or laser, usually early as requested. Hunter gun accuracy while aiming at the contestant, split by tier, was:

| Tier | Hits / shots | Hit rate |
|---|---:|---:|
| Easy | 393 / 2,830 | 13.9% |
| Medium | 419 / 2,354 | 17.8% |
| Hard | 380 / 1,051 | 36.2% |
| Expert | 298 / 566 | 52.7% |

This focus-target metric excludes shots at NPCs and is not directly comparable to the replay's all-target FFA rate. Weighted house movement was 97.9% of frames at >=1 m/s, boost 16.3%, and roof occupancy 9.7%. By tier, moving/boost/roof percentages were easy 93.7/11.8/7.8, medium 99.7/16.1/6.9, hard 99.4/17.5/8.3, and expert 99.2/21.0/17.7. Boost use is close to replay; expert roof occupancy remains below the replay's cited 28%.

### Stadium jet check

Started a mech in the stadium bowl near `(-225, 54)` and commanded jump + boost toward `(53, -143)`. The start was outside the walkable nav region; the mech re-entered walkable nav after **1.55 s** of jetting. The run reached the configured 10 m/s rise cap, drained jet energy, cleared the steep slope, and continued toward the destination. House roof occupancy above is measured from physics positions, confirming simulated landings on roofs.

### Fidelity limits

The current score is calibrated to the target band but remains an approximation. In particular, the subject-focused hunter gun hit rate (21.9% pooled) uses a different target mix than replay FFA gun accuracy (28.0%); simplified NPC/house target selection, sparse sampled roof geometry, and coarse collision/projectile handling still affect outcomes. The expert tier's 17.7% roof occupancy under-represents the 28% replay anchor. Use seed-matched comparisons and treat small score deltas cautiously.

