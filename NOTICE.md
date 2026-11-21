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
