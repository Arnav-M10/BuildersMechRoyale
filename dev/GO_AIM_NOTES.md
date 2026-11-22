# Gun accuracy changes

`brain.go` is a complete `package mech` replacement based on
`scratchpad/gobase/brain619.go`. The three switches are separate `Params` bools
and are enabled in both `DefaultParams()` and `squadParams()`.

## FireGate

- `brain.go:160–162`, `165–170`, and `2047` add and enable `FireGate`.
- `brain.go:1259–1289` factors the original four-iteration lead calculation
  into `leadImpact()` and `leadPoint()`. `lead()` now derives its angles from
  that same predicted point.
- `brain.go:991–1014` computes the gun impact point and checks
  `b.W.Clear(muzzle, gunPoint)` for mech targets. `brain.go:1045–1048` applies
  the result only to gun firing. Laser, lightning, mortar, NPC and crate shots
  retain their existing behavior.

## LeadDamp

- `brain.go:19–22` adds previous horizontal velocity and a per-track list of
  reversal times.
- `brain.go:261–279` counts direction changes greater than 60 degrees only
  when both horizontal speeds exceed 3 m/s, samples are at most 0.5 s apart,
  and prunes reversals older than 4 s.
- `brain.go:997–1010` computes `x = 2 * (reversals / 4 s) * flight_time`, then
  `k = (1 - exp(-x)) / x` (`k = 1` for tiny `x`). It scales the target velocity
  before producing both gun aim and the impact point. The reversal measure is
  horizontal; the resulting factor scales the full lead velocity vector.

## AimGate

- `brain.go:160–162`, `165–170`, and `2047` add and enable `AimGate`.
- `brain.go:84–85` and `203–220` track whether a valid prior aim command
  exists, so the zero-value `[0,0]` at startup cannot earn turn credit.
- `brain.go:1027–1040` retains one tick of turn/pitch credit only when that
  prior command was already within the new point's yaw and pitch tolerances.
  Otherwise it requires the current aim to be within tolerance without credit.
  This `slew` closure is used for the gun alignment test at `brain.go:1047`.

## Static validation

The replacement keeps the base package and imports unchanged. I checked that
the added identifiers exist in the supplied `brain619.go`, `world.go`, and
`types.go`, that all three params are initialized in both parameter paths, and
that delimiters balance. Go is unavailable in this environment, so I could
not run `go test`, `go build`, or `gofmt`.
