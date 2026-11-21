# Giddy practice core

Line references below refer to the supplied `giddy2/brain.go`.

Set the offline knob with `MF_P='{"core":"giddy"}'`. The default remains
`P["core"] = "ours"`. The giddy path uses the existing protocol loop and map
navigation, and yields to the existing FFA layer after it latches.

## Ported practice rules

- **Squad profile:** `Ghost=false`, `Peek=false`, `LaserFirst=true`, laser range
  120, loot radius 150, crate farming through the match, no supply drops,
  mortar pickup disabled, NPC farming enabled, and the squad target/retreat/
  finish/boost values are from `squadParams` and `DefaultParams`
  (`brain.go:146-159, 1940-1964`).
- **Engagement and target choice:** engage inside 60 m, after being hit within
  6 s, when closing faster than 3 m/s inside 180 m, when below 40% HP inside
  160 m, when aimed at inside 190 m, or in late game. Select visible targets
  within 240 m with `distance + 0.1*HP`, the 200 point low-HP and stick bonuses,
  40 for recent attackers, and 15 for closing targets (`brain.go:523-594`,
  `brain.go:1940-1962`). Closing speed is relative to our mech.
- **Retreat and finishing:** the 0.38 retreat and 0.8 recovery thresholds,
  3/2-person crowd thresholds, laser outgunned check, 1.5 s LoseRule window,
  late/finishing exceptions, Finish 20 m at 38% target HP with at most two
  nearby threats (`brain.go:605-657, 730-789`).
- **Weapon range and fight movement:** preferred distances are gun 85, laser
  120, lightning 28 when energy is above 12, NPC 58, and at least 64/80 against
  enemy lightning/mortar when we lack lightning (`brain.go:1343-1365`). Movement
  scores staying and 16 walkable points 6 m away for range, LOS when a weapon is
  ready, crossfire, zone safety, nearby cubes, clearance, and strafing
  (`brain.go:1367-1461`). Fight boost starts above 40 energy except while
  charging a laser or below 60 with lightning; FightLoot pulls to cubes within
  70 m, reduced to 39.9 m at or below half HP (`brain.go:765-789`).
- **Zone timing:** the source's 1.45 walk-time estimate, 8 s zone deadline pad,
  3 s must-move lead and 14 s should-move lead, center/margin zone goal, and
  current/next-zone position penalties (`brain.go:359-420, 677-682`).
- **Loot and weapons:** LaserSeek 240, LaserFirst slot preference, gun retention,
  and take/drop rules (`brain.go:1166-1187, 1093-1112`). Loot is ranked by
  `value/(distance+18)` with a 1.35 sticky multiplier: pickups 60 (130 for an
  empty second slot, 150 for laser), cubes 75 or 25 at the stat cap, crates 60%
  of cube value, and NPCs 85. Pickups/cubes/crates/NPCs use 150/105/150/150 m
  radii, subject to the source's 260 m, zone-deadline, height, and main-component
  filters. Crates and NPCs require HP above 50%, with crates also requiring no
  danger. Failed destinations are skipped for 60 s after 30 s of pursuit
  (`brain.go:1579-1762`, `brain.go:1940-1964`).
- **Aim, fire, and reload:** 0.075 s latency, four-pass gun lead with
  `(distance-3)/speed`, direct hitscan aim for lightning and charged lasers,
  gun slew tolerances, lethal partial laser release, full-charge aligned
  release, and overtime release. Idle facing prioritizes an engaged/recent
  attacker, NPC hurt bearing, mech hurt bearing, then movement plus the source
  scan sinusoid (`brain.go:880-1086, 1189-1205`).
- **Dodge and cover:** mortar-impact trajectory dodge, projectile closest-point
  dodge, and lateral dodge against a charging laser (`brain.go:1240-1283,
  1836-1884`). Retreat cover checks current hiding then searches nearby cover
  while penalizing zone danger (`brain.go:1463-1569`).
- **Upgrades:** LaserUp order is plating, armor, laser damage, laser charge,
  nanites, gun damage, gun rate, then the remaining source order; unknown offers
  sort after known choices (`brain.go:446-488`, `brain.go:1955-1962`).

## Limits of the port

- `geo.py` does not expose the source's clearance raster or its exact weighted
  grid BFS. Fight clearance is estimated from 1 m free-space rings; cover uses
  `cover_near` candidates and Euclidean distance to approximate the source's
  bounded BFS. Navigation itself uses this bot's `nav_to`/A*.
- The source includes a mortar ballistic aiming branch (`brain.go:1024-1040`),
  which is not reproduced. The squad profile sets `NoMortar=true`, so the port
  will not pick up a mortar; a pre-equipped mortar would remain unused.
- The port uses this bot's observation-memory pruning and Python RNG rather
  than the source's Go map iteration/RNG. Its practice decisions and thresholds
  are ported, but individual strafe/loot tie sequences are not expected to match.
- The FFA implementation is not in `giddy2`; the existing `ffa_*` latch and
  targeting/movement rules remain active on top of the giddy core.

## Validation

- `MF_P='{"core":"giddy"}' python3 dev/fuzz.py 70`: 0 errors; p99 2.68 ms.
- `MF_P='{"core":"giddy"}' python3 dev/check/check.py --map-dir arena -- python3 -u bot.py`:
  PASS, all 300 replies on time, none late (p99 0.7 ms on this machine).
- `MF_P='{"core":"giddy"}' python3 dev/sim/run.py --matches 16 --seed 20261121 --jobs 2 --no-validate -- python3 -u bot.py`:
  mean score 6.875, histogram `1:3 2:0 3:0 4:1 5:0 6:2 7:0 8:0 9:0 10:1 11:2 12:2 13:1 14:3 15:0 16:1`;
  house gun hit rates were easy 15.6%, medium 14.3%, hard 14.5%, expert 19.9%.
  This is only a runner sanity check, not a combat-ranking estimate.
