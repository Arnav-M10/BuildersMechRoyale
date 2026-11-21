# Overnight log (Sat 2026-11-21)

Best real practice score so far: **3.81** (v2 `b5a3086`, v3 `2bbdf12`). Main plays like v3 with every new behaviour off by default.

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

## Submissions
None overnight (no browser control without a permission prompt). The hash to submit will be listed at the top here when one wins.
