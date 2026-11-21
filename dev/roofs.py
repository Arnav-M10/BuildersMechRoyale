"""Find hideout roofs: walkable roof regions high above ground where a mech can stand far from every edge."""
import sys, collections, json
sys.path.insert(0, '.')
import geo
g = geo.Geo.load('arena', '/tmp/mf_cache.pkl')
NX, NY = g.NX, g.NY
H = [0.0] * (NX * NY)
roof = bytearray(NX * NY)
for k in range(NX * NY):
    if g.hi[k] > g.lo[k] and g.hi[k] - g.terr[k] > 10.0:
        roof[k] = 1
        H[k] = g.hi[k]
# distance (in cells, 4-neighbour BFS) from each roof cell to a cell that is not the same roof level (|dh| > 1.2)
dist = [0] * (NX * NY)
q = collections.deque()
for k in range(NX * NY):
    if not roof[k]:
        continue
    i, j = k % NX, k // NX
    edge = False
    for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        ii, jj = i + di, j + dj
        if ii < 0 or jj < 0 or ii >= NX or jj >= NY:
            edge = True; break
        kk = jj * NX + ii
        if not roof[kk] or abs(H[kk] - H[k]) > 1.2:
            edge = True; break
    if edge:
        dist[k] = 1; q.append(k)
while q:
    k = q.popleft(); i, j = k % NX, k // NX
    for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        ii, jj = i + di, j + dj
        if 0 <= ii < NX and 0 <= jj < NY:
            kk = jj * NX + ii
            if roof[kk] and dist[kk] == 0 and abs(H[kk] - H[k]) <= 1.2:
                dist[kk] = dist[k] + 1; q.append(kk)
# local maxima of depth = hideout spots
spots = []
for k in range(NX * NY):
    d = dist[k]
    if d >= 9:
        i, j = k % NX, k // NX
        if all(dist[(j + dj) * NX + i + di] <= d for di in (-1, 0, 1) for dj in (-1, 0, 1) if 0 <= i + di < NX and 0 <= j + dj < NY):
            spots.append((d, g.X0 + i, g.Y0 + j, H[k], H[k] - g.terr[k]))
spots.sort(reverse=True)
# dedupe within 25 m
out = []
for s in spots:
    if all((s[1] - o[1]) ** 2 + (s[2] - o[2]) ** 2 > 625 for o in out):
        out.append(s)
print(len(out), 'spots (depth >= 9 m from any edge)')
for s in out[:40]:
    print('depth %2d  x %7.1f y %7.1f  roof z %5.1f  above ground %5.1f' % s)
json.dump(out, open('/tmp/roof_spots.json', 'w'))
