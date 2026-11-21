"""Static map model: terrain, solid prisms, line of sight, a walkability grid and A* paths.

Everything is derived from v2/world.json + v2/terrain.i16 (protocol.md section 11). Building the
model takes a few seconds in pure Python, so the Docker build precomputes it into cache.pkl; at
runtime the cache is used only if its source hash matches the mounted /arena files.
"""
from __future__ import annotations

import hashlib
import heapq
import math
import os
import pickle
import sys
from array import array

CACHE_VERSION = 3
NAV = 2            # nav node spacing in metres (every 2nd terrain vertex)
CLEAR_R = 3.0      # clearance from walls for nav nodes (mech radius 2.5 + margin)
HEAD = 9.5         # a solid whose base is lower than ground + HEAD blocks a mech
STEP = 1.2         # step-up height
TALL = 8.0         # solid this tall above ground counts as cover
COVER_R = 6.0      # a cover node has a tall wall within this distance
NOSOLID_LO = 1.0e4
NOSOLID_HI = -1.0e4


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


def source_hash(map_dir: str) -> str:
    h = hashlib.sha256()
    for name in ("v2/world.json", "v2/terrain.i16"):
        with open(os.path.join(map_dir, name), "rb") as f:
            h.update(f.read())
    return h.hexdigest()


class Geo:
    # ------------------------------------------------------------------ build
    @classmethod
    def build(cls, map_dir: str) -> "Geo":
        import json

        g = cls()
        with open(os.path.join(map_dir, "v2/world.json")) as f:
            w = json.load(f)
        t = w["terrain"]
        g.X0, g.Y0, g.NX, g.NY = float(t["minX"]), float(t["minY"]), int(t["nx"]), int(t["ny"])
        step = float(t.get("step", 1.0))
        if abs(step - 1.0) > 1e-9:
            raise ValueError("terrain step %r unsupported" % step)
        scale = float(t.get("scale", 0.01))
        b = w["bounds"]
        g.bounds = (float(b["minX"]), float(b["maxX"]), float(b["minY"]), float(b["maxY"]))
        raw = array("h")
        with open(os.path.join(map_dir, "v2/terrain.i16"), "rb") as f:
            raw.frombytes(f.read())
        if sys.byteorder != "little":
            raw.byteswap()
        NX, NY = g.NX, g.NY
        if len(raw) != NX * NY:
            raise ValueError("terrain size mismatch")
        terr = array("f", [v * scale for v in raw])
        lo = array("f", [NOSOLID_LO]) * (NX * NY)
        hi = array("f", [NOSOLID_HI]) * (NX * NY)
        multi = {}  # idx -> list of extra (lo, hi) intervals disjoint from the main one

        X0, Y0 = g.X0, g.Y0
        for p in w["prisms"]:
            base, top = float(p["base"]), float(p["top"])
            if top <= base:
                continue
            rings = [p["ring"]] + list(p.get("holes") or [])
            edges = []
            ymin, ymax = 1e18, -1e18
            for r in rings:
                n = len(r) // 2
                for k in range(n):
                    x1, y1 = r[2 * k], r[2 * k + 1]
                    x2, y2 = r[2 * ((k + 1) % n)], r[2 * ((k + 1) % n) + 1]
                    if y1 != y2:
                        edges.append((x1, y1, x2, y2))
                    ymin = min(ymin, y1)
                    ymax = max(ymax, y1)
            j0 = max(0, int(math.ceil(ymin - Y0)))
            j1 = min(NY - 1, int(math.floor(ymax - Y0)))
            for j in range(j0, j1 + 1):
                y = Y0 + j
                xs = []
                for (x1, y1, x2, y2) in edges:
                    if (y1 <= y < y2) or (y2 <= y < y1):
                        xs.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
                if len(xs) < 2:
                    continue
                xs.sort()
                row = j * NX
                for q in range(0, len(xs) - 1, 2):
                    i0 = max(0, int(math.ceil(xs[q] - X0)))
                    i1 = min(NX - 1, int(math.floor(xs[q + 1] - X0)))
                    for i in range(i0, i1 + 1):
                        idx = row + i
                        clo, chi = lo[idx], hi[idx]
                        if chi < clo:  # empty
                            lo[idx], hi[idx] = base, top
                        elif base <= chi + 0.3 and top >= clo - 0.3:  # overlapping: merge
                            lo[idx] = min(clo, base)
                            hi[idx] = max(chi, top)
                        else:
                            lst = multi.setdefault(idx, [])
                            lst.append((base, top))
        # fold multi intervals that now overlap the main one
        for idx, lst in list(multi.items()):
            keep = []
            for (a, b2) in lst:
                if a <= hi[idx] + 0.3 and b2 >= lo[idx] - 0.3:
                    lo[idx] = min(lo[idx], a)
                    hi[idx] = max(hi[idx], b2)
                else:
                    keep.append((a, b2))
            if keep:
                multi[idx] = keep
            else:
                del multi[idx]

        # ---- blocked cells (1 m): solids a mech walks into, steep terrain, outside bounds
        blocked = bytearray(NX * NY)
        tall = bytearray(NX * NY)
        bx0, bx1, by0, by1 = g.bounds
        tanmax = math.tan(math.radians(50.0)) * 0.98
        for j in range(NY):
            y = Y0 + j
            row = j * NX
            yout = y < by0 + 3.0 or y > by1 - 3.0
            for i in range(NX):
                idx = row + i
                gr = terr[idx]
                bl = False
                if yout:
                    bl = True
                else:
                    x = X0 + i
                    if x < bx0 + 3.0 or x > bx1 - 3.0:
                        bl = True
                if not bl and hi[idx] > lo[idx]:
                    if lo[idx] < gr + HEAD and hi[idx] > gr + STEP:
                        bl = True
                        if hi[idx] > gr + TALL:
                            tall[idx] = 1
                if not bl and idx in multi:
                    for (a, b2) in multi[idx]:
                        if a < gr + HEAD and b2 > gr + STEP:
                            bl = True
                if not bl:
                    if i + 1 < NX and abs(terr[idx + 1] - gr) > tanmax:
                        bl = True
                    elif j + 1 < NY and abs(terr[idx + NX] - gr) > tanmax:
                        bl = True
                    elif i > 0 and abs(terr[idx - 1] - gr) > tanmax:
                        bl = True
                    elif j > 0 and abs(terr[idx - NX] - gr) > tanmax:
                        bl = True
                if bl:
                    blocked[idx] = 1

        # ---- nav nodes every NAV m: free if no blocked cell within CLEAR_R
        NNX = (NX + NAV - 1) // NAV
        NNY = (NY + NAV - 1) // NAV
        nav = bytearray(b"\x01") * (NNX * NNY)
        cover = bytearray(NNX * NNY)
        R = CLEAR_R
        offs = []
        rr = int(math.ceil(R))
        for dj in range(-rr, rr + 1):
            for di in range(-rr, rr + 1):
                if di * di + dj * dj <= R * R:
                    offs.append((di, dj))
        rc = int(math.ceil(COVER_R))
        coffs = []
        for dj in range(-rc, rc + 1):
            for di in range(-rc, rc + 1):
                if di * di + dj * dj <= COVER_R * COVER_R:
                    coffs.append((di, dj))
        for j in range(NY):
            row = j * NX
            for i in range(NX):
                idx = row + i
                if not blocked[idx]:
                    continue
                if (j % NAV == 0) and (i % NAV == 0):
                    nav[(j // NAV) * NNX + i // NAV] = 0
                # boundary cell?
                if not ((i > 0 and not blocked[idx - 1]) or (i + 1 < NX and not blocked[idx + 1]) or
                        (j > 0 and not blocked[idx - NX]) or (j + 1 < NY and not blocked[idx + NX])):
                    continue
                for (di, dj) in offs:
                    ii, jj = i + di, j + dj
                    if ii % NAV or jj % NAV or ii < 0 or jj < 0 or ii >= NX or jj >= NY:
                        continue
                    nav[(jj // NAV) * NNX + ii // NAV] = 0
                if tall[idx]:
                    for (di, dj) in coffs:
                        ii, jj = i + di, j + dj
                        if ii % NAV or jj % NAV or ii < 0 or jj < 0 or ii >= NX or jj >= NY:
                            continue
                        cover[(jj // NAV) * NNX + ii // NAV] = 1
        cover_nodes = [k for k in range(NNX * NNY) if cover[k] and nav[k]]

        g.terr, g.lo, g.hi, g.multi = terr, lo, hi, multi
        g.NNX, g.NNY, g.nav = NNX, NNY, nav
        g._index_cover(cover_nodes)
        g.cover_nodes = cover_nodes
        g._finish()
        return g

    def _index_cover(self, cover_nodes):
        B = 24.0
        self.cover_bucket = B
        bk = {}
        NNX = self.NNX
        for k in cover_nodes:
            I, J = k % NNX, k // NNX
            x, y = self.X0 + I * NAV, self.Y0 + J * NAV
            key = (int((x - self.X0) // B), int((y - self.Y0) // B))
            bk.setdefault(key, []).append(k)
        self.cover_bk = bk

    def _finish(self):
        self.multiset = frozenset(self.multi.keys())

    # ------------------------------------------------------------------ cache
    STATE = ("X0", "Y0", "NX", "NY", "bounds", "terr", "lo", "hi", "multi", "NNX", "NNY", "nav",
             "cover_nodes")

    def save(self, path: str, src_hash: str) -> None:
        d = {k: getattr(self, k) for k in self.STATE}
        d["_version"] = CACHE_VERSION
        d["_hash"] = src_hash
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(d, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)

    @classmethod
    def from_cache(cls, path: str, src_hash: str):
        with open(path, "rb") as f:
            d = pickle.load(f)
        if d.get("_version") != CACHE_VERSION or d.get("_hash") != src_hash:
            return None
        g = cls()
        for k in cls.STATE:
            setattr(g, k, d[k])
        g._index_cover(g.cover_nodes)
        g._finish()
        return g

    @classmethod
    def load(cls, map_dir: str, cache_path: str) -> "Geo":
        h = source_hash(map_dir)
        if cache_path and os.path.exists(cache_path):
            try:
                g = cls.from_cache(cache_path, h)
                if g is not None:
                    return g
                _log("geo cache stale; rebuilding")
            except Exception as e:  # pragma: no cover
                _log("geo cache unreadable:", e)
        return cls.build(map_dir)

    # ------------------------------------------------------------------ queries
    def ground(self, x: float, y: float) -> float:
        """Terrain height (bilinear)."""
        fx = x - self.X0
        fy = y - self.Y0
        i = int(fx)
        j = int(fy)
        if i < 0:
            i, fx = 0, 0.0
        if j < 0:
            j, fy = 0, 0.0
        if i >= self.NX - 1:
            i, fx = self.NX - 2, float(self.NX - 1)
        if j >= self.NY - 1:
            j, fy = self.NY - 2, float(self.NY - 1)
        u, v = fx - i, fy - j
        u = min(max(u, 0.0), 1.0)
        v = min(max(v, 0.0), 1.0)
        T = self.terr
        k = j * self.NX + i
        a = T[k] * (1 - u) + T[k + 1] * u
        b = T[k + self.NX] * (1 - u) + T[k + self.NX + 1] * u
        return a * (1 - v) + b * v

    def surface(self, x: float, y: float) -> float:
        """Height of the highest walkable top under a point (terrain or roof)."""
        i = int(x - self.X0 + 0.5)
        j = int(y - self.Y0 + 0.5)
        if i < 0 or j < 0 or i >= self.NX or j >= self.NY:
            return 0.0
        k = j * self.NX + i
        s = self.terr[k]
        if self.hi[k] > self.lo[k]:
            s = max(s, self.hi[k])
        return s

    def solid_at(self, x: float, y: float, z: float) -> bool:
        i = int(x - self.X0 + 0.5)
        j = int(y - self.Y0 + 0.5)
        if i < 0 or j < 0 or i >= self.NX or j >= self.NY:
            return True
        k = j * self.NX + i
        if z < self.terr[k] - 0.3:
            return True
        if self.lo[k] <= z <= self.hi[k]:
            return True
        if k in self.multiset:
            for (a, b) in self.multi[k]:
                if a <= z <= b:
                    return True
        return False

    def los(self, ax, ay, az, bx, by, bz, step=1.0, skip_end=0.0):
        """True when the straight segment a->b is not blocked by terrain or solids (trees ignored).
        skip_end metres at the far end are not tested (the target's own footprint)."""
        dx, dy, dz = bx - ax, by - ay, bz - az
        d = math.sqrt(dx * dx + dy * dy)
        n = int(d / step)
        if n < 2:
            return True
        X0, Y0, NX, NY = self.X0, self.Y0, self.NX, self.NY
        terr, lo, hi, ms = self.terr, self.lo, self.hi, self.multiset
        inv = 1.0 / n
        last = n - int(skip_end / step) if skip_end > 0 else n
        for k in range(1, last):
            f = k * inv
            x = ax + dx * f
            y = ay + dy * f
            z = az + dz * f
            i = int(x - X0 + 0.5)
            j = int(y - Y0 + 0.5)
            if i < 0 or j < 0 or i >= NX or j >= NY:
                return False
            idx = j * NX + i
            if z < terr[idx] - 0.3:
                return False
            if lo[idx] <= z <= hi[idx]:
                return False
            if idx in ms:
                for (a, b) in self.multi[idx]:
                    if a <= z <= b:
                        return False
        return True

    def first_block(self, ax, ay, az, yaw, maxd, step=1.0):
        """Distance along a horizontal ray to the first solid, or None."""
        c, s = math.cos(yaw), math.sin(yaw)
        d = step
        while d <= maxd:
            if self.solid_at(ax + c * d, ay + s * d, az):
                return d
            d += step
        return None

    # nav ---------------------------------------------------------------
    def node_of(self, x, y):
        I = int(round((x - self.X0) / NAV))
        J = int(round((y - self.Y0) / NAV))
        return I, J

    def node_xy(self, k):
        return (self.X0 + (k % self.NNX) * NAV, self.Y0 + (k // self.NNX) * NAV)

    def free(self, x, y) -> bool:
        I, J = self.node_of(x, y)
        if I < 0 or J < 0 or I >= self.NNX or J >= self.NNY:
            return False
        return bool(self.nav[J * self.NNX + I])

    def nearest_free(self, x, y, maxr=12):
        I, J = self.node_of(x, y)
        NNX, NNY, nav = self.NNX, self.NNY, self.nav
        if 0 <= I < NNX and 0 <= J < NNY and nav[J * NNX + I]:
            return J * NNX + I
        best, bd = None, 1e18
        for r in range(1, maxr + 1):
            for dj in range(-r, r + 1):
                for di in (-r, r) if abs(dj) != r else range(-r, r + 1):
                    ii, jj = I + di, J + dj
                    if 0 <= ii < NNX and 0 <= jj < NNY and nav[jj * NNX + ii]:
                        nx_, ny_ = self.X0 + ii * NAV, self.Y0 + jj * NAV
                        dd = (nx_ - x) ** 2 + (ny_ - y) ** 2
                        if dd < bd:
                            bd, best = dd, jj * NNX + ii
            if best is not None:
                return best
        return None

    def seg_free(self, ax, ay, bx, by, skip_start=1.5):
        dx, dy = bx - ax, by - ay
        d = math.hypot(dx, dy)
        if d < 1e-6:
            return True
        n = int(d)
        X0, Y0, NNX, NNY, nav = self.X0, self.Y0, self.NNX, self.NNY, self.nav
        for k in range(0, n + 1):
            t = k / d
            if k < skip_start:
                continue
            x = ax + dx * t
            y = ay + dy * t
            I = int((x - X0) / NAV + 0.5)
            J = int((y - Y0) / NAV + 0.5)
            if I < 0 or J < 0 or I >= NNX or J >= NNY or not nav[J * NNX + I]:
                return False
        return True

    def astar(self, sx, sy, gx, gy, max_nodes=6000):
        """Weighted A* on the nav grid. Returns a list of (x, y) waypoints (excluding the start),
        ending at the goal node, or at the explored node closest to the goal."""
        s = self.nearest_free(sx, sy)
        g = self.nearest_free(gx, gy)
        if s is None or g is None:
            return None
        if s == g:
            return [self.node_xy(g)]
        NNX, NNY, nav = self.NNX, self.NNY, self.nav
        gI, gJ = g % NNX, g // NNX
        W = 1.3
        SQ2 = 1.41421356

        def h(k):
            dx = abs(k % NNX - gI)
            dy = abs(k // NNX - gJ)
            return W * ((dx + dy) + (SQ2 - 2) * min(dx, dy))

        openh = [(h(s), 0.0, s)]
        gc = {s: 0.0}
        parent = {s: -1}
        closed = set()
        best, besth = s, h(s)
        n = 0
        nbrs = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                (1, 1, SQ2), (1, -1, SQ2), (-1, 1, SQ2), (-1, -1, SQ2))
        while openh:
            f, gk, k = heapq.heappop(openh)
            if k in closed:
                continue
            closed.add(k)
            if k == g:
                best = g
                break
            n += 1
            if n > max_nodes:
                break
            hk = f - gk
            if hk < besth:
                besth, best = hk, k
            I, J = k % NNX, k // NNX
            for (di, dj, c) in nbrs:
                ii, jj = I + di, J + dj
                if ii < 0 or jj < 0 or ii >= NNX or jj >= NNY:
                    continue
                kk = jj * NNX + ii
                if not nav[kk] or kk in closed:
                    continue
                if di and dj and (not nav[J * NNX + ii] or not nav[jj * NNX + I]):
                    continue
                ng = gk + c
                if ng < gc.get(kk, 1e18):
                    gc[kk] = ng
                    parent[kk] = k
                    heapq.heappush(openh, (ng + h(kk), ng, kk))
        path = []
        k = best
        while k != -1 and k != s:
            path.append(self.node_xy(k))
            k = parent[k]
        path.reverse()
        if not path:
            path = [self.node_xy(best)]
        return path

    def cover_near(self, x, y, r):
        B = self.cover_bucket
        ci = int((x - self.X0) // B)
        cj = int((y - self.Y0) // B)
        rb = int(r // B) + 1
        out = []
        r2 = r * r
        for dj in range(-rb, rb + 1):
            for di in range(-rb, rb + 1):
                lst = self.cover_bk.get((ci + di, cj + dj))
                if not lst:
                    continue
                for k in lst:
                    nx_, ny_ = self.node_xy(k)
                    if (nx_ - x) ** 2 + (ny_ - y) ** 2 <= r2:
                        out.append((nx_, ny_))
        return out


def main(argv):
    """python3 geo.py <map_dir> <cache_out>: precompute the map model (used by the Dockerfile)."""
    import time
    map_dir, out = argv[1], argv[2]
    t0 = time.time()
    g = Geo.build(map_dir)
    g.save(out, source_hash(map_dir))
    free = sum(g.nav)
    _log("built geo in %.1fs: %d/%d nav nodes free, %d cover nodes, %d multi cells" %
         (time.time() - t0, free, len(g.nav), len(g.cover_nodes), len(g.multi)))


if __name__ == "__main__":
    main(sys.argv)
