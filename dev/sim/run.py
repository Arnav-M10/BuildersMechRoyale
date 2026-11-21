#!/usr/bin/env python3
"""Run deterministic offline mechfight/1 matches.

Examples (run from the repository root):
  python3 dev/sim/run.py --matches 8 --seed 17 -- python3 -u bot.py
  python3 dev/sim/run.py --matches 8 --seed 17 --ffa -- python3 -u bot.py
"""
from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import json
import multiprocessing as mp
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dev.check.check import Schema
from dev.sim.engine import Match


def load_contract():
    with gzip.open(ROOT / "dev/check/stream.jsonl.gz", "rt", encoding="utf-8") as f:
        hello = json.loads(next(f))
    hello["map"]["dir"] = str(ROOT / "arena")
    return hello


class BotProcess:
    def __init__(self, cmd, slot, hello, schema, tick_budget):
        self.cmd = list(cmd)
        self.slot = slot
        self.schema = schema
        self.tick_budget = tick_budget
        self.lines = queue.Queue()
        self.closed = False
        self.missed = 0
        self.invalid = 0
        self.proc = subprocess.Popen(self.cmd, cwd=ROOT, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     bufsize=0)
        threading.Thread(target=self._read_stdout, daemon=True).start()
        self.hello = dict(hello)
        self.hello["slot"] = slot
        # Per-slot bot seeds are fixed independently from the hidden match seed.
        self.hello["seed"] = 315233926 + slot * 104729

    def _read_stdout(self):
        try:
            for raw in self.proc.stdout:
                self.lines.put((time.monotonic(), raw))
        finally:
            self.lines.put((time.monotonic(), None))

    def _write(self, msg):
        if self.closed or self.proc.poll() is not None:
            return False
        try:
            data = (json.dumps(msg, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False

    def start(self):
        start = time.monotonic()
        if not self._write(self.hello):
            return False
        deadline = start + float(self.hello["config"]["bot"].get("hello_budget_ms", 20000)) / 1000
        while time.monotonic() < deadline:
            try:
                at, raw = self.lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                return False
            if raw is None:
                return False
            try:
                msg = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(msg, dict) and msg.get("type") == "ready":
                return not self.schema.errors(msg, "Ready")
        return False

    def send(self, obs):
        ok = self._write(obs)
        if not ok:
            self.closed = True
        return ok

    def receive(self, tick, deadline):
        while time.monotonic() < deadline:
            try:
                at, raw = self.lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                break
            if raw is None:
                self.closed = True
                break
            try:
                msg = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                self.invalid += 1
                continue
            if not isinstance(msg, dict):
                self.invalid += 1
                continue
            if msg.get("tick") != tick:
                # Stale actions are dropped by the protocol server.
                continue
            if self.schema.errors(msg, "Action"):
                self.invalid += 1
                return None
            return msg
        self.missed += 1
        return None

    def finish(self, place, kills):
        if self.proc.poll() is None:
            self._write({"type": "end", "place": place, "kills": kills})
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=1.0)
        self.closed = True


_CTX = {}


def _worker_init(config, bot_cmd, ffa, validate, cache_path):
    global _CTX
    # The parent created the cache before forking. If a platform uses spawn,
    # load the shared on-disk cache here.
    import geo
    map_dir = str(ROOT / "arena")
    g = geo.Geo.load(map_dir, str(cache_path))
    _CTX = {"geo": g, "config": config, "cmd": bot_cmd,
            "ffa": ffa, "schema": Schema(str(ROOT / "dev/mechfight.schema.json")) if validate else None}


def _run_match(job):
    index, seed = job
    ctx = _CTX
    schema = ctx["schema"]
    game = Match(ctx["geo"], ctx["config"], seed, ffa=ctx["ffa"], validate=schema)
    cmd = ctx["cmd"]
    slots = range(16) if ctx["ffa"] else (0,)
    bots = {}
    for slot in slots:
        bots[slot] = BotProcess(cmd, slot, load_contract(),
                                schema or Schema(str(ROOT / "dev/mechfight.schema.json")),
                                float(ctx["config"]["bot"].get("tick_budget_ms", 50)) / 1000)
    ready = {}
    # Start all FFA copies before waiting for any, keeping hello load parallel.
    hello_started = time.monotonic()
    for slot, proc in bots.items():
        if not proc._write(proc.hello):
            ready[slot] = False
    # Since hello was already written above, collect ready lines directly.
    ready_deadline = hello_started + float(ctx["config"]["bot"].get("hello_budget_ms", 20000)) / 1000
    for slot, proc in bots.items():
        if slot in ready:
            continue
        ok = False
        while time.monotonic() < ready_deadline:
            try:
                _, raw = proc.lines.get(timeout=max(0.0, ready_deadline-time.monotonic()))
            except queue.Empty:
                break
            if raw is None:
                break
            try:
                msg=json.loads(raw)
            except (ValueError,UnicodeDecodeError):
                continue
            if isinstance(msg,dict) and msg.get("type")=="ready":
                ok=not proc.schema.errors(msg,"Ready")
                break
        ready[slot]=ok
    for slot, ok in ready.items():
        if not ok:
            bots[slot].closed = True

    def actions_provider(m):
        if ctx["ffa"]:
            return m.decide(processes=bots,
                            budget=float(ctx["config"]["bot"].get("tick_budget_ms",50))/1000)
        proc=bots[0]
        action=None
        if not ready.get(0) or proc.closed:
            m.actors[0]._crashed=True
        elif m.actors[0].alive:
            obs=m.observation(0)
            m.mark_observed()
            sent=time.monotonic()
            if proc.send(obs):
                action=proc.receive(m.tick,sent+float(ctx["config"]["bot"].get("tick_budget_ms",50))/1000)
                if proc.closed:
                    m.actors[0]._crashed=True
            else:
                proc.closed=True
                m.actors[0]._crashed=True
        return m.decide(subject_action=action)

    try:
        while game.t < game.max_time and sum(1 for a in game.actors.values() if a.kind=="mech" and a.alive)>1:
            actions=actions_provider(game)
            game.step_decision(actions)
        if game.t>=game.max_time:
            for a in game.actors.values():
                if a.kind=="mech" and a.alive:
                    a.death_time=game.t;a.death_cause="clock"
        target_id=0
        result=game.result(target_id)
        for slot,proc in bots.items():
            p=result["placements"].get(slot,1)
            k=game.actors[slot].kills
            proc.finish(p,k)
        result["match"] = index
        result["seed"] = seed
        result["timeouts"] = sum(p.missed for p in bots.values())
        result["invalid_actions"] = sum(p.invalid for p in bots.values())
        if ctx["ffa"]:
            result["contestant_placements"] = [result["placements"].get(i,16) for i in range(16)]
            result["contestant_scores"] = [16-result["placements"].get(i,16) for i in range(16)]
        return result
    except BaseException:
        for proc in bots.values():
            try: proc.finish(16,0)
            except Exception: pass
        raise


def _prepare_cache():
    import geo
    map_dir = str(ROOT / "arena")
    cache = ROOT / "cache.pkl"
    g = geo.Geo.load(map_dir, str(cache))
    if not cache.exists() or geo.Geo.from_cache(str(cache), geo.source_hash(map_dir)) is None:
        g.save(str(cache), geo.source_hash(map_dir))
    return cache


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--matches",type=int,default=8)
    ap.add_argument("--seed",type=int,default=1)
    ap.add_argument("--jobs",type=int,default=None)
    ap.add_argument("--ffa",action="store_true",help="run 16 subprocess copies of the supplied bot")
    ap.add_argument("--no-validate",action="store_true",help="skip generated observation schema validation (actions remain checked)")
    ap.add_argument("cmd",nargs=argparse.REMAINDER)
    args=ap.parse_args(argv)
    cmd=args.cmd[1:] if args.cmd[:1]==["--"] else args.cmd
    if not cmd:ap.error("give the bot command after --")
    if args.matches<1:ap.error("--matches must be positive")
    hello=load_contract()
    config=hello["config"]
    # Build / load the large map once, persist the same cache the bot uses, then fork workers.
    cache=_prepare_cache()
    import geo
    global _CTX
    root_geo=geo.Geo.load(str(ROOT/"arena"),str(cache))
    _CTX={"geo":root_geo,"config":config,"cmd":cmd,"ffa":args.ffa,
          "schema":None if args.no_validate else Schema(str(ROOT/"dev/mechfight.schema.json"))}
    jobs=args.jobs or min(args.matches,max(1,min(4,os.cpu_count() or 1)))
    jobs=max(1,min(args.matches,jobs))
    jobs_list=[(i+1,args.seed+i) for i in range(args.matches)]
    results=[]
    print("mode: %s | matches: %d | workers: %d | validation: %s" %
          ("FFA" if args.ffa else "bot vs house squad",args.matches,jobs,"on" if not args.no_validate else "off"),flush=True)
    if jobs==1:
        for job in jobs_list:
            r=_run_match(job);results.append(r)
            print("finished match %d/%d: place %d, kills %d, time %.1fs" %
                  (job[0],args.matches,r["place"],r["kills"],r["time"]),flush=True)
    else:
        try:
            ctx=mp.get_context("fork")
        except ValueError:
            ctx=mp.get_context()
        with concurrent.futures.ProcessPoolExecutor(max_workers=jobs,mp_context=ctx,
             initializer=_worker_init,initargs=(config,cmd,args.ffa,not args.no_validate,cache)) as pool:
            futs={pool.submit(_run_match,j):j for j in jobs_list}
            for fut in concurrent.futures.as_completed(futs):
                r=fut.result();results.append(r)
                print("finished match %d/%d: place %d, kills %d, time %.1fs" %
                      (r["match"],args.matches,r["place"],r["kills"],r["time"]),flush=True)
    results.sort(key=lambda x:x["match"])
    if args.ffa:
        placements=[p for r in results for p in r["contestant_placements"]]
        scores=[16-p for p in placements]
        hist={p:placements.count(p) for p in range(1,17)}
        print("mean (16-place): %.3f across %d bot seats"%(sum(scores)/len(scores),len(scores)))
        print("placement histogram: "+" ".join("%d:%d"%(p,hist[p]) for p in range(1,17)))
        print("timeouts %d | invalid actions %d" %
              (sum(r["timeouts"] for r in results), sum(r["invalid_actions"] for r in results)))
        for r in results:
            print("match %d seed %d placements %s"%(r["match"],r["seed"],
                  ",".join(str(x) for x in r["contestant_placements"])))
            for a in r["actors"]:
                died = "alive" if a["death_time"] is None else "%.1fs" % a["death_time"]
                cause = a["cause"] or "alive"
                print("  bot %d: place %d kills %d death %s cause %s" %
                      (a["id"],a["place"],a["kills"],died,cause))
    else:
        scores=[16-r["place"] for r in results]
        hist={p:sum(1 for r in results if r["place"]==p) for p in range(1,17)}
        print("mean (16-place): %.3f"%(sum(scores)/len(scores)))
        print("placement histogram: "+" ".join("%d:%d"%(p,hist[p]) for p in range(1,17)))
        for r in results:
            print("match %d seed %d: place %d score %d kills %d death %s cause %s time %.1fs timeouts %d invalid %d" %
                  (r["match"],r["seed"],r["place"],16-r["place"],r["kills"],
                   ("alive at end" if r["death_time"] is None else "%.1fs"%r["death_time"]),
                   r["cause"] or "alive",r["time"],r["timeouts"],r["invalid_actions"]))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
