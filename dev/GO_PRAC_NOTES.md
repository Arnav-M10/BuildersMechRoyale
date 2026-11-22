# Practice-only candidates for `brain619.go`

Both files are complete replacements for the supplied `gobase/brain619.go` and are independent A/B candidates. They share the baseline policy; `a` changes only practice drop selection and `b` changes only cube detours while fighting under multi-attacker pressure. The practice guard is the existing `b.pracOn`/`squadParams` split; FFA behavior is unchanged.

## A — allow safe supply-drop capture

- `a/brain.go:1974` enables `UseDrops` inside `squadParams`; no other profile sets it.
- The existing chooser at `a/brain.go:1692-1694,1754-1759` only considers a drop if the walk ETA fits its landing time, and outside the early econ branch it also requires `!danger`. Shared `consider` checks at `a/brain.go:1600-1614` bound the trip to 260 m, the current circle, and the zone deadline. This does not blindly chase every drop.
- Mechanism: capture grants full HP and energy plus three cubes, which improves survival and damage before the next hunter joins. This is especially valuable after the first duel, when the squad has not yet ramped up.
- Evidence: JishnuMadav reports official practice moving 5.10→5.46 with supply-drop capture (`README.md:98-100`). This is one result on another implementation, so it supports the direction but does not guarantee the same gain on this bot. Expected effect here: positive, plausibly around +0.1 to +0.35; confidence medium-low.

## B — stop direct cube runs once two attackers are active

- `b/brain.go:612-622` counts distinct known mechs that are currently marked to engage or that hit us in the last three seconds.
- `b/brain.go:781` keeps the existing practice FightLoot behavior against one attacker, but skips its direct route-to-cube override when `pressure >= 2`. The normal `fightMove` still retains its small nearby-cube preference (`b/brain.go:1395-1399,1448-1451`). Outside practice the original condition runs unchanged.
- Mechanism: `squadParams` sets `FightLoot=70` (`b/brain.go:1964`), and that override replaces the fight movement with a direct cube route (`b/brain.go:781-797`). A cube detour can turn the torso away from the active fight and sacrifice strafing/line of fire just as a second hunter arrives. The gate preserves the chance to snowball from cubes in the one-hunter phase, but favors combat movement once two attackers are active.
- Evidence and expected effect: this is a structural hypothesis tied to the practice escalation schedule, not an established official gain. Cube collection has helped some reported runs, while separate cube-greed tests were noisy. I expect a small positive mean if multi-attacker cube detours are common, with occasional larger avoided losses; confidence low. I cannot honestly promise +0.3 for this variant without a real A/B.

## Selection and validation notes

The strongest measured candidate is A. I chose B over changing laser/upgrade dials because this base already has laser-first upgrades, laser seeking, and lethal partial release enabled in its practice profile (`squadParams` at lines 1952-1977); the reported successful laser changes are already substantially present. The supplied test environment has no Go compiler, so I did not claim a compile or runtime test. Both complete files were copied from the named base and checked to contain only their described edits.
