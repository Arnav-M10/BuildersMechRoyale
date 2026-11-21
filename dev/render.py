import sys, zlib, struct
sys.path.insert(0, '.')
import geo
g = geo.Geo.load('arena', '/tmp/mf_cache.pkl')
W, H = g.NNX, g.NNY
cov = set(g.cover_nodes)
rows = []
for J in range(H - 1, -1, -1):
    r = bytearray([0])
    for I in range(W):
        k = J * W + I
        x, y = g.node_xy(k)
        i = int(x - g.X0); j = int(y - g.Y0); c = j * g.NX + i
        if g.nav[k]:
            v = (60, 160, 60) if k in cov else (200, 200, 200)
        else:
            h = g.hi[c] - g.terr[c] if g.hi[c] > g.lo[c] else 0
            v = (min(255, 80 + int(h * 3)), 40, 40) if h > 0 else (40, 40, 120)
        r += bytes(v)
    rows.append(bytes(r))
raw = b''.join(rows)
def chunk(t, d): return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', W, H, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')
open(sys.argv[1], 'wb').write(png)
