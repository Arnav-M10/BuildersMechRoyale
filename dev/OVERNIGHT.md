# Overnight log (Sat 2026-11-21)

Best real practice score so far: **5.13** (v5 `2dfc546`). Main's defaults = v5 settings + later fixes.

## How candidates are judged
- Calibrated sim (Codex), squad mode, 192 paired seeds per variant, from a frozen code snapshot.
- A change is accepted only if it wins on two independent seed sets, then passes fuzz, check.py and the docker sandbox check.

## Results so far
| Round | Seeds | Variant | vs base |
|---|---|---|---|
| 1 (old sim, collision bug) | 2000 | regen | +0.30 ± 0.14 (artifact of the wedge bug) |
| 2 (fixed sim) | 5000 | regen / regen+retreat50 / +juke / all | −0.07 / +0.10 / −0.10 / −0.17 (±0.26) |
| 3 | 9000 | commit / noflee / ambush / combos | running |

## Death analysis (42 sim squad matches)
Median death 64 s; 26/42 deaths had a single visible enemy; 33/42 died in a retreat mode; 18/42 still gun-only.
Typical loss: a 130–160 m gun duel against the first hunter, then a flight across open ground.

## Codex debate
- Strategist (round 1): "commit by HP ratio" is naive (ignores loadout and hunters out of view); ambush feeds escalation.
  Its bet: exposure-aware disengagement. Implemented as knob `race` (disengage when time-to-death < 0.8 × time-to-kill; cover only within 35 m, otherwise fight while strafing).
- Engineer: grown-mech collision fixed in the sim; investigating two mid-game zone deaths.

## Other teams' public repos (read, not run; ideas reimplemented in our own code)
- **Team Inventors** (ParishruthiGanesh/mech-royale, 5.15, MIT): real-server A/B history says never retreat ("berserk"), a 110 m fight distance and fetching better weapons mid-fight each "scored best".
- **TTUN/Wolverines/r1ver** (varchaskukreja/TTUN, 5.10–5.21, MIT): an "economy" bot that farms drones, crates, drops and cubes hard, upgrades armor/plating/nanites first, and retreats only when clearly losing.
- skandra08 ("lh-" bots, 2.29): their best single change was stay-and-fight (no disengage) in practice.
- Evans-Kwashie EXPERIMENTS.md: hold triggers through the cooldown boundary; give the gun and laser separate aim solutions.

## Submissions (via Claude in Chrome)
| Time | Commit | What | Score |
|---|---|---|---|
| Sat 2:55 AM | `2dfc546` (exp/v5) | berserk + 110 m + hp×dist targeting + mid-fight looting (sim: +1.02 ± 0.67 over base, 48 seeds) | **5.13** (from 3.81; above the 4.96 top-32 cutoff) |
| Sat ~3:00 AM | `b7bb624` (main) | v5 + zone-forecast fix (server sends the current circle) + displacement stuck check | 5.10 (= v5 within noise; kept, it's correct) |
| Sat ~3:05 AM | `bee838d` (exp/v7) | v5 + TTUN-style economy (sim +1.03 ± 0.47 vs v5) | **4.56** (worse than 5.13: the sim overrates farming; econ stays off) |
| Sat 3:22 AM | `173b7c0` (exp/v8) | v6 + TTUN gun tolerance on the gun's own lead (both top public bots fire much more freely) | 4.79 (worse; gtol stays off) |
| Sat 2:55 AM (Mac) | `0fdb4d9` (exp/v9) | v6 + Inventors' upgrade order | 4.90 (no gain; default order kept) |
| Sat 3:01 AM (Mac) | `575bceb` (exp/v10) | v6 + slower strafe flips (1.0–2.6 s) | 4.77 (worse) |
| Sat 3:06 AM (Mac) | `2dfc546` (v5 again) | NOISE CHECK: same commit that scored 5.13 | **5.13 again**: scoring is deterministic. Small changes still reshuffle matches chaotically, so treat |Δ| < 0.2 as noise for generalization |

## Sim sweeps since v5
| Seeds | Variant (vs v5) | Result |
|---|---|---|
| 13000×96 | + econ | **+1.03 ± 0.47** |
| 13000×96 | want_d 70 instead of 110 | **−0.91 ± 0.35** (long range matters) |
| 14000×96 | v6 (main) + killloot / gtol / precharge / wdist (econ off) | paused (user asked for quieter fans) |

## Codex debate, round 2
Codex diffed us against Inventors and TTUN and found: want_d overrode weapon ranges (making lightning useless at 110 m), tsel only ranked 6 targets, econ treated dead drones as alive, fight-loot chased stale cubes. All fixed or turned into knobs. Its #1 suggestion (gun-only 70 m) conflicts with the −0.91 result above; sent back for rebuttal. Codex is now building an FFA layer (detector, selective engagement, endgame hold) for qualifying and the final.

**Lesson:** the sim misjudges economy-style changes (predicted +1.03, real −0.57). Real submissions decide anything that trades fighting for farming.

## Quiet mode (user asleep, fans) until ~4:30 AM Mac time, then max mode
Testing only on the real server, about one submission per 15 minutes, one change at a time on top of the best (5.13 v5 / 5.10 v6).
Queue: v9 = Inventors' upgrade order (`0fdb4d9`, built and checked); v10 = slower strafing (1.0–2.6 s flips, from the 'not even close' 5.21 bot); v11 = 145 m distance with a laser (same source).

v10 (`575bceb`, slower strafing) built and checked; submit at the next paced slot. v11 (want_laser 145) next.

### giddygiddyboy (rank 4, 5.54, MIT Go, justnau/giddygiddyboy-mech-royale) — read for ideas
- Practice-specific params: laser first (seek a laser up to 240 m), laser 120 m, gun 85 m, cubes mid-fight within 70 m, finish weak targets at 14 m, sticky targets, low-HP bonus.
- Selective engagement even in practice: only mechs within 60 m, recent attackers, approaching (<180 m), weak (<40% HP, <160 m), or aiming at us (<190 m).
- Aim: muzzle predicted with own velocity × 0.075 s; travel time from the 3 m spawn point; gun tolerance as tight as ours (consistent with v8 failing).
- Movement: samples 16 spots 6 m away; wants line of sight only when a weapon is ready, so it ducks out of sight while the gun reloads.
- A fitted logistic "odds" model (HP ratio, cubes, weapon count, busy-ness) decides fights in FFA.
Built as knobs: `selfcomp` (muzzle compensation). Next: reload-duck.

| Sat 3:22 AM (Mac) | `d8eeec4` (exp/v11) | v6 + 145 m with a laser | 5.13 (= v5; neutral) |
| Sat 3:55 AM (Mac) | `d202d4d` (exp/v13) | v6 + selective engagement + giddygiddyboy target scoring/stickiness (Codex round-3 #1) | 4.79 (worse) |
| Sat 4:02 AM (Mac) | `ad0a8b6` (exp/v14) | v6 + giddy-style local fight-position search (Codex round-3 #3) | scoring… |

### Codex round 3
Explained the stall: v5 is a coherent policy, and single-knob edits add costs without changing the plan (v10's slow strafe makes us predictable; v8's loose gate wastes shots while slewing; econ pulls us out of position). Ranked next: (1) selective engagement + giddy target scoring/stickiness, (2) giddy's lose-rule/crowd retreat (we disagree: berserk was our biggest real gain), (3) a 16-candidate local fight-position search, (4) laser juke with a zone guard. It would hold selfcomp/duck/killloot/precharge/wdist as below the 0.2 bar.

Pattern: giddygiddyboy's pieces (select, its tolerance via gtol) don't transplant alone; v5 remains the best at 5.13. Max mode (4:33) plan: run the sim on every variant already scored for real, to measure sim-vs-real agreement before trusting it for screening.
