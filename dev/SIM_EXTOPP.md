# External-opponent FFA simulator

`dev/sim_extopp.patch` applies to `../BuildersMechRoyale/dev/sim/`. The normal no-flag squad runner remains the default. `--ffa` now means one supplied subject plus house bots in unused seats; `--ffa-self` preserves the old 16-copy mode, and `--ffa-mixed` is unchanged.

## What changed

- `--real-config PATH` reads the first hello from JSON, JSONL, or JSONL.GZ and uses its `config` for every match. The protocol header and map file list remain the local contract; `map.dir` is always the absolute worktree `arena/` path.
- `--opp-cmd CMD --opp-count K` runs K independent opponent subprocesses in slots 1..K. Unused seats use independent FFA house bots that roam/loot and retaliate against their own recent attacker. K must be 1–15; K=0 is the `--ffa` house-only mode.
- FFA processes all get their observation before response collection and share the same tick deadline. A bounded asynchronous stdin writer and nonblocking response polling keep a stalled slot from blocking the other slots. A dead or crashed bot is stopped as soon as its place is final.
- Direct `docker run` commands receive a unique per-match/per-slot container name. `finish()` sends `end`, waits briefly, then uses `docker rm -f` and a process kill as bounded cleanup. Docker's `--rm` remains in effect.
- External FFA output reports subject place, kills, death time/cause, mean `16 - place`, opponent mean place (all 15, and external seats separately), wall seconds per match, timeouts, and invalid actions.

## Commands for the Go image

The runner replaces the literal `<ROOT>` in both the subject command and `--opp-cmd` with this worktree's absolute path. Use the same Docker command for subject and opponents:

```sh
python3 dev/sim/run.py --matches 1 --seed 6000 --jobs 1 \
  --real-config dev/check/stream.jsonl.gz --ffa -- \
  docker run --rm -i --network none --cpus 1 --memory 512m --read-only \
    --user 65534:65534 -v "<ROOT>/arena:<ROOT>/arena:ro" mf-go-giddy3
```

```sh
python3 dev/sim/run.py --matches 4 --seed 6000 --jobs 2 \
  --real-config dev/check/stream.jsonl.gz --ffa --opp-count 15 \
  --opp-cmd 'docker run --rm -i --network none --cpus 1 --memory 512m --read-only --user 65534:65534 -v "<ROOT>/arena:<ROOT>/arena:ro" mf-go-giddy3' -- \
  docker run --rm -i --network none --cpus 1 --memory 512m --read-only \
    --user 65534:65534 -v "<ROOT>/arena:<ROOT>/arena:ro" mf-go-giddy3
```

```sh
MF_P='{"core":"giddy"}' python3 dev/sim/run.py --matches 4 --seed 6000 --jobs 2 \
  --real-config dev/check/stream.jsonl.gz --ffa --opp-count 15 \
  --opp-cmd 'docker run --rm -i --network none --cpus 1 --memory 512m --read-only --user 65534:65534 -v "<ROOT>/arena:<ROOT>/arena:ro" mf-go-giddy3' -- \
  python3 -u bot.py
```

## Verification here

- `python3 -m py_compile dev/sim/run.py dev/sim/engine.py` passed; `patch --dry-run -p1 -d ../BuildersMechRoyale < dev/sim_extopp.patch` passed.
- With `--real-config`, Python giddy subject vs 15 house bots completed with schema validation on: place 14, 0 kills, zone death at 207.9 sim seconds, 116.44 wall seconds, 0 timeouts/invalid actions. This exercises the house-filled FFA path, but does not satisfy the requested Go-bot nonzero-kill check.
- Python giddy subject vs 15 Python protocol opponents completed one schema-validated match (place 6, 0 kills, 61.04 wall seconds) and a two-worker, two-match smoke (mean 57.60 wall seconds per match, 0 timeouts/invalid actions). This verifies fan-out, shared deadlines, and multiworker completion; Python copies are only plumbing stand-ins, not evidence about Go matchups.
- I could not run the three requested Go/Docker validations in this environment. `docker image inspect mf-go-giddy3` failed because the configured Colima socket `/Users/arnav/.colima/default/docker.sock` returned permission denied; the default Docker socket `/var/run/docker.sock` is absent. The exact Go commands are above for the machine where the image/daemon are available.
