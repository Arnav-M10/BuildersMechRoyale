# Radical practice levers

All four additions are giddy-core-only and default off in `P`. Each is isolated so Claude can test or discard it independently. They aim to increase kills before the subject dies; they do not change the practice squad rules.

## Protocol mechanisms

- **`g_lure_npc`** — §7 says drones shoot “at whoever attacks them, or at mechs within 45 m with line of sight while the drone is within 30 m of home.” When a recent hunter's straight pursuit line crosses a drone's 45 m envelope, this points our retreat farther outward on the opposite side. It requires the bot to remain more than 76 m from the observed drone, keeps the next 22 m inside both circles, and checks the predicted hunter crossing for LOS. The protocol does not expose drone homes; the bot therefore only treats an NPC's first position seen by t=40 s as a tentative home and requires the drone to remain within the configured leash. This is a conservative proxy, not exact home knowledge.
- **`g_zone_trap`** — §9 says zone damage ignores armor. During the shrink window, it caches a reachable point just inside the next circle, opposite the average direction of recent hunters, and prefers cover cells with less hunter LOS. Urgent zone routing still takes priority. The goal is to make pursuers approach across the exposed side of a shrinking ring while we stay safe; the sim shows this positioning can cost our own kills, so leave it off unless live evidence supports it.
- **`g_choke`** — §7 says walls block gun, mortar, and laser shots; §8's 120° yaw wedge and LOS rules make cover geometry matter. It searches nearby cover cells for a reachable position with a shot lane to one recent hunter, favoring less LOS exposure to other hunters, then keeps the giddy fight search near that point. The search is cached for six seconds and considers at most 100 candidate cells.
- **`g_drop_bait`** — §7's supply-drop rule says the capture clock advances only while a mech is alone in the 8 m ring for 4 s; contested capture freezes. If an observed enemy is the active holder and is in the ring, the target scorer forces engagement and gives it a large priority bonus. This exploits a predictable stationary target and can interrupt its full-HP/full-energy/three-cube reward. The current sim house bots did not capture a drop in these 48 seeds, so the trigger remains unverified there.

## Paired simulator A/B

Each row ran 48 squad matches with seeds 6000–6047 and six workers. The baseline was exactly `core=giddy`, `g_nav`, `g_cover`, `g_stuck`, `g_frontarc`, and `g_cube_guard`. Each variant added only the named knob. “Score” is the runner's mean `16 - place`; mean place is shown too. Trigger counts come from `MF_TRACE` debug-label samples, logged every 0.5 s; “matches” is the number of per-match trace files with at least one matching label.

| Variant | Mean score | Mean place | Mean kills | Triggered matches / trace samples |
| --- | ---: | ---: | ---: | ---: |
| Baseline | 5.479 | 10.521 | 4.562 | — |
| `g_lure_npc` | 6.125 | 9.875 | 4.958 | 9 / 112 (`npc-lure`) |
| `g_zone_trap` | 4.833 | 11.167 | 3.729 | 38 / 4,347 (`zone-trap*`) |
| `g_choke` | 4.458 | 11.542 | 3.708 | 44 / 3,612 (`choke-*`) |
| `g_drop_bait` | 5.479 | 10.521 | 4.562 | 0 / 0 (`drop-bait-fight`) |

These are simulator-only signals, not practice-score predictions. In particular, the lure batch had 10 missed actions across its 48 matches versus 1 in baseline (the runner keeps the prior continuous action on a miss); the other batches had 0 zone, 0 choke, and 3 drop-bait misses. Six workers put enough load on the host for deadline noise, and this sim is not a reliable combat-ranking proxy. The drop-bait result is a no-op because no holder was observed. I would keep every knob off until real-server draws establish a gain; the lure is the only one that merits a focused live experiment from this A/B.

## Verification

- `MF_P` with `core=giddy`, every existing boolean `g_*` switch enabled, `g_drops=400`, and all four new switches enabled: `python3 dev/fuzz.py 120` — 0 errors, p99 13.85 ms (max 34.85 ms; requirement was p99 below 25 ms).
- Same `MF_P` with `python3 dev/check/check.py --map-dir arena -- python3 -u bot.py` — PASS, 300/300 observations answered, no missed 50 ms deadlines, p99 4.6 ms.
- A/B runs used `python3 dev/sim/run.py --matches 48 --seed 6000 --jobs 6 --no-validate -- python3 -u bot.py`; generated observations were separately covered by the schema check above.
