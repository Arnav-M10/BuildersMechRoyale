# Practice levers from other teams

I screened all 156 README files in the supplied `scan2` corpus. This ranks the concrete practice changes with the clearest reported official-score evidence. Official results are noisy 48-match draws; simulator and replay metrics are identified separately and are not treated as official placement gains.

## Ranked levers

1. **Capture an uncontested supply drop for recovery and cubes.** JishnuMadav reports an official increase from **5.10 to 5.46** after adding supply-drop capture (+0.36). The selected version follows the announced drop, reaches it after landing, and takes the full-HP/full-energy/three-cube reward. Hiresh's Outlast also reports rising from 3.81 to a later 4.04 entry while adding drop/cube/recovery logic, but that revision bundled changes and is not an isolated causal comparison. This is the strongest portable survival lever. Sources: `JishnuMadav_mech-royale.md`, `Hiresh2007_buildathon-mech-royale.md`.

2. **Treat rounded laser charge as full at the displayed rounded value.** Jishnu reports **5.46 to 5.73** (+0.27) for the charge-rounding fix. The observation can say `0.56` when the configured full charge is `0.563`; a strict `charge >= full_charge` test never recognizes that observation as full and can hold until the automatic release. The relevant code should use a tolerance derived from two-decimal rounding, not a large arbitrary margin. Source: `JishnuMadav_mech-royale.md`.

3. **Release an aligned partial laser when its conservative damage is lethal.** Jishnu reports **5.73 to 5.81** (+0.08). This avoids holding a lethal beam longer while the target moves or the hunter closes. It is already represented in this bot's giddy path by `kill_now`, alignment checking, and `g_lethalk`; preserve that behavior when changing laser gates. Source: `JishnuMadav_mech-royale.md`.

4. **At critical HP, stop fighting and recover to 60%; avoid charging outside the safe circle or when rotation is urgent.** Jishnu's selected **5.85** followed its 5.81 runtime with both changes, so the combined official gain is **+0.04** (the two effects are not separated). Charging halves movement speed; outside-zone damage prevents healing and restarts the five-second regen timer. Release an existing charge when movement urgency takes priority. Source: `JishnuMadav_mech-royale.md`.

5. **Farm crates before 150 seconds when the route stays safe.** LanceHuang245 reports the farm variant at **4.88 vs 4.75** (+0.13 official). Its simulator estimate was only +0.17 ±0.28 over 288 pairs, so the real draw is the useful evidence; the improvement is modest and uncertain. Source: `LanceHuang245_mech-royale.md`.

6. **Collect confirmed death-drop cubes.** Hiresh reports the build following confirmed eliminations for dropped cubes at **4.69 vs the previous 4.63** (+0.06 official). This is a small result and the README describes the overall controller, not a controlled single-knob submission. Cubes add max HP and damage, but also grow the hitbox, so use reachable, uncontested drops. Source: `Hiresh2007_buildathon-mech-royale.md`.

7. **Use cover and weapon-aware firing as combined basics, not guaranteed score deltas.** Sphugne's staged sequence reports official practice scores of 0.88 (movement), 1.60 (combat/lead/loot), 1.90 (cover), 1.96 (evasion), 2.06 (corrected isolation), 2.08 (navigation), 2.13 (rear-angle positioning), and 2.19 (aim-timed gun trigger). This supports the package, but the changes are cumulative and do not isolate each feature's marginal effect. Source: `sphugne-mech-royale-bot.md`.

## Useful counterevidence

- Lance's patient/hold-fire-and-hide variant scored **4.04 vs 4.54** officially (−0.50). Staying passive at range can cost placement if it leaves hunters alive or sacrifices useful fights.
- Lance's gun-plus-laser/skip-lightning variant scored **4.69 vs 4.88** officially (−0.19), despite favorable practice-sim results. Weapon-choice simulator rankings are not reliable evidence by themselves.
- Jishnu's later official variants also show regressions: more aggressive growth 5.73→5.67, faster evasion 5.67→5.17, wider laser spacing 5.71→5.65, and earlier squad cover/zone travel 5.65→5.48. The README records these as sequential submissions, not controlled one-variable deltas; do not transplant their parameter changes blindly.
- Jishnu's table also lists a "laser retreat from lightning" entry at 5.71. The README does not provide a paired preceding score for that specific change, so it is a useful hypothesis but not a defensible effect estimate. Shenoy's scouting entry likewise reports a 5.17 official result but attributes its larger comparison to local simulation, not a paired official baseline.
- Hiresh's movement/exposure/navigation work has substantial replay and local-model gains, but its own README notes that the referee can rank local policies differently from the official squad. Treat modeled placement changes as hypotheses only.

## Mechanic implications

- **Drops are a timed objective.** Protocol §7 says three drops are called at 70/140/210 seconds and land 25 seconds later. Stand alone inside the 8 m ring for four seconds. Two or more mechs freeze capture progress, and a change in the solo holder resets it. They restore HP and energy and grant three cubes. A visible contest check and a zone/deadline check are necessary; merely routing to the announced location is not enough.
- **Charging has a survival cost.** Protocol §7: a charging laser halves movement speed. Protocol §9: zone damage ignores armor and prevents health regeneration; it also resets the five-second no-damage timer. Zone travel must override charging.
- **Facing matters.** Protocol §7 gives 1.5× damage when the hit comes from outside the torso's 180° front arc. Aim yaw is the facing direction. Retreat and strafe choices should keep recent attackers in the front arc where feasible.
- **Hiding can break local contact, not shared practice awareness.** Protocol §8 gives 45 m all-direction vision, then 200 m inside a 120° yaw wedge with line of sight, plus five seconds of memory for a seen mech's true position. Hearing extends farther (for example, laser 380 m and boost/jet 90 m). Practice house bots share sightings, so one bot spotting us can keep the squad oriented; geometry still blocks direct shots and breaking all sightlines permits regen.
- **Growth is a trade.** Protocol §10 gives each cube +4% max HP and damage up to the stat cap, while size grows 5% per cube without a cap. Early useful cubes improve duels; oversized late mechs are easier to hit. Prioritize drops and loose cubes only when the route/capture is viable.
- **Placement is survival order.** Protocol §4 awards `16 - place`; kills do not directly add score. A kill can still improve placement by removing a hunter and dropping cubes, but starting an unnecessary fight risks dying sooner.

## Applied here

The patch adds default-off `g_dropheal`, `g_zonelaser`, and `g_chargefix` knobs. It also adds `g_critical_recovery`, porting the official critical-HP recovery component as a separate default-off switch. The giddy path already had aligned lethal partial releases, so that proven behavior was retained rather than duplicated.
