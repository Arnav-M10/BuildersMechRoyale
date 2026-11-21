# Kill-per-life experiments

Practice still scores death order, not kills directly (protocol §4). In the practice squad, however, a confirmed house-bot kill usually removes one coordinated hunter and drops cubes, so kills can both move our placement and make later fights easier. The current bot averages about 5.2 kills per life; the requested target is 7+. These are default-off candidates based on mechanics, not a claim of measured score improvement. The sim is only a crash/sanity tool here.

## Implemented knobs

### `g_hunt_early`

For `t <= 120 s`, at least 12 mechs alive, one or two mech tracks seen in the last five seconds, and at most two mechs in the current observation, make visible LOS targets engageable even if they are not yet closing, aiming, weak, or shooting us. Existing giddy fight movement then closes toward its weapon-dependent preference: about 120 m with a laser, 85 m with gun, or 28 m with energized lightning. Three or more current visible mechs disables this extra chase. This tries to remove the first hunter before the squad's pressure grows; it does not override zone emergency or the existing urgent dodges.

Mechanic basis: protocol §7 says gun shots need lead, laser is hitscan after charge, and lightning is short-range/cone-limited. Protocol §4 means removing a house bot can improve placement. The weapon-specific range comes from the already-portable giddy `prefRange`; the early chase itself has no official A/B evidence in the supplied READMEs.

### `g_dmg_up`

Switch giddy offers to the alternate order:

`laser_damage > gun_damage > gun_rate > gun_mag > laser_charge > plating > armor > nanites > remaining upgrades`.

This puts multiplicative attack improvements ahead of defense so an early kill can remove a hunter. Protocol §10 says cube damage scaling multiplies weapon damage. The config says laser damage is +18%, gun damage +15%, and gun cooldown ×0.87 (about +14.9% firing rate). Gun magazine is +8 rounds plus −20% reload; on uninterrupted fire, base 12×0.24 s with 2.4 s reload gives about 54.5% firing uptime, versus 20×0.24 s with 1.92 s reload, about 71.4% after the upgrade. Laser charge is −25%: with base 0.75 s charge and 1.65 s cooldown, a full shot's cycle changes from 2.40 s to 2.2125 s, about +8.5% full-charge throughput. These are idealized mechanical rates; actual accuracy and target availability matter.

Plating is +15% max HP and armor adds 8% damage reduction per stack. Both can extend life against the squad; the offensive-first order is a deliberate snowball hypothesis, not an established optimum. The old giddy order remains the default.

### `g_focus`

Among already-engageable visible targets, rank by estimated time-to-kill plus a small distance cost, with a 1.5-second stick bonus and a recent-attacker bonus. Estimate our available DPS from held weapons and their ranges. When the enemy's torso aim points more than 90° away from us, divide their HP by 1.5 in the estimate, matching the rear-arc damage opportunity in protocol §7. The gun 0.28 hit-rate factor comes from the supplied live-mech data; laser and lightning factors are conservative heuristics, so the ordering should be treated as approximate. This focuses damage into finishes instead of repeatedly switching targets.

### `g_late_hold`

From 130 seconds onward, search no more often than every three seconds for a reachable cover location concealed from recently seen hunters (or recent sound bearings) and inside the next circle. Keep a safe anchor as the circle changes. When no target is visible, route to and hold the anchor; during a fight, bias the existing 16-point movement search toward it while preserving its line-of-sight/peek reward. Zone urgency, shell dodge, retreat, and an uncontested healing-drop diversion still take priority.

This can reduce simultaneous firing angles and give us short peeks, but it cannot force the shared-vision squad to arrive one at a time. Practice houses coordinate, and cover only blocks attacks that geometry actually occludes (protocol §8 and §7). If no safe cover point is found, this knob falls back to the existing giddy policy.

### `g_killloot` (additional lever)

After a recorded kill, consider the death position as a cube pile only when cube pursuit is still enabled, no recent danger is active, no remembered enemy is within 60 m of us or the pile, and the spot is within 170 m and reachable/safe. Protocol §10 says a killed mech drops all its cubes plus two; each cube adds +4% max HP and damage up to the stat cap, but also +5% size without a cap. The existing cube guard limits the risk of oversized, post-cap detours. This reuses the already-recorded `kill_spots` memory that the non-giddy path consumes.

## Other relevant existing behavior

Keep `g_frontarc` available alongside these experiments. Protocol §7 makes hits from outside the torso's 180° front arc deal 1.5× damage. Facing a recent rear attacker protects our life and can improve shot opportunity. I did not add another facing knob because `g_frontarc` already handles attacker-facing priority and idle bearings.

## Verification

The requested fuzz runs used the giddy core, all current structural/combat `g_*` switches, `g_drops=400`, and all five new switches enabled. Seeds 110 and 111 each completed 3,000 mutated observations with zero exceptions; p99 was 11.64 ms and 11.46 ms (seed 111 had a single 32.42 ms maximum outlier). `dev/check/check.py --map-dir arena -- python3 -u bot.py` passed 300/300 observations on time (p99 4.1 ms, max 5.0 ms). A one-match, one-worker simulator smoke run on seed 110 completed without protocol/runner errors (place 14, two kills, death at 62.8 s); that single result is not useful for ranking and was not used to tune the knobs.
