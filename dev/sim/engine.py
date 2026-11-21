"""Deterministic, standard-library mechfight/1 headless simulator.

This intentionally models decisions at 10 Hz and physics at 20 Hz. The static
map geometry is supplied by the repository's geo.Geo implementation.
"""
from __future__ import annotations

import copy
import math
import random
import time
from dataclasses import dataclass, field


DT = 0.05
DECISION_TICKS = 2
PI = math.pi
TAU = 2.0 * math.pi

_SOLID_PS = {}


def _solid_prefix(g):
    """2-D prefix sums over cells holding any solid interval, cached per map object."""
    ps = _SOLID_PS.get(id(g))
    if ps is not None:
        return ps
    from array import array
    NX, NY = g.NX, g.NY
    W = NX + 1
    ps = array("i", [0]) * (W * (NY + 1))
    lo, hi, multi = g.lo, g.hi, g.multi
    for j in range(NY):
        run = 0
        row = j * NX
        prev = j * W
        cur = (j + 1) * W
        for i in range(NX):
            k = row + i
            if lo[k] < hi[k] or k in multi:
                run += 1
            ps[cur + i + 1] = ps[prev + i + 1] + run
    _SOLID_PS[id(g)] = ps
    return ps


def clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def wrap(a):
    return (a + PI) % TAU - PI


def norm2(x, y):
    return math.hypot(x, y)


def unit(x, y):
    d = math.hypot(x, y)
    return (x / d, y / d) if d > 1e-9 else (0.0, 0.0)


def direction(yaw, pitch):
    cp = math.cos(pitch)
    return cp * math.cos(yaw), cp * math.sin(yaw), math.sin(pitch)


def yaw_pitch(src, dst):
    dx, dy, dz = dst[0] - src[0], dst[1] - src[1], dst[2] - src[2]
    return math.atan2(dy, dx), math.atan2(dz, max(1e-9, math.hypot(dx, dy)))


@dataclass
class Weapon:
    kind: str
    cooldown: float = 0.0
    ammo: float = 0.0
    reload: float = 0.0
    charge: float = 0.0
    charging: bool = False
    release_pending: bool = False
    equip: float = 0.0
    shorted: float = 0.0


@dataclass
class Actor:
    id: int
    kind: str
    pos: list
    vel: list
    aim: list
    hp: float
    energy: float
    cubes: float = 0.0
    upgrades: dict = field(default_factory=dict)
    weapons: list = field(default_factory=lambda: [None, None])
    alive: bool = True
    stats: dict = field(default_factory=dict)
    weapon_stats: dict = field(default_factory=dict)
    max_hp: float = 1400.0
    max_energy: float = 100.0
    grounded: bool = True
    commanded_aim: list = field(default_factory=lambda: [0.0, 0.0])
    last_hurt: float = -1e9
    last_dealt: float = -1e9
    last_energy_spend: float = -1e9
    last_step: float = -1e9
    offer: dict | None = None
    offer_queue: int = 0
    threshold_index: int = 0
    events: list = field(default_factory=list)
    death_time: float | None = None
    death_cause: str | None = None
    kills: int = 0
    last_damage_by: int | None = None
    last_damage_at: float = -1e9
    npc_home: tuple | None = None
    npc_target: int | None = None
    house_tier: int = 0
    house_strafe: float = 1.0
    house_strafe_until: float = 0.0
    house_next_act: float = 0.0
    house_action: dict = field(default_factory=dict)
    seen: dict = field(default_factory=dict)
    house_roam_angle: float = 0.0
    house_roof_goal: tuple | None = None
    house_roof_goal_until: float = 0.0
    house_roof_next: float = 0.0
    house_roof_arrived: bool = False
    house_ticks: int = 0
    house_moving_ticks: int = 0
    house_boost_ticks: int = 0
    house_roof_ticks: int = 0
    house_path: list = field(default_factory=list)
    house_path_target: tuple | None = None
    house_path_refresh: float = 0.0
    gun_shots: int = 0
    gun_mech_hits: int = 0
    gun_hunt_shots: int = 0
    gun_hunt_hits: int = 0
    house_target_id: int | None = None


@dataclass
class Projectile:
    id: int
    kind: str
    owner: int
    pos: list
    vel: list
    ttl: float
    damage: float
    radius: float = 0.3
    travelled: float = 0.0
    target: int | None = None


@dataclass
class Item:
    id: int
    kind: str
    pos: list
    hp: float = 0.0
    weapon: Weapon | None = None


@dataclass
class SupplyDrop:
    id: int
    pos: list
    called: float
    lands: float
    holder: int | None = None
    progress: float = 0.0
    announced: bool = False


class Match:
    def __init__(self, geo, config, seed, ffa=False, validate=None):
        self.geo = geo
        self.cfg = config
        self.seed = int(seed)
        self.rng = random.Random(self.seed)
        self.ffa = bool(ffa)
        self.validate = validate
        self.mechanics = config["mech"]
        self.base_weapons = config["weapons"]
        self.actors = {}
        self.crates = {}
        self.cubes = {}
        self.pickups = {}
        self.projectiles = []
        self.beams = []
        self.sound_log = []
        self.next_projectile_id = 1
        self.next_cube_id = 5000
        self.next_pickup_id = 8000
        self.next_crate_id = 1000
        self.events_by_actor = {}
        self.t = 0.0
        self.tick = 0
        self.prev_obs_t = 0.0
        self.max_time = float(config["match"]["duration_s"])
        self._load_map_points()
        self.zone_circles = self._make_zone_circles()
        self._spawn_actors()
        self._spawn_items()
        self._spawn_drops()

    def _load_map_points(self):
        g = self.geo
        self.free_nodes = [i for i, v in enumerate(g.nav) if v]
        self.roof_nodes = []
        for j in range(g.NY):
            row = j * g.NX
            for i in range(g.NX):
                k = row + i
                if g.hi[k] > g.lo[k] and g.hi[k] - g.terr[k] > 2.0:
                    self.roof_nodes.append(k)
        if not self.free_nodes:
            raise RuntimeError("Geo.nav has no walkable cells")

    def _point(self, zone=None, roof=None, min_distance=None, occupied=None):
        occupied = occupied or []
        for _ in range(2500):
            if roof is True and self.roof_nodes:
                k = self.rng.choice(self.roof_nodes)
                i, j = k % self.geo.NX, k // self.geo.NX
                x, y, z = self.geo.X0 + i, self.geo.Y0 + j, float(self.geo.hi[k])
            else:
                k = self.rng.choice(self.free_nodes)
                x, y = self.geo.node_xy(k)
                z = self.geo.ground(x, y)
            gr = self.geo.ground(x, y)
            if zone:
                cx, cy, r = zone
                if norm2(x - cx, y - cy) > r:
                    continue
            is_roof = z - gr > 2.0
            if roof is not None and bool(roof) != is_roof:
                continue
            if any(norm2(x - ox, y - oy) < min_distance for ox, oy in occupied) if min_distance else False:
                continue
            return [float(x), float(y), float(z)]
        # fall back to any free cell meeting as many constraints as possible
        for k in self.free_nodes:
            x, y = self.geo.node_xy(k)
            if zone and norm2(x - zone[0], y - zone[1]) > zone[2]:
                continue
            return [float(x), float(y), float(self.geo.surface(x, y))]
        return [0.0, 0.0, 0.0]

    def _make_zone_circles(self):
        z = self.cfg["zone"]
        center = tuple(map(float, z["start_center"]))
        radius = float(z["start_radius"])
        out = [(0.0, center, radius)]
        for p in z["phases"]:
            nr = float(p["radius"])
            dr = max(0.0, radius - nr)
            angle = self.rng.random() * TAU
            # The next ring remains completely inside the current one.
            offset = self.rng.random() * max(0.0, dr - 2.0) * 0.78
            center = (center[0] + math.cos(angle) * offset,
                      center[1] + math.sin(angle) * offset)
            start = out[-1][0] + (float(z["phases"][len(out)-1]["hold_s"]) +
                                  float(z["phases"][len(out)-1]["shrink_s"]) if len(out) > 1 else 0.0)
            out.append((start, center, nr))
            radius = nr
        # Recompute clean phase starts: entry 0 is the initial circle, entry i is phase i's end circle.
        t = 0.0
        out = [(0.0, tuple(map(float, z["start_center"])), float(z["start_radius"]))]
        for i, p in enumerate(z["phases"]):
            t += float(p["hold_s"]) + float(p["shrink_s"])
            out.append((t, center if i == len(z["phases"])-1 else out[-1][1], float(p["radius"])))
        # Generate nested centers deterministically for each phase.
        old = out[0][1]
        rebuilt = [out[0]]
        for i, p in enumerate(z["phases"]):
            oldr = out[i][2]
            nr = float(p["radius"])
            a = self.rng.random() * TAU
            d = self.rng.random() * max(0.0, oldr - nr - 1.0) * 0.65
            old = (old[0] + math.cos(a) * d, old[1] + math.sin(a) * d)
            rebuilt.append((out[i+1][0], old, nr))
        return rebuilt

    def zone_state(self, t=None):
        t = self.t if t is None else float(t)
        phases = self.cfg["zone"]["phases"]
        prev = self.zone_circles[0]
        phase_idx = len(phases) - 1
        phase_start = 0.0
        for i, p in enumerate(phases):
            hold = float(p["hold_s"])
            shrink = float(p["shrink_s"])
            ss, se = phase_start + hold, phase_start + hold + shrink
            nxt = self.zone_circles[i + 1]
            if t < se or i == len(phases) - 1:
                phase_idx = i
                if t <= ss:
                    c, r = prev[1], prev[2]
                else:
                    f = clamp((t - ss) / max(1e-9, shrink), 0.0, 1.0)
                    c = (prev[1][0] + (nxt[1][0] - prev[1][0]) * f,
                         prev[1][1] + (nxt[1][1] - prev[1][1]) * f)
                    r = prev[2] + (nxt[2] - prev[2]) * f
                future = nxt
                return {"center": [c[0], c[1]], "radius": r,
                        "next_center": [future[1][0], future[1][1]],
                        "next_radius": future[2], "shrink_start": ss,
                        "shrink_end": se, "dps": float(p["dps"])}, phase_idx
            phase_start = se
            prev = nxt
        p = phases[-1]
        last = self.zone_circles[-1]
        return {"center": list(last[1]), "radius": last[2],
                "next_center": list(last[1]), "next_radius": last[2],
                "shrink_start": self.max_time, "shrink_end": self.max_time,
                "dps": float(p["dps"])}, len(phases)-1

    def _spawn_actors(self):
        z = self.zone_state(0)[0]
        start_zone = (z["center"][0], z["center"][1], z["radius"] *
                      float(self.cfg["match"].get("spawn_zone_fraction", 0.8)))
        points = []
        for i in range(16):
            p = self._point(start_zone, min_distance=130.0, occupied=[(q[0], q[1]) for q in points])
            points.append(p)
        for i in range(16):
            a = Actor(i, "mech", points[i], [0.0, 0.0, 0.0],
                      [self.rng.uniform(-PI, PI), 0.0], float(self.mechanics["hp"]),
                      float(self.mechanics["energy"]))
            a.house_roam_angle = self.rng.random() * TAU
            a.commanded_aim = list(a.aim)
            if i == 0 or self.ffa:
                a.house_tier = 0
            else:
                a.house_tier = 1 if 1 <= i <= 4 else 2 if i <= 8 else 3 if i <= 12 else 4
            a.weapons[0] = self._new_weapon("gun")
            self._rebuild_stats(a)
            a.hp = a.max_hp
            self.actors[i] = a
        npc = self.cfg.get("npc", {})
        for j in range(int(npc.get("count", 12))):
            p = self._point(start_zone, min_distance=45.0,
                            occupied=[(a.pos[0], a.pos[1]) for a in self.actors.values()])
            aid = 100 + j
            a = Actor(aid, "npc", p, [0.0, 0.0, 0.0],
                      [self.rng.uniform(-PI, PI), 0.0], float(npc.get("hp", 350)), 100.0)
            a.npc_home = (p[0], p[1])
            a.max_hp = float(npc.get("hp", 350))
            a.house_action = {"move": [0.0, 0.0], "aim": list(a.aim), "fire": [False, False]}
            self.actors[aid] = a

    def _new_weapon(self, kind):
        s = self.cfg["weapons"][kind]
        return Weapon(kind=kind, ammo=float(s.get("mag", 0.0)))

    def _rebuild_stats(self, a):
        if a.kind != "mech":
            return
        stats = copy.deepcopy(self.mechanics)
        wstats = copy.deepcopy(self.base_weapons)
        defs = {x["id"]: x for x in self.cfg.get("upgrades", [])}
        for uid, n in a.upgrades.items():
            u = defs.get(uid)
            if not u:
                continue
            for effect in u.get("effects", []):
                path = effect.get("path", "").split(".")
                dst = stats if path[0] == "mech" else wstats if path[0] == "weapons" else None
                if dst is None:
                    continue
                for part in path[1:-1]:
                    dst = dst.get(part, {})
                k = path[-1]
                if "mul" in effect:
                    dst[k] = float(dst.get(k, 0.0)) * (float(effect["mul"]) ** n)
                elif "add" in effect:
                    dst[k] = float(dst.get(k, 0.0)) + float(effect["add"]) * n
        a.stats = stats
        a.weapon_stats = wstats
        a.max_energy = float(stats.get("energy", self.mechanics["energy"]))
        a.max_hp = float(stats.get("hp", self.mechanics["hp"])) * (
            1.0 + float(self.cfg["cubes"].get("hp_per_cube", 0.04)) *
            min(a.cubes, float(self.cfg["cubes"].get("stat_cap", 10))))
        a.energy = min(a.energy, a.max_energy)
        for w in a.weapons:
            if w:
                # Upgrade stats are derived on observation; keep ammo/cooldown item state.
                pass

    def _spawn_items(self):
        z = self.zone_state(0)[0]
        cr = self.cfg.get("crates", {})
        for _ in range(int(cr.get("count", 36))):
            roof = self.rng.random() < float(cr.get("roof_fraction", 0.25))
            p = self._point((z["center"][0], z["center"][1], z["radius"] * 0.95), roof=roof)
            iid = self.next_crate_id
            self.next_crate_id += 1
            self.crates[iid] = Item(iid, "crate", p, float(cr.get("hp", 300)))
        loot = self.cfg.get("loot", {})
        weights = loot.get("weights", {})
        kinds = [k for k in ("mortar", "laser", "lightning") if float(weights.get(k, 0)) > 0]
        ws = [float(weights[k]) for k in kinds]
        for _ in range(int(loot.get("floor_count", 40))):
            roof = self.rng.random() < float(loot.get("floor_roof_fraction", 0.25))
            p = self._point((z["center"][0], z["center"][1], z["radius"] * 0.98), roof=roof)
            k = self.rng.choices(kinds, weights=ws, k=1)[0] if kinds else "gun"
            self._new_pickup(k, p)

    def _spawn_drops(self):
        d = self.cfg.get("drops") or {}
        self.drops = []
        z = self.zone_state(0)[0]
        for i, called in enumerate(d.get("times", [])):
            landing = self.zone_state(float(called) + float(d.get("fall_s", 25)))[0]
            p = self._point((landing["center"][0], landing["center"][1],
                             max(0.0, landing["radius"] - float(d.get("radius", 8)))))
            self.drops.append(SupplyDrop(20000 + i, p, float(called),
                                         float(called) + float(d.get("fall_s", 25))))

    def _new_pickup(self, kind, pos, weapon=None):
        iid = self.next_pickup_id
        self.next_pickup_id += 1
        self.pickups[iid] = Item(iid, kind, list(pos), weapon=weapon)
        return iid

    def _sound(self, kind, pos, source):
        self.sound_log.append((self.t, kind, list(pos), source))

    def _event(self, actor_id, event):
        a = self.actors.get(actor_id)
        if a is not None and a.alive:
            a.events.append(event)

    def _weapon_observation(self, a, w):
        stats = copy.deepcopy(a.weapon_stats.get(w.kind, self.base_weapons[w.kind]))
        d = {"kind": w.kind, "cooldown": max(0.0, w.cooldown, w.equip), "stats": stats}
        if w.kind == "gun":
            d.update(ammo=w.ammo, reload=max(0.0, w.reload))
        if w.kind == "laser":
            d["charge"] = w.charge if w.charging else 0.0
        return d

    def _actor_size(self, a):
        if a.kind == "npc":
            return float(self.cfg["npc"].get("radius", 1.8)), float(self.cfg["npc"].get("height", 3.5))
        scale = 1.0 + float(self.cfg["cubes"].get("size_per_cube", 0.05)) * a.cubes
        return float(a.stats.get("radius", self.mechanics["radius"])) * scale, float(a.stats.get("height", self.mechanics["height"])) * scale

    def _visible(self, observer, pos, radius=0.0, height=None):
        vx, vy, vz = observer.pos
        eye_h = float(observer.stats.get("eye_height", self.mechanics["eye_height"])) if observer.kind == "mech" else 8.0
        if observer.kind == "mech":
            eye_h *= 1.0 + float(self.cfg["cubes"].get("size_per_cube", 0.05)) * observer.cubes
        eye = [vx, vy, vz + eye_h]
        d = norm2(pos[0] - vx, pos[1] - vy)
        vision = self.cfg["vision"]
        near = float(vision.get("near_range", 45))
        if observer.kind == "mech":
            near += float(observer.stats.get("near_range_bonus", 0.0))
        yaw = math.atan2(pos[1] - vy, pos[0] - vx)
        fov = math.radians(float(vision.get("fov_deg", 120))) * 0.5
        in_fov = abs(wrap(yaw - observer.aim[0])) <= fov
        long_range = float(vision.get("range", 200))
        if observer.kind == "mech":
            long_range += float(observer.stats.get("vision_bonus", 0.0))
        if d > near and (d > long_range or not in_fov):
            return False
        # Check center and top, matching the protocol's either-center-or-top rule.
        h = float(height) if height is not None else max(0.2, radius * 2.0)
        for z in (pos[2] + max(0.2, h * 0.5), pos[2] + max(0.2, h)):
            if self.geo.los(eye[0], eye[1], eye[2], pos[0], pos[1], z, step=1.5,
                            skip_end=max(1.0, radius)):
                return True
        return False

    def observation(self, actor_id):
        a = self.actors[actor_id]
        z, _ = self.zone_state()
        visible = {}
        for other in self.actors.values():
            if not other.alive or other.id == actor_id:
                continue
            rad, h = self._actor_size(other)
            if self._visible(a, other.pos, rad, h):
                visible[other.id] = other
                a.seen[other.id] = self.t
        mechs = []
        memory_s = float(self.cfg["vision"].get("memory_s", 5.0))
        for oid, seen_at in list(a.seen.items()):
            other = self.actors.get(oid)
            if other is None or not other.alive or self.t - seen_at > memory_s:
                a.seen.pop(oid, None)
                continue
            # Protocol memory tracks current real position for a short time after sight is lost.
            rad, h = self._actor_size(other)
            d = {"id": other.id, "kind": other.kind, "pos": list(other.pos),
                 "vel": list(other.vel), "aim": list(other.aim)}
            if other.kind == "npc" or self.cfg["vision"].get("show_enemy_hp", True):
                d["hp"] = max(0.0, other.hp)
                d["max_hp"] = other.max_hp
            if other.kind == "mech":
                d["cubes"] = other.cubes
                d["weapons"] = [w.kind for w in other.weapons if w]
                d["charging"] = any(w and w.kind == "laser" and w.charging for w in other.weapons)
            mechs.append(d)
        mechs.sort(key=lambda x: x["id"])
        projectiles = []
        for p in self.projectiles:
            if p.owner == actor_id:
                continue
            if self._visible(a, p.pos, 0.0):
                projectiles.append({"id": p.id, "kind": p.kind, "pos": list(p.pos), "vel": list(p.vel)})
        items = {"crates": [], "cubes": [], "pickups": []}
        for i, it in self.crates.items():
            if self._visible(a, it.pos, 1.2):
                items["crates"].append({"id": i, "pos": list(it.pos), "hp": max(0.0, it.hp)})
        for i, it in self.cubes.items():
            if self._visible(a, it.pos, 0.15):
                items["cubes"].append({"id": i, "pos": list(it.pos)})
        for i, it in self.pickups.items():
            if self._visible(a, it.pos, 0.15):
                items["pickups"].append({"id": i, "kind": it.kind, "pos": list(it.pos)})
        sound = self._heard_sounds(a)
        beams = []
        for b in self.beams:
            if b["by"] != actor_id and self.prev_obs_t - 1e-8 <= b["time"] <= self.t + 1e-8:
                if any(self._visible(a, p, 0.0) for p in b["path"]):
                    beams.append({"by": b["by"], "kind": b["kind"], "path": b["path"],
                                  "age": max(0.0, self.t - b["time"])})
        drops = []
        dc = self.cfg.get("drops", {})
        for d in self.drops:
            if self.t >= d.called:
                drops.append({"id": d.id, "pos": list(d.pos), "radius": float(dc.get("radius", 8)),
                              "lands_in": max(0.0, d.lands - self.t),
                              "capture_s": float(dc.get("capture_s", 4)),
                              "progress": d.progress, "holder": d.holder})
        obs = {"type": "obs", "tick": self.tick, "t": self.t,
               "alive": sum(1 for x in self.actors.values() if x.kind == "mech" and x.alive),
               "self": self._self_obs(a), "mechs": mechs,
               "projectiles": projectiles, "crates": items["crates"], "cubes": items["cubes"],
               "pickups": items["pickups"], "sounds": sound, "beams": beams,
               "events": list(a.events), "zone": z, "offer": copy.deepcopy(a.offer), "drops": drops}
        a.events.clear()
        if self.validate:
            errors = self.validate.errors(obs, "Observation")
            if errors:
                raise ValueError("invalid generated observation for actor %s tick %s: %s" %
                                 (actor_id, self.tick, "; ".join(errors)))
        return obs

    def mark_observed(self):
        """Advance the shared sound/beam observation cursor after a full batch."""
        self.prev_obs_t = self.t
        self.sound_log = [s for s in self.sound_log if s[0] >= self.t - 1e-8]
        self.beams = [b for b in self.beams if b["time"] >= self.t - 1e-8]

    def _self_obs(self, a):
        wobs = [self._weapon_observation(a, w) if w else None for w in a.weapons]
        stats = copy.deepcopy(a.stats)
        if a.kind == "mech":
            scale = 1.0 + float(self.cfg["cubes"].get("size_per_cube", 0.05)) * a.cubes
            for key in ("radius", "height", "eye_height", "muzzle_height"):
                if key in stats:
                    stats[key] *= scale
        return {"id": a.id, "pos": list(a.pos), "vel": list(a.vel), "aim": list(a.aim),
                "hp": max(0.0, a.hp), "max_hp": a.max_hp,
                "energy": a.energy, "max_energy": a.max_energy,
                "grounded": a.grounded, "cubes": a.cubes,
                "upgrades": [u for u, n in a.upgrades.items() for _ in range(n)],
                "in_zone": self._in_zone(a), "weapons": wobs, "stats": stats}

    def _heard_sounds(self, a):
        out = []
        cfg = self.cfg["sound"]
        for ts, kind, pos, source in self.sound_log:
            if ts < self.prev_obs_t - 1e-8 or ts > self.t + 1e-8 or source == a.id:
                continue
            d = norm2(pos[0] - a.pos[0], pos[1] - a.pos[1])
            limit = float(cfg.get("range", {}).get(kind, 0.0))
            hearing = float(a.stats.get("hearing", 1.0)) if a.kind == "mech" else 1.0
            if d > limit * hearing:
                continue
            near = float(cfg.get("near", 60.0))
            mid = float(cfg.get("mid", 160.0))
            category = "near" if d < near else "mid" if d < mid else "far"
            sigma = math.radians(float(cfg.get("bearing_noise_deg", 7.0))) * hearing
            bearing = math.atan2(pos[1] - a.pos[1], pos[0] - a.pos[0])
            bearing = wrap(bearing + self.rng.gauss(0.0, sigma))
            out.append({"kind": kind, "bearing": bearing, "dist": category,
                        "age": max(0.0, self.t - ts)})
        return out

    def _in_zone(self, a):
        z, _ = self.zone_state()
        return norm2(a.pos[0] - z["center"][0], a.pos[1] - z["center"][1]) <= z["radius"]

    def decide(self, subject_action=None, processes=None, schema=None, budget=0.05):
        """Get a lockstep action for each living mech; processes maps id to BotProcess."""
        actions = {}
        processes = processes or {}
        if self.ffa:
            active = {aid: proc for aid, proc in processes.items()}
            outgoing = {}
            for aid, a in self.actors.items():
                if a.kind == "mech" and a.alive and aid in active:
                    proc = active[aid]
                    if getattr(proc, "closed", False):
                        a._crashed = True
                    else:
                        outgoing[aid] = self.observation(aid)
            self.mark_observed()
            deadline = time.monotonic() + budget
            sent_ids = set()
            received = {}
            for aid, obs in outgoing.items():
                proc = active[aid]
                send_async = getattr(proc, "send_async", None)
                status = send_async(obs) if send_async else proc.send(obs)
                if status is True:
                    sent_ids.add(aid)
                elif status is None:
                    proc.missed += 1
                    received[aid] = None
                else:
                    self.actors[aid]._crashed = True
            pending = {aid for aid in sent_ids if not getattr(self.actors[aid], "_crashed", False)}
            while pending and time.monotonic() < deadline:
                progressed = False
                for aid in list(pending):
                    proc = active[aid]
                    done, action = proc.receive_available(self.tick)
                    if done:
                        received[aid] = action
                        pending.remove(aid)
                        progressed = True
                if not progressed:
                    time.sleep(min(0.0005, max(0.0, deadline - time.monotonic())))
            for aid in pending:
                active[aid].missed += 1
                received[aid] = None
            for aid, a in self.actors.items():
                if a.kind != "mech":
                    continue
                if a.alive:
                    proc = active.get(aid)
                    if proc:
                        if getattr(proc, "closed", False):
                            a._crashed = True
                        action = received.get(aid)
                    else:
                        if aid == 0:
                            action = None
                        else:
                            actions[aid] = self.house_action(a, None, ffa_mode=True)
                            continue
                    actions[aid] = self._accept_action(a, action)
        else:
            a = self.actors[0]
            if a.alive:
                if subject_action is None:
                    actions[0] = self._accept_action(a, None)
                else:
                    actions[0] = self._accept_action(a, subject_action)
            # House bots always receive the shared global target track and act in-process.
            for aid, h in self.actors.items():
                if h.alive and h.kind == "mech" and aid != 0:
                    actions[aid] = self.house_action(h, self.actors[0])
        for aid, a in self.actors.items():
            if a.kind == "npc" and a.alive:
                actions[aid] = self.npc_action(a)
        return actions

    def _accept_action(self, a, action):
        if getattr(a, "_crashed", False):
            return {"move": [0.0, 0.0], "aim": list(a.aim), "boost": False,
                    "fire": [False, False]}
        if action is not None:
            a._missed = 0.0
            clean = dict(action)
            # Keep only continuous values that the protocol can carry safely.
            move = clean.get("move", [0.0, 0.0])
            if isinstance(move, list) and len(move) >= 2 and all(isinstance(x, (int, float)) for x in move[:2]):
                x, y = float(move[0]), float(move[1])
                d = math.hypot(x, y)
                if d > 1.0:
                    x, y = x / d, y / d
                clean["move"] = [x, y]
            else:
                clean["move"] = [0.0, 0.0]
            aim = clean.get("aim", a.commanded_aim)
            if isinstance(aim, list) and len(aim) >= 2:
                clean["aim"] = [float(aim[0]), float(aim[1])]
            else:
                clean["aim"] = list(a.commanded_aim)
            clean["boost"] = bool(clean.get("boost", False))
            clean["fire"] = list(clean.get("fire", [False, False]))[:2]
            clean["fire"] += [False] * (2 - len(clean["fire"]))
            a.last_action = clean
            return clean
        missed = getattr(a, "_missed", 0.0) + 0.1
        a._missed = missed
        last = getattr(a, "last_action", {})
        if missed <= float(self.cfg["bot"].get("coast_s", 1.0)) and a.alive:
            return {"move": list(last.get("move", [0.0, 0.0])),
                    "aim": list(last.get("aim", a.commanded_aim)),
                    "boost": bool(last.get("boost", False)), "fire": [False, False]}
        return {"move": [0.0, 0.0], "aim": list(a.aim), "boost": False, "fire": [False, False]}

    def _house_hunters(self, subject):
        roster = [x for x in self.actors.values()
                  if x.kind == "mech" and x.id != 0 and x.alive]
        if not roster:
            return set()
        # One weak bot hunts at the start. Time and house-bot deaths each
        # increase pressure; the whole squad only commits in the late game.
        time_fraction = clamp(self.t / max(1.0, self.max_time), 0.0, 1.0)
        by_time = 1 + int(14 * min(1.0, time_fraction / 0.65))
        house_deaths = 15 - len(roster)
        by_deaths = 1 + 4 * house_deaths
        count = min(len(roster), max(1, by_time, by_deaths))
        # Keep the tier order, then send the nearest available bot in that tier.
        ordered = sorted(roster, key=lambda x: (x.house_tier,
                                                norm2(x.pos[0] - subject.pos[0],
                                                      x.pos[1] - subject.pos[1]),
                                                x.id))
        return {x.id for x in ordered[:count]}

    def _house_roam(self, a, zone):
        cx, cy = zone["center"]
        radius = max(20.0, float(zone["radius"]) * 0.58)
        angle = a.house_roam_angle + self.t * (11.7 / radius)
        tx, ty = cx + math.cos(angle) * radius, cy + math.sin(angle) * radius
        dx, dy = tx - a.pos[0], ty - a.pos[1]
        # Follow the circle smoothly instead of stopping at each waypoint.
        tangent = (-math.sin(angle), math.cos(angle))
        if math.hypot(dx, dy) < 18.0:
            dx += tangent[0] * 14.0
            dy += tangent[1] * 14.0
        return list(unit(dx, dy))

    def _house_roof_goal(self, a, tier, zone):
        if a.house_roof_goal is not None:
            gx, gy, gz = a.house_roof_goal
            dist = norm2(gx - a.pos[0], gy - a.pos[1])
            top = self.geo.surface(a.pos[0], a.pos[1])
            on_roof = (top - self.geo.ground(a.pos[0], a.pos[1]) > 2.0 and
                       abs(a.pos[2] - top) < 2.5)
            if dist < 14.0 and on_roof:
                if not a.house_roof_arrived:
                    a.house_roof_arrived = True
                    a.house_roof_goal_until = self.t + {1: 12.0, 2: 12.0, 3: 18.0, 4: 28.0}[tier]
                if self.t < a.house_roof_goal_until:
                    return a.house_roof_goal
                a.house_roof_goal = None
                a.house_roof_arrived = False
            elif self.t < a.house_roof_goal_until:
                return a.house_roof_goal
            else:
                a.house_roof_goal = None
                a.house_roof_arrived = False
        if self.t < a.house_roof_next or not self.roof_nodes:
            return None

        # Rooftop rates in the recordings ranged from about 8% to 28% of
        # frames. Seek one periodically, more often for the expert tier.
        period = {1: 150.0, 2: 140.0, 3: 100.0, 4: 35.0}[tier]
        a.house_roof_next = self.t + period
        cx, cy = zone["center"]
        zone_r = float(zone["radius"])
        best = None
        for _ in range(48):
            k = self.rng.choice(self.roof_nodes)
            i, j = k % self.geo.NX, k // self.geo.NX
            x, y = self.geo.X0 + i, self.geo.Y0 + j
            d = norm2(x - a.pos[0], y - a.pos[1])
            if d < 24.0 or d > 145.0 or norm2(x - cx, y - cy) > zone_r * 0.7:
                continue
            z = float(self.geo.hi[k])
            score = abs(d - 75.0) - max(0.0, z - a.pos[2]) * 0.08
            if best is None or score < best[0]:
                best = (score, (float(x), float(y), z))
        if best is None:
            return None
        a.house_roof_goal = best[1]
        a.house_roof_goal_until = self.t + 34.0
        a.house_roof_arrived = False
        return a.house_roof_goal

    def _house_path_move(self, a, target):
        goal = (float(target.pos[0]), float(target.pos[1]))
        changed = (a.house_path_target is None or
                   norm2(goal[0] - a.house_path_target[0],
                         goal[1] - a.house_path_target[1]) > 32.0)
        if self.t >= a.house_path_refresh or changed:
            a.house_path = self.geo.astar(a.pos[0], a.pos[1], goal[0], goal[1],
                                          max_nodes=1800)
            a.house_path_target = goal
            a.house_path_refresh = self.t + 2.0
        while a.house_path and norm2(a.house_path[0][0] - a.pos[0],
                                     a.house_path[0][1] - a.pos[1]) < 7.0:
            a.house_path.pop(0)
        if not a.house_path:
            return None
        return list(unit(a.house_path[0][0] - a.pos[0],
                         a.house_path[0][1] - a.pos[1]))

    def _house_weapon_goal(self, a, tier):
        ranks = {"gun": 0, "mortar": 1, "lightning": 2, "laser": 3}
        held = a.weapons[1].kind if a.weapons[1] else None
        held_rank = ranks.get(held, 0)
        horizon = {1: 42.0, 2: 58.0, 3: 76.0, 4: 92.0}[tier]
        best = None
        for iid, p in self.pickups.items():
            rank = ranks.get(p.kind, 0)
            d = norm2(p.pos[0] - a.pos[0], p.pos[1] - a.pos[1])
            if rank <= held_rank or d > horizon or abs(p.pos[2] - a.pos[2]) > 10:
                continue
            score = rank * 10.0 - d * (0.12 if tier == 1 else 0.08)
            if best is None or score > best[0]:
                best = (score, iid, p)
        return best[2] if best else None

    def _house_pick_upgrade(self, a):
        if not a.offer:
            return None
        priority = {
            "plating": 0, "gun_damage": 1, "gun_mag": 2, "armor": 3,
            "gun_rate": 4, "nanites": 5, "laser_damage": 6,
            "gun_velocity": 7, "laser_charge": 8, "lightning_efficiency": 9,
        }
        choices = a.offer.get("choices", [])
        if not choices:
            return None
        selected = min(range(len(choices)),
                       key=lambda i: (priority.get(choices[i].get("id"), 50), i))
        return selected

    def house_action(self, a, subject, ffa_mode=False):
        tier = a.house_tier
        if self.t + 1e-8 < a.house_next_act:
            return a.house_action
        reaction = {1: 0.5, 2: 0.25, 3: 0.15, 4: 0.1}[tier]
        a.house_next_act = self.t + reaction

        if ffa_mode:
            attacker = self.actors.get(a.last_damage_by) if a.last_damage_by is not None else None
            recently_attacked = (attacker is not None and attacker.alive and attacker.id != a.id and
                                 self.t - a.last_damage_at < 8.0)
            target = attacker if recently_attacked else None
        else:
            hunters = self._house_hunters(subject) if subject and subject.alive else set()
            recently_attacked = (subject is not None and subject.alive and
                                 a.last_damage_by == subject.id and self.t - a.last_damage_at < 8.0)
            squad_hunter = a.id in hunters
            target = subject if (squad_hunter or recently_attacked) else None
        if target is None:
            npc_ids = {x.id for x in self.actors.values() if x.kind == "npc" and x.alive}
            npc_attacker = self.actors.get(a.last_damage_by) if a.last_damage_by in npc_ids else None
            if npc_attacker and self.t - a.last_damage_at < 8.0:
                target = npc_attacker
            else:
                target = min((x for x in self.actors.values()
                              if x.kind == "npc" and x.alive and x.npc_target == a.id),
                             key=lambda x: norm2(x.pos[0] - a.pos[0], x.pos[1] - a.pos[1]),
                             default=None)
        active = target is not None
        a.house_target_id = target.id if target else None
        dx = target.pos[0] - a.pos[0] if target else 0.0
        dy = target.pos[1] - a.pos[1] if target else 0.0
        dist = math.hypot(dx, dy)

        desired = a.aim[:]
        visible = False
        if target:
            tr, th = self._actor_size(target)
            visible = dist < 280 and self._visible(a, target.pos, tr, th)
            lead = {1: 0.85, 2: 0.92, 3: 0.97, 4: 1.0}[tier]
            gun_speed = max(1.0, float(a.weapon_stats["gun"].get("speed", 252.0)))
            travel = min(1.8, dist / gun_speed) * lead
            aim_at = [target.pos[0] + target.vel[0] * travel,
                      target.pos[1] + target.vel[1] * travel,
                      target.pos[2] + 4.5 + target.vel[2] * travel]
            desired = list(yaw_pitch([a.pos[0], a.pos[1], a.pos[2] + 7], aim_at))
            # The two Astra probes averaged 18–20% gun hits, while the red
            # builds and expert were 45–49% on mechs in the live replays.
            err = {1: 5.5, 2: 4.5, 3: 0.7, 4: 0.5}[tier]
            desired[0] += self.rng.gauss(0, math.radians(err))
            desired[1] += self.rng.gauss(0, math.radians(err * 0.6))

        zone = self.zone_state(self.t + 8.0)[0]
        zx, zy = zone["center"]
        zone_dist = norm2(a.pos[0] - zx, a.pos[1] - zy)
        zone_push = zone_dist > max(10.0, float(zone["radius"]) * 0.72)
        move = [0.0, 0.0]
        roof_goal = None
        if active and target and target.kind == "mech" and tier >= 3 and (dist > 90.0 or a.house_roof_goal is not None):
            roof_goal = self._house_roof_goal(a, tier, zone)
        if active and target:
            if tier >= 3 and self.t >= a.house_strafe_until:
                a.house_strafe *= -1.0 if self.rng.random() < 0.65 else 1.0
                a.house_strafe_until = self.t + self.rng.uniform(0.45, 1.1)
            ux, uy = unit(dx, dy)
            lateral = {1: 0.0, 2: 0.2, 3: 0.72, 4: 0.95}[tier]
            if any(w and w.kind == "laser" for w in a.weapons):
                radius = 76.0
            elif any(w and w.kind == "lightning" for w in a.weapons):
                radius = 34.0
            else:
                radius = 57.0
            if dist > radius + 30.0:
                lateral = 0.0
            radial = 1.0 if dist > radius + 14 else -0.75 if dist < radius - 14 else 0.0
            if tier >= 3 and a.hp / max(a.max_hp, 1.0) < 0.28 and dist < 125:
                radial = -1.0
            move = list(unit(ux * radial - uy * lateral * a.house_strafe,
                             uy * radial + ux * lateral * a.house_strafe))
            if (target.kind == "mech" and dist > radius + 30.0 and not roof_goal and
                    not self.geo.seg_free(a.pos[0], a.pos[1], target.pos[0], target.pos[1])):
                path_move = self._house_path_move(a, target)
                if path_move:
                    move = path_move
            if roof_goal and target.kind == "mech" and (dist > 90.0 or a.house_roof_arrived):
                gx, gy, _ = roof_goal
                gd = norm2(gx - a.pos[0], gy - a.pos[1])
                if a.house_roof_arrived and gd > 1e-6:
                    gux, guy = unit(gx - a.pos[0], gy - a.pos[1])
                    move = list(unit(gux * (gd - 12.0) - guy * 12.0,
                                     guy * (gd - 12.0) + gux * 12.0))
                elif not a.house_roof_arrived:
                    move = list(unit(gx - a.pos[0], gy - a.pos[1]))
        else:
            # FFA-style roaming and looting. An FFA house bot only retaliates
            # against its own recent attacker; it does not join a squad target.
            weapon_goal = self._house_weapon_goal(a, tier)
            cube_goal = None
            cube_dist = 1e9
            for cube in self.cubes.values():
                d = norm2(cube.pos[0] - a.pos[0], cube.pos[1] - a.pos[1])
                if d < cube_dist and d <= 90:
                    cube_goal, cube_dist = cube, d
            if tier >= 1:
                roof_goal = self._house_roof_goal(a, tier, zone)
            if weapon_goal:
                move = list(unit(weapon_goal.pos[0] - a.pos[0],
                                 weapon_goal.pos[1] - a.pos[1]))
            elif cube_goal:
                move = list(unit(cube_goal.pos[0] - a.pos[0],
                                 cube_goal.pos[1] - a.pos[1]))
            elif roof_goal:
                gx, gy, _ = roof_goal
                d = norm2(gx - a.pos[0], gy - a.pos[1])
                if a.house_roof_arrived and d > 1e-6:
                    ux, uy = unit(gx - a.pos[0], gy - a.pos[1])
                    move = list(unit(ux * (d - 12.0) - uy * 12.0,
                                     uy * (d - 12.0) + ux * 12.0))
                else:
                    move = list(unit(gx - a.pos[0], gy - a.pos[1]))
            else:
                move = self._house_roam(a, zone)
        if zone_push:
            zu = unit(zx - a.pos[0], zy - a.pos[1])
            move = list(unit(zu[0] * 0.85 + move[0] * 0.15,
                             zu[1] * 0.85 + move[1] * 0.15))

        take, drop = self._house_take(a, tier)
        fire = [False, False]
        if active and target and visible:
            yaw_err = abs(wrap(desired[0] - a.aim[0]))
            pitch_err = abs(desired[1] - a.aim[1])
            tol = {1: 0.14, 2: 0.12, 3: 0.085, 4: 0.065}[tier]
            for slot, w in enumerate(a.weapons):
                if not w:
                    continue
                if w.kind == "gun" and dist < a.weapon_stats["gun"]["range"]:
                    fire[slot] = yaw_err < tol and pitch_err < tol * 1.5 and w.ammo > 0 and w.reload <= 0
                elif w.kind == "laser" and dist < a.weapon_stats["laser"]["range"]:
                    if w.charging:
                        fire[slot] = not (w.charge >= a.weapon_stats["laser"]["charge"] and yaw_err < tol * 1.4)
                    else:
                        fire[slot] = yaw_err < tol * 2 and w.cooldown <= 0
                elif w.kind == "lightning" and dist < a.weapon_stats["lightning"]["range"]:
                    fire[slot] = yaw_err < math.radians(a.weapon_stats["lightning"]["cone_deg"] * 0.45) and a.energy >= a.weapon_stats["lightning"]["energy"]
                elif w.kind == "mortar" and dist < 170:
                    fire[slot] = yaw_err < tol * 1.3 and w.cooldown <= 0

        boost_duty = {1: 0.10, 2: 0.12, 3: 0.15, 4: 0.18}[tier]
        boost_phase = (self.t + a.id * 1.73) % 6.0
        boost = bool(math.hypot(*move) > 0.2 and boost_phase < 6.0 * boost_duty and a.energy > 18)
        zone_escape = zone_dist > float(zone["radius"]) * 0.9
        if zone_escape and a.energy > 18:
            boost = True
        roof_climb = bool(roof_goal and not a.house_roof_arrived and
                          norm2(roof_goal[0] - a.pos[0], roof_goal[1] - a.pos[1]) < 28.0 and
                          roof_goal[2] > a.pos[2] + 2.0)
        if roof_climb and a.energy > 18:
            boost = True
        # A short periodic hop helps the upper tiers clear low walls and climb
        # onto roofs without turning every movement command into a jump.
        jump_period = {1: 16.0, 2: 13.0, 3: 10.0, 4: 7.0}[tier]
        jump = bool((roof_climb and a.grounded) or
                    (zone_escape and a.grounded and self.t % 2.0 < 0.1) or
                    (self.t % jump_period < 0.1 and math.hypot(*move) > 0.2))
        action = {"move": move, "aim": desired, "boost": boost,
                  "jump": jump, "fire": fire,
                  "reload": any(w and w.kind == "gun" and w.ammo <= 0 for w in a.weapons),
                  "take": take}
        pick = self._house_pick_upgrade(a)
        if pick is not None:
            action["pick"] = pick
        if drop is not None:
            action["drop"] = drop
        a.house_action = action
        return action

    def _house_take(self, a, tier):
        ranks = {"gun": 0, "mortar": 1, "lightning": 2, "laser": 3}
        current = a.weapons[1].kind if a.weapons[1] else None
        current_rank = ranks.get(current, 0)
        best = None
        for iid, p in self.pickups.items():
            d = norm2(p.pos[0] - a.pos[0], p.pos[1] - a.pos[1])
            if d > 3.8 or abs(p.pos[2] - a.pos[2]) > 4:
                continue
            rank = ranks.get(p.kind, 0)
            if rank <= current_rank:
                continue
            val = rank - d * 0.02
            if best is None or val > best[0]:
                best = (val, iid, rank)
        if best is None:
            return None, None
        # Slot zero is the starter gun and is never dropped. Upgrades replace
        # only slot one's mortar/lightning; laser has the highest preference.
        return best[1], (1 if a.weapons[1] else None)

    def npc_action(self, a):
        cfg = self.cfg["npc"]
        target = self.actors.get(a.npc_target) if a.npc_target is not None else None
        if self.t - a.last_damage_at < 8.0 and a.last_damage_by is not None:
            attacker = self.actors.get(a.last_damage_by)
            if attacker and attacker.alive and attacker.kind == "mech":
                target = attacker
                a.npc_target = attacker.id
        if not target or not target.alive or norm2(target.pos[0] - a.npc_home[0], target.pos[1] - a.npc_home[1]) > float(cfg.get("chase_leash", 60)):
            target = None
            best = 1e9
            for m in self.actors.values():
                if m.kind != "mech" or not m.alive:
                    continue
                d = norm2(m.pos[0] - a.npc_home[0], m.pos[1] - a.npc_home[1])
                if d < float(cfg.get("aggro_range", 45)) and d < best:
                    best, target = d, m
            if target:
                a.npc_target = target.id
        if target is None:
            # Light deterministic wandering, bounded by the leash.
            dx, dy = a.npc_home[0] - a.pos[0], a.npc_home[1] - a.pos[1]
            if math.hypot(dx, dy) > float(cfg.get("leash", 30)) * 0.7:
                move = list(unit(dx, dy))
            else:
                move = [math.cos(a.id * 1.13 + self.t * 0.03), math.sin(a.id * 1.13 + self.t * 0.03)]
            return {"move": move, "aim": list(a.aim), "fire": [False, False]}
        dx, dy = target.pos[0] - a.pos[0], target.pos[1] - a.pos[1]
        dist = math.hypot(dx, dy)
        aim = yaw_pitch([a.pos[0], a.pos[1], a.pos[2] + 2.8],
                        [target.pos[0] + target.vel[0] * 0.2, target.pos[1] + target.vel[1] * 0.2,
                         target.pos[2] + 2.0])
        return {"move": list(unit(dx, dy)) if dist > 18 else [0.0, 0.0],
                "aim": list(aim), "fire": [dist < 200 and self.t % 1.0 < 0.1, False]}

    def _apply_action(self, a, action):
        if not a.alive:
            return
        a.commanded_aim = list(action.get("aim", a.commanded_aim))
        move = action.get("move", [0.0, 0.0])
        a._move = move[:2]
        a._boost = bool(action.get("boost", False))
        a._jump = bool(action.get("jump", False))
        a._fire = action.get("fire", [False, False])
        a._reload = bool(action.get("reload", False))
        a._take = action.get("take")
        a._drop = action.get("drop")
        a._pick = action.get("pick")
        if a._take is None and a._drop is not None:
            self._drop_weapon(a, int(a._drop))
            a._drop = None
        if a._pick is not None and a.offer:
            self._choose_upgrade(a, int(a._pick))
            a._pick = None
        for i, w in enumerate(a.weapons):
            if w and w.kind == "gun" and a._reload and w.reload <= 0 and w.ammo < a.weapon_stats["gun"].get("mag", 12):
                w.reload = float(a.weapon_stats["gun"].get("reload", 2.4))
                self._sound("reload", a.pos, a.id)

    def _take_weapon(self, a, iid, drop_slot):
        p = self.pickups.get(iid)
        if not p:
            self._event(a.id, {"type": "take_failed", "id": iid, "reason": "gone"})
            return
        loot = self.cfg["loot"]
        if norm2(p.pos[0] - a.pos[0], p.pos[1] - a.pos[1]) > float(loot.get("pickup_radius", 4)) or abs(p.pos[2] - a.pos[2]) > 4:
            self._event(a.id, {"type": "take_failed", "id": iid, "reason": "range"})
            return
        if any(w and w.kind == p.kind for w in a.weapons):
            self._event(a.id, {"type": "take_failed", "id": iid, "reason": "have"})
            return
        empty = next((i for i, w in enumerate(a.weapons) if w is None), None)
        slot = empty if empty is not None else int(drop_slot) if drop_slot in (0, 1) else None
        if slot is None:
            self._event(a.id, {"type": "take_failed", "id": iid, "reason": "full"})
            return
        old = a.weapons[slot]
        if old and old.kind == "laser" and old.charging:
            self._event(a.id, {"type": "take_failed", "id": iid, "reason": "busy"})
            return
        if old:
            self._drop_weapon(a, slot)
        self.pickups.pop(iid, None)
        w = p.weapon if p.weapon is not None else self._new_weapon(p.kind)
        p.weapon = None
        w.equip = max(w.equip, float(loot.get("equip_s", 1.0)),
                      float(a.weapon_stats[p.kind].get("cooldown", 0.0)))
        a.weapons[slot] = w
        self._event(a.id, {"type": "took", "id": iid, "kind": p.kind, "slot": slot})

    def _resolve_takes(self):
        requests = {}
        for a in self.actors.values():
            iid = getattr(a, "_take", None)
            if a.kind == "mech" and a.alive and iid is not None:
                requests.setdefault(int(iid), []).append(a)
            a._take = None
        radius = float(self.cfg["loot"].get("pickup_radius", 4.0))
        for iid, actors in requests.items():
            p = self.pickups.get(iid)
            if p is None:
                for a in actors:
                    self._event(a.id, {"type": "take_failed", "id": iid, "reason": "gone"})
                continue
            in_range = [a for a in actors if norm2(a.pos[0]-p.pos[0], a.pos[1]-p.pos[1]) <= radius
                        and abs(a.pos[2]-p.pos[2]) <= 4]
            for a in actors:
                if a not in in_range:
                    self._event(a.id, {"type": "take_failed", "id": iid, "reason": "range"})
            if not in_range:
                continue
            winner = min(in_range, key=lambda a: (norm2(a.pos[0]-p.pos[0], a.pos[1]-p.pos[1]), a.id))
            self._take_weapon(winner, iid, winner._drop)
            for a in in_range:
                if a is not winner:
                    self._event(a.id, {"type": "take_failed", "id": iid, "reason": "gone"})
            for a in in_range:
                a._drop = None

    def _drop_weapon(self, a, slot):
        if slot not in (0, 1) or not a.weapons[slot]:
            return
        w = a.weapons[slot]
        if w.kind == "laser" and w.charging:
            return
        iid = self._new_pickup(w.kind, a.pos, weapon=w)
        a.weapons[slot] = None
        self._event(a.id, {"type": "dropped", "id": iid, "kind": w.kind, "slot": slot})

    def _choose_upgrade(self, a, idx):
        if not a.offer:
            return
        choices = a.offer["choices"]
        if idx < 0 or idx >= len(choices):
            return
        uid = choices[idx]["id"]
        a.upgrades[uid] = a.upgrades.get(uid, 0) + 1
        oldmax = a.max_hp
        self._rebuild_stats(a)
        a.hp = min(a.max_hp, a.hp + max(0.0, a.max_hp - oldmax))
        a.events.append({"type": "upgrade", "id": uid})
        a.offer = None
        self._draw_offer(a)

    def _draw_offer(self, a):
        if a.offer or a.offer_queue <= 0:
            return
        a.offer_queue -= 1
        defs = self.cfg.get("upgrades", [])
        held = {w.kind for w in a.weapons if w}
        avail = [u for u in defs if a.upgrades.get(u["id"], 0) < int(u.get("max", 1)) and
                 (not u.get("requires") or u["requires"] in held)]
        if not avail:
            return
        choices = self.rng.sample(avail, min(3, len(avail)))
        a.offer = {"id": int(self.rng.randrange(1, 2**31)),
                   "choices": [{"id": u["id"], "name": u["name"], "desc": u["desc"]} for u in choices],
                   "expires": self.t + float(self.cfg["bot"].get("offer_timeout_s", 5.0))}

    def _cube(self, a, amount=1):
        oldmax = a.max_hp
        a.cubes += amount
        self._rebuild_stats(a)
        a.hp = min(a.max_hp, a.hp + max(0.0, a.max_hp - oldmax))
        a.events.append({"type": "pickup", "cubes": a.cubes})
        thresholds = self.cfg["cubes"].get("upgrade_thresholds", [])
        while a.threshold_index < len(thresholds) and a.cubes >= float(thresholds[a.threshold_index]):
            a.threshold_index += 1
            a.offer_queue += 1
        self._draw_offer(a)

    def _try_cubes(self):
        r = float(self.cfg["cubes"].get("pickup_radius", 3.5))
        for iid, cube in list(self.cubes.items()):
            candidates = []
            for a in self.actors.values():
                if a.kind != "mech" or not a.alive:
                    continue
                d = norm2(a.pos[0] - cube.pos[0], a.pos[1] - cube.pos[1])
                if d <= r and abs(a.pos[2] - cube.pos[2]) <= 4:
                    candidates.append((d, a.id, a))
            if candidates:
                _, _, a = min(candidates, key=lambda v: (v[0], v[1]))
                self._cube(a)
                self.cubes.pop(iid, None)

    def _damage(self, target, amount, weapon, attacker_id=None, source_pos=None, self_mult=1.0, effect=None):
        attacker = self.actors.get(attacker_id) if attacker_id is not None else None
        raw = float(amount)
        if attacker and attacker.kind == "mech":
            raw *= 1.0 + float(self.cfg["cubes"].get("damage_per_cube", 0.04)) * min(
                attacker.cubes, float(self.cfg["cubes"].get("stat_cap", 10)))
        if isinstance(target, Item):
            target.hp -= raw
            if attacker and attacker.kind == "mech":
                attacker.last_dealt = self.t
                attacker.events.append({"type": "hit", "target": target.id,
                                        "amount": max(0.0, raw), "weapon": weapon})
            return max(0.0, raw), False
        if target is None or not target.alive:
            return 0.0, False
        rear = False
        bearing = None
        if source_pos is not None:
            bearing = math.atan2(source_pos[1] - target.pos[1], source_pos[0] - target.pos[0])
            if target.kind == "mech":
                rear = abs(wrap(bearing - target.aim[0])) > math.radians(
                    float(target.stats.get("front_arc_deg", 180.0)) * 0.5)
                if rear:
                    raw *= float(target.stats.get("rear_mult", 1.5))
        if target.kind == "mech" and weapon != "zone":
            raw *= max(0.0, 1.0 - float(target.stats.get("armor", 0.0)))
        raw *= self_mult
        target.hp -= raw
        target.last_hurt = self.t
        target.last_damage_by = attacker_id
        target.last_damage_at = self.t
        if attacker:
            attacker.last_dealt = self.t
        if target.kind == "mech":
            target.events.append({"type": "damaged", "amount": raw, "weapon": weapon,
                                  "bearing": bearing, "by": attacker_id, "rear": rear})
            if effect:
                target.events.append({"type": "effect", "effect": effect, "by": attacker_id})
        if attacker and attacker.kind == "mech":
            ev = {"type": "hit", "target": target.id, "amount": raw, "weapon": weapon, "rear": rear}
            if effect:
                ev["effect"] = effect
            attacker.events.append(ev)
            if target.kind == "mech" and weapon == "gun":
                attacker.gun_mech_hits += 1
                if attacker.house_tier and attacker.house_target_id == target.id:
                    attacker.gun_hunt_hits += 1
        if target.hp <= 0:
            self._kill(target, weapon, attacker_id)
        return raw, rear

    def _kill(self, a, cause, by):
        if not a.alive:
            return
        a.alive = False
        a.hp = 0.0
        a.death_time = self.t
        a.death_cause = cause if by is None else "%s by %s" % (cause, by)
        if a.kind == "mech":
            attacker = self.actors.get(by) if by is not None else None
            if attacker and attacker.kind == "mech" and attacker.id != a.id:
                attacker.kills += 1
                attacker.events.append({"type": "kill", "target": a.id})
            cube_count = int(a.cubes + float(self.cfg["cubes"].get("death_drop_extra", 2)))
            for _ in range(cube_count):
                p = [a.pos[0] + self.rng.uniform(-2, 2), a.pos[1] + self.rng.uniform(-2, 2), a.pos[2]]
                self.cubes[self.next_cube_id] = Item(self.next_cube_id, "cube", p)
                self.next_cube_id += 1
            for w in a.weapons:
                if w and w.kind != "gun" and self.rng.random() <= float(self.cfg["loot"].get("death_drop", 1.0)):
                    self._new_pickup(w.kind, a.pos, weapon=w)
        else:
            for _ in range(int(self.cfg["npc"].get("cubes", 2))):
                p = [a.pos[0] + self.rng.uniform(-1, 1), a.pos[1] + self.rng.uniform(-1, 1), a.pos[2]]
                self.cubes[self.next_cube_id] = Item(self.next_cube_id, "cube", p)
                self.next_cube_id += 1
        self._sound("death", a.pos, a.id)

    def _ray_blocked(self, a, b, max_range=None):
        dx, dy, dz = b[0]-a[0], b[1]-a[1], b[2]-a[2]
        d = math.sqrt(dx*dx+dy*dy+dz*dz)
        if max_range is not None and d > max_range:
            f = max_range / max(d, 1e-9)
            b = [a[0]+dx*f, a[1]+dy*f, a[2]+dz*f]
            d = max_range
        n = max(1, int(d / 0.5))
        for i in range(1, n+1):
            f = i / n
            x, y, z = a[0]+(b[0]-a[0])*f, a[1]+(b[1]-a[1])*f, a[2]+(b[2]-a[2])*f
            if self.geo.solid_at(x, y, z):
                return True, [x, y, z]
        return False, b

    def _muzzle(self, a, weapon):
        yaw, pitch = a.aim
        dx, dy, dz = direction(yaw, pitch)
        height = float(a.stats.get("muzzle_height", self.mechanics["muzzle_height"]))
        if a.kind == "mech":
            height *= 1.0 + float(self.cfg["cubes"].get("size_per_cube", 0.05)) * a.cubes
        if weapon == "mortar":
            height += 2.5 * (1.0 + float(self.cfg["cubes"].get("size_per_cube", 0.05)) * a.cubes)
        origin = [a.pos[0], a.pos[1], a.pos[2] + height]
        return [origin[0] + dx * 3, origin[1] + dy * 3, origin[2] + dz * 3]

    def _fire_gun(self, a, slot, w):
        s = a.weapon_stats["gun"]
        if w.cooldown > 0 or w.equip > 0 or w.reload > 0 or w.ammo <= 0:
            return
        yaw, pitch = a.aim
        spread = math.radians(float(s.get("spread_deg", 0.6)))
        yaw += self.rng.gauss(0, spread)
        pitch += self.rng.gauss(0, spread)
        origin = self._muzzle(a, "gun")
        d = direction(yaw, pitch)
        blocked, end = self._ray_blocked(origin, [origin[i] + d[i] * float(s.get("range", 230)) for i in range(3)])
        muzzle_end = [origin[i] + d[i] * min(3.0, norm2(end[0]-origin[0], end[1]-origin[1])) for i in range(3)]
        if self.geo.solid_at(*muzzle_end):
            w.cooldown = float(s["cooldown"])
            return
        if a.kind == "mech":
            a.gun_shots += 1
            if a.house_tier and a.house_target_id == 0:
                a.gun_hunt_shots += 1
        self.next_projectile_id += 1
        self.projectiles.append(Projectile(self.next_projectile_id, "gun", a.id, origin,
             [d[0]*float(s["speed"]), d[1]*float(s["speed"]), d[2]*float(s["speed"])],
             float(s.get("range",230))/max(1.0,float(s["speed"])), float(s["damage"]),
             float(self.cfg["projectile"].get("gun_radius",0.3))))
        w.cooldown = float(s["cooldown"])
        w.ammo -= 1
        self._sound("gun", a.pos, a.id)

    def _fire_mortar(self, a, w):
        s = a.weapon_stats["mortar"]
        if w.cooldown > 0 or w.equip > 0:
            return
        origin = self._muzzle(a, "mortar")
        d = direction(*a.aim)
        self.next_projectile_id += 1
        self.projectiles.append(Projectile(self.next_projectile_id, "mortar", a.id, origin,
             [d[0]*float(s["speed"]), d[1]*float(s["speed"]), d[2]*float(s["speed"])],
             float(s.get("ttl",7)), float(s["damage"])))
        w.cooldown = float(s["cooldown"])
        self._sound("mortar", a.pos, a.id)

    def _fire_laser(self, a, w):
        s = a.weapon_stats["laser"]
        charge_s = max(1e-3, float(s.get("charge", 0.75)))
        frac = float(s.get("min_frac", 0.35)) + (1.0 - float(s.get("min_frac", 0.35))) * min(1.0, w.charge / charge_s)
        damage = float(s.get("damage", 315)) * frac
        origin = self._muzzle(a, "laser")
        d = direction(*a.aim)
        end = [origin[i] + d[i] * float(s.get("range", 280)) for i in range(3)]
        blocked, end = self._ray_blocked(origin, end)
        candidates = []
        for target in self.actors.values():
            if not target.alive or target.id == a.id:
                continue
            r, h = self._actor_size(target)
            along = (target.pos[0]-origin[0])*d[0] + (target.pos[1]-origin[1])*d[1] + (target.pos[2]+h*0.5-origin[2])*d[2]
            if along < 0 or along > float(s.get("range",280)):
                continue
            cx, cy = origin[0]+d[0]*along, origin[1]+d[1]*along
            if norm2(target.pos[0]-cx, target.pos[1]-cy) <= r + 0.7 and target.pos[2]-1 <= origin[2]+d[2]*along <= target.pos[2]+h+1:
                candidates.append((along, target))
        for it in self.crates.values():
            along = (it.pos[0]-origin[0])*d[0] + (it.pos[1]-origin[1])*d[1] + (it.pos[2]+1.2-origin[2])*d[2]
            if 0 <= along <= float(s.get("range",280)):
                cx, cy = origin[0]+d[0]*along, origin[1]+d[1]*along
                if norm2(it.pos[0]-cx, it.pos[1]-cy) < 1.6:
                    candidates.append((along, it))
        candidates.sort(key=lambda x:x[0])
        if blocked:
            maxd = norm2(end[0]-origin[0], end[1]-origin[1])
            candidates = [x for x in candidates if x[0] <= maxd]
        pierce = int(s.get("pierce",0))
        hit_path = [origin]
        for _, target in candidates[:1 + max(0,pierce)]:
            effect = None
            dmg = damage
            if isinstance(target, Actor) and target.kind == "mech" and any(x and x.kind == "mortar" for x in target.weapons):
                dmg *= 1.0 + float(s.get("cookoff",0.6))
                effect = "cookoff"
            self._damage(target, dmg, "laser", a.id, a.pos, effect=effect)
            hit_path.append(list(target.pos))
        if len(hit_path) == 1:
            hit_path.append(end)
        self.beams.append({"by": a.id, "kind":"laser", "path":hit_path, "time":self.t})
        self._sound("laser", a.pos, a.id)
        w.cooldown = float(s.get("cooldown",1.65))
        w.charge = 0.0
        w.charging = False
        w.release_pending = False

    def _fire_lightning(self, a, w):
        s = a.weapon_stats["lightning"]
        if w.cooldown > 0 or w.equip > 0 or a.energy < float(s.get("energy",3)):
            return
        a.energy -= float(s.get("energy",3))
        a.last_energy_spend = self.t
        yaw, pitch = a.aim
        origin = self._muzzle(a, "lightning")
        rnge = float(s.get("range",45))
        cone = math.radians(float(s.get("cone_deg",30)))
        targets = []
        for target in self.actors.values():
            if not target.alive or target.id == a.id:
                continue
            dx, dy = target.pos[0]-a.pos[0], target.pos[1]-a.pos[1]
            dist = math.hypot(dx,dy)
            if dist > rnge or abs(wrap(math.atan2(dy,dx)-yaw)) > cone/2:
                continue
            r,h = self._actor_size(target)
            dest = [target.pos[0],target.pos[1],target.pos[2]+h*0.55]
            if self.geo.los(*origin,*dest,step=1.0,skip_end=r):
                targets.append((dist,target))
        for it in self.crates.values():
            dx,dy=it.pos[0]-a.pos[0],it.pos[1]-a.pos[1]
            dist=math.hypot(dx,dy)
            if dist<=rnge and abs(wrap(math.atan2(dy,dx)-yaw))<=cone/2 and self.geo.los(*origin,it.pos[0],it.pos[1],it.pos[2]+1.2):
                targets.append((dist,it))
        targets.sort(key=lambda x:x[0])
        path=[origin]
        if targets:
            _,target=targets[0]
            amount=float(s["damage"])
            for jump in range(1+int(s.get("chain",2))):
                self._damage(target,amount,"lightning",a.id,a.pos,effect="short" if isinstance(target,Actor) and target.kind=="mech" and any(q and q.kind=="laser" for q in target.weapons) else None)
                path.append(list(target.pos))
                if isinstance(target,Actor) and target.kind=="mech":
                    for q in target.weapons:
                        if q and q.kind=="laser":
                            q.charging=False; q.charge=0; q.shorted=float(s.get("short_s",2))
                old=target
                opts=[]
                for dist2,x in targets[1:]:
                    if x is old or x in [y for _,y in targets[:jump+1]]:
                        continue
                    if norm2(x.pos[0]-old.pos[0],x.pos[1]-old.pos[1])<=float(s.get("chain_range",14)):
                        opts.append((norm2(x.pos[0]-old.pos[0],x.pos[1]-old.pos[1]),x))
                if not opts: break
                _,target=min(opts,key=lambda x:x[0])
                amount*=float(s.get("chain_falloff",0.7))
        else:
            dx,dy,dz=direction(yaw,pitch)
            _,end=self._ray_blocked(origin,[origin[0]+dx*rnge,origin[1]+dy*rnge,origin[2]+dz*rnge])
            path.append(end)
        self.beams.append({"by":a.id,"kind":"lightning","path":path,"time":self.t})
        self._sound("lightning",a.pos,a.id)
        w.cooldown=float(s.get("cooldown",0.25))

    def _explode(self, p, pos, direct=None):
        s=self.cfg["weapons"]["mortar"]
        rad=float(s.get("splash_radius",7))
        for target in list(self.actors.values())+list(self.crates.values()):
            if (isinstance(target,Actor) and not target.alive) or target is self.actors.get(p.owner):
                pass
            if isinstance(target,Actor) and (not target.alive or target.kind not in ("mech","npc")):
                continue
            d=norm2(target.pos[0]-pos[0],target.pos[1]-pos[1])
            tr= self._actor_size(target)[0] if isinstance(target,Actor) else 1.4
            if d>rad+tr: continue
            dest=[target.pos[0],target.pos[1],target.pos[2]+(self._actor_size(target)[1]*0.5 if isinstance(target,Actor) else 1.2)]
            if not self.geo.los(pos[0],pos[1],pos[2],*dest,step=1.0,skip_end=tr): continue
            if target is direct:
                amount=float(s.get("damage",200))
            else:
                f=max(float(s.get("splash_edge",0.3)),1.0-d/max(rad,1e-6))
                amount=float(s.get("splash",160))*f
            source=p.owner
            selfmult=float(self.mechanics.get("self_damage_mult",0.5)) if target is self.actors.get(p.owner) else 1.0
            eff=None
            raw,_=self._damage(target,amount,"mortar",source,pos,selfmult,effect="concuss" if isinstance(target,Actor) and target.kind=="mech" else None)
            if isinstance(target,Actor) and target.alive and d < rad+tr:
                dx,dy=unit(target.pos[0]-pos[0],target.pos[1]-pos[1])
                scale=max(float(s.get("splash_edge",0.3)),1.0-d/max(rad,1e-6))
                target.vel[0]+=dx*float(s.get("knock",16))*scale
                target.vel[1]+=dy*float(s.get("knock",16))*scale
                target.vel[2]+=float(s.get("knock_up",9))*scale
                if target.kind=="mech":
                    target.energy=max(0.0,target.energy-float(s.get("drain",45))*scale)
                    target.last_energy_spend=self.t
        self._sound("explosion",pos,p.owner)

    def _resolve_projectiles(self):
        for p in list(self.projectiles):
            if p.kind == "mortar":
                p.vel[2] -= float(self.cfg["weapons"]["mortar"].get("gravity",22))*DT
            start=list(p.pos)
            end=[p.pos[i]+p.vel[i]*DT for i in range(3)]
            steps=max(1,int(math.sqrt(sum((end[i]-start[i])**2 for i in range(3)))/0.75))
            hit=None; hitpos=None; direct=None
            for k in range(1,steps+1):
                f=k/steps; q=[start[i]+(end[i]-start[i])*f for i in range(3)]
                if self.geo.solid_at(*q):
                    hit=True; hitpos=q; break
                for target in self.actors.values():
                    if not target.alive or target.id==p.owner: continue
                    r,h=self._actor_size(target)
                    if norm2(q[0]-target.pos[0],q[1]-target.pos[1])<=r+p.radius and target.pos[2]-0.4<=q[2]<=target.pos[2]+h:
                        hit=True;hitpos=q;direct=target;break
                if hit: break
                for crate in self.crates.values():
                    if norm2(q[0]-crate.pos[0], q[1]-crate.pos[1]) <= 1.4+p.radius and crate.pos[2]-0.3 <= q[2] <= crate.pos[2]+2.4:
                        hit=True;hitpos=q;direct=crate;break
                if hit: break
            p.travelled += math.sqrt(sum((end[i]-start[i])**2 for i in range(3)))
            p.ttl-=DT
            if hit:
                if p.kind=="mortar":
                    self._explode(p,hitpos,direct)
                else:
                    if direct:
                        self._damage(direct,p.damage,"npc" if p.kind=="npc" else "gun",p.owner,start)
                self.projectiles.remove(p)
            else:
                p.pos=end
                if p.ttl<=0 or p.travelled>300:
                    if p.kind=="mortar": self._explode(p,p.pos)
                    self.projectiles.remove(p)

    def _weapon_tick(self,a,dt):
        if not a.alive:return
        for i,w in enumerate(a.weapons):
            if not w:continue
            w.cooldown=max(0,w.cooldown-dt); w.equip=max(0,w.equip-dt);w.reload=max(0,w.reload-dt);w.shorted=max(0,w.shorted-dt)
            if w.kind=="gun" and w.reload<=0 and w.ammo<=0 and w.cooldown<=0:
                w.reload=float(a.weapon_stats["gun"].get("reload",2.4))
            if w.kind=="gun" and w.reload>0:
                continue
            fire=i<len(a._fire) and bool(a._fire[i])
            if w.kind=="gun" and fire:
                self._fire_gun(a,i,w)
            elif w.kind=="mortar" and fire:
                self._fire_mortar(a,w)
            elif w.kind=="lightning" and fire:
                self._fire_lightning(a,w)
            elif w.kind=="laser":
                s=a.weapon_stats["laser"]
                if w.shorted>0:
                    continue
                if fire and w.cooldown<=0 and w.equip<=0:
                    if not w.charging:
                        w.charging=True;w.charge=0.0;w.release_pending=False
                        self._sound("laser_charge",a.pos,a.id)
                    w.charge+=dt
                    if w.charge>=float(s.get("charge",0.75))+float(s.get("hold_s",2.0)):
                        self._fire_laser(a,w)
                elif w.charging:
                    if w.charge<float(s.get("min_charge",0.15)):
                        w.release_pending=True
                        w.charge+=dt
                        if w.charge>=float(s.get("min_charge",0.15)):
                            self._fire_laser(a,w)
                    else:
                        self._fire_laser(a,w)
        # successful gun reload completion
        for w in a.weapons:
            if w and w.kind=="gun" and w.reload<=0 and w.ammo<=0:
                w.ammo=float(a.weapon_stats["gun"].get("mag",12))

    def _cylinder_overlap(self, x, y, feet_z, radius, height):
        """Count map cells intersecting the actor's horizontal disk and vertical span.

        Geo's interval raster stores one-metre square cells. Test the circle against each
        intersecting cell square, not a handful of points inside a reduced radius. This
        matters for cube-grown mechs and for low bridges that intersect only the upper body.
        """
        g = self.geo
        bx0, bx1, by0, by1 = g.bounds
        overlap = int(x - radius < bx0 or x + radius > bx1 or
                      y - radius < by0 or y + radius > by1)
        i0 = max(0, int(math.floor(x - radius - g.X0 - 0.5)))
        i1 = min(g.NX - 1, int(math.ceil(x + radius - g.X0 + 0.5)))
        j0 = max(0, int(math.floor(y - radius - g.Y0 - 0.5)))
        j1 = min(g.NY - 1, int(math.ceil(y + radius - g.Y0 + 0.5)))
        # O(1) early out: no solid cell at all in the bounding box (the common open-ground case)
        if i1 >= i0 and j1 >= j0:
            ps = _solid_prefix(g)
            W = g.NX + 1
            if (ps[(j1 + 1) * W + i1 + 1] - ps[j0 * W + i1 + 1]
                    - ps[(j1 + 1) * W + i0] + ps[j0 * W + i0]) == 0:
                return overlap
        low, high = feet_z + 0.05, feet_z + height - 0.05
        lo, hi = g.lo, g.hi
        multi = g.multi
        for j in range(j0, j1 + 1):
            cy = g.Y0 + j
            dy = max(0.0, abs(cy - y) - 0.5)
            for i in range(i0, i1 + 1):
                cx = g.X0 + i
                dx = max(0.0, abs(cx - x) - 0.5)
                if dx * dx + dy * dy > radius * radius:
                    continue
                k = j * g.NX + i
                if lo[k] < hi[k] and lo[k] < high and hi[k] > low:
                    overlap += 1
                    continue
                for a, b in multi.get(k, ()):
                    if a < high and b > low:
                        overlap += 1
                        break
        # Out-of-map cells also block the cylinder. Count a single penalty above; the
        # simulator's movement bounds keep this from overwhelming local wall scores.
        return overlap

    def _push_out_of_solids(self, x, y, feet_z, radius, height, preferred=(0.0, 0.0),
                            support_z=None, max_push=0.6):
        """Move an already-overlapping mech at most 0.6 m toward the clearest side."""
        if self._cylinder_overlap(x, y, feet_z, radius, height) == 0:
            return x, y
        pd = math.hypot(preferred[0], preferred[1])
        if pd > 1e-9:
            pref = (preferred[0] / pd, preferred[1] / pd)
        else:
            pref = (0.0, 0.0)
        best_dir = (0.0, 0.0)
        best_key = None
        for k in range(32):
            a = TAU * k / 32.0
            ux, uy = math.cos(a), math.sin(a)
            # Look ahead while moving only 0.6 m this tick. The lookahead resolves ties
            # on grid-aligned walls without teleporting through them.
            score = 0
            support_bad = 0
            for d in (max_push, max_push * 2, max_push * 3):
                qx, qy = x + ux * d, y + uy * d
                score += self._cylinder_overlap(qx, qy, feet_z, radius, height)
                if support_z is not None:
                    support_bad += self._cylinder_overlap(qx, qy, support_z, radius, height)
            key = (support_bad, score, -ux * pref[0] - uy * pref[1])
            if best_key is None or key < best_key:
                best_key, best_dir = key, (ux, uy)
        return x + best_dir[0] * max_push, y + best_dir[1] * max_push

    def _open_normal(self, x, y, feet_z, radius, height):
        """Approximate the free-side normal at a wall contact for tangent sliding."""
        sx = sy = 0.0
        probe = 0.25
        for k in range(24):
            a = TAU * k / 24.0
            ux, uy = math.cos(a), math.sin(a)
            if self._cylinder_overlap(x + ux * probe, y + uy * probe,
                                      feet_z, radius, height) == 0:
                sx += ux
                sy += uy
        d = math.hypot(sx, sy)
        return (sx / d, sy / d) if d > 1e-6 else (0.0, 0.0)

    def _physics_actor(self,a,dt):
        if not a.alive:return
        stats=a.stats if a.kind=="mech" else self.cfg["mech"]
        if a.kind=="mech":
            ya,pa=a.commanded_aim
            yaw_rate=float(stats.get("turn_rate",4)); pitch_rate=float(stats.get("pitch_rate",3))
            if any(w and w.kind=="laser" and w.charging for w in a.weapons):
                turn_mult=float(stats.get("charge_turn_mult",1.0))
                yaw_rate*=turn_mult; pitch_rate*=turn_mult
            a.aim[0]=wrap(a.aim[0]+clamp(wrap(ya-a.aim[0]),-yaw_rate*dt,yaw_rate*dt))
            a.aim[1]=clamp(a.aim[1]+clamp(pa-a.aim[1],-pitch_rate*dt,pitch_rate*dt),float(stats.get("min_pitch",-.7)),float(stats.get("max_pitch",1.3)))
        mv=getattr(a,"_move",[0,0]); m=math.hypot(mv[0],mv[1]);
        if m>1: mv=[mv[0]/m,mv[1]/m];m=1
        if a.kind=="mech":
            charging=any(w and w.kind=="laser" and w.charging for w in a.weapons)
            grounded=a.grounded
            speed=float(stats.get("boost_speed" if grounded and getattr(a,"_boost",False) else "walk_speed",11.7))
            if charging:speed*=float(stats.get("charge_move_mult",0.5))
            desired=[mv[0]*speed,mv[1]*speed]
            if getattr(a,"_jump",False) and grounded:
                a.vel[2]=float(stats.get("jump_speed",11));a.grounded=False
            jetting=False
            if getattr(a,"_boost",False):
                if grounded and m>0:
                    cost=float(stats.get("boost_drain",17))*dt
                    if a.energy>=cost:
                        a.energy-=cost;a.last_energy_spend=self.t
                    else: desired=[mv[0]*float(stats.get("walk_speed",11.7)),mv[1]*float(stats.get("walk_speed",11.7))]
                elif not grounded:
                    cost=float(stats.get("jet_drain",15))*dt
                    if a.energy>=cost:
                        a.energy-=cost;a.last_energy_spend=self.t
                        jetting=True
            accel=float(stats.get("ground_accel",58.5) if grounded else stats.get("air_accel",12))
        else:
            desired=[mv[0]*float(self.cfg["npc"].get("speed",5)),mv[1]*float(self.cfg["npc"].get("speed",5))]
            accel=18
        dx=desired[0]-a.vel[0];dy=desired[1]-a.vel[1];d=math.hypot(dx,dy)
        if d>accel*dt and d>1e-8:dx*=accel*dt/d;dy*=accel*dt/d
        a.vel[0]+=dx;a.vel[1]+=dy
        oldx,oldy,oldz=a.pos
        rad, actor_height = self._actor_size(a)
        step=float(stats.get("step_up",1.2))
        ground_move = bool(a.grounded)

        def surface_ok(x, y):
            z = self._walk_surface(x, y, oldz) if ground_move else oldz
            if z is None:
                return None
            move_d = math.hypot(x - oldx, y - oldy)
            if ground_move:
                if z - oldz > step + 0.12 and oldz <= z - step:
                    return None
                if (move_d > 0.1 and z > oldz and
                        (z - oldz) / move_d > math.tan(math.radians(float(stats.get("max_slope_deg", 50))))):
                    return None
            if self._cylinder_overlap(x, y, z, rad, actor_height):
                return None
            return z

        # Cubes can grow a mech while it is already beside a wall or under an eave.
        # Nudge an overlapping cylinder out by at most 0.6 m per tick before trying its
        # requested displacement; do not let jump/jet acceleration drive it deeper in.
        start_feet = self._walk_surface(oldx, oldy, oldz) if ground_move else oldz
        start_overlap = self._cylinder_overlap(oldx, oldy, start_feet, rad, actor_height)
        nx, ny = oldx, oldy
        embedded = start_overlap > 0
        if embedded:
            nx, ny = self._push_out_of_solids(oldx, oldy, start_feet, rad, actor_height,
                                               preferred=(a.vel[0], a.vel[1]),
                                               support_z=oldz if start_feet != oldz else None)
            if ground_move:
                support = self._walk_surface(nx, ny, oldz)
                if support is not None:
                    oldz = support
                    a.pos[2] = support
            embedded = self._cylinder_overlap(nx, ny, oldz, rad, actor_height) > 0

        # Sweep the full cylinder in short increments. At first contact, binary-search
        # the last safe point, remove only the component into the wall, and retry the
        # tangent component. This works for diagonal walls and large radii where either
        # world-axis candidate alone can still point into the obstruction.
        remaining = [0.0, 0.0] if start_overlap else [a.vel[0] * dt, a.vel[1] * dt]
        for _ in range(4):
            distance = math.hypot(remaining[0], remaining[1])
            if distance < 0.01 or embedded:
                break
            count = max(1, int(math.ceil(distance / 0.22)))
            hit_wall = False
            for _ in range(count):
                sx, sy = remaining[0] / count, remaining[1] / count
                tx, ty = nx + sx, ny + sy
                if surface_ok(tx, ty) is not None:
                    nx, ny = tx, ty
                    remaining[0] -= sx
                    remaining[1] -= sy
                    continue
                lo, hi = 0.0, 1.0
                for _ in range(10):
                    mid = (lo + hi) * 0.5
                    if surface_ok(nx + sx * mid, ny + sy * mid) is None:
                        hi = mid
                    else:
                        lo = mid
                nx += sx * lo
                ny += sy * lo
                remaining[0] -= sx * lo
                remaining[1] -= sy * lo
                contact_z = self._walk_surface(nx, ny, oldz) if ground_move else oldz
                normal = self._open_normal(nx, ny, contact_z if contact_z is not None else oldz,
                                           rad, actor_height)
                into = remaining[0] * normal[0] + remaining[1] * normal[1]
                if into < 0.0:
                    remaining[0] -= into * normal[0]
                    remaining[1] -= into * normal[1]
                else:
                    remaining = [0.0, 0.0]
                hit_wall = True
                break
            if not hit_wall:
                break

        nz=self._walk_surface(nx,ny,oldz)
        a.pos[0]=nx;a.pos[1]=ny
        if a.grounded and nz is not None and nz>=oldz-step-0.2:
            a.pos[2]=nz;a.vel[2]=max(0.0,a.vel[2]);a.grounded=True
        else:
            a.vel[2]-=float(stats.get("gravity",22))*dt
            if a.kind=="mech" and jetting:
                a.vel[2]=min(float(stats.get("max_rise_speed",10)),
                             a.vel[2]+float(stats.get("jet_accel",34))*dt)
            znext=oldz+a.vel[2]*dt
            if embedded:
                znext=oldz;a.vel[2]=0.0
            elif znext>oldz:
                # A ceiling can be above the feet but below the head. Sweep the whole
                # vertical span; if a jet meets an eave, slide outward by <=0.6 m/tick.
                levels=max(1,int(math.ceil((znext-oldz)/0.2)))
                blocked_at=None
                for k in range(1,levels+1):
                    qz=oldz+(znext-oldz)*k/levels
                    if self._cylinder_overlap(nx,ny,qz,rad,actor_height):
                        blocked_at=qz
                        break
                if blocked_at is not None:
                    ex,ey=nx,ny
                    if jetting:
                        ex,ey=self._push_out_of_solids(nx,ny,blocked_at,rad,actor_height,
                                                        preferred=(a.vel[0],a.vel[1]),
                                                        support_z=oldz)
                    support_clear=self._cylinder_overlap(ex,ey,oldz,rad,actor_height)==0
                    vertical_clear=support_clear
                    if vertical_clear:
                        for k in range(1,levels+1):
                            qz=oldz+(znext-oldz)*k/levels
                            if self._cylinder_overlap(ex,ey,qz,rad,actor_height):
                                vertical_clear=False
                                break
                    if support_clear and (ex != nx or ey != ny):
                        nx,ny=ex,ey
                        a.pos[0],a.pos[1]=nx,ny
                    if not vertical_clear:
                        znext=oldz;a.vel[2]=0.0
            if nz is not None and a.vel[2]<=0 and znext<=nz:
                a.pos[2]=nz;a.vel[2]=0.0;a.grounded=True
            else:
                a.pos[2]=znext;a.grounded=False
        # Keep actors apart horizontally.
        for b in self.actors.values():
            if b.id==a.id or not b.alive:continue
            br,_=self._actor_size(b)
            ddx=a.pos[0]-b.pos[0];ddy=a.pos[1]-b.pos[1];dd=math.hypot(ddx,ddy);need=rad+br
            if dd<need and dd>1e-6:
                push=(need-dd)*0.5;ux,uy=ddx/dd,ddy/dd
                a.pos[0]+=ux*push;a.pos[1]+=uy*push;b.pos[0]-=ux*push;b.pos[1]-=uy*push
        if a.kind=="mech" and not a._boost and self.t-a.last_energy_spend>=float(stats.get("energy_regen_delay",0.6)):
            a.energy=min(a.max_energy,a.energy+float(stats.get("energy_regen",18))*dt)
        if a.kind == "mech" and a.house_tier:
            a.house_ticks += 1
            if norm2(a.vel[0], a.vel[1]) > 1.0:
                a.house_moving_ticks += 1
            if getattr(a, "_boost", False):
                a.house_boost_ticks += 1
            top = self.geo.surface(a.pos[0], a.pos[1])
            if top - self.geo.ground(a.pos[0], a.pos[1]) > 2.0 and abs(a.pos[2] - top) < 2.5:
                a.house_roof_ticks += 1

    def _walk_surface(self, x, y, feet_z):
        """Pick terrain or a reachable roof; a bridge does not support a mech below it."""
        g = self.geo
        i = int(x - g.X0 + 0.5); j = int(y - g.Y0 + 0.5)
        if i < 0 or j < 0 or i >= g.NX or j >= g.NY:
            return None
        k = j * g.NX + i
        ground = g.ground(x, y)
        top, base = float(g.hi[k]), float(g.lo[k])
        if top > base and top > ground + 1.0 and feet_z >= base - 0.25:
            return top
        return ground

    def _pickup_weapons_and_drops(self):
        for d in self.drops:
            if self.t >= d.lands and not d.announced:
                d.announced = True
                self._sound("drop", d.pos, d.id)
            if self.t<d.lands:continue
            inside=[]
            r=float(self.cfg.get("drops",{}).get("radius",8))
            for a in self.actors.values():
                if a.kind=="mech" and a.alive and norm2(a.pos[0]-d.pos[0],a.pos[1]-d.pos[1])<=r:inside.append(a)
            if len(inside)==1:
                holder=inside[0]
                if d.holder==holder.id:d.progress+=DT
                else:d.holder=holder.id;d.progress=0.0
                if d.progress>=float(self.cfg.get("drops",{}).get("capture_s",4)):
                    d.progress=0.0;d.holder=None
                    event_start=len(holder.events)
                    for _ in range(int(self.cfg.get("drops",{}).get("cubes",3))):self._cube(holder)
                    holder.events[event_start:]=[e for e in holder.events[event_start:] if e.get("type")!="pickup"]
                    holder.hp=holder.max_hp;holder.energy=holder.max_energy
                    holder.events.append({"type":"drop","id":d.id,"cubes":holder.cubes})
                    d.called=1e99
            else:
                d.holder=None;d.progress=0.0

    def _try_crate_drops(self, crate):
        c=self.cfg["crates"]
        for _ in range(int(c.get("cubes",1))):
            self.cubes[self.next_cube_id]=Item(self.next_cube_id,"cube",list(crate.pos));self.next_cube_id+=1
        if self.rng.random()<float(self.cfg["loot"].get("crate_weapon_chance",0.5)):
            ks=[k for k in ("mortar","laser","lightning") if float(self.cfg["loot"].get("weights",{}).get(k,0))>0]
            if ks:self._new_pickup(self.rng.choice(ks),crate.pos)

    def _weapon_fire(self,a):
        if not a.alive:return
        for i,w in enumerate(a.weapons):
            if not w or i>=len(a._fire) or not a._fire[i]:continue
            if w.kind=="gun":self._fire_gun(a,i,w)
            elif w.kind=="mortar":self._fire_mortar(a,w)
            elif w.kind=="laser":
                if w.charging:continue
                if w.cooldown<=0 and w.equip<=0 and w.shorted<=0:
                    w.charging=True;w.charge=0.0;self._sound("laser_charge",a.pos,a.id)
            elif w.kind=="lightning":self._fire_lightning(a,w)

    def _zone_damage(self):
        z,_=self.zone_state()
        for a in self.actors.values():
            if a.kind!="mech" or not a.alive:continue
            if norm2(a.pos[0]-z["center"][0],a.pos[1]-z["center"][1])>z["radius"]:
                self._damage(a,float(z["dps"])*DT,"zone",None,None)

    def _health_regen(self):
        for a in self.actors.values():
            if a.kind!="mech" or not a.alive:continue
            if self._in_zone(a) and self.t-max(a.last_hurt,a.last_dealt)>=float(a.stats.get("regen_delay",5)):
                a.hp=min(a.max_hp,a.hp+float(a.stats.get("regen_per_s",45))*DT)
            if a.offer and self.t>=a.offer["expires"]:
                self._choose_upgrade(a,0)

    def _npc_fire(self):
        for a in self.actors.values():
            if a.kind!="npc" or not a.alive:continue
            target=self.actors.get(a.npc_target) if a.npc_target is not None else None
            if not target or not target.alive:continue
            if self.t-getattr(a,"_npc_fire_t",-1e9)<float(self.cfg["npc"].get("cooldown",1)):continue
            dist=norm2(target.pos[0]-a.pos[0],target.pos[1]-a.pos[1])
            if dist>float(self.cfg["npc"].get("aggro_range",45))+20:continue
            if not self.geo.los(a.pos[0],a.pos[1],a.pos[2]+2.8,target.pos[0],target.pos[1],target.pos[2]+4.5):continue
            a._npc_fire_t=self.t
            yaw,pitch=yaw_pitch([a.pos[0],a.pos[1],a.pos[2]+2.8],
                [target.pos[0]+target.vel[0]*.2,target.pos[1]+target.vel[1]*.2,target.pos[2]+4.5])
            spread=math.radians(float(self.cfg["npc"].get("spread_deg",2.5)))
            yaw+=self.rng.gauss(0,spread);pitch+=self.rng.gauss(0,spread)
            d=direction(yaw,pitch)
            self.next_projectile_id+=1
            self.projectiles.append(Projectile(self.next_projectile_id,"npc",a.id,
                [a.pos[0],a.pos[1],a.pos[2]+2.8],
                [d[0]*float(self.cfg["npc"].get("projectile_speed",90)),d[1]*float(self.cfg["npc"].get("projectile_speed",90)),d[2]*float(self.cfg["npc"].get("projectile_speed",90))],
                2.4,float(self.cfg["npc"].get("damage",25)),float(self.cfg["projectile"].get("npc_radius",.3))))
            self._sound("npc_gun",a.pos,a.id)

    def _step(self, actions):
        self.t += DT
        self.tick += 1
        for a in self.actors.values():
            if a.alive:self._physics_actor(a,DT)
        for a in self.actors.values():
            if a.alive:self._weapon_tick(a,DT)
        self._npc_fire()
        self._resolve_projectiles()
        self._pickup_weapons_and_drops()
        self._try_cubes()
        # Crates that fell to zero are removed after all same-tick shots.
        for iid,c in list(self.crates.items()):
            if c.hp<=0:
                self._try_crate_drops(c);self.crates.pop(iid,None)
        self._zone_damage()
        self._health_regen()
        self._step_sounds()
        self.beams=[b for b in self.beams if self.t-b["time"]<=0.15]

    def _step_sounds(self):
        cfg=self.cfg["sound"]
        for a in self.actors.values():
            if a.kind!="mech" or not a.alive or not a.grounded:continue
            speed=norm2(a.vel[0],a.vel[1])
            if speed<=float(cfg.get("step_speed",5)) or self.t-a.last_step<float(cfg.get("step_every",.6)):continue
            a.last_step=self.t
            kind="boost" if getattr(a,"_boost",False) else "step"
            self._sound(kind,a.pos,a.id)

    def step_decision(self, actions):
        # Accept discrete inputs once per observation; continuous inputs persist
        # for both 20 Hz physics ticks in this 10 Hz decision interval.
        for aid, a in self.actors.items():
            if a.alive and aid in actions:
                self._apply_action(a, actions[aid])
        self._resolve_takes()
        for _ in range(DECISION_TICKS):
            self._step(actions)
            if sum(1 for x in self.actors.values() if x.kind=="mech" and x.alive)<=1:
                break

    def result(self, actor_id=0):
        mechs=[a for a in self.actors.values() if a.kind=="mech"]
        deaths=sorted((a for a in mechs if not a.alive),key=lambda a:(a.death_time if a.death_time is not None else self.t,a.id))
        alive=sorted((a for a in mechs if a.alive),key=lambda a:(-a.hp,a.id))
        place={}; remaining=len(mechs)
        i=0
        while i<len(deaths):
            tm=deaths[i].death_time
            tied=[]
            while i<len(deaths) and abs((deaths[i].death_time or 0)-(tm or 0))<1e-7:
                tied.append(deaths[i]);i+=1
            for a in tied:place[a.id]=remaining
            remaining-=len(tied)
        for j,a in enumerate(alive):place[a.id]=j+1
        target=self.actors[actor_id]
        house_gun = {}
        house_motion = {}
        for tier in range(1, 5):
            group = [a for a in mechs if a.id != 0 and a.house_tier == tier]
            house_gun[tier] = {"shots": sum(a.gun_shots for a in group),
                               "hits": sum(a.gun_mech_hits for a in group),
                               "hunt_shots": sum(a.gun_hunt_shots for a in group),
                               "hunt_hits": sum(a.gun_hunt_hits for a in group)}
            ticks = sum(a.house_ticks for a in group)
            house_motion[tier] = {
                "moving": sum(a.house_moving_ticks for a in group),
                "boosting": sum(a.house_boost_ticks for a in group),
                "roof": sum(a.house_roof_ticks for a in group),
                "ticks": ticks,
            }
        return {"place":place.get(actor_id,16),"kills":target.kills,
                "death_time":target.death_time,"cause":target.death_cause,
                "placements":place,"alive":len(alive),"time":self.t,
                "house_gun":house_gun,"house_motion":house_motion,
                "actors":[{"id":a.id,"place":place.get(a.id),"kills":a.kills,
                           "death_time":a.death_time,"cause":a.death_cause,"hp":a.hp} for a in mechs]}

    def run_until_end(self, action_provider, max_decisions=None):
        decisions=0
        while self.t < self.max_time and sum(1 for a in self.actors.values() if a.kind=="mech" and a.alive)>1:
            actions=action_provider(self)
            self.step_decision(actions)
            decisions+=1
            if max_decisions and decisions>=max_decisions:break
        if self.t>=self.max_time:
            for a in self.actors.values():
                if a.kind=="mech" and a.alive:
                    a.death_time=self.t;a.death_cause="clock"
        return self.result()

