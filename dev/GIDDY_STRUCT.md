# Giddy structural port

The giddy practice core now has four opt-in knobs, all defaulting to `False`: `g_clear`, `g_escape`, `g_laserpre`, and `g_npcmem`. They are scoped to `core="giddy"`; the existing default core and its movement behavior are unchanged.

- `g_clear`: `geo.py` caches a four-connected distance-to-blocked-nav raster capped at 20 m. `Geo.astar(..., clearance=True)` applies the source's `<4 m` edge penalty and 1.15 weighted-octile heuristic. The Python path search is capped at 4,000 expansions with the existing decision deadline (Go allows about 22k). Cache version is 8.
- `g_escape`: adds step-height-aware nav components for escape decisions, caches a 20-cell `MainNear` raster, and plans launch/landing points with the source BFS scoring. It waits for 70%/70 energy, jumps at launch, steers toward landing, and boosts to clear the highest surface along the next 30 m. Failed plans are cached for 6 s.
- `g_laserpre`: for a visible mech inside horizontal laser range, starts a ready laser charge before aim alignment. The old alignment gate is unchanged when off.
- `g_npcmem`: retains NPC tracks for 120 s; the giddy farm selector still only farms tracks seen within 12 s.

## Fidelity limits

The Go escape search allows 60k expansions; this Python port caps it at 10k to protect the 50 ms protocol budget. A very remote off-main region can therefore have no plan until its search neighborhood reaches `MainNear`; failed plans retry after 6 s. Ground nav step heights use terrain samples because this geometry cache does not retain Go's per-prism bridge/deck classification. Airborne roof following uses `Geo.surface`, which finds the highest mapped prism surface but can differ from Go's prism-kind-aware `TopAt`. These are the main remaining escape differences.

## Verification

- Fresh `cache.pkl` build: 0.54 s, 12 MB (under the 60 s / 20 MB limits).
- `MF_P='{"core":"giddy","g_nav":true,"g_cover":true,"g_stuck":true,"g_clear":true,"g_escape":true,"g_laserpre":true,"g_npcmem":true}' python3 dev/fuzz.py 90`: 0 errors, p99 6.40 ms, max 13.55 ms.
- Same command with seed 91: 0 errors, p99 6.38 ms, max 14.22 ms.
- Same `MF_P` with `python3 dev/check/check.py --map-dir arena -- python3 -u bot.py`: PASS; 300/300 responses on time, response p99 3.0 ms.
