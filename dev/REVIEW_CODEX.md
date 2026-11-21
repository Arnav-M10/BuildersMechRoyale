# Codex adversarial review: `bot.py` and `geo.py`

Scope: protocol, failure handling, timing on a one CPU runtime, movement/combat logic, and the geometry helpers. I reviewed the copied current files without editing either file. I also ran the repository conformance checker and the eight match simulator run.

## Ranked findings

### P1 — Projectile dodge work has no time bound

`bot.py:898-958` integrates every visible mortar shell in 50 ms increments for up to seven seconds: 140 iterations per shell, with a terrain lookup on each iteration. The same pass also examines every visible bullet. Unlike A* and cover selection, this loop never checks `self.time_left()`.

Concrete failure scenario: several opponents fire mortars into the same late-game area, leaving dozens of visible shells in one observation. On the one CPU match container, this work can consume the 26 ms internal budget and leave too little time for movement selection and JSON output; if it crosses the server's 50 ms deadline, the action is dropped and only coasting remains. This is a worst-case risk, not a failure reproduced in the current checks.

Suggested mitigation: stop projectile analysis with several milliseconds reserved for the rest of `act()`. The companion patch adds checks between projectiles and during long shell trajectories.

### P2 — A skipped path search falls back to steering through blocked terrain

`bot.py:673-684` skips a new A* search when less than 12 ms remain. If there is no cached path, `nav_to()` then returns the straight-line direction to the goal without checking whether that segment is free.

Concrete failure scenario: an observation contains many projectiles, leaving little internal budget; the bot requests a new goal on the other side of a building; A* is skipped and `self.path` is empty. The mech steers into the building until the next route refresh or stuck recovery rather than waiting briefly and retrying the route on the next decision. The companion patch only uses direct movement when `seg_free()` confirms it.

## Checks and protocol status

- `python3 dev/check/check.py --map-dir arena -- python3 -u bot.py` passed: 300/300 answers, no stale or wrong ticks, no invalid lines, no debug cap violations; p99 answer time 0.5 ms and max 3.3 ms on this machine.
- The eight offline matches produced no bot timeouts or invalid actions. The check times are not a substitute for the scorer's one CPU container, so the unbounded projectile loop remains the principal timing risk.
- I found no current schema or message-shape error: the bot echoes ticks, emits a two-boolean `fire`, uses a protocol-valid name, and exits on `end`. `Geo` loads the precomputed cache in the intended Docker setup; the map's expensive build is done at image build time.

