"""mechfight/1 bot: survive the zone, loot up, fight on its own terms.

Reads JSON lines on stdin, writes one action per observation on stdout (protocol.md).
Logs go to stderr only.
"""
from __future__ import annotations

import gc
import json
import math
import os
import queue
import random
import sys
import threading
import time
import traceback

PI = math.pi
TAU = 2.0 * PI
BOT_NAME = "oval-warden"

# Strategy knobs (tuned by A/B submissions on the fixed practice seeds)
P = {
    "core": "giddy",       # "ours" or giddygiddyboy's practice-squad core
    "g_hpw": 0.21,         # giddy core: target score HP weight (giddy latest squad: 0.21)
    "g_lethalk": 0.7,     # giddy core: partial-laser lethal release factor (giddy latest: 0.7)
    "g_fb": False,         # giddy core: FightBoost 40 (giddy latest squad dropped it)
    "chase": True,        # push toward a target we cannot see when healthy
    "retreat_hp": 0.38,   # break line of sight below this HP fraction
    "laser_band": (50.0, 120.0),
    "gun_band": (45.0, 110.0),
    "lit_band": (0.0, 32.0),
    "regen": False,       # after a fight, hold still in cover until regen_hp
    "regen_hp": 0.85,
    "w_laser": 5.0,
    "w_lightning": 4.0,
    "strafe": (0.3, 0.95),  # seconds between strafe flips
    "tol": 0.75,          # fire when predicted miss < tol * target radius
    "laser_dodge": False,  # boosted direction reversal timed for an enemy laser release
    "juke_at": 0.5,       # seconds after we first see the charge
    "commit": False,      # keep dueling a lone enemy whose HP <= ours * commit_ratio
    "commit_ratio": 1.3,
    "noflee": False,      # no cover found -> keep fighting instead of running across the open
    "ambush": False,      # gun only: break LOS beyond ambush_d and let the enemy come close
    "ambush_d": 100.0,
    "race": False,        # disengage by the damage race (time-to-death vs time-to-kill), not HP alone
    "race_margin": 0.8,   # leave when ttd < ttk * margin
    "race_ttd": 6.0,      # ...and ttd below this many seconds
    "cover_max": 35.0,    # race mode: only dash to cover this close; else fight while strafing
    "berserk": True,      # never retreat from a fight (practice hunters share vision; running loses)
    "want_d": 110.0,        # >0: hold this fight distance (smooth radial), instead of weapon bands
    "tsel": True,         # target = lowest hp * (1 + dist/120) among visible
    "fightloot": True,    # mid-fight: scoop cubes <30 m, fetch a better weapon <200 m, take drops
    "econ": False,        # TTUN-style economy: hunt drones/crates/drops for cubes; armor/plating/nanites first
    "killloot": False,    # walk back to where our kills died (their cubes + 2 drop there) when no threat is close
    "gtol": False,        # gun fires on its own lead solution with TTUN tolerance 1.2*atan(2.2*scale/d)
    "precharge": False,   # start a laser charge on a target within 60 m even without line of sight
    "wdist": False,       # with want_d: lightning 30 m, laser want_d, gun-only 70 m; >=70 m if outnumbered or vs lightning
    "upg": "default",     # upgrade order: "default", "econ" (TTUN), "inv" (Team Inventors)
    "want_laser": 0.0,    # >0: hold this distance instead of want_d while carrying a laser
    "selfcomp": False,    # aim from where our muzzle will be (own velocity x 0.075 s); travel from the 3 m spawn point
    "duck": False,        # gun-only and reloading: step to a spot within ~12 m out of the target's sight
    "select": False,      # engage only close/attacking/approaching/weak/aiming mechs, or late (giddygiddyboy rules)
    "posearch": False,    # fight movement = best of stay + 16 spots 6 m away (range, LOS when ready, crossfire, zone, cubes)
    "ffa_detect": True,   # latch from crossfire or unexplained eliminations
    "ffa_engage": True,   # only take qualifying FFA fights; otherwise avoid via cover
    "ffa_hold": True,     # hold a covered position inside the next circle in the endgame
    "ffa_lurk": True,     # FFA opening (t<75 s and >9 alive): fight only attackers or mechs within 25 m
    "ffa_end": True,      # FFA endgame (<=4 alive): only attackers or weak close mechs; hold 0.3*r when r<=70
    "ffa_heal": True,     # FFA only: disengage when ganged/losing the race, heal in cover, resume at 75% HP
    "ffa_fast": True,     # latch FFA on 2 (not 3) unexplained deaths before the first zone shrink
    "ffa_juke": False,    # FFA only: timed laser juke (laser_dodge) once FFA is latched
    "ffa_econ": False,    # FFA only: TTUN-style farming (drones/crates/drops/cubes) when not fighting
    "fightboost": 0.0,    # >0: boost throughout fights when energy is above this (not while charging a laser;
                          #     with lightning only above 60), as in giddygiddyboy's 6.00 squad params
    "lurk_t": 75.0,       # ffa_lurk lasts until this match time...
    "lurk_alive": 9,      # ...or until this many mechs are left
    "salt": 50,            # mixed into the RNG seed (strafe timing etc.); the strategy itself is unchanged
}
# offline tuning only: the simulator may override knobs through MF_P (never set in matches)
if os.environ.get("MF_P"):
    try:
        for _k, _v in json.loads(os.environ["MF_P"]).items():
            P[_k] = tuple(_v) if isinstance(_v, list) else _v
    except Exception:
        pass

try:
    import geo as geomod
except Exception:  # pragma: no cover - the bot still plays without the map model
    geomod = None


TRACE = None
if os.environ.get("MF_TRACE"):
    try:
        TRACE = open(os.environ["MF_TRACE"].replace("%p", str(os.getpid())), "w")
    except Exception:
        TRACE = None


def log(*a):
    try:
        print(*a, file=sys.stderr, flush=True)
    except Exception:
        pass


def wrap(a):
    return (a + PI) % TAU - PI


def clamp(v, a, b):
    return a if v < a else (b if v > b else v)


def hyp(x, y):
    return math.sqrt(x * x + y * y)


def unit(x, y):
    d = math.sqrt(x * x + y * y)
    if d < 1e-9:
        return 0.0, 0.0
    return x / d, y / d


# weapon preference for the two slots: never drop the gun; laser > lightning (the organizers' strongest
# house bots win with gun + laser); mortar invites laser cook-off and self-splash: never taken
W_VAL = {"gun": 9.0, "laser": P["w_laser"], "lightning": P["w_lightning"], "mortar": 0.0}

UPGRADE_RANK = [
    "plating", "gun_damage", "armor", "gun_mag", "gun_rate", "nanites", "laser_damage",
    "laser_charge", "gun_velocity", "lightning_efficiency", "servos", "gyros", "lightning_range",
    "sensors", "capacitor", "reactor", "jets", "lightning_chain", "laser_pierce",
    "mortar_reload", "mortar_velocity", "mortar_splash",
]

# giddy2 DefaultParams with squadParams(LaserUp=true) applied (brain.go:446-450, 1940-1964).
GIDDY_LASER_UPGRADE_RANK = [
    "plating", "armor", "laser_damage", "laser_charge", "nanites", "gun_damage", "gun_rate",
    "lightning_efficiency", "lightning_range", "servos", "gun_mag", "gun_velocity", "gyros",
    "lightning_chain", "reactor", "capacitor", "mortar_reload", "mortar_splash", "mortar_velocity",
    "sensors", "jets", "laser_pierce",
]


def lead_vel(v, pv, T):
    """Effective horizontal velocity for leading a shot T seconds ahead. Fitted on 62k samples of
    live house-bot motion: jukes keep turning, so extrapolate the last 0.1 s acceleration
    (within 2.5 m of truth: 94% vs 89% for constant velocity at T=0.3 s; 85% vs 81% at 0.4 s)."""
    if pv is not None and v[0] * pv[0] + v[1] * pv[1] < 0.0:
        pv = None  # just reversed: extrapolating the old acceleration overshoots
    if pv is None or T > 0.65:
        k = 1.0 if pv is None else 0.9
        return v[0] * k, v[1] * k
    if T <= 0.5:
        a, b = 1.5, -0.62
    else:
        a, b = 1.37, -0.56
    return a * v[0] + b * pv[0], a * v[1] + b * pv[1]


def zone_obs_next_r(obs):
    try:
        return float((obs.get("zone") or {}).get("next_radius", 1e9))
    except Exception:
        return 1e9


def climbing_pending(brain):
    return brain.climb_req is not None


def in_safe_zone(brain, obs, px, py, t):
    """True when we can afford to sit still for ~20 s here without the zone reaching us."""
    cx, cy, cr = Brain.zone_at(obs.get("zone") or {}, t + 20.0)
    return hyp(px - cx, py - cy) < cr - 10.0


UPGRADE_RANK_INV = [  # Team Inventors' order (5.15 real)
    "plating", "armor", "laser_damage", "gun_damage", "nanites", "laser_charge", "gun_rate",
    "lightning_efficiency", "servos", "gun_velocity", "gyros", "gun_mag", "lightning_chain",
    "reactor", "lightning_range", "capacitor", "mortar_reload", "mortar_splash", "mortar_velocity",
    "sensors", "jets", "laser_pierce",
]
UPGRADE_RANK_ECON = [  # TTUN's exact order
    "armor", "plating", "nanites", "lightning_efficiency", "laser_damage", "laser_charge", "gun_damage", "gun_rate",
    "lightning_range", "lightning_chain", "gyros", "servos", "gun_mag", "capacitor", "reactor", "jets", "gun_velocity",
    "sensors", "mortar_reload", "mortar_velocity", "mortar_splash", "laser_pierce",
]


class Brain:
    def __init__(self, hello):
        cfg = hello.get("config") or {}
        self.cfg = cfg
        zone_phases = (cfg.get("zone") or {}).get("phases") or []
        self.first_zone_shrink = float(zone_phases[0].get("hold_s", 60.0)) if zone_phases else 60.0
        self.my_id = hello.get("slot")
        seed = hello.get("seed")
        self.rng = random.Random((seed if isinstance(seed, int) else 12345) ^ (int(P["salt"]) * 2654435761 & 0xFFFFFFFF))
        mech = cfg.get("mech") or {}
        self.base_mech = mech
        self.W = cfg.get("weapons") or {}
        self.vision = cfg.get("vision") or {}
        loot = cfg.get("loot") or {}
        self.loot_r = float(loot.get("pickup_radius", 4.0))
        cubes = cfg.get("cubes") or {}
        self.cube_r = float(cubes.get("pickup_radius", 3.5))
        self.size_per_cube = float(cubes.get("size_per_cube", 0.05))
        self.dmg_per_cube = float(cubes.get("damage_per_cube", 0.04))
        self.stat_cap = float(cubes.get("stat_cap", 10))
        self.npc_cfg = cfg.get("npc") or {}
        self.drops_cfg = cfg.get("drops") or {}
        self.mortar_g = float((self.W.get("mortar") or {}).get("gravity", 22.0))
        self.gravity = float(mech.get("gravity", 22.0))
        self.geo = None
        if geomod is not None:
            try:
                here = os.path.dirname(os.path.abspath(__file__))
                md = (hello.get("map") or {}).get("dir") or "/arena"
                t0 = time.perf_counter()
                self.geo = geomod.Geo.load(md, os.path.join(here, "cache.pkl"))
                log("geo loaded in %.2fs" % (time.perf_counter() - t0))
            except Exception:
                log("geo load failed:\n" + traceback.format_exc())
                self.geo = None
        # memory
        self.t = 0.0
        self.enemies = {}       # id -> dict(pos, vel, hp, max_hp, weapons, seen, aim, charging, cubes)
        self.npcs = {}
        self.pickups = {}       # id -> (kind, pos, seen)
        self.crates = {}        # id -> (pos, hp, seen)
        self.cubes_mem = {}     # id -> (pos, seen)
        self.attacked_by = {}   # id -> last time it damaged us
        self.my_kills = 0
        self.ffa = False
        self.ffa_beam_clues = 0
        self.ffa_beam_seen = {}
        self.charge_seen = {}   # enemy id -> time we first saw it charging
        self.hints = []         # (t, bearing, weight) threat directions from sounds/damage
        self.last_hurt = -99.0
        self.giddy_last_hurt = -99.0
        self.giddy_hurt_bearing = 0.0
        self.giddy_npc_hurt = -99.0
        self.giddy_npc_bearing = 0.0
        self.last_hit_dealt = -99.0
        self.shorted_until = -1.0
        self.take_block = {}
        self.target_id = None
        self.target_since = 0.0
        self.target_t_last = -99.0
        self.giddy_stick_id = None
        self.giddy_stick_at = -99.0
        self.strafe = 1.0
        self.strafe_until = 0.0
        self.path = None
        self.path_goal = None
        self.path_t = -99.0
        self.hist = []          # (t, x, y, commanded)
        self.stuck_until = -1.0
        self.stuck_dir = (0.0, 0.0)
        self.stuck_log = []
        self.juke_key = None
        self.ffa_healing = False
        self.safe_circle = (0.0, 0.0, 1e6)
        self.dmg_in = []
        self.dmg_out = []
        self.kill_spots = []
        self.gun_busy = False
        self.laser_charging_now = False
        self.has_special = False
        self.muzzle_h = 7.0
        self.eye_h = 8.0
        self.pz_now = 0.0
        self.duck_spot = None
        self.duck_t = -99.0
        self.juke_dir = 1.0
        self.cover_goal = None
        self.cover_t = -99.0
        self.home = None
        self.giddy_last_threat_at = -100.0
        self.giddy_lose_at = -1.0
        self.giddy_retreat = False
        self.giddy_bad = {}
        self.giddy_loot_id = None
        self.giddy_loot_since = -99.0
        self.giddy_main_comp = -1
        if self.geo is not None:
            try:
                self.giddy_main_comp = self.geo.main_comp()
            except Exception:
                pass
        self.home_key = None
        self.scan_yaw = None
        self.prev_hp = None
        self.mode = "start"
        self.deadline = 0.0
        self.last_reload_cmd = -99.0
        self.blocked_goal = {}
        self.climb_req = None
        self.my_comp = -1
        self.main_comp = -1
        if self.geo is not None:
            try:
                self.main_comp = self.geo.main_comp()
            except Exception:
                self.main_comp = -1

    # ----------------------------------------------------------------- helpers
    def time_left(self):
        return self.deadline - time.perf_counter()

    def los(self, a, b, skip_end=1.5, step=1.0):
        g = self.geo
        if g is None:
            return True
        try:
            return g.los(a[0], a[1], a[2], b[0], b[1], b[2], step=step, skip_end=skip_end)
        except Exception:
            return True

    def free(self, x, y):
        g = self.geo
        if g is None:
            return True
        return g.free(x, y)

    def ground(self, x, y):
        g = self.geo
        if g is None:
            return 0.0
        return g.ground(x, y)

    NOW = 0.0  # match time of the observation being handled

    @staticmethod
    def zone_at(z, t):
        """The zone circle at match time t. The server reports the CURRENT circle (already moved
        during a shrink, verified on live observations), so forecast only from now onward."""
        c = z.get("center") or [0.0, 0.0]
        r = float(z.get("radius", 1e4))
        nc = z.get("next_center") or c
        nr = float(z.get("next_radius", r))
        ss = float(z.get("shrink_start", 1e9))
        se = float(z.get("shrink_end", 1e9))
        now = Brain.NOW
        if t <= max(ss, now):
            return c[0], c[1], r
        start = max(ss, now)
        if se <= start or t >= se:
            return nc[0], nc[1], nr
        s = (t - start) / (se - start)
        return c[0] + (nc[0] - c[0]) * s, c[1] + (nc[1] - c[1]) * s, r + (nr - r) * s

    # ----------------------------------------------------------------- memory
    def update_memory(self, obs, me):
        t = self.t
        my_id = me.get("id", self.my_id)
        seen_mechs = set()
        for m in obs.get("mechs") or []:
            mid = m.get("id")
            if mid is None or mid == my_id:
                continue
            rec = {
                "id": mid, "pos": list(m.get("pos") or [0, 0, 0]), "vel": list(m.get("vel") or [0, 0, 0]),
                "aim": list(m.get("aim") or [0, 0]), "hp": m.get("hp"), "max_hp": m.get("max_hp"),
                "weapons": list(m.get("weapons") or []), "charging": bool(m.get("charging")),
                "cubes": m.get("cubes") or 0, "seen": t, "kind": m.get("kind", "mech"),
            }
            old = self.enemies.get(mid) if rec["kind"] != "npc" else self.npcs.get(mid)
            if old is not None and 0.07 <= t - old["seen"] <= 0.13:
                rec["pvel"] = old["vel"]
            if rec["kind"] == "npc":
                self.npcs[mid] = rec
            else:
                seen_mechs.add(mid)
                self.enemies[mid] = rec
                if rec["charging"]:
                    self.charge_seen.setdefault(mid, t)
                else:
                    self.charge_seen.pop(mid, None)
        for mid in list(self.enemies):
            if t - self.enemies[mid]["seen"] > 20.0:
                del self.enemies[mid]
        for mid in list(self.npcs):
            if t - self.npcs[mid]["seen"] > 10.0:
                del self.npcs[mid]
        px, py = me["pos"][0], me["pos"][1]
        # loot memory
        vis_p = set()
        for p in obs.get("pickups") or []:
            if p.get("id") is None:
                continue
            vis_p.add(p["id"])
            self.pickups[p["id"]] = (p.get("kind"), list(p.get("pos") or [0, 0, 0]), t)
        for pid in list(self.pickups):
            k, pos, seen = self.pickups[pid]
            if pid in vis_p:
                continue
            d = hyp(pos[0] - px, pos[1] - py)
            if t - seen > 90.0 or (d < 25.0 and t - seen > 0.5):
                del self.pickups[pid]
        vis_c = set()
        for c in obs.get("crates") or []:
            if c.get("id") is None:
                continue
            vis_c.add(c["id"])
            self.crates[c["id"]] = (list(c.get("pos") or [0, 0, 0]), c.get("hp", 300), t)
        for cid in list(self.crates):
            pos, hp, seen = self.crates[cid]
            if cid in vis_c:
                continue
            d = hyp(pos[0] - px, pos[1] - py)
            if t - seen > 60.0 or (d < 30.0 and t - seen > 0.5):
                del self.crates[cid]
        vis_q = set()
        for c in obs.get("cubes") or []:
            if c.get("id") is None:
                continue
            vis_q.add(c["id"])
            self.cubes_mem[c["id"]] = (list(c.get("pos") or [0, 0, 0]), t)
        for cid in list(self.cubes_mem):
            pos, seen = self.cubes_mem[cid]
            if cid in vis_q:
                continue
            d = hyp(pos[0] - px, pos[1] - py)
            if t - seen > 45.0 or (d < 20.0 and t - seen > 0.5):
                del self.cubes_mem[cid]
        # events
        for ev in obs.get("events") or []:
            et = ev.get("type")
            if et == "damaged":
                self.last_hurt = t
                if ev.get("by") is not None:
                    self.dmg_in.append((t, float(ev.get("amount") or 0.0)))
                by = ev.get("by")
                if by is not None:
                    self.attacked_by[by] = t
                    self.giddy_last_hurt = t
                    if ev.get("bearing") is not None:
                        self.giddy_hurt_bearing = float(ev["bearing"])
                if ev.get("weapon") == "npc" and ev.get("bearing") is not None:
                    self.giddy_npc_hurt = t
                    self.giddy_npc_bearing = float(ev["bearing"])
                b = ev.get("bearing")
                if b is not None:
                    self.hints.append((t, float(b), 3.0))
            elif et == "hit":
                self.last_hit_dealt = t
                if ev.get("target") is not None and ev.get("target") < 16:
                    self.dmg_out.append((t, float(ev.get("amount") or 0.0)))
            elif et == "effect":
                if ev.get("effect") == "short":
                    ls = (self.W.get("laser") or {}).get("short_s")
                    ls = (self.W.get("lightning") or {}).get("short_s", 2.0) if ls is None else ls
                    self.shorted_until = t + float(ls)
            elif et == "take_failed":
                pid = ev.get("id")
                reason = ev.get("reason")
                if pid is not None:
                    self.take_block[pid] = t + (1e9 if reason in ("gone", "have") else 2.0)
                    if reason == "gone":
                        self.pickups.pop(pid, None)
            elif et == "took":
                self.pickups.pop(ev.get("id"), None)
            elif et == "kill":
                self.my_kills += 1
                tid = ev.get("target")
                dead = self.enemies.pop(tid, None)
                if dead is not None:
                    self.kill_spots.append((t, dead["pos"][0], dead["pos"][1], dead["pos"][2]))
        for s in obs.get("sounds") or []:
            k = s.get("kind")
            b = s.get("bearing")
            if b is None:
                continue
            dist = s.get("dist")
            w = {"gun": 1.5, "laser": 2.0, "laser_charge": 2.5, "lightning": 2.0, "mortar": 1.2,
                 "step": 2.0, "boost": 1.8, "jet": 1.8, "reload": 1.5, "explosion": 0.6}.get(k)
            if w is None:
                continue
            if dist == "near":
                w *= 1.6
            elif dist == "far":
                w *= 0.5
            self.hints.append((t - float(s.get("age", 0.0)), float(b), w))
        self.hints = [h for h in self.hints if t - h[0] < 3.0][-24:]
        self.dmg_in = [d for d in self.dmg_in if t - d[0] <= 2.5]
        self.dmg_out = [d for d in self.dmg_out if t - d[0] <= 3.0]

    def update_ffa(self, obs, me, t):
        if self.ffa or not P["ffa_detect"]:
            return
        alive = int(obs.get("alive", 16))
        # The aggregate death count has no cause attribution. Trust it only before the
        # first zone shrink, when ring casualties cannot explain the gap. Crossfire beams
        # remain a high-confidence signal throughout the match.
        if t < self.first_zone_shrink and 16 - alive - self.my_kills >= (2 if P["ffa_fast"] else 3):
            self.ffa = True
            log("FFA latched from unexplained deaths at t=%.1f" % t)
            return
        my_pos = me.get("pos") or [0.0, 0.0, 0.0]
        mechs = obs.get("mechs") or []
        for beam in obs.get("beams") or []:
            by = beam.get("by")
            path = beam.get("path") or []
            if by is None or by == self.my_id or not isinstance(path, list) or len(path) < 2:
                continue
            try:
                by = int(by)
                age = float(beam.get("age", 0.0))
                if by < 0 or by >= 16 or not all(isinstance(p, list) and len(p) >= 3 for p in path):
                    continue
                if any(hyp(float(p[0]) - my_pos[0], float(p[1]) - my_pos[1]) < 20.0 for p in path):
                    continue
                end = [float(v) for v in path[-1][:3]]
                hit_id, hit_d = None, 7.0
                for other in mechs:
                    oid, opos = other.get("id"), other.get("pos")
                    if (oid is None or oid == self.my_id or oid == by or
                            other.get("kind", "mech") != "mech" or not isinstance(opos, list) or len(opos) < 3):
                        continue
                    d = math.sqrt(sum((float(opos[k]) - end[k]) ** 2 for k in range(3)))
                    if d < hit_d:
                        hit_d, hit_id = d, int(oid)
                if hit_id is None:
                    continue
                sig = (by, str(beam.get("kind", "")), hit_id, round(t - age, 2))
                if sig not in self.ffa_beam_seen:
                    self.ffa_beam_seen[sig] = t
                    self.ffa_beam_clues += 1
                    if self.ffa_beam_clues >= 2:
                        self.ffa = True
                        log("FFA latched from crossfire beams at t=%.1f" % t)
                        return
            except (TypeError, ValueError, OverflowError):
                continue
        self.ffa_beam_seen = {k: seen for k, seen in self.ffa_beam_seen.items() if t - seen < 10.0}

    def ffa_engageable(self, e, t, alive, hpf, current_target_id):
        attacked = t - self.attacked_by.get(e["id"], -99.0) < 4.0
        if P["ffa_lurk"] and t < P["lurk_t"] and alive > P["lurk_alive"]:
            d_ = e.get("dist", 1e9)
            return attacked or d_ < 25.0 or (e["id"] == current_target_id and d_ < 30.0)  # let others thin out
        if P["ffa_end"] and alive <= 4:
            if attacked:
                return True
            hp, max_hp = e.get("hp"), e.get("max_hp") or 1400.0
            if hp is None:
                return False
            frac = float(hp) / float(max_hp)
            return frac < 0.30 and e.get("dist", 1e9) < 90.0 and hpf >= frac + 0.10
        if attacked:
            return True
        if e["id"] == current_target_id and t - self.target_since < 1.0:
            return True
        if e.get("dist", 1e9) < 35.0 or alive <= 4:
            return True
        hp, max_hp = e.get("hp"), e.get("max_hp")
        return (hp is not None and max_hp and float(hp) / float(max_hp) < 0.45 and
                e.get("dist", 1e9) < 120.0 and hpf > 0.4)

    def recent_ffa_threat(self, t, px, py):
        tracks = [e for e in self.enemies.values() if t - e.get("seen", -99.0) <= 8.0]
        threat = max(tracks, key=lambda e: (self.attacked_by.get(e["id"], -99.0), e.get("seen", -99.0))) if tracks else None
        threat_t = max(self.attacked_by.get(threat["id"], -99.0), threat.get("seen", -99.0)) if threat else -99.0
        hints = [h for h in self.hints if t - h[0] <= 6.0]
        hint = max(hints, key=lambda h: h[0]) if hints else None
        if hint is not None and hint[0] > threat_t:
            b = hint[1]
            return {"id": None, "pos": [px + math.cos(b) * 40.0, py + math.sin(b) * 40.0, 0.0], "seen": hint[0]}
        return threat

    # ----------------------------------------------------------------- upgrades
    def choose_upgrade(self, offer):
        ch = offer.get("choices") or []
        best, bi = 1e9, 0
        if P["core"] == "giddy":
            for i, c in enumerate(ch):
                cid = c.get("id")
                r = GIDDY_LASER_UPGRADE_RANK.index(cid) if cid in GIDDY_LASER_UPGRADE_RANK else 50
                if r < best:
                    best, bi = r, i
            return bi
        for i, c in enumerate(ch):
            cid = c.get("id")
            rank = UPGRADE_RANK_ECON if (P["econ"] or P["upg"] == "econ") else (
                UPGRADE_RANK_INV if P["upg"] == "inv" else UPGRADE_RANK)
            r = rank.index(cid) if cid in rank else 50
            if r < best:
                best, bi = r, i
        return bi

    # ----------------------------------------------------------------- main
    def act(self, obs, t_recv):
        self.deadline = t_recv + 0.026
        t = float(obs.get("t", 0.0))
        self.t = t
        Brain.NOW = t
        me = obs["self"]
        px, py, pz = me["pos"]
        vx, vy, vz = me.get("vel") or [0.0, 0.0, 0.0]
        stats = me.get("stats") or {}
        my_aim = me.get("aim") or [0.0, 0.0]
        hp = float(me.get("hp", 1.0))
        max_hp = float(me.get("max_hp", 1400.0)) or 1.0
        hpf = hp / max_hp
        energy = float(me.get("energy", 0.0))
        cubes = float(me.get("cubes", 0))
        self.update_memory(obs, me)
        self.update_ffa(obs, me, t)
        alive = int(obs.get("alive", 16))
        ffa_active = self.ffa and P["ffa_engage"]
        core_giddy = P["core"] == "giddy"
        self.gun_busy = False
        self.laser_charging_now = any(w and w.get("kind") == "laser" and (w.get("charge") or 0) > 0 for w in (me.get("weapons") or []))
        self.has_special = any(w and w.get("kind") in ("laser", "lightning") for w in (me.get("weapons") or []))
        self.muzzle_h = float((me.get("stats") or {}).get("muzzle_height", 7.0))
        self.eye_h = float((me.get("stats") or {}).get("eye_height", 8.0))
        self.mech_height = float((me.get("stats") or {}).get("height", 9.0))
        self.pz_now = float(me["pos"][2])
        for w in (me.get("weapons") or []):
            if w and w.get("kind") == "gun" and ((w.get("reload") or 0) > 0.25 or (w.get("ammo") is not None and w.get("ammo") <= 0)):
                self.gun_busy = True
        self.climb_req = None
        self.my_comp = -1
        if self.geo is not None:
            try:
                self.my_comp = self.geo.comp_at(px, py)
            except Exception:
                self.my_comp = -1
        act = {}
        offer = obs.get("offer")
        if offer:
            act["pick"] = self.choose_upgrade(offer)

        slots = list(me.get("weapons") or [None, None])
        while len(slots) < 2:
            slots.append(None)
        kinds = [w.get("kind") if w else None for w in slots]
        held = set(k for k in kinds if k)
        muzzle_h = float(stats.get("muzzle_height", 7.0))
        eye_h = float(stats.get("eye_height", 8.0))
        turn_rate = float(stats.get("turn_rate", 4.0))
        pitch_rate = float(stats.get("pitch_rate", 3.0))
        walk = float(stats.get("walk_speed", 11.7))
        min_pitch = float(stats.get("min_pitch", -0.7))
        max_pitch = float(stats.get("max_pitch", 1.3))
        mz = pz + muzzle_h
        eye = (px, py, pz + eye_h)
        muzzle = (px, py, mz)

        zone = obs.get("zone") or {}
        self._giddy_zone = zone
        zx, zy, zr = self.zone_at(zone, t)
        lx, ly, lr = self.zone_at(zone, t + 6.0)
        nx_, ny_ = (zone.get("next_center") or [zx, zy])[:2]
        nr = float(zone.get("next_radius", zr))
        ss = float(zone.get("shrink_start", 1e9))

        # ---------------------------------------------------------- targets
        cands = []
        for e in self.enemies.values():
            if t - e["seen"] > 0.01:
                continue  # only what this observation lists
            ex, ey, ez = e["pos"]
            d = hyp(ex - px, ey - py)
            cands.append((d, e))
        cands.sort(key=lambda c: c[0])
        tlist = []
        for d, e in cands[:(len(cands) if (ffa_active or core_giddy) else (10 if P["tsel"] else 6))]:
            ex, ey, ez = e["pos"]
            sc = 1.0 + self.size_per_cube * float(e.get("cubes") or 0)
            com = (ex, ey, ez + 4.5 * sc)
            visible = self.los(muzzle, com, skip_end=2.5 * sc)
            if not visible:  # try head height
                visible = self.los(muzzle, (ex, ey, ez + 8.0 * sc), skip_end=2.5 * sc)
                if visible:
                    com = (ex, ey, ez + 7.0 * sc)
            e["los"] = visible
            e["com"] = com
            e["dist"] = d
            tlist.append(e)

        target = None
        best = 1e18
        avoid = None
        current_target_id = self.target_id
        late = float(zone_obs_next_r(obs)) <= 70.0 or int(obs.get("alive") or 16) <= 3
        for e in tlist:
            d = e["dist"]
            ehp = e.get("hp") or 1400.0
            if core_giddy:
                e["engage"] = late or self.engageable(e, px, py, t, vx, vy)
            if ffa_active and not self.ffa_engageable(e, t, alive, hpf, current_target_id):
                if avoid is None or d < avoid["dist"]:
                    avoid = e
                continue
            if core_giddy:
                if not e["engage"] or not e["los"] or d > 240.0:
                    continue
                emax = e.get("max_hp") or 1400.0
                s = d + P["g_hpw"] * float(ehp)
                if float(ehp) <= 0.3 * float(emax):
                    s -= 200.0
                if e["id"] == self.giddy_stick_id and t - self.giddy_stick_at < 2.0:
                    s -= 200.0
                if t - self.attacked_by.get(e["id"], -99.0) < 4.0:
                    s -= 40.0
                ex_, ey_ = e["pos"][0], e["pos"][1]
                ux_, uy_ = unit(px - ex_, py - ey_)
                ev_ = e.get("vel") or [0.0, 0.0, 0.0]
                closing = (ev_[0] - vx) * ux_ + (ev_[1] - vy) * uy_
                e["closing"] = closing
                if closing > 3.0:
                    s -= 15.0
                if s < best:
                    best, target = s, e
                continue
            if P["select"] and not late and not self.engageable(e, px, py, t, vx, vy):
                continue
            emax = e.get("max_hp") or 1400.0
            s = d
            s -= (1.0 - ehp / emax) * 90.0
            if P["tsel"]:
                s = float(ehp) * (1.0 + d / 120.0) + (0.0 if e["los"] else 1e6)
            if P["select"]:
                # giddygiddyboy's practice scoring: distance + 0.2*HP, sticky, low-HP/attacker/closing bonuses
                if not e["los"] or d > 240.0:
                    continue
                s = d + 0.2 * float(ehp)
                if float(ehp) <= 0.3 * float(emax):
                    s -= 100.0
                if e["id"] == self.target_id and t - self.target_t_last < 2.0:
                    s -= 120.0
                if t - self.attacked_by.get(e["id"], -99.0) < 4.0:
                    s -= 40.0
                ux_, uy_ = unit(px - e["pos"][0], py - e["pos"][1])
                ev_ = e.get("vel") or [0.0, 0.0, 0.0]
                if ev_[0] * ux_ + ev_[1] * uy_ > 3.0:
                    s -= 15.0
            if t - self.attacked_by.get(e["id"], -99) < 3.0:
                s -= 50.0
            if not e["los"]:
                s += 140.0
            if e["id"] == self.target_id:
                s -= 35.0
            if e["charging"]:
                s -= 25.0
            if s < best:
                best, target = s, e
        if ffa_active and target is None:
            for e in self.enemies.values():
                if t - e.get("seen", -99.0) > 1.5 or self.ffa_engageable(e, t, alive, hpf, current_target_id):
                    continue
                d = hyp(e["pos"][0] - px, e["pos"][1] - py)
                if avoid is None or d < avoid.get("dist", 1e9):
                    e["dist"] = d
                    avoid = e
        if target is not None and target["id"] != self.target_id:
            self.target_id = target["id"]
            self.target_since = t
        if target is not None:
            self.target_t_last = t
            if core_giddy:
                self.giddy_stick_id, self.giddy_stick_at = target["id"], t
        if target is None:
            self.target_id = None

        threats = [e for e in self.enemies.values() if t - e["seen"] < 8.0]
        visible_threats = [e for e in tlist if e["los"]]
        giddy_danger = False
        if core_giddy:
            giddy_danger, _, _, _ = self.giddy_combat_state(t, me, tlist, threats, target, hpf)
            self._giddy_current_loot = (self.giddy_choose_loot(obs, hpf, giddy_danger, kinds)
                                        if not ffa_active and target is None else None)
        else:
            self._giddy_current_loot = None

        # NPCs attacking us
        npc_target = None
        if core_giddy:
            if t - self.giddy_npc_hurt < 3.0:
                best_off = 0.7
                for n in self.npcs.values():
                    if t - n["seen"] > 0.5:
                        continue
                    d = hyp(n["pos"][0] - px, n["pos"][1] - py)
                    if d > 150.0:
                        continue
                    nc = self.cfg.get("npc") or {}
                    com = (n["pos"][0], n["pos"][1], n["pos"][2] + float(nc.get("height", 4.0)) * 0.5)
                    if not self.los(muzzle, com):
                        continue
                    off = abs(wrap(math.atan2(n["pos"][1] - py, n["pos"][0] - px) -
                                   self.giddy_npc_bearing))
                    if t - self.attacked_by.get(n["id"], -99.0) < 3.0:
                        off = 0.0
                    if off < best_off:
                        best_off, npc_target = off, n
                        n["dist"], n["los"] = d, True
        else:
            for n in self.npcs.values():
                if t - n["seen"] > 0.01:
                    continue
                d = hyp(n["pos"][0] - px, n["pos"][1] - py)
                n["dist"] = d
                if t - self.attacked_by.get(n["id"], -99) < 4.0 or d < 40.0:
                    if npc_target is None or d < npc_target["dist"]:
                        npc_target = n
        if ffa_active and target is None and not P["ffa_econ"]:
            npc_target = None

        giddy_farm = None
        if core_giddy and not ffa_active and target is None:
            if npc_target is not None:
                giddy_farm = npc_target
            else:
                choice = self._giddy_current_loot
                if choice is not None and choice["kind"] == "npc":
                    n = self.npcs.get(choice["id"])
                    if n is not None:
                        d = hyp(n["pos"][0] - px, n["pos"][1] - py)
                        com = (n["pos"][0], n["pos"][1], n["pos"][2] + float(self.npc_cfg.get("height", 4.0)) * 0.5)
                        if t - n["seen"] < 1.0 and d < 75.0 and self.los(muzzle, com):
                            giddy_farm = n
                elif choice is not None and choice["kind"] == "crate":
                    c = self.crates.get(choice["id"])
                    if c is not None:
                        pos, chp, seen = c
                        d = hyp(pos[0] - px, pos[1] - py)
                        cp = (pos[0], pos[1], pos[2] + float((self.cfg.get("crates") or {}).get("height", 5.0)) * 0.5)
                        if d < 50.0 and self.los(muzzle, cp):
                            giddy_farm = {"id": choice["id"], "pos": pos, "kind": "crate", "hp": chp,
                                          "vel": [0.0, 0.0, 0.0]}

        ffa_face_yaw = None
        if self.ffa and target is None and ((P["ffa_engage"] and avoid is not None) or
                                            (P["ffa_hold"] and alive <= 6)):
            face = avoid or self.recent_ffa_threat(t, px, py)
            if face is not None:
                ffa_face_yaw = math.atan2(face["pos"][1] - py, face["pos"][0] - px)

        # ---------------------------------------------------------- aim & fire
        fire = [False, False]
        aim_yaw, aim_pitch = None, 0.0
        aim_point = None
        lead_speed = None
        shoot = None  # (point3, vel3, kind:'mech'/'npc'/'crate', radius, height, los)
        if target is not None:
            shoot = target
        elif core_giddy and not ffa_active and giddy_farm is not None:
            shoot = giddy_farm
        elif npc_target is not None:
            shoot = npc_target
        farm = giddy_farm if core_giddy and not ffa_active else None
        if not core_giddy and shoot is None and not threats and (not ffa_active or P["ffa_econ"]):
            farm = self.pick_farm(px, py, pz, muzzle, hpf, zx, zy, zr)
            if farm is not None:
                shoot = farm

        laser_slot = kinds.index("laser") if "laser" in kinds else None
        gun_slot = kinds.index("gun") if "gun" in kinds else None
        lit_slot = kinds.index("lightning") if "lightning" in kinds else None
        laser_w = slots[laser_slot] if laser_slot is not None else None
        gun_w = slots[gun_slot] if gun_slot is not None else None
        lit_w = slots[lit_slot] if lit_slot is not None else None
        laser_charge = float(laser_w.get("charge", 0.0)) if laser_w else 0.0
        lst = (laser_w or {}).get("stats") or self.W.get("laser") or {}
        laser_full = float(lst.get("charge", 0.75))
        laser_hold = float(lst.get("hold_s", 2.0))
        dmgmul = 1.0 + self.dmg_per_cube * min(cubes, self.stat_cap)

        if shoot is not None:
            sp = shoot.get("pos") or shoot.get("_pos")
            kind = shoot.get("kind", "mech")
            if kind == "mech":
                com = shoot["com"]
                rad = 2.5 * (1.0 + self.size_per_cube * float(shoot.get("cubes") or 0))
            elif kind == "npc":
                com = (sp[0], sp[1], sp[2] + 1.75)
                rad = 1.8
            else:
                com = (sp[0], sp[1], sp[2] + 1.2)
                rad = 1.4
            svel = shoot.get("vel") or [0.0, 0.0, 0.0]
            pvel = shoot.get("pvel")
            if kind == "mech":
                los_ok = shoot["los"]
            else:
                los_ok = self.los(muzzle, com, skip_end=rad + 0.5)
            ox, oy = px, py
            if P["selfcomp"]:
                ox, oy = px + vx * 0.075, py + vy * 0.075  # where the muzzle is when the shot leaves
            d3 = math.sqrt((com[0] - ox) ** 2 + (com[1] - oy) ** 2 + (com[2] - mz) ** 2)
            gst = (gun_w or {}).get("stats") or self.W.get("gun") or {}
            gspeed = float(gst.get("speed", 252.0))
            grange = float(gst.get("range", 230.0))
            # laser about to fire -> aim hitscan, else lead for the gun
            hitscan = laser_w is not None and laser_charge >= laser_full - 0.12 and los_ok
            if gun_w is None:
                hitscan = True
            lat = 0.075 if P["selfcomp"] else 0.05
            if hitscan:
                tp = (com[0] + svel[0] * 0.07, com[1] + svel[1] * 0.07, com[2] + clamp(svel[2] * 0.07, -1, 1))
            else:
                tlead = (max(0.0, d3 - 3.0) if P["selfcomp"] else d3) / gspeed + lat
                tp = com
                for _ in range(3):
                    vzl = clamp(svel[2] * tlead, -3.0, 3.0)
                    ex_, ey_ = lead_vel(svel, pvel, tlead)
                    tp = (com[0] + ex_ * tlead, com[1] + ey_ * tlead, com[2] + vzl)
                    dd = math.sqrt((tp[0] - ox) ** 2 + (tp[1] - oy) ** 2 + (tp[2] - mz) ** 2)
                    tlead = (max(0.0, dd - 3.0) if P["selfcomp"] else dd) / gspeed + lat
            hd = hyp(tp[0] - ox, tp[1] - oy)
            aim_yaw = math.atan2(tp[1] - oy, tp[0] - ox)
            aim_pitch = clamp(math.atan2(tp[2] - mz, max(hd, 0.1)), min_pitch, max_pitch)
            yaw_err = abs(wrap(aim_yaw - my_aim[0]))
            pit_err = abs(aim_pitch - my_aim[1])
            # error left after one sim tick of slewing
            ye1 = max(0.0, yaw_err - turn_rate * 0.05)
            pe1 = max(0.0, pit_err - pitch_rate * 0.05)
            tol_y = math.atan2(rad * P["tol"], max(hd, 1.0))
            tol_p = math.atan2(2.5 if kind == "mech" else 0.8, max(hd, 1.0))
            aligned = ye1 <= tol_y and pe1 <= tol_p
            aligned_now = yaw_err <= tol_y and pit_err <= tol_p
            busy_fight = kind != "mech" and bool(visible_threats)
            gun_ok = aligned
            if P["gtol"] and gun_w is not None:
                # the gun's own lead solution, judged on its own (not on a laser's hitscan aim)
                tl = d3 / gspeed + 0.05
                gp = com
                for _ in range(2):
                    gx_, gy_ = lead_vel(svel, pvel, tl)
                    gp = (com[0] + gx_ * tl, com[1] + gy_ * tl, com[2] + clamp(svel[2] * tl, -3.0, 3.0))
                    tl = math.sqrt((gp[0] - px) ** 2 + (gp[1] - py) ** 2 + (gp[2] - mz) ** 2) / gspeed + 0.05
                ghd = hyp(gp[0] - px, gp[1] - py)
                gy_err = abs(wrap(math.atan2(gp[1] - py, gp[0] - px) - my_aim[0]))
                gp_err = abs(clamp(math.atan2(gp[2] - mz, max(ghd, 0.1)), min_pitch, max_pitch) - my_aim[1])
                gtl = 1.2 * math.atan2(2.2 * (rad / 2.5), max(ghd, 1.0))
                gun_ok = (max(0.0, gy_err - turn_rate * 0.05) <= gtl and
                          max(0.0, gp_err - pitch_rate * 0.05) <= 2.0 * gtl + 0.03)
            if los_ok and not busy_fight:
                # gun
                if gun_w is not None and d3 < grange * 0.98 and gun_ok:
                    if (gun_w.get("reload") or 0) <= 0 and (gun_w.get("ammo") is None or gun_w.get("ammo", 1) > 0):
                        fire[gun_slot] = True
                # lightning: auto-targets nearest in its cone
                if lit_w is not None:
                    lw = lit_w.get("stats") or self.W.get("lightning") or {}
                    lrange = float(lw.get("range", 45.0))
                    lcost = float(lw.get("energy", 3.0))
                    if hd < lrange * 0.93 and yaw_err < math.radians(10) and energy >= lcost + 2:
                        if (lit_w.get("cooldown") or 0) <= 0.1:
                            fire[lit_slot] = True
            # laser charge / release
            if laser_w is not None:
                lrange = float(lst.get("range", 280.0))
                charging = laser_charge > 0.0
                in_range = d3 < lrange * 0.97
                worth = kind == "mech" or (kind == "npc") or (kind == "crate" and not threats)
                if charging:
                    # hold until full and aligned (hitscan aim), then let go
                    full = laser_charge >= laser_full - 0.051
                    mn = float(lst.get("min_charge", 0.15))
                    frac = float(lst.get("min_frac", 0.35))
                    cur_dmg = float(lst.get("damage", 315)) * (frac + (1 - frac) * min(1.0, laser_charge / laser_full)) * dmgmul
                    ehp = shoot.get("hp") if kind != "crate" else shoot.get("hp", 300)
                    kill_now = ehp is not None and laser_charge >= mn and cur_dmg >= float(ehp) and aligned_now
                    overtime = laser_charge >= laser_full + laser_hold - 0.35
                    if los_ok and in_range and ((full and aligned) or kill_now):
                        fire[laser_slot] = False  # release -> fires
                    elif overtime:
                        fire[laser_slot] = False
                    else:
                        fire[laser_slot] = True
                else:
                    ready = (laser_w.get("cooldown") or 0) <= 0.05 and t >= self.shorted_until
                    pre = P["precharge"] and kind == "mech" and d3 < 60.0
                    if ready and (los_ok or pre) and in_range and worth and yaw_err < 0.6:
                        fire[laser_slot] = True
            aim_point = tp
        else:
            # keep a charging laser from wasting itself: hold until overtime, then release anyway
            if laser_w is not None and laser_charge > 0.0:
                fire[laser_slot] = laser_charge < laser_full + laser_hold - 0.35

        if core_giddy and not (ffa_active and target is None):
            aim_yaw, aim_pitch, fire = self.giddy_aim_fire(obs, shoot, slots, tlist)

        if aim_yaw is None and not core_giddy:
            aim_yaw, aim_pitch = self.scan_aim(t, px, py, my_aim, threats, vx, vy)
        if ffa_face_yaw is not None:
            aim_yaw, aim_pitch = ffa_face_yaw, 0.0

        act["fire"] = fire

        # reload when idle
        if gun_w is not None and gun_w.get("ammo") is not None:
            gst = gun_w.get("stats") or {}
            mag = float(gst.get("mag", 12))
            if core_giddy:
                if (gun_w.get("reload") or 0) <= 0 and gun_w["ammo"] < mag and target is None and \
                        farm is None and (gun_w["ammo"] <= mag * 0.6 or t - self.giddy_last_threat_at > 3.0):
                    act["reload"] = True
            elif (gun_w.get("reload") or 0) <= 0 and not fire[gun_slot]:
                if gun_w["ammo"] <= 0 or (gun_w["ammo"] < mag * 0.5 and not visible_threats and shoot is None):
                    act["reload"] = True

        # ---------------------------------------------------------- weapon pickups
        take = (self.giddy_choose_take(px, py, pz, kinds, slots) if core_giddy else
                self.choose_take(px, py, pz, kinds, laser_charge))
        if take is not None:
            act["take"] = take[0]
            if take[1] is not None:
                act["drop"] = take[1]

        # ---------------------------------------------------------- movement
        move, boost, jump, label = self.movement(obs, me, t, px, py, pz, vx, vy, hpf, energy, kinds,
                                                  target, tlist, threats, visible_threats,
                                                  zx, zy, zr, lx, ly, lr, nx_, ny_, nr, ss, walk,
                                                  laser_charge > 0, farm, avoid)
        mx, my = move
        n = hyp(mx, my)
        if n > 1.0:
            mx, my = mx / n, my / n
        if core_giddy and aim_yaw is None:
            aim_yaw, aim_pitch = self.giddy_idle_aim(t, px, py, mx, my, my_aim, stats)
        if ffa_face_yaw is not None:
            aim_yaw, aim_pitch = ffa_face_yaw, 0.0
        act["aim"] = [round(aim_yaw, 4), round(aim_pitch, 4)]
        act["move"] = [round(mx, 3), round(my, 3)]
        act["boost"] = bool(boost)
        act["jump"] = bool(jump)
        self.hist.append((t, px, py, hyp(mx, my), hyp(vx, vy)))
        if len(self.hist) > 40:
            del self.hist[:-40]
        self.prev_hp = hp
        act["debug"] = {"label": ("%s hp%d e%d c%d" % (label, hp, energy, cubes))[:80]}
        if TRACE is not None and int(obs.get("tick", 0)) % 10 == 0:
            try:
                vis = ["%d:%d@%d%s" % (e["id"], e.get("hp") or 0, e["dist"], "L" if e["los"] else "") for e in tlist]
                TRACE.write("t%.1f pos(%.0f,%.0f,%.0f) hp%.0f e%.0f in_zone=%s zone(%.0f,%.0f r%.0f) next(%.0f,%.0f r%.0f) %s ffa=%s k%d b%d mv=%s w=%s tgt=%s c%d alive%s vis=%s\n" % (
                    t, px, py, pz, hp, energy, me.get("in_zone"), zx, zy, zr, nx_, ny_, nr, label, self.ffa,
                    self.my_kills, self.ffa_beam_clues,
                    act["move"], kinds, self.target_id, cubes, obs.get("alive"), " ".join(vis)))
                TRACE.flush()
            except Exception:
                pass
        return act

    # ----------------------------------------------------------------- farming
    def pick_farm(self, px, py, pz, muzzle, hpf, zx, zy, zr):
        """A crate or a passive NPC to shoot while no mech is around."""
        best, bs = None, 1e18
        for cid, (pos, chp, seen) in self.crates.items():
            if self.t - seen > 0.01 or (P["econ"] and hpf <= 0.4):
                continue
            d = hyp(pos[0] - px, pos[1] - py)
            if d > 110:
                continue
            if hyp(pos[0] - zx, pos[1] - zy) > zr - 5:
                continue
            s = d
            if s < bs:
                rec = {"id": cid, "pos": pos, "kind": "crate", "hp": chp, "vel": [0, 0, 0]}
                best, bs = rec, s
        if hpf > (0.5 if (P["econ"] or (self.ffa and P["ffa_econ"])) else 0.6):
            for n in self.npcs.values():
                if self.t - n["seen"] > 0.01:
                    continue
                d = hyp(n["pos"][0] - px, n["pos"][1] - py)
                nhp = n.get("hp")
                if d > 100 or (nhp is not None and nhp <= 0):
                    continue
                s = d + 30
                if s < bs:
                    best, bs = n, s
        return best

    # ----------------------------------------------------------------- pickups
    def pickup_gain(self, kind, kinds, laser_charging):
        """(gain, drop_slot) for taking a weapon of `kind`, or None."""
        if kind not in W_VAL or W_VAL[kind] <= 0 or kind in kinds:
            return None
        if None in kinds:
            return (W_VAL[kind], None)
        worst, ws = None, 1e9
        for i, k in enumerate(kinds):
            v = W_VAL.get(k, 0.0)
            if k == "laser" and laser_charging:
                continue
            if v < ws:
                ws, worst = v, i
        if worst is None:
            return None
        if W_VAL[kind] > ws + 0.5:
            return (W_VAL[kind] - ws, worst)
        return None

    def choose_take(self, px, py, pz, kinds, laser_charge):
        best = None
        for pid, (kind, pos, seen) in self.pickups.items():
            if self.take_block.get(pid, -1) > self.t:
                continue
            if self.t - seen > 0.01:
                continue
            if hyp(pos[0] - px, pos[1] - py) > self.loot_r - 0.3 or abs(pos[2] - pz) > 3.8:
                continue
            g = self.pickup_gain(kind, kinds, laser_charge > 0)
            if g is None:
                continue
            if best is None or g[0] > best[2]:
                best = (pid, g[1], g[0])
        if best is None:
            return None
        return best[0], best[1]

    # ----------------------------------------------------------------- aim when idle
    def scan_aim(self, t, px, py, my_aim, threats, vx, vy):
        # most recent strong hint (damage, sounds) or remembered enemies
        wsum_x = wsum_y = 0.0
        for (ht, b, w) in self.hints:
            age = t - ht
            f = w * max(0.0, 1.0 - age / 3.0)
            wsum_x += math.cos(b) * f
            wsum_y += math.sin(b) * f
        for e in threats:
            age = t - e["seen"]
            f = 2.5 * max(0.0, 1.0 - age / 8.0)
            b = math.atan2(e["pos"][1] - py, e["pos"][0] - px)
            wsum_x += math.cos(b) * f
            wsum_y += math.sin(b) * f
        if hyp(wsum_x, wsum_y) > 0.8:
            self.scan_yaw = None
            return math.atan2(wsum_y, wsum_x), 0.0
        # sweep the horizon
        if self.scan_yaw is None:
            self.scan_yaw = my_aim[0]
        self.scan_yaw = wrap(self.scan_yaw + 0.28)
        return self.scan_yaw, -0.05

    # ----------------------------------------------------------------- movement
    def nav_to(self, px, py, gx, gy, allow_climb=False):
        """Direction toward goal along a planned path."""
        dx, dy = gx - px, gy - py
        d = hyp(dx, dy)
        if d < 0.8:
            return 0.0, 0.0
        g = self.geo
        if g is None:
            return dx / d, dy / d
        try:
            if d < 70.0 and g.seg_free(px, py, gx, gy):
                self.path = None
                return dx / d, dy / d
            cg = g.comp_at(gx, gy)
            if allow_climb and self.my_comp >= 0 and cg >= 0 and cg != self.my_comp:
                # walled off from the goal (stadium bowl, courtyard): jet over, straight at it
                self.path = None
                self.climb_req = (dx / d, dy / d)
                return dx / d, dy / d
            need = (self.path is None or self.path_goal is None or
                    hyp(self.path_goal[0] - gx, self.path_goal[1] - gy) > 6.0 or
                    self.t - self.path_t > 4.0 or not self.path)
            skipped = False
            if need:
                if self.time_left() > 0.012:
                    p = g.astar(px, py, gx, gy, max_nodes=2500, deadline=self.deadline - 0.004)
                    self.path = p
                    self.path_goal = (gx, gy)
                    self.path_t = self.t
                else:
                    skipped = True
            path = self.path
            if not path:
                # no route: straight if clear; if the search was only skipped for time, wait one
                # decision instead of walking into a wall; if it failed, slide along walls anyway
                if g.seg_free(px, py, gx, gy) or not skipped:
                    return dx / d, dy / d
                return 0.0, 0.0
            while len(path) > 1 and hyp(path[0][0] - px, path[0][1] - py) < 3.0:
                path.pop(0)
            tgt = path[0]
            top = min(len(path), 14) - 1
            for k in range(top, 0, -1):
                if g.seg_free(px, py, path[k][0], path[k][1]):
                    tgt = path[k]
                    del path[:k]
                    break
            ux, uy = unit(tgt[0] - px, tgt[1] - py)
            if ux == 0.0 and uy == 0.0:
                return dx / d, dy / d
            return ux, uy
        except Exception:
            log(traceback.format_exc())
            return dx / d, dy / d

    def in_zone_pt(self, x, y, cx, cy, r, margin):
        return hyp(x - cx, y - cy) <= r - margin

    def find_cover(self, px, py, threats, cx, cy, cr):
        g = self.geo
        if g is None or not threats:
            return None
        cands = g.cover_near(px, py, 55.0)
        cands = [c for c in cands if hyp(c[0] - cx, c[1] - cy) < cr - 6.0 and self.reachable(c[0], c[1])]
        cands.sort(key=lambda c: (c[0] - px) ** 2 + (c[1] - py) ** 2)
        ths = sorted(threats, key=lambda e: hyp(e["pos"][0] - px, e["pos"][1] - py))[:3]
        for c in cands[:40]:
            if self.time_left() < 0.006:
                break
            gz = g.ground(c[0], c[1])
            ok = True
            for e in ths:
                ex, ey, ez = e["pos"]
                # cover must not be closer to the threat than ~25 m
                if hyp(ex - c[0], ey - c[1]) < 25.0:
                    ok = False
                    break
                if g.los(ex, ey, ez + 8.0, c[0], c[1], gz + 6.0, step=1.5):
                    ok = False
                    break
            if ok:
                return c
        return None

    def movement(self, obs, me, t, px, py, pz, vx, vy, hpf, energy, kinds, target, tlist, threats,
                 visible_threats, zx, zy, zr, lx, ly, lr, nx_, ny_, nr, ss, walk, charging, farm,
                 ffa_avoid=None):
        boost = False
        jump = False
        label = ""
        grounded = bool(me.get("grounded", True))
        in_zone = bool(me.get("in_zone", True))
        # safe circle we must stay in (the zone 6 s from now, with margin)
        margin = clamp(0.12 * lr, 3.0, 12.0)
        dz = hyp(px - lx, py - ly)
        self.safe_circle = (lx, ly, lr)

        mv = None
        giddy_active = P["core"] == "giddy" and not (self.ffa and P["ffa_engage"])
        # ---- 1. mortar shells / bullets about to hit us
        dodge = None if giddy_active else self.projectile_dodge(obs, px, py, pz, vx, vy)
        shell_dodge = self.giddy_dodge_shell(obs, px, py, pz, me) if giddy_active else None

        # ---- 2. zone emergency
        zone_push = None
        if not giddy_active and ((not in_zone) or dz > lr - margin * 0.5):
            if lr > 1.0:
                f = max(0.0, (lr - margin * 1.5)) / max(dz, 1e-6)
                gx, gy = lx + (px - lx) * min(1.0, f), ly + (py - ly) * min(1.0, f)
            else:
                gx, gy = lx, ly
            zone_push = self.snap_goal(gx, gy, lx, ly, lr)

        hp_low = hpf < P["retreat_hp"]
        if P["berserk"]:
            hp_low = False
        if self.ffa and P["ffa_heal"]:
            rate_in = sum(a for _, a in self.dmg_in) / 2.5
            rate_out = max(40.0, sum(a for _, a in self.dmg_out) / 3.0)
            ttd = float(me.get("hp", 1.0)) / rate_in if rate_in > 1.0 else 1e9
            ttk = (float(target.get("hp") or 1400.0) / rate_out) if target is not None else 1e9
            start = ((hpf < 0.65 and len(visible_threats) >= 2) or (ttd < 0.9 * ttk and ttd < 8.0) or
                     (hpf < 0.35 and t - self.last_hurt < 4.0))
            if start:
                self.ffa_healing = True
            elif self.ffa_healing and hpf >= 0.75:
                self.ffa_healing = False
            if target is not None and target.get("hp") is not None and float(target["hp"]) < 200 and hpf > 0.25:
                self.ffa_healing = False  # finish it
            hp_low = self.ffa_healing
        if P["race"] and not P["berserk"]:
            hp_now = float(me.get("hp", 1.0))
            rate_in = sum(a for _, a in self.dmg_in) / 2.5
            rate_out = max(40.0, sum(a for _, a in self.dmg_out) / 3.0)
            ttd = hp_now / rate_in if rate_in > 1.0 else 1e9
            ehp = float(target.get("hp") or 1400.0) if target is not None else 1e9
            ttk = ehp / rate_out
            losing = ttd < ttk * P["race_margin"] and ttd < P["race_ttd"]
            outnumbered = len(visible_threats) >= 2 and hpf < 0.6
            hp_low = losing or outnumbered or (hpf < 0.2 and ttk > 2.0)
        if hp_low and P["commit"] and target is not None and len(visible_threats) <= 1:
            ehp = target.get("hp")
            if ehp is not None and float(ehp) <= float(me.get("hp", 0)) * P["commit_ratio"]:
                hp_low = False  # a winnable duel: running only hands the hunter free shots
        fight = target is not None and (target["los"] or t - self.attacked_by.get(target["id"], -99) < 3.0
                                         or target["dist"] < 120)

        if giddy_active:
            gx, gy = self.giddy_zone_goal(obs.get("zone") or {}, px, py)
            zdist = hyp(gx - px, gy - py)
            t_need = zdist * 1.45 / max(1.0, walk) + 4.0
            deadline = self.giddy_zone_deadline(obs.get("zone") or {}, px, py, 8.0)
            zone_must = (not in_zone) or t + t_need + 3.0 >= deadline
            zone_should = t + t_need + 14.0 >= deadline
            late = nr <= 70.0 or int(obs.get("alive", 16)) <= 3
            hp_now = float(me.get("hp", 1.0))
            finishing = target is not None and target["dist"] < 120.0 and (
                float(target.get("hp") or 1400.0) < 200.0 or float(target.get("hp") or 1400.0) < hp_now * 0.5)
            if shell_dodge is not None:
                mv, boost, label = shell_dodge, energy > 15.0, "dodge-shell"
            elif zone_must and zdist > 2.0:
                mv = self.nav_to(px, py, gx, gy, allow_climb=True)
                boost = (not in_zone or t + t_need >= deadline) and energy > 5.0
                label = "zone"
            elif self.giddy_retreat and not finishing and not late:
                if self.cover_goal is None or t - self.cover_t > 0.4:
                    recent_threats = [e for e in threats if t - e.get("seen", -99.0) <= 2.5]
                    self.cover_goal = self.giddy_cover_goal(px, py, recent_threats, obs.get("zone") or {})
                    self.cover_t = t
                if self.cover_goal is not None and hyp(self.cover_goal[0] - px, self.cover_goal[1] - py) > 1.5:
                    mv = self.nav_to(px, py, self.cover_goal[0], self.cover_goal[1])
                    boost = energy > 10.0 and hyp(self.cover_goal[0] - px, self.cover_goal[1] - py) > 2.0
                    label = "cover"
                elif self.cover_goal is not None:
                    mv, label = (0.0, 0.0), "hide"
                elif target is not None:
                    mv = self.giddy_fight_move(t, px, py, kinds, target, energy, tlist, 1.0)
                    label = "cornered"
                else:
                    mv = self.nav_to(px, py, gx, gy, allow_climb=True)
                    label = "flee"
            elif target is not None:
                weapons = [w for w in (me.get("weapons") or []) if w]
                gw = next((w for w in weapons if w.get("kind") == "gun"), None)
                lw = next((w for w in weapons if w.get("kind") == "laser"), None)
                lit = next((w for w in weapons if w.get("kind") == "lightning"), None)
                gs = (gw or {}).get("stats") or self.W.get("gun") or {}
                ls = (lw or {}).get("stats") or self.W.get("laser") or {}
                lts = (lit or {}).get("stats") or self.W.get("lightning") or {}
                d = target["dist"]
                ready = (gw is not None and float(gw.get("ammo", 0)) > 0 and
                         float(gw.get("reload", 0.0)) <= 0.25 and d <= float(gs.get("range", 230.0)) - 10.0)
                hot = (lw is not None and float(lw.get("charge", 0.0)) >= float(ls.get("charge", 0.75)) - 0.3 and
                       d <= float(ls.get("range", 280.0)) - 5.0)
                if hot or (lit is not None and d <= float(lts.get("range", 45.0)) + 6.0 and
                           energy >= float(lts.get("energy", 3.0)) * 3.0):
                    ready = True
                if lit is not None or lw is not None:  # squadParams.Peek=false
                    ready = True
                want_los = 1.0 if ready else -1.0
                crowd = sum(1 for e in tlist if e.get("engage") or
                            (e.get("los") and e.get("dist", 1e9) < 150.0))
                finish_range = (20.0 if float(target.get("hp") or 1400.0) <
                                0.38 * float(target.get("max_hp") or 1400.0) and hpf > 0.42 and crowd <= 2 else None)
                mv = self.giddy_fight_move(t, px, py, kinds, target, energy, tlist, want_los, finish_range)
                if lit is not None and d > 50.0 and energy > 55.0 and want_los > 0.0:
                    boost = True
                if P["g_fb"] and energy > 40.0 and not self.laser_charging_now and (lit is None or energy > 60.0):
                    boost = True
                radius = 70.0 * (0.57 if hpf <= 0.5 else 1.0)
                nearest, nearest_d = None, radius
                for cpos, seen in self.cubes_mem.values():
                    d_cube = hyp(cpos[0] - px, cpos[1] - py)
                    if d_cube < nearest_d:
                        nearest, nearest_d = cpos, d_cube
                if nearest is not None:
                    mv = self.nav_to(px, py, nearest[0], nearest[1])
                    label = "fight:cube"
                else:
                    label = "fight"
            elif zone_should and zdist > 2.0:
                mv = self.nav_to(px, py, gx, gy, allow_climb=True)
                label = "zone"
            else:
                choice = getattr(self, "_giddy_current_loot", None)
                if choice is not None:
                    kind, pos = choice["kind"], choice["pos"]
                    d = hyp(pos[0] - px, pos[1] - py)
                    if kind == "npc" and farm is not None and farm.get("kind") == "npc":
                        mv = self.giddy_fight_move(t, px, py, kinds, farm, energy, tlist, 1.0)
                        label = "loot:npc"
                    elif kind == "crate" and farm is not None and farm.get("kind") == "crate":
                        mv = unit(px - pos[0], py - pos[1]) if d < 12.0 else (0.0, 0.0)
                        label = "loot:crate"
                    else:
                        mv = self.nav_to(px, py, pos[0], pos[1])
                        label = "loot:" + kind
                elif zdist > 3.0:
                    mv = self.nav_to(px, py, gx, gy, allow_climb=True)
                    label = "zone-walk"
                else:
                    mv, label = (0.0, 0.0), "idle"
        elif zone_push is not None:
            mv = self.nav_to(px, py, zone_push[0], zone_push[1], allow_climb=True)
            boost = (not in_zone) and energy > 20
            label = "zone"
            if target is not None and target["los"]:
                sx, sy = self.strafe_vec(t, px, py, target)
                mv = (mv[0] + sx * 0.35, mv[1] + sy * 0.35)
        elif self.ffa and P["ffa_engage"] and target is None and ffa_avoid is not None:
            ex, ey, ez = ffa_avoid["pos"]
            enemy_sees = self.los((ex, ey, ez + 8.0), (px, py, pz + 6.0))
            cover = self.find_cover(px, py, [ffa_avoid], lx, ly, lr) if enemy_sees else None
            if cover is not None:
                mv = self.nav_to(px, py, cover[0], cover[1])
                boost = energy > 35.0
            elif enemy_sees:
                mv = unit(px - ex, py - ey)
                boost = energy > 35.0
            else:
                mv = (0.0, 0.0)
            label = "ffa-hide"
        elif (self.ffa and P["ffa_hold"] and target is None and int(obs.get("alive", 16)) <= 6
              and not (P["ffa_heal"] and self.ffa_healing)):
            dn = hyp(px - nx_, py - ny_)
            ang = math.atan2(py - ny_, px - nx_) if dn > 1e-6 else 0.0
            rk = 0.30 if (P["ffa_end"] and nr <= 70.0) else 0.55
            radius = min(max(0.0, nr * rk), max(12.0, dn - 15.0))
            gx, gy = nx_ + math.cos(ang) * radius, ny_ + math.sin(ang) * radius
            recent = self.recent_ffa_threat(t, px, py)
            cover = self.find_cover(px, py, [recent], nx_, ny_, nr) if recent is not None else None
            if cover is not None:
                gx, gy = cover[0], cover[1]
            mv = self.nav_to(px, py, gx, gy)
            boost = energy > 40.0 and hyp(gx - px, gy - py) > 25.0
            label = "ffa-hold"
        elif hp_low and threats and (len(visible_threats) > 0 or t - self.last_hurt < 4.0):
            # ---- 3. break line of sight and regenerate
            if self.cover_goal is None or t - self.cover_t > 1.2:
                c = self.find_cover(px, py, threats, lx, ly, lr)
                self.cover_t = t
                if c is not None and P["race"] and hyp(c[0] - px, c[1] - py) > P["cover_max"]:
                    c = None  # too far: a long run in the open is worse than fighting it out
                self.cover_goal = c if (c is not None or P["race"]) else self.cover_goal
            if self.cover_goal is not None and hyp(self.cover_goal[0] - px, self.cover_goal[1] - py) > 1.5:
                mv = self.nav_to(px, py, self.cover_goal[0], self.cover_goal[1])
                boost = energy > 30
                label = "cover"
            else:
                # no cover found or arrived: back away from nearest threat while strafing
                if self.cover_goal is not None:
                    mv = (0.0, 0.0)
                    label = "hide"
                elif (P["noflee"] or P["race"]) and target is not None:
                    mv, boost, label = self.fight_move(t, px, py, kinds, target, hpf, energy, visible_threats, walk)
                    label = "stand " + label
                else:
                    e = min(threats, key=lambda e: hyp(e["pos"][0] - px, e["pos"][1] - py))
                    ax, ay = unit(px - e["pos"][0], py - e["pos"][1])
                    sx, sy = self.strafe_vec(t, px, py, e)
                    mv = (ax * 0.8 + sx * 0.6, ay * 0.8 + sy * 0.6)
                    boost = energy > 40
                    label = "flee"
            if visible_threats and self.cover_goal is not None and label == "hide":
                # still seen at the cover spot: look again
                self.cover_t = -99
        elif self.ffa and P["ffa_heal"] and self.ffa_healing and not threats:
            mv, label = (0.0, 0.0), "ffa-heal"  # nothing around: stand still (silent) until 75% HP
        elif fight and P["fightloot"] and self.fight_loot(obs, t, px, py, pz, hpf, kinds, target) is not None:
            gx, gy, why = self.fight_loot(obs, t, px, py, pz, hpf, kinds, target)
            mv = self.nav_to(px, py, gx, gy)
            if hyp(gx - px, gy - py) > 12.0 and target is not None:
                sx, sy = self.strafe_vec(t, px, py, target)  # weave so we are not a straight-line target
                mv = (mv[0] + sx * 0.5, mv[1] + sy * 0.5)
            boost = why == "drop" and energy > 40
            label = "fl-" + why
        elif fight:
            mv, boost, label = self.fight_move(t, px, py, kinds, target, hpf, energy, visible_threats, walk)
        else:
            self.cover_goal = None if hpf > 0.6 else self.cover_goal
            mv, boost, label = self.wander(obs, me, t, px, py, pz, hpf, energy, kinds, threats,
                                           zx, zy, zr, lx, ly, lr, nx_, ny_, nr, ss, farm)

        # keep inside the (future) zone
        if not giddy_active and zone_push is None and mv is not None and (mv[0] or mv[1]):
            fx, fy = px + mv[0] * 6.0, py + mv[1] * 6.0
            if hyp(fx - lx, fy - ly) > lr - margin:
                ix, iy = unit(lx - px, ly - py)
                mv = (mv[0] * 0.3 + ix, mv[1] * 0.3 + iy)

        # ---- an enemy laser is charging at us: hard reversal + boost just before it can release
        if (P["laser_dodge"] or (self.ffa and P["ffa_juke"])) and not climbing_pending(self):
            jk = self.laser_juke(t, px, py, pz, vx, vy, visible_threats, energy)
            if jk is not None:
                mv, boost = jk, energy > 8
                label += " juke"

        if giddy_active:
            dx_, dy_, laser_dodge = self.giddy_dodge(obs, px, py, pz, vx, vy, me)
            if dx_ != 0.0 or dy_ != 0.0:
                mv = (mv[0] * 0.4 + dx_ * 0.9, mv[1] * 0.4 + dy_ * 0.9)
                n = hyp(mv[0], mv[1])
                if n > 1.0:
                    mv = (mv[0] / n, mv[1] / n)
                if laser_dodge and energy > 25.0:
                    boost = True
        elif dodge is not None:
            dx_, dy_, urgent = dodge
            if urgent:
                mv = (dx_, dy_)
                boost = energy > 15
                label += "!"
            elif mv is not None:
                mv = (mv[0] * 0.5 + dx_, mv[1] * 0.5 + dy_)

        if mv is None:
            mv = (0.0, 0.0)

        # ---- jet over a wall to reach a walled-off goal (stadium bowl, courtyards)
        climbing = False
        if self.climb_req is not None and not (dodge is not None and dodge[2]):
            cx_, cy_ = self.climb_req
            mv = (cx_, cy_)
            climbing = True
            g = self.geo
            ahead_blocked = g is not None and not g.seg_free(px, py, px + cx_ * 7.0, py + cy_ * 7.0, skip_start=0.5)
            if grounded:
                if ahead_blocked:
                    if energy >= 85.0:
                        jump = True
                        boost = True
                    else:
                        mv = (cx_ * 0.2, cy_ * 0.2)  # lean on the wall while energy refills
                        boost = False
                label += " climb"
            else:
                boost = energy > 1.0
                label += " jet"

        # ---- stuck detection
        if climbing:
            self.hist.clear()
        elif self.stuck_until > t:
            mv = self.stuck_dir
            if grounded:
                jump = True
            boost = energy > 10
            label += " unstick"
            if len(self.stuck_log) >= 3:
                # stuck again and again: jet straight up and out (long burn)
                boost = energy > 1
                label += "+"
        else:
            if len(self.hist) >= 15:
                t0 = self.hist[-15][0]
                # commanded to walk for ~1.5 s but barely moving: wedged on something
                x0, y0 = self.hist[-15][1], self.hist[-15][2]
                pushing = all(h[3] > 0.6 for h in self.hist[-15:])
                slow = all(h[4] < 1.5 for h in self.hist[-15:]) or hyp(px - x0, py - y0) < 1.0
                if t - t0 < 2.0 and pushing and slow:
                    a = math.atan2(mv[1], mv[0]) + (PI / 2 if self.rng.random() < 0.5 else -PI / 2)
                    self.stuck_dir = (math.cos(a), math.sin(a))
                    self.stuck_log = [x for x in self.stuck_log if t - x < 12.0] + [t]
                    self.stuck_until = t + (0.9 if len(self.stuck_log) < 3 else 3.0)
                    self.path = None
                    self.hist.clear()
        if charging and not giddy_active:
            boost = False  # half speed anyway, keep energy
        return mv, boost, jump, label

    def laser_juke(self, t, px, py, pz, vx, vy, visible_threats, energy):
        """Movement to slip an incoming laser: the shooter's charge is visible and it fires along
        its aim when released (full charge after ~0.75 s), so reverse lateral direction near then."""
        best = None
        for e in visible_threats:
            if not e.get("charging"):
                continue
            t0 = self.charge_seen.get(e["id"])
            if t0 is None:
                continue
            ex, ey = e["pos"][0], e["pos"][1]
            d = hyp(px - ex, py - ey)
            if d < 1.0:
                continue
            ang = abs(wrap(math.atan2(py - ey, px - ex) - (e.get("aim") or [0, 0])[0]))
            if ang > math.atan2(6.0, d) + 0.04:
                continue  # not pointed at us
            el = t - t0
            if el < P["juke_at"] - 0.25:
                continue
            ux, uy = unit(px - ex, py - ey)
            perp = (-uy, ux)
            side = 1.0 if (vx * perp[0] + vy * perp[1]) >= 0 else -1.0
            key = (e["id"], round(t0, 2))
            if self.juke_key != key:
                self.juke_key = key
                self.juke_dir = -side  # reverse whatever we were doing
            best = (perp[0] * self.juke_dir, perp[1] * self.juke_dir)
        return best

    def kill_loot(self, t, px, py, threats):
        """Where a recent kill dropped its cubes, if no live threat is within 60 m of us or of it."""
        self.kill_spots = [k for k in self.kill_spots if t - k[0] < 25.0][-4:]
        cx, cy, cr = self.safe_circle
        best, bd = None, 170.0
        for (kt, kx, ky, kz) in self.kill_spots:
            d = hyp(kx - px, ky - py)
            if d < 3.0 or d > bd or hyp(kx - cx, ky - cy) > cr - 5.0 or not self.reachable(kx, ky):
                continue
            if any(hyp(e["pos"][0] - kx, e["pos"][1] - ky) < 60.0 or hyp(e["pos"][0] - px, e["pos"][1] - py) < 60.0
                   for e in threats if self.t - e["seen"] < 3.0):
                continue
            best, bd = (kx, ky), d
        return best

    def fight_loot(self, obs, t, px, py, pz, hpf, kinds, target):
        """A cube within 30 m, a better weapon within 200 m, or a supply drop worth taking now."""
        cx, cy, cr = self.safe_circle
        if hpf > 0.3:
            best, bd = None, 30.0
            for cid, (pos, seen) in self.cubes_mem.items():
                if t - seen > 0.01 or not self.reachable(pos[0], pos[1]):
                    continue
                d = hyp(pos[0] - px, pos[1] - py)
                if d < bd and abs(pos[2] - pz) < 4.0 and hyp(pos[0] - cx, pos[1] - cy) < cr - 3.0:
                    best, bd = (pos[0], pos[1], "cube"), d
            if best is not None:
                return best
        best, bd = None, 200.0
        for pid, (kind, pos, seen) in self.pickups.items():
            if self.take_block.get(pid, -1) > t or self.pickup_gain(kind, kinds, False) is None:
                continue
            if pos[2] - self.ground(pos[0], pos[1]) > 2.5 or not self.reachable(pos[0], pos[1]):
                continue
            d = hyp(pos[0] - px, pos[1] - py)
            if d < bd and hyp(pos[0] - cx, pos[1] - cy) < cr - 3.0:
                best, bd = (pos[0], pos[1], "arm"), d
        if best is not None:
            return best
        for dr in obs.get("drops") or []:
            pos = dr.get("pos")
            if not pos:
                continue
            d = hyp(pos[0] - px, pos[1] - py)
            holder = dr.get("holder")
            if holder not in (None, self.my_id) and float(dr.get("progress") or 0) > 1.5 and d > 30:
                continue
            if d < 150.0 and d / 11.7 < float(dr.get("lands_in") or 0) + 20.0 and hyp(pos[0] - cx, pos[1] - cy) < cr - 3.0:
                ring = float(dr.get("radius") or 8.0)
                if d < ring - 2.0:
                    return (pos[0] + ring * 0.45 * math.cos(t * 1.3), pos[1] + ring * 0.45 * math.sin(t * 1.3), "drop")
                return (pos[0], pos[1], "drop")
        return None

    def pos_search(self, t, px, py, e, want, sx, sy, visible_threats):
        """giddygiddyboy-style local fight position: stay or move 6 m in one of 16 directions."""
        g = self.geo
        ready = self.has_special or not self.gun_busy
        wl = 1.0 if ready else -1.0
        ex, ey, ez = e["pos"]
        sc = 1.0 + self.size_per_cube * float(e.get("cubes") or 0)
        tc = (ex, ey, ez + 4.5 * sc)
        cx0, cy0, cr0 = self.safe_circle
        others = sorted((o for o in visible_threats if o["id"] != e["id"] and o["dist"] < 170.0),
                        key=lambda o: o["dist"])[:2]
        cube = None
        for cid, (cpos, seen) in self.cubes_mem.items():
            if t - seen < 0.01 and hyp(cpos[0] - px, cpos[1] - py) < 40.0:
                if cube is None or hyp(cpos[0] - px, cpos[1] - py) < hyp(cube[0] - px, cube[1] - py):
                    cube = cpos
        best, bmv = -1e18, None
        for k in range(-1, 16):
            if k >= 0 and self.time_left() < 0.006:
                break
            if k < 0:
                cx, cy, r, dx, dy = px, py, 0.0, 0.0, 0.0
            else:
                a = k * PI / 8.0
                dx, dy, r = math.cos(a), math.sin(a), 6.0
                cx, cy = px + dx * r, py + dy * r
                if not g.free(cx, cy) or not g.seg_free(px, py, cx, cy, skip_start=0.5):
                    continue
            cz = g.ground(cx, cy) if k >= 0 else self.pz_now
            dd = hyp(ex - cx, ey - cy)
            s = -1.1 * abs(dd - want) - 0.4 * r
            los = g.los(cx, cy, cz + self.muzzle_h, tc[0], tc[1], tc[2], step=1.5, skip_end=2.5 * sc)
            if los:
                s += 45.0 * wl
            body = (cx, cy, cz + 5.4)
            for o in others:
                if g.los(o["pos"][0], o["pos"][1], o["pos"][2] + 8.0, body[0], body[1], body[2], step=1.5):
                    s -= 30.0
            dz = hyp(cx - cx0, cy - cy0)
            if dz > cr0 - 6.0:
                s -= 4.0 * (dz - (cr0 - 6.0))
            if cube is not None:
                s += 1.5 * (hyp(cube[0] - px, cube[1] - py) - hyp(cube[0] - cx, cube[1] - cy))
            if wl > 0:
                s += 9.0 * (dx * sx + dy * sy)
            if k < 0:
                s -= 8.0
                if wl < 0 and not los:
                    s += 14.0
            if s > best:
                best, bmv = s, (dx, dy)
        return bmv

    def engageable(self, e, px, py, t, vx=0.0, vy=0.0):
        """giddygiddyboy's practice engage rule: fight mechs that are close, hitting us, closing in,
        weak, or aiming at us; leave the rest alone until they come."""
        d = e["dist"]
        if d < 60.0 or t - self.attacked_by.get(e["id"], -99.0) < 6.0:
            return True
        ex, ey = e["pos"][0], e["pos"][1]
        ux, uy = unit(px - ex, py - ey)
        v = e.get("vel") or [0.0, 0.0, 0.0]
        if (v[0] - vx) * ux + (v[1] - vy) * uy > 3.0 and d < 180.0:
            return True
        ehp, emax = e.get("hp"), e.get("max_hp") or 1400.0
        if ehp is not None and float(ehp) < 0.4 * float(emax) and d < 160.0:
            return True
        aim = e.get("aim") or [0.0, 0.0]
        if abs(wrap(math.atan2(py - ey, px - ex) - aim[0])) < 0.22 and d < 190.0:
            return True
        return False

    def giddy_combat_state(self, t, me, tlist, threats, target, hpf):
        """SquadParams retreat/lose/crowd rules (giddy2 brain.go:605-657)."""
        hp = float(me.get("hp", 0.0))
        max_hp = max(1.0, float(me.get("max_hp", 1400.0)))
        engaged = [e for e in tlist if e.get("engage")]
        danger = bool(engaged) or any(t - e.get("seen", -99.0) <= 2.5 and
                                      t - self.attacked_by.get(e["id"], -99.0) < 4.0 for e in threats)
        crowd = sum(1 for e in tlist if e.get("engage") or (e.get("los") and e.get("dist", 1e9) < 150.0))
        if danger or t - self.giddy_last_hurt < 3.0:
            self.giddy_last_threat_at = t
        finishing = target is not None and target["dist"] < 120.0 and (
            float(target.get("hp") or 1400.0) < 200.0 or
            float(target.get("hp") or 1400.0) < hp * 0.5)
        has_laser = any(w and w.get("kind") == "laser" for w in (me.get("weapons") or []))
        outgunned = (target is not None and "laser" in (target.get("weapons") or []) and not has_laser
                     and target["dist"] > 55.0 and hpf < 0.8)
        swamped = crowd >= 3 or (crowd == 2 and hpf < 0.65) or outgunned
        if not self.giddy_retreat and ((hpf < 0.38 and t - self.giddy_last_threat_at < 2.0) or swamped):
            self.giddy_retreat = True
            self.cover_t = -1.0
        if self.giddy_retreat and not swamped and (hpf > 0.8 or
                (t - self.giddy_last_threat_at > 7.0 and hpf > 0.55)):
            self.giddy_retreat = False
        near = sum(1 for e in tlist if e.get("dist", 1e9) < 160.0)
        losing = (near >= 2 and hpf < 0.55) or (near >= 3 and hpf < 0.8)
        if target is not None:
            tf = float(target.get("hp") or 1400.0) / max(1.0, float(target.get("max_hp") or 1400.0))
            if hpf < 0.35 and tf > hpf + 0.1:
                losing = True
            if tf < 0.25 and hpf > 0.2 and near <= 2:
                losing = False
        if losing:
            self.giddy_lose_at = t
        self.giddy_retreat = (t - self.giddy_lose_at < 1.5 and self.giddy_lose_at > 0.0)
        return danger, crowd, finishing, swamped

    def giddy_pref_range(self, kinds, energy, target):
        if "lightning" in kinds and energy > 12.0:
            r = 28.0
        elif "laser" in kinds:
            r = 120.0
        else:
            r = 85.0
        if target.get("kind") == "npc":
            return 58.0
        enemy_weapons = target.get("weapons") or []
        if "lightning" not in kinds:
            if "lightning" in enemy_weapons:
                r = max(r, 64.0)
            if "mortar" in enemy_weapons:
                r = max(r, 80.0)
        return r

    def giddy_zone_goal(self, zone, px, py):
        nc = zone.get("next_center") or zone.get("center") or [px, py]
        nr = float(zone.get("next_radius", zone.get("radius", 1e4)))
        if nr <= 30.0:
            return float(nc[0]), float(nc[1])
        dx, dy = px - float(nc[0]), py - float(nc[1])
        d = hyp(dx, dy)
        if d < 1.0:
            return px, py
        r = max(0.0, nr - clamp(0.3 * nr, 3.0, 30.0) - 5.0)
        if d <= r:
            return px, py
        return float(nc[0]) + dx / d * r, float(nc[1]) + dy / d * r

    def giddy_zone_deadline(self, zone, x, y, pad):
        nc = zone.get("next_center") or zone.get("center") or [x, y]
        nr = float(zone.get("next_radius", zone.get("radius", 1e9)))
        if hyp(x - nc[0], y - nc[1]) <= nr - pad:
            return 1e30
        ss = float(zone.get("shrink_start", 1e9))
        se = float(zone.get("shrink_end", ss))
        for k in range(25):
            tt = ss + (se - ss) * k / 24.0
            cx, cy, cr = self.giddy_zone_at(zone, tt)
            if hyp(x - cx, y - cy) > cr - pad:
                return tt
        return se

    def giddy_zone_penalty(self, zone, x, y, t):
        cx, cy, cr = self.giddy_zone_at(zone, t)
        p = 0.0
        d = hyp(x - cx, y - cy) - cr
        if d > -5.0:
            p += 150.0 + 5.0 * (d + 5.0)
        nc = zone.get("next_center") or [cx, cy]
        nr = float(zone.get("next_radius", cr))
        dn = hyp(x - nc[0], y - nc[1]) - nr
        if dn > -5.0:
            soon = clamp(1.0 - (float(zone.get("shrink_start", t)) - t) / 25.0, 0.1, 1.0)
            p += soon * (25.0 + 0.8 * (dn + 5.0))
        return p

    def giddy_zone_at(self, zone, t):
        """Reconstruct the source's hold-to-next interpolation from the live zone report."""
        c = zone.get("center") or [0.0, 0.0]
        r = float(zone.get("radius", 1e4))
        nc = zone.get("next_center") or c
        nr = float(zone.get("next_radius", r))
        ss, se, now = float(zone.get("shrink_start", 1e9)), float(zone.get("shrink_end", 1e9)), self.t
        if se <= ss:
            return float(c[0]), float(c[1]), r
        if now <= ss:
            hx, hy, hr = float(c[0]), float(c[1]), r
        else:
            fnow = clamp((now - ss) / (se - ss), 0.0, 1.0)
            if fnow >= 0.999:
                hx, hy, hr = float(c[0]), float(c[1]), r
            else:
                den = max(1e-6, 1.0 - fnow)
                hx = (float(c[0]) - float(nc[0]) * fnow) / den
                hy = (float(c[1]) - float(nc[1]) * fnow) / den
                hr = (r - nr * fnow) / den
        if t <= ss:
            return hx, hy, hr
        if t >= se:
            return float(nc[0]), float(nc[1]), nr
        f = clamp((t - ss) / (se - ss), 0.0, 1.0)
        return hx + (float(nc[0]) - hx) * f, hy + (float(nc[1]) - hy) * f, hr + (nr - hr) * f

    def giddy_cover_goal(self, px, py, threats, zone):
        """Cover search via geo.cover_near/free/LOS, approximating the source's bounded BFS."""
        g = self.geo
        if g is None:
            return None
        ts = sorted((e for e in threats if hyp(e["pos"][0] - px, e["pos"][1] - py) < 230.0),
                    key=lambda e: hyp(e["pos"][0] - px, e["pos"][1] - py))[:4]
        if not ts:
            return None
        height = float(getattr(self, "mech_height", 9.0))

        def hidden(x, y):
            z = g.ground(x, y)
            for e in ts:
                ex, ey, ez = e["pos"]
                if (g.los(ex, ey, ez + 8.0, x, y, z + height, step=1.5) or
                        g.los(ex, ey, ez + 8.0, x, y, z + height * 0.5, step=1.5)):
                    return False
            return True

        if hidden(px, py) and self.giddy_zone_penalty(zone, px, py, self.t) == 0.0:
            return px, py
        cands = sorted(g.cover_near(px, py, 55.0), key=lambda p: (p[0] - px) ** 2 + (p[1] - py) ** 2)[:350]
        best, best_s = None, 1e18
        for x, y in cands:
            if not g.free(x, y) or not hidden(x, y):
                continue
            path_d = hyp(x - px, y - py)
            near = min(hyp(e["pos"][0] - x, e["pos"][1] - y) for e in ts)
            score = path_d + self.giddy_zone_penalty(zone, x, y, self.t) - min(near, 80.0) * 0.25
            if score < best_s:
                best, best_s = (x, y), score
        return best

    def giddy_fight_move(self, t, px, py, kinds, target, energy, tlist, want_los, range_override=None):
        """The stay-plus-16 six-metre search from brain.go:1367-1461."""
        g = self.geo
        if g is None:
            return self.fight_move(t, px, py, kinds, target, 1.0, energy, tlist, 11.7)[0]
        rp = float(range_override) if range_override is not None else self.giddy_pref_range(kinds, energy, target)
        ex, ey, ez = target["pos"]
        bx, by = ex - px, ey - py
        bd = hyp(bx, by) + 1e-9
        ux, uy = bx / bd, by / bd
        if t >= self.strafe_until:
            if self.rng.random() < 0.6:
                self.strafe = -self.strafe
            self.strafe_until = t + 0.3 + self.rng.random() * 0.7
        sx, sy = -uy * self.strafe, ux * self.strafe
        scale = 1.0 + self.size_per_cube * float(target.get("cubes") or 0)
        tc = (ex, ey, ez + 4.5 * scale)
        cube = None
        for pos, seen in self.cubes_mem.values():
            d = hyp(pos[0] - px, pos[1] - py)
            if d < 40.0 and abs(pos[2] - self.pz_now) < 4.0 and (cube is None or d < hyp(cube[0] - px, cube[1] - py)):
                cube = pos
        zone = getattr(self, "_giddy_zone", {})
        best, bmv, blocked_side = -1e18, (0.0, 0.0), False
        for k in range(-1, 16):
            if k < 0:
                cx, cy, dx, dy, radius = px, py, 0.0, 0.0, 0.0
            else:
                a = k * PI / 8.0
                dx, dy, radius = math.cos(a), math.sin(a), 6.0
                cx, cy = px + dx * radius, py + dy * radius
                if not g.free(cx, cy) or not g.seg_free(px, py, cx, cy, skip_start=0.5):
                    if dx * sx + dy * sy > 0.9:
                        blocked_side = True
                    continue
            cz = self.pz_now if k < 0 else g.ground(cx, cy)
            d = hyp(ex - cx, ey - cy)
            score = -1.1 * abs(d - rp) - 0.4 * radius
            los = g.los(cx, cy, cz + self.muzzle_h, tc[0], tc[1], tc[2], step=1.5,
                        skip_end=2.5 * scale)
            if los:
                score += 45.0 * want_los
            body = (cx, cy, cz + float(getattr(self, "mech_height", 9.0)) * 0.6)
            for other in tlist:
                if other is target or hyp(other["pos"][0] - cx, other["pos"][1] - cy) > 170.0:
                    continue
                op = other["pos"]
                oscale = 1.0 + self.size_per_cube * float(other.get("cubes") or 0)
                oeye = float((self.cfg.get("mech") or {}).get("eye_height", 8.0)) * oscale
                if g.los(op[0], op[1], op[2] + oeye, body[0], body[1], body[2], step=1.5):
                    score -= 30.0
            score -= self.giddy_zone_penalty(zone, cx, cy, t)
            if cube is not None:
                score += 1.5 * (hyp(cube[0] - px, cube[1] - py) - hyp(cube[0] - cx, cube[1] - cy))
            # geo exposes binary free/blocked navigation, not the source's clearance map;
            # estimate clearance in 1 m rings for the same <4 m penalty.
            clearance = 4.0
            for rr in (1.0, 2.0, 3.0, 4.0):
                if any(not g.free(cx + math.cos(q * PI / 4.0) * rr,
                                  cy + math.sin(q * PI / 4.0) * rr) for q in range(8)):
                    clearance = rr - 1.0
                    break
            if clearance < 4.0:
                score -= (4.0 - clearance) * 2.5
            if want_los > 0:
                score += 9.0 * (dx * sx + dy * sy)
            if k < 0:
                score -= 8.0
                if want_los < 0 and not los:
                    score += 14.0
            if score > best:
                best, bmv = score, (dx, dy)
        if blocked_side:
            self.strafe = -self.strafe
            self.strafe_until = t + 0.5
        return bmv

    def giddy_want_pickup(self, kinds, kind):
        # brain.go:1166-1186; DefaultParams keeps NoMortar=true and LaserFirst=true.
        slots = list(kinds) + [None, None]
        if kind == "gun":
            return slots[0] is None
        if kind in kinds or (kind == "mortar"):
            return False
        rank = {"lightning": 3, "laser": 2, "mortar": 1}
        if kind == "laser":
            new_rank = 4
        else:
            new_rank = rank.get(kind, 0)
        old = slots[1]
        old_rank = 4 if old == "laser" else rank.get(old, 0)
        return new_rank > old_rank

    def giddy_idle_aim(self, t, px, py, mx, my, my_aim, stats):
        """The no-target facing order from brain.go:1056-1078."""
        watch = None
        for e in self.enemies.values():
            age = t - e.get("seen", -99.0)
            if age > 2.5:
                continue
            if not ((age <= 0.01 and e.get("engage")) or t - self.attacked_by.get(e["id"], -99.0) < 6.0):
                continue
            d = hyp(e["pos"][0] - px, e["pos"][1] - py)
            if watch is None or d < watch[0]:
                watch = (d, e)
        if watch is not None:
            p = watch[1]["pos"]
            yaw = math.atan2(p[1] - py, p[0] - px)
        elif t - self.giddy_npc_hurt < 2.5:
            yaw = self.giddy_npc_bearing
        elif t - self.giddy_last_hurt < 2.5:
            yaw = self.giddy_hurt_bearing
        elif mx != 0.0 or my != 0.0:
            yaw = math.atan2(my, mx) + 1.15 * math.sin(t * 1.3)
        else:
            yaw = my_aim[0] + 0.35
        return yaw, clamp(0.0, float(stats.get("min_pitch", -0.7)), float(stats.get("max_pitch", 1.3)))

    def giddy_aim_fire(self, obs, shoot, slots, tlist):
        """Squad-sourced gun lead and weapon trigger logic (brain.go:880-1054, 1189-1205)."""
        fire = [False, False]
        laser_slot = next((i for i, w in enumerate(slots) if w and w.get("kind") == "laser"), None)
        gun_slot = next((i for i, w in enumerate(slots) if w and w.get("kind") == "gun"), None)
        lit_slot = next((i for i, w in enumerate(slots) if w and w.get("kind") == "lightning"), None)
        laser = slots[laser_slot] if laser_slot is not None else None
        gun = slots[gun_slot] if gun_slot is not None else None
        light = slots[lit_slot] if lit_slot is not None else None
        me = obs["self"]
        px, py, pz = me["pos"]
        vx, vy, vz = me.get("vel") or [0.0, 0.0, 0.0]
        st = me.get("stats") or {}
        turn = float(st.get("turn_rate", 4.0))
        pitch_rate = float(st.get("pitch_rate", 3.0))
        mz = pz + float(st.get("muzzle_height", 7.0))
        latency = 0.075
        muzzle = (px + vx * latency, py + vy * latency, mz)
        aim_now = me.get("aim") or [0.0, 0.0]
        laser_stats = (laser or {}).get("stats") or self.W.get("laser") or {}
        full_charge = float(laser_stats.get("charge", 0.75))
        hold_s = float(laser_stats.get("hold_s", 2.0))
        charge = float((laser or {}).get("charge", 0.0))
        yaw = pitch = None
        if shoot is None:
            if laser is not None and charge > 0.0 and laser_slot is not None:
                fire[laser_slot] = charge < full_charge + hold_s - 0.35
            return yaw, pitch, fire

        kind = shoot.get("kind", "mech")
        pos = shoot.get("pos") or [0.0, 0.0, 0.0]
        tv = shoot.get("vel") or [0.0, 0.0, 0.0]
        if kind == "mech":
            scale = 1.0 + self.size_per_cube * float(shoot.get("cubes") or 0)
            radius = float((self.cfg.get("mech") or {}).get("radius", 2.5)) * scale
            height = float((self.cfg.get("mech") or {}).get("height", 9.0)) * scale
            com = (pos[0], pos[1], pos[2] + height * 0.5)
            if self.geo is not None and not self.geo.los(*muzzle, *com, step=1.0, skip_end=2.5 * scale):
                com = (pos[0], pos[1], pos[2] + height * 0.85)
            los_ok = bool(shoot.get("los", True))
        elif kind == "npc":
            nc = self.cfg.get("npc") or {}
            radius = float(nc.get("radius", 2.0))
            height = float(nc.get("height", 4.0))
            com = (pos[0], pos[1], pos[2] + height * 0.5)
            los_ok = self.geo is None or self.geo.los(*muzzle, *com, step=1.0, skip_end=radius + 0.5)
        else:
            cc = self.cfg.get("crates") or {}
            radius = float(cc.get("radius", 2.0))
            height = float(cc.get("height", 4.0))
            com = (pos[0], pos[1], pos[2] + height * 0.5)
            los_ok = self.geo is None or self.geo.los(*muzzle, *com, step=1.0, skip_end=radius + 0.5)
        d3 = math.sqrt((com[0] - muzzle[0]) ** 2 + (com[1] - muzzle[1]) ** 2 + (com[2] - mz) ** 2)
        gst = (gun or {}).get("stats") or self.W.get("gun") or {}
        speed = float(gst.get("speed", 252.0))
        gun_range = float(gst.get("range", 230.0))
        dist = hyp(pos[0] - px, pos[1] - py)
        direct = (com[0] + tv[0] * (latency + 0.05),
                  com[1] + tv[1] * (latency + 0.05), com[2])
        hd = hyp(direct[0] - muzzle[0], direct[1] - muzzle[1])
        hy = math.atan2(direct[1] - muzzle[1], direct[0] - muzzle[0])
        hp = math.atan2(direct[2] - muzzle[2], max(hd, 0.1))
        gy, gp = hy, hp
        if gun is not None:
            flight_t = 0.0
            for _ in range(4):
                tt = latency + flight_t
                lead_pos = (com[0] + tv[0] * tt,
                            com[1] + tv[1] * tt,
                            com[2] + (tv[2] * tt * 0.5 if abs(tv[2]) >= 0.5 else 0.0))
                dd = math.sqrt((lead_pos[0] - muzzle[0]) ** 2 + (lead_pos[1] - muzzle[1]) ** 2 +
                               (lead_pos[2] - muzzle[2]) ** 2)
                flight_t = max(0.0, dd - 3.0) / max(1.0, speed)
            ghd = hyp(lead_pos[0] - muzzle[0], lead_pos[1] - muzzle[1])
            gy = math.atan2(lead_pos[1] - muzzle[1], lead_pos[0] - muzzle[0])
            gp = math.atan2(lead_pos[2] - muzzle[2], max(ghd, 0.1))
            yaw, pitch = gy, gp
        else:
            yaw, pitch = hy, hp
        tol_y = math.atan2(radius * 0.75, max(dist, 1.0))
        tol_p = math.atan2(height * 0.4, max(dist, 1.0))

        def slew(want_y, want_p):
            ye = max(0.0, abs(wrap(want_y - aim_now[0])) - turn * 0.05)
            pe = max(0.0, abs(want_p - aim_now[1]) - pitch_rate * 0.05)
            return ye < tol_y and pe < tol_p

        yaw_err = abs(wrap(hy - aim_now[0]))
        pitch_err = abs(hp - aim_now[1])
        aligned_now = yaw_err < tol_y * 0.9 and pitch_err < tol_p
        if los_ok:
            if gun is not None and gun_slot is not None and dist <= gun_range * 0.96 and \
                    (gun.get("ammo") is None or float(gun.get("ammo", 1)) > 0) and \
                    float(gun.get("reload", 0.0)) <= 0.0 and slew(gy, gp):
                fire[gun_slot] = True
            if light is not None and lit_slot is not None:
                lw = (light.get("stats") or self.W.get("lightning") or {})
                cone = math.radians(float(lw.get("cone_deg", 20.0)))
                lrange = float(lw.get("range", 45.0))
                lcost = float(lw.get("energy", 3.0))
                if dist < lrange - 1.5 and abs(wrap(hy - aim_now[0])) < cone * 0.5 - 0.05 and \
                        float(me.get("energy", 0.0)) >= lcost:
                    fire[lit_slot] = True
        if laser is not None and laser_slot is not None and kind == "mech" and \
                    dist <= float(laser_stats.get("range", 280.0)) * 0.97:
            in_range = True
            min_charge = float(laser_stats.get("min_charge", 0.15))
            min_frac = float(laser_stats.get("min_frac", 0.35))
            if charge > 0.0:
                lethal = charge >= min_charge and charge < full_charge - 0.051
                dmg = float(laser_stats.get("damage", 315.0)) * (
                    min_frac + (1.0 - min_frac) * min(1.0, charge / max(0.01, full_charge)))
                dmg *= 1.0 + self.dmg_per_cube * min(float(me.get("cubes", 0)), self.stat_cap)
                if "mortar" in (shoot.get("weapons") or []):
                    dmg *= 1.0 + float(laser_stats.get("cookoff", 0.6))
                kill_now = (lethal and shoot.get("hp") is not None and dmg * (P["g_lethalk"] if P["core"] == "giddy" else 0.8) >= float(shoot["hp"])
                            and aligned_now)
                full = charge >= full_charge - 0.051
                overtime = charge >= full_charge + hold_s - 0.35
                if los_ok and in_range and ((full and aligned_now) or kill_now):
                    fire[laser_slot] = False
                    if charge > full_charge * 0.45:
                        yaw, pitch = hy, hp
                elif overtime:
                    fire[laser_slot] = False
                else:
                    fire[laser_slot] = True
                    if charge > full_charge * 0.45:
                        yaw, pitch = hy, hp
            else:
                ready = float(laser.get("cooldown", 0.0)) <= 0.05 and self.t >= self.shorted_until
                if ready and los_ok and d3 < float(laser_stats.get("range", 280.0)) * 0.97 and \
                        abs(wrap(hy - aim_now[0])) < 0.6:
                    fire[laser_slot] = True
        if pitch is not None:
            pitch = clamp(pitch, float(st.get("min_pitch", -0.7)), float(st.get("max_pitch", 1.3)))
        return yaw, pitch, fire

    def giddy_choose_take(self, px, py, pz, kinds, slots):
        for pid, (kind, pos, seen) in self.pickups.items():
            if self.take_block.get(pid, -1) > self.t or self.t - seen > 0.01:
                continue
            if hyp(pos[0] - px, pos[1] - py) > self.loot_r - 0.3 or abs(pos[2] - pz) > 3.6:
                continue
            if not self.giddy_want_pickup(kinds, kind):
                continue
            drop = None
            if len(slots) > 1 and slots[1] is not None and slots[0] is not None:
                if slots[1].get("kind") == "laser" and float(slots[1].get("charge", 0.0)) > 0.0:
                    return None
                drop = 1
            return pid, drop
        return None

    def giddy_choose_loot(self, obs, hpf, danger, kinds):
        """Regular (non-Econ) chooseLoot with squadParams values (brain.go:1579-1762)."""
        me = obs["self"]
        px, py = me["pos"][:2]
        zone = obs.get("zone") or {}
        now = self.t
        cx, cy, cr = self.zone_at(zone, now)
        walk = float((me.get("stats") or {}).get("walk_speed", 11.7))
        best, best_u = None, 0.0

        def consider(kind, ident, pos, value):
            nonlocal best, best_u
            if self.giddy_bad.get(ident, -1.0) > now:
                return
            d = hyp(pos[0] - px, pos[1] - py)
            if d > 260.0 or hyp(pos[0] - cx, pos[1] - cy) > cr - 6.0:
                return
            dl = self.giddy_zone_deadline(zone, pos[0], pos[1], 6.0)
            if now + d * 1.4 / max(1.0, walk) + 5.0 > dl:
                return
            if pos[2] - self.ground(pos[0], pos[1]) > 3.2:
                return
            if self.geo is not None:
                nf = self.geo.nearest_free(pos[0], pos[1])
                if nf is None or self.geo.comp[nf] != self.giddy_main_comp:
                    return
            u = value / (d + 18.0)
            if ident == self.giddy_loot_id:
                u *= 1.35
            if u > best_u:
                best_u = u
                best = {"kind": kind, "id": ident, "pos": list(pos), "value": value}

        for pid, (kind, pos, seen) in self.pickups.items():
            rad = 240.0 if kind == "laser" else 150.0
            if self.giddy_want_pickup(kinds, kind) and hyp(pos[0] - px, pos[1] - py) < rad:
                v = 60.0
                if len(kinds) > 1 and kinds[1] is None:
                    v = 130.0
                if kind == "laser":
                    v = max(v, 150.0)
                consider("pickup", pid, pos, v)
        cube_v = 25.0 if float(me.get("cubes", 0)) >= self.stat_cap else 75.0
        for cid, (pos, seen) in self.cubes_mem.items():
            if hyp(pos[0] - px, pos[1] - py) < 105.0:
                consider("cube", cid, pos, cube_v)
        if hpf > 0.5 and not danger and now < 1e9:
            for cid, (pos, chp, seen) in self.crates.items():
                if hyp(pos[0] - px, pos[1] - py) < 150.0:
                    consider("crate", cid, pos, cube_v * 0.6)
            if float(me.get("cubes", 0)) < self.stat_cap:
                for nid, n in self.npcs.items():
                    if now - n.get("seen", -99.0) < 12.0 and hyp(n["pos"][0] - px, n["pos"][1] - py) < 150.0:
                        consider("npc", nid, n["pos"], 85.0)
        if best is None:
            self.giddy_loot_id = None
            return None
        d = hyp(best["pos"][0] - px, best["pos"][1] - py)
        if best["id"] != self.giddy_loot_id:
            self.giddy_loot_id, self.giddy_loot_since = best["id"], now
        elif now - self.giddy_loot_since > 30.0 or (
                best["kind"] == "crate" and now - self.giddy_loot_since > 24.0 and d < 50.0):
            self.giddy_bad[best["id"]] = now + 60.0
            self.giddy_loot_id = None
            return None
        self.giddy_bad = {k: v for k, v in self.giddy_bad.items() if v > now}
        return best

    def giddy_dodge_shell(self, obs, px, py, pz, me):
        """Mortar-impact dodge from brain.go:1240-1283, on the geo solid grid."""
        g = self.geo
        if g is None:
            return None
        mw = self.cfg.get("weapons", {}).get("mortar", {})
        splash = float(mw.get("splash_radius", 12.0))
        radius = float((me.get("stats") or {}).get("radius", 2.5))
        limit = splash + radius + 2.0
        gravity = float(mw.get("gravity", self.mortar_g))
        for p in obs.get("projectiles") or []:
            if p.get("kind") != "mortar":
                continue
            pos, vel = p.get("pos"), p.get("vel")
            if not pos or not vel:
                continue
            x, y, z = map(float, pos)
            vz = float(vel[2])
            for step in range(1, 81):
                tt = step * 0.05
                x += float(vel[0]) * 0.05
                y += float(vel[1]) * 0.05
                z += vz * 0.05
                vz -= gravity * 0.05
                if g.solid_at(x, y, z):
                    d = hyp(x - px, y - py)
                    if d >= limit or tt >= 2.5:
                        break
                    dx, dy = px - x, py - y
                    if d < 0.5:
                        dx, dy = -float(vel[1]), float(vel[0])
                        d = hyp(dx, dy)
                    dx, dy = dx / max(d, 1e-9), dy / max(d, 1e-9)
                    for rot in (0.0, 0.7, -0.7, 1.4, -1.4):
                        c, s = math.cos(rot), math.sin(rot)
                        ex, ey = dx * c - dy * s, dx * s + dy * c
                        if g.seg_free(px, py, px + ex * 6.0, py + ey * 6.0):
                            return ex, ey
                    break
        return None

    def giddy_dodge(self, obs, px, py, pz, vx, vy, me):
        """Bullet and charging-laser dodge from brain.go:1836-1883."""
        height = float((me.get("stats") or {}).get("height", 9.0))
        radius = float((me.get("stats") or {}).get("radius", 2.5)) + 1.1
        cx, cy, cz = px, py, pz + height * 0.56
        dx = dy = 0.0
        laser = False
        for p in obs.get("projectiles") or []:
            if p.get("kind") == "mortar":
                continue
            pos, vel = p.get("pos"), p.get("vel")
            if not pos or not vel:
                continue
            v2 = sum(float(v) ** 2 for v in vel)
            if v2 < 1.0:
                continue
            tc = sum((c - q) * float(v) for c, q, v in zip((cx, cy, cz), pos, vel)) / v2
            if tc <= 0.0 or tc > 0.7:
                continue
            mx = float(pos[0]) + float(vel[0]) * tc - cx
            my = float(pos[1]) + float(vel[1]) * tc - cy
            if hyp(mx, my) >= radius:
                continue
            vh = hyp(float(vel[0]), float(vel[1]))
            if vh < 1.0:
                continue
            sx, sy = -float(vel[1]) / vh, float(vel[0]) / vh
            if sx * mx + sy * my > 0:
                sx, sy = -sx, -sy
            dx, dy = dx + sx, dy + sy
        for m in obs.get("mechs") or []:
            if m.get("kind", "mech") != "mech" or not m.get("charging"):
                continue
            pos = m.get("pos") or [0.0, 0.0, 0.0]
            aim = m.get("aim") or [0.0, 0.0]
            to_me = math.atan2(cy - pos[1], cx - pos[0])
            if abs(wrap(float(aim[0]) - to_me)) > 0.15:
                continue
            dx += math.cos(to_me + PI / 2.0 * self.strafe) * 1.5
            dy += math.sin(to_me + PI / 2.0 * self.strafe) * 1.5
            laser = True
        d = hyp(dx, dy)
        return (dx / d, dy / d, laser) if d > 0.01 else (0.0, 0.0, False)

    def duck_find(self, t, px, py, e):
        """A free spot 6 or 12 m away that the target's eye cannot see (re-evaluated every 0.5 s)."""
        g = self.geo
        if g is None:
            return None
        if self.duck_spot is not None and t - self.duck_t < 0.5:
            return self.duck_spot
        self.duck_t = t
        ex, ey, ez = e["pos"]
        cx, cy, cr = self.safe_circle
        best, bd = None, 1e9
        for r in (6.0, 12.0):
            for k in range(16):
                a = k * PI / 8.0
                qx, qy = px + math.cos(a) * r, py + math.sin(a) * r
                if not g.free(qx, qy) or not g.seg_free(px, py, qx, qy, skip_start=0.5):
                    continue
                if hyp(qx - cx, qy - cy) > cr - 3.0:
                    continue
                if g.los(ex, ey, ez + 8.0, qx, qy, g.ground(qx, qy) + 6.0, step=1.5):
                    continue
                dd = r + 0.3 * abs(hyp(qx - ex, qy - ey) - hyp(px - ex, py - ey))
                if dd < bd:
                    best, bd = (qx, qy), dd
            if best is not None:
                break
        self.duck_spot = best
        return best

    def strafe_vec(self, t, px, py, e):
        if t >= self.strafe_until:
            if self.rng.random() < 0.7:
                self.strafe = -self.strafe
            self.strafe_until = t + self.rng.uniform(P["strafe"][0], P["strafe"][1])
        ax, ay = unit(e["pos"][0] - px, e["pos"][1] - py)
        sx, sy = -ay * self.strafe, ax * self.strafe
        if self.geo is not None and not self.geo.seg_free(px, py, px + sx * 6.0, py + sy * 6.0, skip_start=2.0):
            self.strafe = -self.strafe
            sx, sy = -sx, -sy
            self.strafe_until = t + self.rng.uniform(0.4, 0.9)
        return sx, sy

    def fight_move(self, t, px, py, kinds, target, hpf, energy, visible_threats, walk):
        e = target
        d = e["dist"]
        ew = set(e.get("weapons") or [])
        has_laser = "laser" in kinds
        has_lit = "lightning" in kinds
        if has_laser:
            rmin, rmax = P["laser_band"]
        elif has_lit:
            rmin, rmax = P["lit_band"]
        else:
            rmin, rmax = P["gun_band"]
        if has_lit:
            ehp = (e.get("hp") or 1400) / max(1.0, e.get("max_hp") or 1400)
            if ("laser" in ew and "lightning" not in ew) or (ehp < 0.5 and hpf > 0.5 and energy > 30):
                rmin, rmax = P["lit_band"]
        if "lightning" in ew and not has_lit:
            rmin = max(rmin, 60.0)
            rmax = max(rmax, rmin + 40.0)
        if len(visible_threats) >= 2:
            rmin = max(rmin, 60.0)
            rmax = max(rmax, rmin + 40.0)
        ambush = P["ambush"] and not has_laser
        if ambush and e["los"] and d > P["ambush_d"]:
            # long gun duels favour nobody but cost us HP: get out of its sight, let it come
            if self.cover_goal is None or t - self.cover_t > 1.5:
                cx_, cy_, cr_ = self.safe_circle
                self.cover_goal = self.find_cover(px, py, [e], cx_, cy_, cr_)
                self.cover_t = t
            if self.cover_goal is not None and hyp(self.cover_goal[0] - px, self.cover_goal[1] - py) > 1.5:
                return self.nav_to(px, py, self.cover_goal[0], self.cover_goal[1]), energy > 40, "ambush-go"
        if not e["los"] and ambush and d > 50.0:
            return (0.0, 0.0), False, "ambush"
        if not e["los"]:
            # out of sight: push in when healthy, otherwise hold still and regen
            if P["chase"] and hpf > 0.6 and (hpf > (e.get("hp") or 1400) / max(1.0, e.get("max_hp") or 1400) - 0.1):
                mv = self.nav_to(px, py, e["pos"][0], e["pos"][1])
                return mv, False, "hunt"
            return (0.0, 0.0), False, "wait"
        if P["duck"] and self.gun_busy and not has_laser and not has_lit and e["los"]:
            spot = self.duck_find(t, px, py, e)
            if spot is not None:
                return self.nav_to(px, py, spot[0], spot[1]), False, "duck"
        radial = 0.0
        if d > rmax:
            radial = 1.0
        elif d < rmin:
            radial = -1.0
        if P["want_d"] > 0:
            want = P["want_d"]
            if P["want_laser"] > 0 and has_laser:
                want = P["want_laser"]
            if P["wdist"]:
                if has_lit and energy > 25:
                    want = 30.0
                elif not has_laser:
                    want = 70.0
                if len(visible_threats) >= 2 or ("lightning" in ew and not has_lit):
                    want = max(want, 70.0)
            ehp_abs = e.get("hp")
            if ehp_abs is not None and float(ehp_abs) < 300.0 and hpf > 0.3:
                want = min(want, 40.0)  # finish it
            radial = clamp((d - want) / 15.0, -1.0, 1.0)
        sx, sy = self.strafe_vec(t, px, py, e)
        ax, ay = unit(e["pos"][0] - px, e["pos"][1] - py)
        if P["posearch"] and e["los"] and self.geo is not None and self.time_left() > 0.012:
            want = P["want_d"] if P["want_d"] > 0 else (rmin + rmax) * 0.5
            if P["want_d"] > 0 and e.get("hp") is not None and float(e["hp"]) < 300.0 and hpf > 0.3:
                want = min(want, 40.0)
            mvp = self.pos_search(t, px, py, e, want, sx, sy, visible_threats)
            if mvp is not None:
                return mvp, False, "pos%d" % int(d)
        if radial > 0.5:
            dirn = self.nav_to(px, py, e["pos"][0], e["pos"][1])
            mv = (dirn[0] * 0.85 * radial + sx * 0.5, dirn[1] * 0.85 * radial + sy * 0.5)
        elif radial > 0:
            mv = (ax * radial * 0.8 + sx, ay * radial * 0.8 + sy)
        else:
            mv = (ax * radial * 0.8 + sx, ay * radial * 0.8 + sy)
        boost = energy > 55 and d > 60 and not has_lit
        if radial < 0 and d < rmin * 0.6 and energy > 30:
            boost = True
        if P["fightboost"] > 0 and energy > P["fightboost"] and not self.laser_charging_now and (not has_lit or energy > 60):
            boost = True
        return mv, boost, "fight%d" % int(d)

    def projectile_dodge(self, obs, px, py, pz, vx, vy):
        """(dx, dy, urgent) away from a shell landing near us or a bullet on a collision course."""
        g = self.mortar_g
        best = None
        cz = pz + 4.5
        for p in obs.get("projectiles") or []:
            if self.time_left() < 0.008:
                break  # keep time for the rest of the decision when the sky is full of shells
            pos = p.get("pos")
            vel = p.get("vel")
            if not pos or not vel:
                continue
            kind = p.get("kind")
            if kind == "mortar":
                # integrate to impact
                x, y, z = pos
                ux, uy, uz = vel
                tt = 0.0
                hit = None
                while tt < 7.0:
                    if (int(tt * 20) & 15) == 0 and self.time_left() < 0.008:
                        break
                    tt += 0.05
                    x += ux * 0.05
                    y += uy * 0.05
                    uz -= g * 0.05
                    z += uz * 0.05
                    if hyp(x - px, y - py) < 3.0 and pz - 1 < z < pz + 9.5:
                        hit = (x, y, tt)
                        break
                    if z < self.ground(x, y) + 0.2:
                        hit = (x, y, tt)
                        break
                if hit is None:
                    continue
                hx, hy, ht = hit
                fx, fy = px + vx * ht * 0.5, py + vy * ht * 0.5
                dd = hyp(fx - hx, fy - hy)
                if dd < 11.0:
                    ax, ay = unit(fx - hx, fy - hy)
                    if ax == 0 and ay == 0:
                        ax, ay = unit(-uy, ux) if hyp(ux, uy) > 0 else (1.0, 0.0)
                    best = (ax, ay, True)
            else:
                rx, ry, rz = pos[0] - px, pos[1] - py, pos[2] - cz
                ux, uy, uz = vel[0] - vx, vel[1] - vy, vel[2]
                vv = ux * ux + uy * uy + uz * uz
                if vv < 1.0:
                    continue
                tc = -(rx * ux + ry * uy + rz * uz) / vv
                if tc < 0.12 or tc > 1.2:
                    continue
                cx, cy, cz2 = rx + ux * tc, ry + uy * tc, rz + uz * tc
                if abs(cz2) > 5.5 or hyp(cx, cy) > 3.2:
                    continue
                # move perpendicular to the bullet, away from its closest point
                bx, by = unit(vel[0], vel[1])
                perp = (-by, bx)
                side = -(cx * perp[0] + cy * perp[1])
                if abs(side) < 0.3:
                    side = 1.0 if (vx * perp[0] + vy * perp[1]) >= 0 else -1.0
                sgn = 1.0 if side > 0 else -1.0
                if best is None or not best[2]:
                    best = (perp[0] * sgn, perp[1] * sgn, False)
        return best

    def wander(self, obs, me, t, px, py, pz, hpf, energy, kinds, threats, zx, zy, zr, lx, ly, lr,
               nx_, ny_, nr, ss, farm):
        """Loot, then hold a covered spot inside the next zone."""
        if P["killloot"] and hpf > 0.3:
            ks = self.kill_loot(t, px, py, threats)
            if ks is not None:
                return self.nav_to(px, py, ks[0], ks[1]), energy > 50, "killloot"
        if P["econ"] or (self.ffa and P["ffa_econ"]):
            r = self.econ_goal(obs, t, px, py, pz, hpf, kinds, zx, zy, zr)
            if r is not None:
                gx, gy, why = r
                d = hyp(gx - px, gy - py)
                if d < 1.5:
                    return (0.0, 0.0), False, "econ-" + why
                return self.nav_to(px, py, gx, gy), energy > 70 and d > 60 and not threats, "econ-" + why
        # regen discipline: hurt and recently in a fight -> stand still somewhere hidden until healed
        # (regen needs 5 s without damage dealt/taken; standing still is also silent)
        if P["regen"] and hpf < P["regen_hp"] and t - max(self.last_hurt, self.last_hit_dealt) < 40.0 and in_safe_zone(self, obs, px, py, t):
            recent = [e for e in self.enemies.values() if t - e["seen"] < 15.0]
            if recent:
                if self.cover_goal is None or t - self.cover_t > 3.0:
                    c = self.find_cover(px, py, recent, lx, ly, lr)
                    self.cover_t = t
                    self.cover_goal = c
                if self.cover_goal is not None and hyp(self.cover_goal[0] - px, self.cover_goal[1] - py) > 1.5:
                    return self.nav_to(px, py, self.cover_goal[0], self.cover_goal[1]), False, "regen-go"
            return (0.0, 0.0), False, "regen"
        goals = []
        # time we have before we must head for the next circle
        dn = hyp(px - nx_, py - ny_)
        travel = max(0.0, dn - nr * 0.7) / 10.0
        must_go = t > ss - travel - 12.0 and dn > nr * 0.8

        def ok_spot(x, y):
            # inside the zone as it will be when we get there + a little
            eta = hyp(x - px, y - py) / 10.0
            cx, cy, cr = self.zone_at(obs.get("zone") or {}, t + eta + 10.0)
            return hyp(x - cx, y - cy) < cr - 8.0

        # weapon pickups worth having
        for pid, (kind, pos, seen) in self.pickups.items():
            if self.take_block.get(pid, -1) > t:
                continue
            gsh = self.pickup_gain(kind, kinds, False)
            if gsh is None:
                continue
            if pos[2] - self.ground(pos[0], pos[1]) > 2.5:
                continue  # roof loot
            d = hyp(pos[0] - px, pos[1] - py)
            if d > 230 or not ok_spot(pos[0], pos[1]) or not self.reachable(pos[0], pos[1]):
                continue
            v = (80.0 if None in kinds else 45.0) + gsh[0] * 6.0
            goals.append((v - d * 0.22, pos[0], pos[1], "loot " + kind))
        for cid, (pos, seen) in self.cubes_mem.items():
            if pos[2] - self.ground(pos[0], pos[1]) > 2.5:
                continue
            d = hyp(pos[0] - px, pos[1] - py)
            if d > 200 or not ok_spot(pos[0], pos[1]) or not self.reachable(pos[0], pos[1]):
                continue
            goals.append((70.0 - d * 0.22, pos[0], pos[1], "cube"))
        for did_drop in obs.get("drops") or []:
            pos = did_drop.get("pos")
            if not pos:
                continue
            d = hyp(pos[0] - px, pos[1] - py)
            lands = float(did_drop.get("lands_in", 0.0))
            eta = d / 11.0
            if d > 260 or not ok_spot(pos[0], pos[1]):
                continue
            # skip drops other mechs are sitting on
            crowd = sum(1 for e in threats if hyp(e["pos"][0] - pos[0], e["pos"][1] - pos[1]) < 60)
            if crowd:
                continue
            v = 75.0 + (35.0 if hpf < 0.7 else 0.0)
            if lands > eta + 8.0:
                v -= 20.0
            goals.append((v - d * 0.2, pos[0], pos[1], "drop"))
        if farm is not None:
            fp = farm["pos"]
            d = hyp(fp[0] - px, fp[1] - py)
            if farm.get("kind") == "crate" and fp[2] - self.ground(fp[0], fp[1]) < 2.5:
                # walk up to it while shooting, cube drops there
                if d > 25:
                    goals.append((50.0 - d * 0.22, fp[0], fp[1], "crate"))
        # crates remembered but not visible: go look
        for cid, (pos, chp, seen) in self.crates.items():
            if pos[2] - self.ground(pos[0], pos[1]) > 2.5:
                continue
            d = hyp(pos[0] - px, pos[1] - py)
            if d > 150 or not ok_spot(pos[0], pos[1]) or not self.reachable(pos[0], pos[1]):
                continue
            goals.append((40.0 - d * 0.22, pos[0], pos[1], "crate"))

        # home spot inside the next circle
        hk = (round(nx_, 1), round(ny_, 1), round(nr, 1))
        if self.home is None or self.home_key != hk:
            hx0, hy0 = self.pick_home(px, py, nx_, ny_, nr)
            self.home = self.snap_goal(hx0, hy0, nx_, ny_, nr)
            self.home_key = hk
        hx, hy = self.home
        dh = hyp(hx - px, hy - py)
        home_v = 30.0 + (60.0 if must_go else 0.0) + (25.0 if hpf < 0.5 else 0.0)
        goals.append((home_v - dh * 0.05, hx, hy, "home"))
        if must_go:
            goals = [gq for gq in goals if hyp(gq[1] - nx_, gq[2] - ny_) < nr * 0.85 or gq[3] == "home"]
        goals.sort(key=lambda gq: -gq[0])
        v, gx, gy, label = goals[0]
        d = hyp(gx - px, gy - py)
        if label == "home" and d < 4.0:
            return (0.0, 0.0), False, "hold"
        mv = self.nav_to(px, py, gx, gy, allow_climb=(label == "home"))
        boost = energy > 70 and d > 40 and not threats
        # walk quietly near home
        if label == "home" and d < 25 and not must_go:
            mv = (mv[0] * 0.4, mv[1] * 0.4)
        return mv, boost, label

    def snap_goal(self, x, y, cx, cy, cr):
        """Move a goal onto walkable ground we can reach (our region, else the main one) inside the
        circle, so the bot never paths into a walled-off pocket."""
        g = self.geo
        if g is None:
            return (x, y)
        try:
            for comp in (self.my_comp, self.main_comp):
                if comp is None or comp < 0:
                    continue
                q = g.nearest_in_comp(x, y, comp, maxr=40)
                if q is None:
                    continue
                dq = hyp(q[0] - cx, q[1] - cy)
                if (cr <= 1.0 and dq <= 8.0) or (cr > 1.0 and dq < max(cr - 3.0, cr * 0.8)):
                    return q
        except Exception:
            log(traceback.format_exc())
        return (x, y)

    def reachable(self, x, y):
        g = self.geo
        if g is None or self.my_comp < 0:
            return True
        try:
            return g.comp_at(x, y) == self.my_comp
        except Exception:
            return True

    def econ_goal(self, obs, t, px, py, pz, hpf, kinds, zx, zy, zr):
        """TTUN-style loot choice (lower score is better): drops d-80, weapons d-20*pref, cubes d-30,
        drones (HP>50%, <170 m: close to 38 m) d-20, crates (HP>45%, <150 m: close to 30 m) d+5."""
        zone = obs.get("zone") or {}
        nx_, ny_ = (zone.get("next_center") or [zx, zy])[:2]
        nr = float(zone.get("next_radius", zr))
        ss = float(zone.get("shrink_start", 1e9))
        dn = hyp(px - nx_, py - ny_)
        if dn > nr * 0.8 and t > ss - max(0.0, dn - nr * 0.6) / 11.0 - 12.0:
            return None  # time to move into the next circle: let the normal logic handle it
        cx, cy, cr = self.zone_at(zone, t + 12.0)

        def ok(x, y):
            return hyp(x - cx, y - cy) < cr - 8.0 and self.reachable(x, y)

        best, bs = None, 1e18
        pref = {"laser": 2.6, "lightning": 2.4}
        for pid, (kind, pos, seen) in self.pickups.items():
            if self.take_block.get(pid, -1) > t or self.pickup_gain(kind, kinds, False) is None:
                continue
            if pos[2] - self.ground(pos[0], pos[1]) > 2.5 or not ok(pos[0], pos[1]):
                continue
            sc = hyp(pos[0] - px, pos[1] - py) - pref.get(kind, 1.0) * 20.0
            if sc < bs:
                best, bs = (pos[0], pos[1], "arm"), sc
        for cid, (pos, seen) in self.cubes_mem.items():
            if pos[2] - self.ground(pos[0], pos[1]) > 2.5 or not ok(pos[0], pos[1]):
                continue
            sc = hyp(pos[0] - px, pos[1] - py) - 30.0
            if sc < bs:
                best, bs = (pos[0], pos[1], "cube"), sc
        for dr in obs.get("drops") or []:
            pos = dr.get("pos")
            if not pos or (dr.get("holder") not in (None, self.my_id)):
                continue
            d = hyp(pos[0] - px, pos[1] - py)
            if d > 220 or float(dr.get("lands_in") or 0) > d / 11.7 + 3.0 or not ok(pos[0], pos[1]):
                continue
            sc = d - 80.0
            if sc < bs:
                ring = float(dr.get("radius") or 8.0)
                best, bs = ((px, py, "drop") if d < ring - 3.0 else (pos[0], pos[1], "drop")), sc
        if hpf > 0.5:
            for n in self.npcs.values():
                pos = n["pos"]
                d = hyp(pos[0] - px, pos[1] - py)
                nhp = n.get("hp")
                if d > 170 or (nhp is not None and nhp <= 0) or self.t - n["seen"] > 5.0 or not ok(pos[0], pos[1]):
                    continue
                sc = d - 20.0
                if sc < bs:
                    best, bs = ((pos[0], pos[1], "drone") if d > 38 else (px, py, "drone")), sc
        if hpf > 0.45:
            for cid, (pos, chp, seen) in self.crates.items():
                if pos[2] - self.ground(pos[0], pos[1]) > 2.5 or not ok(pos[0], pos[1]):
                    continue
                d = hyp(pos[0] - px, pos[1] - py)
                if d > 150:
                    continue
                sc = d + 5.0
                if sc < bs:
                    best, bs = ((pos[0], pos[1], "crate") if d > 30 else (px, py, "crate")), sc
        return best

    def pick_home(self, px, py, cx, cy, r):
        """A cover node inside the circle, on our side of its centre."""
        d = hyp(px - cx, py - cy)
        if d < 1e-6:
            tx, ty = cx, cy
        else:
            k = min(1.0, (r * 0.45) / d)
            tx, ty = cx + (px - cx) * k, cy + (py - cy) * k
        g = self.geo
        if g is None:
            return (tx, ty)
        try:
            best, bd = None, 1e18
            for c in g.cover_near(tx, ty, max(20.0, min(60.0, r * 0.5))):
                if hyp(c[0] - cx, c[1] - cy) > r * 0.6:
                    continue
                dd = hyp(c[0] - tx, c[1] - ty)
                if dd < bd:
                    bd, best = dd, c
            if best is not None:
                return best
            k = g.nearest_free(tx, ty, maxr=20)
            if k is not None:
                return g.node_xy(k)
        except Exception:
            log(traceback.format_exc())
        return (tx, ty)


# --------------------------------------------------------------------- protocol loop
def _send(msg):
    sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _reader(q):
    try:
        for line in sys.stdin:
            if line.strip():
                q.put((time.perf_counter(), line))
    except Exception:
        log(traceback.format_exc())
    q.put((time.perf_counter(), None))


CONTINUOUS = ("move", "aim", "boost")


def main():
    q = queue.Queue()
    threading.Thread(target=_reader, args=(q,), daemon=True).start()
    brain = None
    last = {}
    errors = 0
    while True:
        batch = [q.get()]
        while True:
            try:
                batch.append(q.get_nowait())
            except queue.Empty:
                break
        latest = None
        for (tr, raw) in batch:
            if raw is None:
                return
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            kind = msg.get("type")
            if kind == "hello":
                try:
                    brain = Brain(msg)
                except Exception:
                    log("init failed:\n" + traceback.format_exc())
                    brain = None
                gc.collect()
                try:
                    gc.freeze()
                except AttributeError:
                    pass
                _send({"type": "ready", "name": BOT_NAME})
            elif kind == "obs":
                latest = (tr, msg)
            elif kind == "end":
                return
        if latest is None:
            continue
        tr, obs = latest
        action = {}
        if brain is not None:
            try:
                action = dict(brain.act(obs, tr) or {})
                last = {k: action[k] for k in CONTINUOUS if k in action}
            except Exception:
                errors += 1
                if errors <= 5 or errors % 100 == 0:
                    log("act failed at tick %s (%d):\n%s" % (obs.get("tick"), errors, traceback.format_exc()))
                action = dict(last)
        action["tick"] = obs.get("tick")
        try:
            _send(action)
        except (TypeError, ValueError):
            log("unserializable action:\n" + traceback.format_exc())
            _send(dict(last, tick=obs.get("tick")))


if __name__ == "__main__":
    main()
