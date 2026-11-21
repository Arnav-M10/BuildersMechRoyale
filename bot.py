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
    "chase": True,        # push toward a target we cannot see when healthy
    "retreat_hp": 0.38,   # break line of sight below this HP fraction
    "laser_band": (50.0, 120.0),
}

try:
    import geo as geomod
except Exception:  # pragma: no cover - the bot still plays without the map model
    geomod = None


TRACE = None
if os.environ.get("MF_TRACE"):
    try:
        TRACE = open(os.environ["MF_TRACE"], "w")
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
W_VAL = {"gun": 9.0, "lightning": 5.0, "laser": 4.0, "mortar": 0.0}

UPGRADE_RANK = [
    "plating", "gun_damage", "armor", "gun_mag", "gun_rate", "nanites", "laser_damage",
    "laser_charge", "gun_velocity", "lightning_efficiency", "servos", "gyros", "lightning_range",
    "sensors", "capacitor", "reactor", "jets", "lightning_chain", "laser_pierce",
    "mortar_reload", "mortar_velocity", "mortar_splash",
]


def lead_vel(v, pv, T):
    """Effective horizontal velocity for leading a shot T seconds ahead. Fitted on 62k samples of
    live house-bot motion: jukes keep turning, so extrapolate the last 0.1 s acceleration
    (within 2.5 m of truth: 94% vs 89% for constant velocity at T=0.3 s; 85% vs 81% at 0.4 s)."""
    if pv is None or T > 0.65:
        k = 1.0 if pv is None else 0.9
        return v[0] * k, v[1] * k
    if T <= 0.5:
        a, b = 1.5, -0.62
    else:
        a, b = 1.37, -0.56
    return a * v[0] + b * pv[0], a * v[1] + b * pv[1]


class Brain:
    def __init__(self, hello):
        cfg = hello.get("config") or {}
        self.cfg = cfg
        self.my_id = hello.get("slot")
        seed = hello.get("seed")
        self.rng = random.Random(seed if isinstance(seed, int) else 12345)
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
        self.charge_seen = {}   # enemy id -> time we first saw it charging
        self.hints = []         # (t, bearing, weight) threat directions from sounds/damage
        self.last_hurt = -99.0
        self.last_hit_dealt = -99.0
        self.shorted_until = -1.0
        self.take_block = {}
        self.target_id = None
        self.target_since = 0.0
        self.strafe = 1.0
        self.strafe_until = 0.0
        self.path = None
        self.path_goal = None
        self.path_t = -99.0
        self.hist = []          # (t, x, y, commanded)
        self.stuck_until = -1.0
        self.stuck_dir = (0.0, 0.0)
        self.stuck_log = []
        self.cover_goal = None
        self.cover_t = -99.0
        self.home = None
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

    @staticmethod
    def zone_at(z, t):
        c = z.get("center") or [0.0, 0.0]
        r = float(z.get("radius", 1e4))
        nc = z.get("next_center") or c
        nr = float(z.get("next_radius", r))
        ss = float(z.get("shrink_start", 1e9))
        se = float(z.get("shrink_end", 1e9))
        if t <= ss:
            return c[0], c[1], r
        if se <= ss or t >= se:
            return nc[0], nc[1], nr
        s = (t - ss) / (se - ss)
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
            if old is not None and 0.0 < t - old["seen"] <= 0.16:
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
                by = ev.get("by")
                if by is not None:
                    self.attacked_by[by] = t
                b = ev.get("bearing")
                if b is not None:
                    self.hints.append((t, float(b), 3.0))
            elif et == "hit":
                self.last_hit_dealt = t
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
                tid = ev.get("target")
                self.enemies.pop(tid, None)
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

    # ----------------------------------------------------------------- upgrades
    def choose_upgrade(self, offer):
        ch = offer.get("choices") or []
        best, bi = 1e9, 0
        for i, c in enumerate(ch):
            cid = c.get("id")
            r = UPGRADE_RANK.index(cid) if cid in UPGRADE_RANK else 50
            if r < best:
                best, bi = r, i
        return bi

    # ----------------------------------------------------------------- main
    def act(self, obs, t_recv):
        self.deadline = t_recv + 0.026
        t = float(obs.get("t", 0.0))
        self.t = t
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
        for d, e in cands[:6]:
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
        for e in tlist:
            d = e["dist"]
            ehp = e.get("hp") or 1400.0
            emax = e.get("max_hp") or 1400.0
            s = d
            s -= (1.0 - ehp / emax) * 90.0
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
        if target is not None and target["id"] != self.target_id:
            self.target_id = target["id"]
            self.target_since = t
        if target is None:
            self.target_id = None

        threats = [e for e in self.enemies.values() if t - e["seen"] < 8.0]
        visible_threats = [e for e in tlist if e["los"]]

        # NPCs attacking us
        npc_target = None
        for n in self.npcs.values():
            if t - n["seen"] > 0.01:
                continue
            d = hyp(n["pos"][0] - px, n["pos"][1] - py)
            n["dist"] = d
            if t - self.attacked_by.get(n["id"], -99) < 4.0 or d < 40.0:
                if npc_target is None or d < npc_target["dist"]:
                    npc_target = n

        # ---------------------------------------------------------- aim & fire
        fire = [False, False]
        aim_yaw, aim_pitch = None, 0.0
        aim_point = None
        lead_speed = None
        shoot = None  # (point3, vel3, kind:'mech'/'npc'/'crate', radius, height, los)
        if target is not None:
            shoot = target
        elif npc_target is not None:
            shoot = npc_target
        farm = None
        if shoot is None and not threats:
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
            d3 = math.sqrt((com[0] - px) ** 2 + (com[1] - py) ** 2 + (com[2] - mz) ** 2)
            gst = (gun_w or {}).get("stats") or self.W.get("gun") or {}
            gspeed = float(gst.get("speed", 252.0))
            grange = float(gst.get("range", 230.0))
            # laser about to fire -> aim hitscan, else lead for the gun
            hitscan = laser_w is not None and laser_charge >= laser_full - 0.12 and los_ok
            if gun_w is None:
                hitscan = True
            if hitscan:
                tp = (com[0] + svel[0] * 0.07, com[1] + svel[1] * 0.07, com[2] + clamp(svel[2] * 0.07, -1, 1))
            else:
                tlead = d3 / gspeed + 0.05
                tp = com
                for _ in range(3):
                    vzl = clamp(svel[2] * tlead, -3.0, 3.0)
                    ex_, ey_ = lead_vel(svel, pvel, tlead)
                    tp = (com[0] + ex_ * tlead, com[1] + ey_ * tlead, com[2] + vzl)
                    dd = math.sqrt((tp[0] - px) ** 2 + (tp[1] - py) ** 2 + (tp[2] - mz) ** 2)
                    tlead = dd / gspeed + 0.05
            hd = hyp(tp[0] - px, tp[1] - py)
            aim_yaw = math.atan2(tp[1] - py, tp[0] - px)
            aim_pitch = clamp(math.atan2(tp[2] - mz, max(hd, 0.1)), min_pitch, max_pitch)
            yaw_err = abs(wrap(aim_yaw - my_aim[0]))
            pit_err = abs(aim_pitch - my_aim[1])
            # error left after one sim tick of slewing
            ye1 = max(0.0, yaw_err - turn_rate * 0.05)
            pe1 = max(0.0, pit_err - pitch_rate * 0.05)
            tol_y = math.atan2(rad * 0.75, max(hd, 1.0))
            tol_p = math.atan2(2.5 if kind == "mech" else 0.8, max(hd, 1.0))
            aligned = ye1 <= tol_y and pe1 <= tol_p
            aligned_now = yaw_err <= tol_y and pit_err <= tol_p
            busy_fight = kind != "mech" and bool(visible_threats)
            if los_ok and not busy_fight:
                # gun
                if gun_w is not None and d3 < grange * 0.98 and aligned:
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
                    if ready and los_ok and in_range and worth and yaw_err < 0.6:
                        fire[laser_slot] = True
            aim_point = tp
        else:
            # keep a charging laser from wasting itself: hold until overtime, then release anyway
            if laser_w is not None and laser_charge > 0.0:
                fire[laser_slot] = laser_charge < laser_full + laser_hold - 0.35

        if aim_yaw is None:
            aim_yaw, aim_pitch = self.scan_aim(t, px, py, my_aim, threats, vx, vy)

        act["aim"] = [round(aim_yaw, 4), round(aim_pitch, 4)]
        act["fire"] = fire

        # reload when idle
        if gun_w is not None and gun_w.get("ammo") is not None:
            gst = gun_w.get("stats") or {}
            mag = float(gst.get("mag", 12))
            if (gun_w.get("reload") or 0) <= 0 and not fire[gun_slot]:
                if gun_w["ammo"] <= 0 or (gun_w["ammo"] < mag * 0.5 and not visible_threats and shoot is None):
                    act["reload"] = True

        # ---------------------------------------------------------- weapon pickups
        take = self.choose_take(px, py, pz, kinds, laser_charge)
        if take is not None:
            act["take"] = take[0]
            if take[1] is not None:
                act["drop"] = take[1]

        # ---------------------------------------------------------- movement
        move, boost, jump, label = self.movement(obs, me, t, px, py, pz, vx, vy, hpf, energy, kinds,
                                                  target, tlist, threats, visible_threats,
                                                  zx, zy, zr, lx, ly, lr, nx_, ny_, nr, ss, walk,
                                                  laser_charge > 0, farm)
        mx, my = move
        n = hyp(mx, my)
        if n > 1.0:
            mx, my = mx / n, my / n
        act["move"] = [round(mx, 3), round(my, 3)]
        act["boost"] = bool(boost)
        act["jump"] = bool(jump)
        self.hist.append((t, px, py, hyp(mx, my), hyp(vx, vy)))
        if len(self.hist) > 40:
            del self.hist[:-40]
        self.prev_hp = hp
        act["debug"] = {"label": ("%s hp%d e%d c%d" % (label, hp, energy, cubes))[:80]}
        if TRACE is not None and int(obs.get("tick", 0)) % 20 == 0:
            try:
                TRACE.write("t%.1f pos(%.0f,%.0f,%.0f) hp%.0f e%.0f in_zone=%s zone(%.0f,%.0f r%.0f) next(%.0f,%.0f r%.0f) %s mv=%s w=%s tgt=%s\n" % (
                    t, px, py, pz, hp, energy, me.get("in_zone"), zx, zy, zr, nx_, ny_, nr, label,
                    act["move"], kinds, self.target_id))
                TRACE.flush()
            except Exception:
                pass
        return act

    # ----------------------------------------------------------------- farming
    def pick_farm(self, px, py, pz, muzzle, hpf, zx, zy, zr):
        """A crate or a passive NPC to shoot while no mech is around."""
        best, bs = None, 1e18
        for cid, (pos, chp, seen) in self.crates.items():
            if self.t - seen > 0.01:
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
        if hpf > 0.6:
            for n in self.npcs.values():
                if self.t - n["seen"] > 0.01:
                    continue
                d = hyp(n["pos"][0] - px, n["pos"][1] - py)
                if d > 100 or (n.get("hp") or 350) <= 0:
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
                 visible_threats, zx, zy, zr, lx, ly, lr, nx_, ny_, nr, ss, walk, charging, farm):
        boost = False
        jump = False
        label = ""
        grounded = bool(me.get("grounded", True))
        in_zone = bool(me.get("in_zone", True))
        # safe circle we must stay in (the zone 6 s from now, with margin)
        margin = clamp(0.12 * lr, 3.0, 12.0)
        dz = hyp(px - lx, py - ly)

        mv = None
        # ---- 1. mortar shells / bullets about to hit us
        dodge = self.projectile_dodge(obs, px, py, pz, vx, vy)

        # ---- 2. zone emergency
        zone_push = None
        if (not in_zone) or dz > lr - margin * 0.5:
            if lr > 1.0:
                f = max(0.0, (lr - margin * 1.5)) / max(dz, 1e-6)
                gx, gy = lx + (px - lx) * min(1.0, f), ly + (py - ly) * min(1.0, f)
            else:
                gx, gy = lx, ly
            zone_push = self.snap_goal(gx, gy, lx, ly, lr)

        hp_low = hpf < P["retreat_hp"]
        fight = target is not None and (target["los"] or t - self.attacked_by.get(target["id"], -99) < 3.0
                                         or target["dist"] < 120)

        if zone_push is not None:
            mv = self.nav_to(px, py, zone_push[0], zone_push[1], allow_climb=True)
            boost = (not in_zone) and energy > 20
            label = "zone"
            if target is not None and target["los"]:
                sx, sy = self.strafe_vec(t, px, py, target)
                mv = (mv[0] + sx * 0.35, mv[1] + sy * 0.35)
        elif hp_low and threats and (len(visible_threats) > 0 or t - self.last_hurt < 4.0):
            # ---- 3. break line of sight and regenerate
            if self.cover_goal is None or t - self.cover_t > 1.2:
                c = self.find_cover(px, py, threats, lx, ly, lr)
                self.cover_t = t
                if c is not None:
                    self.cover_goal = c
            if self.cover_goal is not None and hyp(self.cover_goal[0] - px, self.cover_goal[1] - py) > 1.5:
                mv = self.nav_to(px, py, self.cover_goal[0], self.cover_goal[1])
                boost = energy > 30
                label = "cover"
            else:
                # no cover found or arrived: back away from nearest threat while strafing
                if self.cover_goal is not None:
                    mv = (0.0, 0.0)
                    label = "hide"
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
        elif fight:
            mv, boost, label = self.fight_move(t, px, py, kinds, target, hpf, energy, visible_threats, walk)
        else:
            self.cover_goal = None if hpf > 0.6 else self.cover_goal
            mv, boost, label = self.wander(obs, me, t, px, py, pz, hpf, energy, kinds, threats,
                                           zx, zy, zr, lx, ly, lr, nx_, ny_, nr, ss, farm)

        # keep inside the (future) zone
        if zone_push is None and mv is not None and (mv[0] or mv[1]):
            fx, fy = px + mv[0] * 6.0, py + mv[1] * 6.0
            if hyp(fx - lx, fy - ly) > lr - margin:
                ix, iy = unit(lx - px, ly - py)
                mv = (mv[0] * 0.3 + ix, mv[1] * 0.3 + iy)

        if dodge is not None:
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
                if t - t0 < 2.0 and all(h[3] > 0.6 and h[4] < 1.5 for h in self.hist[-15:]):
                    a = math.atan2(mv[1], mv[0]) + (PI / 2 if self.rng.random() < 0.5 else -PI / 2)
                    self.stuck_dir = (math.cos(a), math.sin(a))
                    self.stuck_log = [x for x in self.stuck_log if t - x < 12.0] + [t]
                    self.stuck_until = t + (0.9 if len(self.stuck_log) < 3 else 3.0)
                    self.path = None
                    self.hist.clear()
        if charging:
            boost = False  # half speed anyway, keep energy
        return mv, boost, jump, label

    def strafe_vec(self, t, px, py, e):
        if t >= self.strafe_until:
            if self.rng.random() < 0.7:
                self.strafe = -self.strafe
            self.strafe_until = t + self.rng.uniform(0.3, 0.95)
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
            rmin, rmax = 0.0, 32.0
        else:
            rmin, rmax = 45.0, 110.0
        if has_lit:
            ehp = (e.get("hp") or 1400) / max(1.0, e.get("max_hp") or 1400)
            if ("laser" in ew and "lightning" not in ew) or (ehp < 0.5 and hpf > 0.5 and energy > 30):
                rmin, rmax = 0.0, 32.0
        if "lightning" in ew and not has_lit:
            rmin = max(rmin, 60.0)
            rmax = max(rmax, rmin + 40.0)
        if len(visible_threats) >= 2:
            rmin = max(rmin, 60.0)
            rmax = max(rmax, rmin + 40.0)
        if not e["los"]:
            # out of sight: push in when healthy, otherwise hold still and regen
            if P["chase"] and hpf > 0.6 and (hpf > (e.get("hp") or 1400) / max(1.0, e.get("max_hp") or 1400) - 0.1):
                mv = self.nav_to(px, py, e["pos"][0], e["pos"][1])
                return mv, False, "hunt"
            return (0.0, 0.0), False, "wait"
        radial = 0.0
        if d > rmax:
            radial = 1.0
        elif d < rmin:
            radial = -1.0
        sx, sy = self.strafe_vec(t, px, py, e)
        ax, ay = unit(e["pos"][0] - px, e["pos"][1] - py)
        if radial > 0:
            dirn = self.nav_to(px, py, e["pos"][0], e["pos"][1])
            mv = (dirn[0] * 0.85 + sx * 0.5, dirn[1] * 0.85 + sy * 0.5)
        else:
            mv = (ax * radial * 0.8 + sx, ay * radial * 0.8 + sy)
        boost = energy > 55 and d > 60 and not has_lit
        if radial < 0 and d < rmin * 0.6 and energy > 30:
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
                if q is not None and (cr <= 1.0 or hyp(q[0] - cx, q[1] - cy) < max(cr - 3.0, cr * 0.8)):
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
