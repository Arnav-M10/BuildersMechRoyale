# Notices

The optional `core="giddy"` practice core in `bot.py` (and its `g_*` knobs) is an independent Python
re-implementation of decision rules and parameter values from **giddygiddyboy-mech-royale**
(github.com/justnau/giddygiddyboy-mech-royale, Go, MIT License), studied while that repository was public
on 2026-11-21. No Go source is included here. Credit for those rules belongs to its author.

Ideas were also read (not copied) from other public MIT-licensed Mech Royale repositories.

## Branch exp/giddy-go
This branch's root Dockerfile builds the Go bot in `mech/` + `cmd/bot/`. The `mech/` package is
giddygiddyboy-mech-royale's code (MIT, by justnau), unmodified; `cmd/bot/main.go` is our own
stdin/stdout wrapper. See LICENSE-giddygiddyboy.
(Branch exp/go-giddy3 uses the ~11 AM 2026-11-21 snapshot of the same MIT package.)

## Branch exp/go-619
giddygiddyboy's MIT `mech` package with the squad settings used by the 6.19 copies
(HPw 0.18, MaxTarget 200), and giddygiddyboy's original MIT cmd/bot/main.go wrapper
(bot name changed). Submitted with the user's explicit approval.
