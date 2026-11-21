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
import shlex
import subprocess
import sys
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dev.check.check import Schema
from dev.sim.engine import Match


def _read_hello(path):
    path = Path(path)
    if not path.is_absolute():
        path = ROOT / path
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        raw = f.read()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        for line in raw.splitlines():
            if line.strip():
                return json.loads(line)
    raise ValueError("hello file is empty: %s" % path)


def load_contract(real_config_path=None):
    with gzip.open(ROOT / "dev/check/stream.jsonl.gz", "rt", encoding="utf-8") as f:
        hello = json.loads(next(f))
    if real_config_path:
        source = _read_hello(real_config_path)
        if not isinstance(source.get("config"), dict):
            raise ValueError("--real-config hello has no config object: %s" % real_config_path)
        hello["config"] = source["config"]
    hello["map"]["dir"] = str(ROOT / "arena")
    return hello


class BotProcess:
    def __init__(self, cmd, slot, hello, schema, tick_budget, child_env=None,
                 container_name=None, async_writes=False):
        self.cmd = list(cmd)
        self.slot = slot
        self.schema = schema
        self.tick_budget = tick_budget
        self.container_name = container_name
        self.async_writes = async_writes
        self.write_queue = queue.Queue(maxsize=1) if async_writes else None
        self.lines = queue.Queue()
        self.closed = False
        self.finished = False
        self.missed = 0
        self.invalid = 0
        if self.container_name and self.cmd[:2] == ["docker", "run"]:
            # Give each docker run a unique address so finish() can remove even a
            # container whose client or bot stops responding.
            cleaned = [self.cmd[0], self.cmd[1]]
            i = 2
            while i < len(self.cmd):
                tok = self.cmd[i]
                if tok == "--name":
                    i += 2
                    continue
                if tok.startswith("--name="):
                    i += 1
                    continue
                cleaned.append(tok)
                i += 1
            self.cmd = cleaned[:2] + ["--name", self.container_name] + cleaned[2:]
        self.proc = subprocess.Popen(self.cmd, cwd=ROOT, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     bufsize=0, env=child_env)
        threading.Thread(target=self._read_stdout, daemon=True).start()
        if self.async_writes:
            threading.Thread(target=self._write_loop, daemon=True).start()
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

    def _write_loop(self):
        while True:
            msg = self.write_queue.get()
            if msg is None:
                return
            if not self._write(msg):
                self.closed = True
                return
            if isinstance(msg, dict) and msg.get("type") == "end":
                try:
                    self.proc.stdin.close()
                except OSError:
                    pass
                return
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

    def send_async(self, obs):
        """Queue a tick without allowing a wedged child pipe to stall all FFA slots.

        Returns True if queued, None if the one-deep queue was full (a tick miss),
        or False if the child has exited.
        """
        if self.proc.poll() is not None:
            self.closed = True
            return False
        if self.closed:
            return False
        try:
            self.write_queue.put_nowait(obs)
            return True
        except queue.Full:
            # Drop the oldest not-yet-written observation and offer the latest tick.
            try:
                self.write_queue.get_nowait()
            except queue.Empty:
                return None
            try:
                self.write_queue.put_nowait(obs)
                return True
            except queue.Full:
                return None

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

    def receive_available(self, tick):
        """Return (done, action) without waiting; FFA polls every live slot to one deadline."""
        while True:
            try:
                _, raw = self.lines.get_nowait()
            except queue.Empty:
                return False, None
            if raw is None:
                self.closed = True
                return True, None
            try:
                msg = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                self.invalid += 1
                continue
            if not isinstance(msg, dict):
                self.invalid += 1
                continue
            if msg.get("tick") != tick:
                continue
            if self.schema.errors(msg, "Action"):
                self.invalid += 1
                return True, None
            return True, msg

    def _remove_container(self):
        if not self.container_name:
            return
        try:
            subprocess.run(["docker", "rm", "-f", self.container_name],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=3.0, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def finish(self, place, kills):
        if self.finished:
            return
        if self.proc.poll() is None:
            if self.async_writes:
                # End supersedes any stale queued tick. A writer blocked in a child
                # pipe is bounded by the wait below and then the container is killed.
                try:
                    self.write_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self.write_queue.put_nowait({"type": "end", "place": place, "kills": kills})
                except queue.Full:
                    pass
            else:
                self._write({"type": "end", "place": place, "kills": kills})
                try:
                    self.proc.stdin.close()
                except OSError:
                    pass
            try:
                self.proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._remove_container()
                try:
                    self.proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=1.0)
        self._remove_container()
        self.closed = True
        self.finished = True


_CTX = {}


def _worker_init(config, hello, bot_cmd, opp_cmd, opp_count, ffa, ffa_self,
                 mixed, external_mode, validate, cache_path):
    global _CTX
    # The parent created the cache before forking. If a platform uses spawn,
    # load the shared on-disk cache here.
    import geo
    map_dir = str(ROOT / "arena")
    g = geo.Geo.load(map_dir, str(cache_path))
    _CTX = {"geo": g, "config": config, "hello": hello, "cmd": bot_cmd,
            "opp_cmd": opp_cmd, "opp_count": opp_count,
            "ffa": ffa, "ffa_self": ffa_self, "mixed": mixed,
            "external_mode": external_mode,
            "schema": Schema(str(ROOT / "dev/mechfight.schema.json")) if validate else None}


def _run_match(job):
    index, seed = job
    wall_started = time.perf_counter()
    ctx = _CTX
    schema = ctx["schema"]
    game = Match(ctx["geo"], ctx["config"], seed, ffa=ctx["ffa"], validate=schema)
    bots = {}
    if not ctx["ffa"]:
        bot_commands = {0: ctx["cmd"]}
    elif ctx["ffa_self"] or ctx["mixed"]:
        bot_commands = {slot: ctx["cmd"] for slot in range(16)}
    else:
        bot_commands = {0: ctx["cmd"]}
        bot_commands.update({slot: ctx["opp_cmd"] for slot in range(1, ctx["opp_count"] + 1)})
    try:
        for slot, command in bot_commands.items():
            child_env = None
            if ctx.get("mixed"):
                child_env = os.environ.copy()
                try:
                    mf_p = json.loads(child_env.get("MF_P", "{}"))
                    if not isinstance(mf_p, dict):
                        mf_p = {}
                except (TypeError, ValueError):
                    mf_p = {}
                # Alternate groups by match so slot/spawn bias does not favor one variant.
                enabled = (slot < 8) == (index % 2 == 1)
                mf_p.update({"ffa_detect": enabled, "ffa_engage": enabled, "ffa_hold": enabled})
                child_env["MF_P"] = json.dumps(mf_p, separators=(",", ":"))
            container_name = None
            if command[:2] == ["docker", "run"]:
                container_name = "mfsim-%d-%d-%d-%d" % (os.getpid(), index, slot, time.time_ns() % 1000000000)
            bots[slot] = BotProcess(command, slot, dict(ctx["hello"]),
                                    schema or Schema(str(ROOT / "dev/mechfight.schema.json")),
                                    float(ctx["config"]["bot"].get("tick_budget_ms", 50)) / 1000,
                                    child_env=child_env, container_name=container_name,
                                    async_writes=ctx["ffa"])
    except BaseException:
        for proc in bots.values():
            try:
                proc.finish(16, 0)
            except Exception:
                pass
        raise
    if ctx["ffa"] and not ctx["ffa_self"] and not ctx["mixed"]:
        # Preserve the calibrated 4/4/4/3 house roster in the slots not occupied
        # by external opponents. In FFA, each house bot only retaliates for itself.
        for aid, actor in game.actors.items():
            if actor.kind != "mech" or aid == 0 or aid in bots:
                continue
            actor.house_tier = 1 if aid <= 4 else 2 if aid <= 8 else 3 if aid <= 12 else 4
    ready = {}
    try:
        # Start every subject/opponent process before waiting; all hellos share a deadline.
        hello_started = time.monotonic()
        for slot, proc in bots.items():
            if not proc._write(proc.hello):
                ready[slot] = False
        ready_deadline = hello_started + float(ctx["config"]["bot"].get("hello_budget_ms", 20000)) / 1000
        pending = set(bots) - set(ready)
        while pending and time.monotonic() < ready_deadline:
            progressed = False
            for slot in list(pending):
                proc = bots[slot]
                while True:
                    try:
                        _, raw = proc.lines.get_nowait()
                    except queue.Empty:
                        break
                    progressed = True
                    if raw is None:
                        proc.closed = True
                        ready[slot] = False
                        pending.discard(slot)
                        break
                    try:
                        msg = json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if isinstance(msg, dict) and msg.get("type") == "ready":
                        ready[slot] = not proc.schema.errors(msg, "Ready")
                        pending.discard(slot)
                        break
            if not progressed:
                time.sleep(min(0.001, max(0.0, ready_deadline - time.monotonic())))
        for slot in pending:
            ready[slot] = False
        for slot, ok in ready.items():
            if not ok:
                bots[slot].finish(16, 0)
                if slot in game.actors:
                    game.actors[slot]._crashed = True
    except BaseException:
        for proc in bots.values():
            try:
                proc.finish(16, 0)
            except Exception:
                pass
        raise

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
            if ctx["ffa"]:
                # A mech's placement is fixed at death. Stop its subprocess/container
                # now instead of retaining up to 16 idle 512 MB containers to match end.
                for slot, proc in bots.items():
                    actor = game.actors[slot]
                    if not proc.finished and (not actor.alive or getattr(actor, "_crashed", False)):
                        interim = game.result(slot)
                        proc.finish(interim["place"], actor.kills)
            # Squad mode: house bots never fight each other, so once the subject is dead its
            # place (the mechs alive at its death) is final. Skip the rest of the match.
            if not ctx["ffa"] and not game.actors[0].alive:
                break
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
            result["opponent_mean_place"] = sum(result["placements"].get(i, 16) for i in range(1, 16)) / 15.0
            result["external_mean_place"] = (
                sum(result["placements"].get(i, 16) for i in range(1, ctx["opp_count"] + 1)) /
                float(ctx["opp_count"]) if ctx["opp_count"] else None)
        result["wall_time"] = time.perf_counter() - wall_started
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
    ap.add_argument("--ffa",action="store_true",help="FFA: supplied subject bot, house bots in unused seats")
    ap.add_argument("--ffa-self",action="store_true",help="legacy FFA: run 16 copies of the subject bot")
    ap.add_argument("--ffa-mixed",action="store_true",help="FFA: slots 0-7 use FFA knobs, slots 8-15 disable them")
    ap.add_argument("--real-config",metavar="PATH",help="use config from this hello JSON/JSONL(.gz), keeping the local arena")
    ap.add_argument("--opp-cmd",metavar="CMD",help="shell-like command for external FFA opponents (for example docker run ...)")
    ap.add_argument("--opp-count",type=int,help="number of external opponents in slots 1..15 (remaining seats use house bots)")
    ap.add_argument("--no-validate",action="store_true",help="skip generated observation schema validation (actions remain checked)")
    ap.add_argument("cmd",nargs=argparse.REMAINDER)
    args=ap.parse_args(argv)
    if args.ffa_mixed or args.ffa_self:
        args.ffa = True
    if args.opp_cmd is not None or args.opp_count is not None:
        args.ffa = True
    if args.ffa_self and args.ffa_mixed:
        ap.error("--ffa-self and --ffa-mixed are mutually exclusive")
    if args.opp_count is None:
        args.opp_count = 0
    if args.opp_count < 0 or args.opp_count > 15:
        ap.error("--opp-count must be between 0 and 15")
    if args.opp_count and not args.opp_cmd:
        ap.error("--opp-count above 0 requires --opp-cmd")
    if args.opp_cmd and args.opp_count == 0:
        ap.error("--opp-cmd requires --opp-count between 1 and 15")
    if args.opp_cmd and (args.ffa_self or args.ffa_mixed):
        ap.error("--opp-cmd cannot be combined with --ffa-self or --ffa-mixed")
    cmd=args.cmd[1:] if args.cmd[:1]==["--"] else args.cmd
    if not cmd:ap.error("give the bot command after --")
    cmd=[part.replace("<ROOT>", str(ROOT)) for part in cmd]
    if args.matches<1:ap.error("--matches must be positive")
    opp_cmd = shlex.split(args.opp_cmd.replace("<ROOT>", str(ROOT))) if args.opp_cmd else None
    hello=load_contract(args.real_config)
    config=hello["config"]
    # Build / load the large map once, persist the same cache the bot uses, then fork workers.
    cache=_prepare_cache()
    import geo
    global _CTX
    root_geo=geo.Geo.load(str(ROOT/"arena"),str(cache))
    external_mode = bool(args.ffa and not args.ffa_self and not args.ffa_mixed)
    _CTX={"geo":root_geo,"config":config,"hello":hello,"cmd":cmd,"opp_cmd":opp_cmd,
          "opp_count":args.opp_count,"ffa":args.ffa,"ffa_self":args.ffa_self,
          "mixed":args.ffa_mixed,"external_mode":external_mode,
          "schema":None if args.no_validate else Schema(str(ROOT/"dev/mechfight.schema.json"))}
    jobs=args.jobs or min(args.matches,max(1,min(4,os.cpu_count() or 1)))
    jobs=max(1,min(args.matches,jobs))
    jobs_list=[(i+1,args.seed+i) for i in range(args.matches)]
    results=[]
    mode = ("FFA mixed 8/8" if args.ffa_mixed else
            "FFA self-copy" if args.ffa_self else
            ("FFA external %d + house %d" % (args.opp_count, 15-args.opp_count) if external_mode else
             ("FFA" if args.ffa else "bot vs house squad")))
    print("mode: %s | matches: %d | workers: %d | validation: %s" %
          (mode,args.matches,jobs,"on" if not args.no_validate else "off"),flush=True)
    if jobs==1:
        for job in jobs_list:
            r=_run_match(job);results.append(r)
            if external_mode:
                print("finished match %d/%d: place %d, kills %d, sim %.1fs, wall %.2fs" %
                      (job[0],args.matches,r["place"],r["kills"],r["time"],r["wall_time"]),flush=True)
            else:
                print("finished match %d/%d: place %d, kills %d, time %.1fs" %
                      (job[0],args.matches,r["place"],r["kills"],r["time"]),flush=True)
    else:
        try:
            ctx=mp.get_context("fork")
        except ValueError:
            ctx=mp.get_context()
        with concurrent.futures.ProcessPoolExecutor(max_workers=jobs,mp_context=ctx,
             initializer=_worker_init,initargs=(config,hello,cmd,opp_cmd,args.opp_count,args.ffa,
                                                args.ffa_self,args.ffa_mixed,external_mode,
                                                not args.no_validate,cache)) as pool:
            futs={pool.submit(_run_match,j):j for j in jobs_list}
            for fut in concurrent.futures.as_completed(futs):
                r=fut.result();results.append(r)
                if external_mode:
                    print("finished match %d/%d: place %d, kills %d, sim %.1fs, wall %.2fs" %
                          (r["match"],args.matches,r["place"],r["kills"],r["time"],r["wall_time"]),flush=True)
                else:
                    print("finished match %d/%d: place %d, kills %d, time %.1fs" %
                          (r["match"],args.matches,r["place"],r["kills"],r["time"]),flush=True)
    results.sort(key=lambda x:x["match"])
    if args.ffa:
        placements=[p for r in results for p in r["contestant_placements"]]
        scores=[16-p for p in placements]
        hist={p:placements.count(p) for p in range(1,17)}
        if external_mode:
            print("mean subject (16-place): %.3f across %d matches" % (sum(r["contestant_scores"][0] for r in results)/len(results), len(results)))
            print("mean opponent place (all 15): %.3f" % (sum(r["opponent_mean_place"] for r in results)/len(results)))
            if args.opp_count:
                print("mean external opponent place (%d seats): %.3f" %
                      (args.opp_count, sum(r["external_mean_place"] * args.opp_count for r in results) /
                       (len(results) * args.opp_count)))
            walls = [r["wall_time"] for r in results]
            print("wall seconds per match: mean %.2f | min %.2f | max %.2f" %
                  (sum(walls)/len(walls), min(walls), max(walls)))
            for r in results:
                subject = next(a for a in r["actors"] if a["id"] == 0)
                died = "alive" if subject["death_time"] is None else "%.1fs" % subject["death_time"]
                cause = subject["cause"] or "alive"
                ext_text = (" | external mean place %.2f" % r["external_mean_place"]
                            if r["external_mean_place"] is not None else "")
                print("match %d seed %d: subject place %d score %d kills %d death %s cause %s | opponents mean place %.2f%s | wall %.2fs" %
                      (r["match"],r["seed"],r["place"],16-r["place"],r["kills"],died,cause,
                       r["opponent_mean_place"],ext_text,r["wall_time"]))
        else:
            print("mean (16-place): %.3f across %d bot seats"%(sum(scores)/len(scores),len(scores)))
            print("placement histogram: "+" ".join("%d:%d"%(p,hist[p]) for p in range(1,17)))
        if args.ffa_mixed:
            for label, enabled in (("knobs-on", True), ("knobs-off", False)):
                group = [r["contestant_placements"][slot]
                         for r in results for slot in range(16)
                         if ((slot < 8) == (r["match"] % 2 == 1)) == enabled]
                gh = {p: group.count(p) for p in range(1, 17)}
                print("%s mean (16-place): %.3f across %d seats | places %s" %
                      (label, sum(16-p for p in group)/len(group), len(group),
                       " ".join("%d:%d" % (p, gh[p]) for p in range(1, 17))))
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
        house_gun = {tier: {"shots": 0, "hits": 0, "hunt_shots": 0, "hunt_hits": 0}
                     for tier in range(1, 5)}
        for result in results:
            for tier, counts in result.get("house_gun", {}).items():
                for key in house_gun[int(tier)]:
                    house_gun[int(tier)][key] += counts[key]
        labels = {1: "easy", 2: "medium", 3: "hard", 4: "expert"}
        print("house gun hits on mechs: " + " | ".join(
            "%s %d/%d = %.1f%%" % (labels[tier], house_gun[tier]["hits"],
                                    house_gun[tier]["shots"],
                                    100.0 * house_gun[tier]["hits"] /
                                    max(1, house_gun[tier]["shots"]))
            for tier in range(1, 5)))
        print("house hunter hits on contestant: " + " | ".join(
            "%s %d/%d = %.1f%%" % (labels[tier], house_gun[tier]["hunt_hits"],
                                    house_gun[tier]["hunt_shots"],
                                    100.0 * house_gun[tier]["hunt_hits"] /
                                    max(1, house_gun[tier]["hunt_shots"]))
            for tier in range(1, 5)))
        house_motion = {tier: {"moving": 0, "boosting": 0, "roof": 0, "ticks": 0}
                        for tier in range(1, 5)}
        for result in results:
            for tier, counts in result.get("house_motion", {}).items():
                for key in house_motion[int(tier)]:
                    house_motion[int(tier)][key] += counts[key]
        print("house movement: " + " | ".join(
            "%s moving %.1f%% boost %.1f%% roof %.1f%%" % (
                labels[tier],
                100.0 * house_motion[tier]["moving"] / max(1, house_motion[tier]["ticks"]),
                100.0 * house_motion[tier]["boosting"] / max(1, house_motion[tier]["ticks"]),
                100.0 * house_motion[tier]["roof"] / max(1, house_motion[tier]["ticks"]))
            for tier in range(1, 5)))
        for r in results:
            print("match %d seed %d: place %d score %d kills %d death %s cause %s time %.1fs timeouts %d invalid %d" %
                  (r["match"],r["seed"],r["place"],16-r["place"],r["kills"],
                   ("alive at end" if r["death_time"] is None else "%.1fs"%r["death_time"]),
                   r["cause"] or "alive",r["time"],r["timeouts"],r["invalid_actions"]))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
