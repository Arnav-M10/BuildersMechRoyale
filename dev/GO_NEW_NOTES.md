# Practice-only survival candidates

Both `s1/brain.go` and `s2/brain.go` are complete replacements for the supplied
`scratchpad/gobase/brain619.go`. They share that baseline and use its existing
`b.pracOn` switch; neither changes the non-practice profile. The base imports
and package remain unchanged. These are candidates, not validated score gains.

## S1 — capture safe supply drops

**Change.** `squadParams()` enables `UseDrops`. The existing `chooseLoot()` then
considers announced drops as destinations. Its shared `consider()` filter caps
the trip at 260 m, requires a reachable point inside the current zone, and
checks the zone deadline with a 5 s margin. The drop is considered only if the
walking ETA fits its landing window; ordinary loot mode also requires `!danger`.

**Mechanism and protocol.** Protocol §7, *Supply drops* (lines 356–362), says a
solo capture takes 4 s and grants 3 cubes, full HP, and full energy. Cubes add
max HP and damage up to the stat cap (§10, lines 425–427). This turns a safe
mid-match detour into a large recovery and combat reset. That matters more as
house bots join the hunt and damage interrupts the 5 s / 45 HP/s regen window
(§7, *Health*, lines 291–292). Placement, rather than kills alone, determines
score (§4, lines 94–95).

**Expected gain.** Another bot's reported official practice result moved
5.10→5.46 with drop capture (`dev/IDEAS_OTHERS.md`, citing the source report).
That supports the mechanism, but it is not an A/B on this bot. Estimate for this
candidate: **about +0.10 to +0.35 mean placement points**, low-to-medium
confidence. It is the most evidence-backed candidate here, but is not enough
by itself to establish the required +0.5.

**Risks.** Drop locations are public. A bot can arrive while a drop is
contested, spend time walking there, and delay safer loot or positioning. The
existing `danger` and zone filters limit that risk but do not predict every
house bot's route or inspect `Drop.Holder`.

## S2 — bank health under hunter pressure

**Change.** During practice, the brain counts current visible mechs as likely
hunters when they recently damaged us, aim toward us, or close quickly at short
range. When at least two hunters are present below 82% HP, or at least three
below 95%, and there is no immediate finish, the existing retreat/cover path is
held until recovery. The recovery threshold becomes 90%, or 95% while three or
more likely hunters remain. The practice retreat path can remain active late in
the match; urgent zone travel still has priority. While moving to cover or
making an urgent zone move, it releases rather than continues charging a laser.
During a moving retreat it also clears both fire slots, so the bot can get the
5 s without dealing damage that health regeneration requires.

**Mechanism and protocol.** Practice house bots share vision and add hunters as
the field thins. Protocol §8 (lines 365–374) says shared sightings do not remove
the value of hard cover: buildings block shots, and a seen mech remains tracked
for 5 s. Cover therefore creates a chance to break incoming damage long enough
for §7's 5 s delay and 45 HP/s regeneration (lines 291–292) to restore the HP
bank before the next fight. Protocol §7, *Weapons* (line 305), says laser
charging halves movement speed, so letting go while escaping restores movement
speed. Zone damage ignores armor and prevents regen (§9, lines 409–421), which
is why zone travel precedes this retreat rule.

**Expected gain.** No direct practice A/B exists for this behavior. Estimated
effect: **about +0.10 to +0.40 mean placement points**, low confidence, with
most benefit expected in runs where multiple hunters converge before the bot
has recovered. S1 and S2 may overlap; their ranges should not be added as if
independent. The combined +0.5 goal remains unverified.

**Risks.** Hunter count is inferred from local observations and aim/velocity,
not house-bot intent, so it can retreat too early. Banking health sacrifices
shots, kills, cubes, and drop rewards while retreating. Releasing a charged
laser can fire it at the current aim before it would otherwise reach full
charge. The higher recovery thresholds may oscillate if hunters repeatedly
break cover and resume damage.

## Static review

- Both Go files were copied from `brain619.go`; only the changes described above
  were made.
- The environment has no `go`/`gofmt` executable, so compilation and runtime
  testing were unavailable. The changed identifiers were checked against the
  supplied `brain619.go`, `types.go`, and `world.go` sources. No new imports were
  added.
- No files outside `dev/go_new/` were written, and no Git staging or commit was
  performed.
