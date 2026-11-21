# Go FFA brain variants

Each `brain.go` is a complete replacement for `package mech`'s `brain.go`.
Copy one file over the final Go package's `mech/brain.go` to evaluate that
variant. The underlying source is `scratchpad/gobase/brain.go`; the shared
target selection is around lines 495–601 there, and movement priority is
around 682–735.

Every strategy branch requires `!b.pracOn` through `ffaOnly()` and also
requires the existing classifier to have opted out (`b.notPractice`) or the
base params to have practice disabled. This extra check matters because the
existing classifier leaves `pracOn` false during its first 20 seconds. It
prevents the variants from changing a practice opening before squad mode is
latched. No combat or movement code is changed while `pracOn` is true.

## Variants

### v1_lurk

- `brain.go:11–14` enables only the lurk strategy.
- `brain.go:368–370` defines the FFA-only guard.
- `brain.go:585–587` suppresses target engagement while `t < 90` and more than
  9 mechs are alive unless the target hit us in the last 4 seconds or is within
  25 m. All other target and movement behavior remains the base behavior.

### v2_vulture

- `brain.go:11–14` enables only vulture behavior.
- `brain.go:261–270` records visible mech HP changes in FFA while the target is inside the current zone (to avoid treating zone damage as third-party combat). `brain.go:335–347`
  uses our `hit` events to distinguish our own damage and clears dead targets.
- `brain.go:396–413` marks a target as third-party engaged if another currently
  observed mech is within 70 m, or its HP recently fell without a recent hit
  from us.
- `brain.go:627–635` lets us engage third-party fights, but suppresses a
  non-retaliatory 1v1 against a >=99%-HP target while more than 6 mechs remain.
  `brain.go:652–654` gives third-party targets a 90-point selection bonus.

### v3_endgame

- `brain.go:11–14` enables only endgame behavior.
- `brain.go:585–589` permits engagement at 4 or fewer alive only when the
  target hit us in the last 4 seconds, or has under 30% HP while our HP fraction
  exceeds theirs by more than 15 percentage points.
- `brain.go:701–704` computes the safe interior of the next circle.
  `brain.go:719–737` moves there before other tactics when outside it; with no
  eligible target, it seeks cover within that circle or holds position there.
  Existing escape, unstuck, and shell-dodge actions remain higher priority.

### v4_all

- `brain.go:11–16` enables all three strategies.
- The target filters at `brain.go:630–649` run in vulture, lurk, then endgame
  order. The early 25 m defense exception is retained through the vulture
  filter; the endgame gate is applied last and takes precedence at 4 alive.
- The endgame circle and cover handling is at `brain.go:764–798`.

## Validation note

The four files have balanced Go braces/parentheses and retain the base imports
(`math`, `math/rand`, `sort`); no new imports are needed. `go` and `gofmt` are
not installed in this environment, so I could not run the compiler. The
variants were kept as direct edits to the supplied base file, with no protocol
or package/API changes.
