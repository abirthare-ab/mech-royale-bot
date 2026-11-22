"""Offline: rasterize terrain + solid prisms (buildings/stadium) onto a 2 m grid.
Writes grid.bin: header + two int16 arrays (cm): ground height, solid top (max of ground and any
prism that starts near the ground). Bridges with base well above ground are ignored (walk under)."""
import json, struct, sys, array, math

KIT = sys.argv[1]
OUT = sys.argv[2]
w = json.load(open(f"{KIT}/arena/v2/world.json"))
t = w["terrain"]
nx, ny, step, minX, minY = t["nx"], t["ny"], t["step"], t["minX"], t["minY"]
ter = array.array("h")
ter.frombytes(open(f"{KIT}/arena/v2/terrain.i16", "rb").read())

def ground(x, y):
    fx = (x - minX) / step; fy = (y - minY) / step
    i = min(max(int(fx), 0), nx - 2); j = min(max(int(fy), 0), ny - 2)
    ax = min(max(fx - i, 0), 1); ay = min(max(fy - j, 0), 1)
    h00 = ter[j * nx + i]; h10 = ter[j * nx + i + 1]; h01 = ter[(j + 1) * nx + i]; h11 = ter[(j + 1) * nx + i + 1]
    return ((h00 * (1 - ax) + h10 * ax) * (1 - ay) + (h01 * (1 - ax) + h11 * ax) * ay) / 100.0

C = 2.0
gx0, gy0 = w["bounds"]["minX"], w["bounds"]["minY"]
GW = int(math.ceil((w["bounds"]["maxX"] - gx0) / C)) + 1
GH = int(math.ceil((w["bounds"]["maxY"] - gy0) / C)) + 1
G = [0.0] * (GW * GH)
for j in range(GH):
    for i in range(GW):
        G[j * GW + i] = ground(gx0 + (i + 0.5) * C, gy0 + (j + 0.5) * C)
T = list(G)

def inside(px, py, ring):
    n = len(ring) // 2; ins = False
    xj, yj = ring[-2], ring[-1]
    for k in range(n):
        xi, yi = ring[2 * k], ring[2 * k + 1]
        if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi:
            ins = not ins
        xj, yj = xi, yi
    return ins

for p in w["prisms"]:
    ring = p["ring"]
    xs = ring[0::2]; ys = ring[1::2]
    i0 = max(0, int((min(xs) - gx0) / C)); i1 = min(GW - 1, int((max(xs) - gx0) / C) + 1)
    j0 = max(0, int((min(ys) - gy0) / C)); j1 = min(GH - 1, int((max(ys) - gy0) / C) + 1)
    for j in range(j0, j1 + 1):
        cy = gy0 + (j + 0.5) * C
        for i in range(i0, i1 + 1):
            cx = gx0 + (i + 0.5) * C
            if not inside(cx, cy, ring):
                continue
            if any(inside(cx, cy, h) for h in p.get("holes", [])):
                continue
            g = G[j * GW + i]
            if p["base"] > g + 9.5:  # clears a mech's head: walk under
                continue
            if p["top"] > T[j * GW + i]:
                T[j * GW + i] = p["top"]

with open(OUT, "wb") as f:
    f.write(struct.pack("<4sffffii", b"MGR1", gx0, gy0, C, 0.0, GW, GH))
    f.write(array.array("h", [int(round(v * 100)) for v in G]).tobytes())
    f.write(array.array("h", [int(round(min(v, 320) * 100)) for v in T]).tobytes())
print(GW, GH, sum(1 for a, b in zip(G, T) if b - a > 1.2), "wall cells")
