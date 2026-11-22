"""Native terrain and span-aware solid geometry; no third-party dependencies.

Only world.json prisms and terrain stop shots. Trees are intentionally excluded:
this is ballistic cover, not the server's foliage/vision model.
"""
import array
import json
import math
import os
import sys

EPS = 1e-8


def _edges(flat):
    pts = list(zip(flat[::2], flat[1::2]))
    return [(a[0], a[1], b[0], b[1]) for a, b in zip(pts, pts[1:] + pts[:1])]


def _inside(x, y, edges):
    inside = False
    for ax, ay, bx, by in edges:
        cross = (bx - ax) * (y - ay) - (by - ay) * (x - ax)
        if abs(cross) < EPS and min(ax, bx) - EPS <= x <= max(ax, bx) + EPS and min(ay, by) - EPS <= y <= max(ay, by) + EPS:
            return True
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
            inside = not inside
    return inside


def _in_solid(x, y, prism):
    return _inside(x, y, prism[2]) and not any(_inside(x, y, hole) for hole in prism[3])


def _cells(ax, ay, bx, by, size):
    """Grid cells traversed by a segment, including both sides of corner hits."""
    ix, iy = math.floor(ax / size), math.floor(ay / size)
    ex, ey = math.floor(bx / size), math.floor(by / size)
    dx, dy = bx - ax, by - ay
    sx, sy = (1 if dx > 0 else -1), (1 if dy > 0 else -1)
    tx = (((ix + (dx > 0)) * size - ax) / dx) if dx else math.inf
    ty = (((iy + (dy > 0)) * size - ay) / dy) if dy else math.inf
    stepx, stepy = (size / abs(dx) if dx else math.inf), (size / abs(dy) if dy else math.inf)
    yield ix, iy
    # Bound by displacement, so roundoff cannot create an unbounded loop.
    for _ in range(abs(ex - ix) + abs(ey - iy) + 2):
        if ix == ex and iy == ey:
            break
        if abs(tx - ty) < EPS:
            yield ix + sx, iy
            yield ix, iy + sy
            ix += sx; iy += sy; tx += stepx; ty += stepy
        elif tx < ty:
            ix += sx; tx += stepx
        else:
            iy += sy; ty += stepy
        yield ix, iy


class Grid:
    def __init__(self, path=None, map_dir=None):
        # Compatible with Grid('/bot/grid.bin'); use authoritative mounted map.
        if map_dir is None:
            roots = [os.environ.get('MECHFIGHT_MAP_DIR', '/arena'), os.path.dirname(path or __file__)]
            map_dir = next((r for r in roots if os.path.isfile(os.path.join(r, 'v2', 'world.json'))), None)
        if map_dir is None:
            raise FileNotFoundError('native arena/v2/world.json is required')
        folder = os.path.join(map_dir, 'v2')
        with open(os.path.join(folder, 'world.json')) as f:
            world = json.load(f)
        t = world['terrain']
        self.x0, self.y0, self.c = t['minX'], t['minY'], t['step']
        self.w, self.h = t['nx'], t['ny']
        self.scale = t.get('scale', .01)
        self.g = array.array('h')
        with open(os.path.join(folder, t['file']), 'rb') as f:
            self.g.frombytes(f.read())
        if sys.byteorder != 'little':
            self.g.byteswap()
        if len(self.g) != self.w * self.h:
            raise ValueError('terrain dimensions mismatch')
        self.max_ground = max(self.g) * self.scale
        self.bounds = world['bounds']
        self.prisms, self.buckets = [], {}
        self.bucket_size = 16.0
        for raw in world['prisms']:
            ring = _edges(raw['ring']); holes = [_edges(h) for h in raw.get('holes', [])]
            xs, ys = raw['ring'][::2], raw['ring'][1::2]
            bbox = min(xs), min(ys), max(xs), max(ys)
            prism = raw['base'], raw['top'], ring, holes, ring + [e for h in holes for e in h], bbox
            index = len(self.prisms); self.prisms.append(prism)
            for ix in range(math.floor(bbox[0] / 16), math.floor(bbox[2] / 16) + 1):
                for iy in range(math.floor(bbox[1] / 16), math.floor(bbox[3] / 16) + 1):
                    self.buckets.setdefault((ix, iy), []).append(index)

    def idx(self, x, y):
        ix, iy = math.floor((x - self.x0) / self.c), math.floor((y - self.y0) / self.c)
        return iy * self.w + ix if 0 <= ix < self.w and 0 <= iy < self.h else -1

    def ground(self, x, y):
        fx = min(self.w - 1.000000001, max(0.0, (x - self.x0) / self.c))
        fy = min(self.h - 1.000000001, max(0.0, (y - self.y0) / self.c))
        ix, iy = int(fx), int(fy)
        ux, uy = fx - ix, fy - iy
        k = iy * self.w + ix; g = self.g
        a = g[k] * (1 - ux) + g[k + 1] * ux
        b = g[k + self.w] * (1 - ux) + g[k + self.w + 1] * ux
        return (a * (1 - uy) + b * uy) * self.scale

    def _point_prisms(self, x, y, radius=0):
        if radius <= 0:
            return self.buckets.get((math.floor(x / 16), math.floor(y / 16)), ())
        ids = set()
        for ix in range(math.floor((x - radius) / 16), math.floor((x + radius) / 16) + 1):
            for iy in range(math.floor((y - radius) / 16), math.floor((y + radius) / 16) + 1):
                ids.update(self.buckets.get((ix, iy), ()))
        return ids

    def top(self, x, y):
        """Highest surface, for visualization only; NOT an occupancy query."""
        if self.idx(x, y) < 0:
            return 999.0
        height = self.ground(x, y)
        for i in self._point_prisms(x, y):
            p = self.prisms[i]
            if p[1] > height and _in_solid(x, y, p):
                height = p[1]
        return height

    def solid_at(self, x, y, z):
        if self.idx(x, y) < 0 or self.ground(x, y) > z + EPS:
            return True
        return any(p[0] - EPS <= z <= p[1] + EPS and _in_solid(x, y, p)
                   for p in (self.prisms[i] for i in self._point_prisms(x, y)))

    def body_blocked(self, x, y, z, radius=0.0, height=9.0, step_up=1.2):
        """Cylinder colliding with a prism or terrain too high to step onto.

        Ground clearance samples center/cardinal rim points; prism/circle
        intersection is exact in XY. Slope/continuous path planning is separate.
        """
        b = self.bounds
        if x - radius < b['minX'] or x + radius > b['maxX'] or y - radius < b['minY'] or y + radius > b['maxY']:
            return True
        points = ((x, y),) if radius <= 0 else ((x, y), (x-radius, y), (x+radius, y), (x, y-radius), (x, y+radius))
        ground = max(self.ground(px, py) for px, py in points)
        if ground > z + step_up + EPS:
            return True
        r2 = radius * radius
        overlapping = []
        for i in self._point_prisms(x, y, radius):
            p = self.prisms[i]
            if p[1] < z - EPS or p[0] >= z + step_up + height - EPS:
                continue
            if _in_solid(x, y, p):
                overlapping.append(p)
                continue
            if radius <= 0:
                continue
            for ax, ay, bx, by in p[4]:
                dx, dy = bx - ax, by - ay
                den = dx * dx + dy * dy
                t = min(1.0, max(0.0, ((x - ax) * dx + (y - ay) * dy) / den)) if den else 0
                if (x - ax - t * dx) ** 2 + (y - ay - t * dy) ** 2 < r2 - EPS:
                    overlapping.append(p)
                    break
        # Walking onto a step raises the whole cylinder, including its head.
        support = max(z, ground, max((p[1] for p in overlapping if p[1] <= z + step_up + EPS), default=z))
        return any(p[1] > z + step_up + EPS and p[0] < support + height - EPS for p in overlapping)

    def obstacle_top(self, x, y, z, height=9.0):
        """Highest actually intersecting obstacle, ignoring clear overhead decks."""
        result = self.ground(x, y)
        for i in self._point_prisms(x, y):
            p = self.prisms[i]
            if p[0] < z + height - EPS and p[1] > z and _in_solid(x, y, p):
                result = max(result, p[1])
        return result

    @staticmethod
    def _prism_hit(a, b, p):
        dx, dy, dz = b[0]-a[0], b[1]-a[1], b[2]-a[2]
        if abs(dz) < EPS:
            if not p[0] - EPS <= a[2] <= p[1] + EPS:
                return False
            low, high = 0.0, 1.0
        else:
            u, v = (p[0]-a[2])/dz, (p[1]-a[2])/dz
            low, high = max(0.0, min(u,v)), min(1.0, max(u,v))
            if low > high + EPS:
                return False
        if _in_solid(a[0]+low*dx, a[1]+low*dy, p) or _in_solid(a[0]+high*dx, a[1]+high*dy, p):
            return True
        for ax, ay, bx, by in p[4]:
            ex, ey = bx-ax, by-ay
            den = dx*ey-dy*ex
            if abs(den) < EPS:
                # Collinear contact along a wall/inner courtyard boundary.
                norm = dx*dx+dy*dy
                if norm > EPS and abs((ax-a[0])*dy-(ay-a[1])*dx) < EPS:
                    u = ((ax-a[0])*dx+(ay-a[1])*dy)/norm
                    v = ((bx-a[0])*dx+(by-a[1])*dy)/norm
                    if max(low,min(u,v)) <= min(high,max(u,v)) + EPS:
                        return True
                continue
            rx, ry = ax-a[0], ay-a[1]
            t, u = (rx*ey-ry*ex)/den, (rx*dy-ry*dx)/den
            if low-EPS <= t <= high+EPS and -EPS <= u <= 1+EPS:
                return True
        return False

    def _terrain_clear(self, a, b):
        if min(a[2], b[2]) > self.max_ground:
            return True
        dx, dy, dz = b[0]-a[0], b[1]-a[1], b[2]-a[2]
        cuts = [0.0, 1.0]
        for origin, delta, start in ((a[0], dx, self.x0), (a[1], dy, self.y0)):
            if abs(delta) < EPS:
                continue
            lo, hi = sorted(((origin-start)/self.c, (origin+delta-start)/self.c))
            for k in range(math.floor(lo)+1, math.ceil(hi)):
                cuts.append((start+k*self.c-origin)/delta)
        cuts.sort()
        # Bilinear terrain restricted to a straight line is quadratic inside
        # each native cell: test endpoints and its internal extremum exactly.
        def clearance(t):
            return a[2]+dz*t-self.ground(a[0]+dx*t, a[1]+dy*t)
        last = clearance(0)
        if last < -EPS:
            return False
        for lo, hi in zip(cuts, cuts[1:]):
            if hi-lo < EPS:
                continue
            end = clearance(hi)
            if end < -EPS:
                return False
            mid = clearance((lo+hi)*.5)
            qa = 2*(last+end-2*mid); qb = end-last-qa
            if qa > EPS:
                u = -qb/(2*qa)
                if 0 < u < 1 and last + qb*u + qa*u*u < -EPS:
                    return False
            last = end
        return True

    def segment_clear(self, a, b):
        """Exact prism and bilinear-terrain intersection, endpoints included."""
        if not self._terrain_clear(a, b):
            return False
        candidates = set()
        for cell in _cells(a[0], a[1], b[0], b[1], self.bucket_size):
            candidates.update(self.buckets.get(cell, ()))
        return not any(self._prism_hit(a,b,self.prisms[i]) for i in candidates)

    def los(self, a, b, step=2.0, skip=0.0):
        # 'step' accepted for existing callers; native intersection isn't sampled.
        if skip:
            distance = math.dist(a, b)
            if distance > skip + 3:
                start, end = skip/distance, 1-3/distance
                delta = [b[i]-a[i] for i in range(3)]
                b = [a[i]+end*delta[i] for i in range(3)]
                a = [a[i]+start*delta[i] for i in range(3)]
        return self.segment_clear(a,b)
