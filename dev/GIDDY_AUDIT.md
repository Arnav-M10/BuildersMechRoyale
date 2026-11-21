# Giddy3 fidelity audit

Compared `giddy3/brain.go`, `world.go`, and `types.go` with the synced Python bot and `geo.py`. The three new behavior switches are off by default; the existing giddy tuning defaults were updated to the values you supplied because the synced `bot.py` still had the earlier `.1 / .8 / True` values.

## Ranked gaps by likely practice impact

1. **The synced giddy parameters were stale.** `giddy3/squadParams` ends at `HPw=.21` and `LethalK=.7` (`brain.go:1952-1977`), and it no longer sets `FightBoost` (`brain.go:774`). The synced Python file had `g_hpw=.1`, `g_lethalk=.8`, and `g_fb=True` (`bot.py:25-28`). That makes it less selective for low-HP targets, delays some partial laser releases, and spends energy boosting during fights. I corrected the defaults. They still accept `MF_P` overrides, and these values only affect the giddy core.

2. **Cover search used straight-line distance and a prism-only candidate list.** Go starts a bounded BFS at the nearest walkable cell, tests up to 350 reachable cells, and scores path length, zone risk, and threat distance (`brain.go:1475-1575`). Python searched `geo.cover_near()` candidates and scored Euclidean distance (`bot.py:1856-1895`); it could choose a hidden point across a wall, then take an exposed or failed route. `g_cover` enables a bounded BFS over reachable nav cells with the source scoring terms and cube-scaled enemy eye height. It defaults to `False`.

3. **The path follower and isolated-region escape were only approximate.** Go uses 45 m for direct walking, refreshes paths every 2.5 s, expands up to 22k nodes, reanchors to the closest of the next ten path cells, and weights narrow clearance (`brain.go:1297-1353`; `world.go:393-440, 590-670`). Python used a 70 m direct threshold, 2.5k nodes, a 4 s refresh, and no numeric clearance cost (`bot.py:1150-1216`; `geo.py:165-210, 451-517`). Go also builds an escape launch/landing plan for off-main regions and follows roof height while jetting (`world.go:714-807`; `brain.go:1131-1176`); Python's existing climb fallback jets toward the requested goal. `g_nav` enables the 45 m threshold, 8k-node budget, 2.5 s refresh, path reanchoring, and a persistent nearest-main-region escape target. It defaults to `False`.

   This is deliberately bounded rather than a literal port: Python's `Geo` stores binary walkability with a 3 m clearance margin, not Go's distance-to-obstacle raster, so `g_nav` still lacks Go's narrow-corridor cost and its exact `EscapePlan` roof-height landing logic. A full match to Go needs a clearance field and a dedicated takeoff/landing planner; the 22k Go expansion cap should not be copied into Python without timing measurements.

4. **Stuck recovery starts later in the existing Python code.** Go reacts after a grounded mech commands movement while staying below 1.2 m/s for 0.7 s; it picks a random direction, jumps, and clears its path (`brain.go:868-883`). Python used 15 observations (about 1.4 s), a 1.5 m/s threshold, and then a longer boost escalation (`bot.py:1542-1577`). That delay leaves a wedged mech exposed longer. `g_stuck` enables an eight-observation grounded check, a random escape direction, a 0.5 s attempt, and no extra stuck boost. It defaults to `False`.

5. **Laser charging starts more cautiously in Python.** The gun lead and nominal aim tolerances match: both use 75 ms latency, four lead iterations with `(distance-3)/speed`, half vertical-velocity extrapolation in air, and the same radius-based gun/laser alignment thresholds (`brain.go:957-1035, 1201-1217`; `bot.py:2057-2193`). But Go begins charging an in-range visible target before its aim is aligned (`brain.go:997-1035`); Python requires yaw error below 0.6 rad and checks 3D muzzle distance before starting (`bot.py:2160-2190`). Against an elevated target near laser range while the gun lead is still slewing, Python can start charge later. The release tolerances themselves match. I left this lower-ranked behavior unchanged pending shot-log evidence; a concrete optional fix is to add a default-off `g_laserpre` switch that uses Go's horizontal range/LOS gate for starting charge.

## Other requested checks

- **Target/upgrade logic:** engagement conditions and target-score terms match the squad path (`brain.go:525-590`; `bot.py:690-723`). The laser upgrade rank also matches `upgradeOrderLaser` with `LaserUp=true` (`brain.go:448-490`; `bot.py:144-149, 581-590`). The stale HP weight was the notable mismatch.
- **Memory, hurt bearing, and zone:** Go retains mech tracks for 20 s; combat selection only uses tracks up to 2.5 s stale, and neither implementation extrapolates hidden mech coordinates (`brain.go:232-320, 525-535`; `bot.py:362-393, 667-690`). Damage bearings are updated under the same event conditions (`brain.go:298-316`; `bot.py:435-450`). The zone deadline samples the same 25 points (`brain.go:361-392`; `bot.py:1825-1854`). A small difference: Python drops NPC tracks after 10 s, while Go retains them for 120 s; Go only considers old NPCs for farming within a 12 s window (`brain.go:270-273, 1746-1750`; `bot.py:391-394`). Extending Python retention to 120 s would close that 10–12 s edge case.
- **`types.go` fields:** `rear_mult` and `front_arc_deg` (`types.go:37-38`) are used only by giddy3's new backstab code (`brain.go:587-588, 1012-1014, 1988-2001`). `Backstab` is not enabled by `squadParams`, so this has no effect on the supplied practice configuration. Python does not read those fields. `near_range_bonus`, `vision_bonus`, and `hearing` (`types.go:39-41`) are not referenced by the giddy brain/world code, so they are not omitted decision inputs.

## Verification

- `MF_P='{"core":"giddy"}' python3 dev/fuzz.py 80`: 0 errors; p99 2.72 ms, max 3.90 ms.
- With `g_nav`, `g_cover`, and `g_stuck` enabled together: 0 errors; p99 5.01 ms, max 11.22 ms on this machine.
- `MF_P='{"core":"giddy","g_nav":true,"g_cover":true,"g_stuck":true}' python3 dev/check/check.py --map-dir arena -- python3 -u bot.py`: 300/300 observations answered, no 50 ms misses, PASS (p99 2.7 ms, max 2.8 ms here).
- Focused off-main-cell smoke with the same options selected `escape climb` as intended.

No practice-match sweep was run; these changes are a fidelity patch, and the optional branches still need score comparison before being enabled for submissions.
