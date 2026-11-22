"""Short-horizon evasive movement using only observed enemy projectiles.

This is a local motion predictor, not a replica of the match simulator.
No incoming collision on the intended path means no behavior change.
"""
import math
import time

DT = .05
STEPS = 16


def cylinder_hit(a, b, radius, height):
    """Continuous segment against a cylinder at the origin (feet z=0)."""
    dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    aa = dx * dx + dy * dy
    bb = a[0] * dx + a[1] * dy
    cc = a[0] * a[0] + a[1] * a[1] - radius * radius
    lo, hi = 0., 1.
    if aa < 1e-12:
        if cc > 0:
            return False
    else:
        disc = bb * bb - aa * cc
        if disc < 0:
            return False
        root = math.sqrt(disc)
        lo, hi = max(lo, (-bb - root) / aa), min(hi, (-bb + root) / aa)
    if abs(dz) < 1e-12:
        return lo <= hi and -.3 <= a[2] <= height + .3
    low, high = (-.3 - a[2]) / dz, (height + .3 - a[2]) / dz
    if low > high:
        low, high = high, low
    return max(lo, low) <= min(hi, high)


def _motion(bot, obs, yaw, fraction, boost):
    me, cfg = obs['self'], bot.cfg['mech']
    stats = me['stats']
    x, y, z = me['pos']
    vx, vy, vz = me['vel']
    grounded = me.get('grounded', True)
    accel = stats.get('ground_accel' if grounded else 'air_accel',
                      cfg.get('ground_accel' if grounded else 'air_accel', 58.5 if grounded else 12))
    speed = stats.get('boost_speed' if boost and grounded else 'walk_speed',
                      cfg.get('boost_speed' if boost and grounded else 'walk_speed', 19.5 if boost and grounded else 11.7)) * fraction
    if any(w and w['kind'] == 'laser' and w.get('charge', 0) > 0 for w in me['weapons']):
        speed *= stats.get('charge_move_mult', cfg.get('charge_move_mult', .5))
    ux, uy = math.cos(yaw) * speed, math.sin(yaw) * speed
    path = [(x, y, z)]
    for _ in range(STEPS):
        dx, dy = ux - vx, uy - vy
        gap = math.hypot(dx, dy)
        blend = min(1., accel * DT / gap) if gap > 1e-12 else 1.
        vx += dx * blend
        vy += dy * blend
        x += vx * DT
        y += vy * DT
        if not grounded:
            vz -= stats.get('gravity', cfg.get('gravity', 22)) * DT
            if boost:
                vz = min(stats.get('max_rise_speed', cfg.get('max_rise_speed', 10)),
                         vz + stats.get('jet_accel', cfg.get('jet_accel', 34)) * DT)
            z += vz * DT
        path.append((x, y, z))
    return path


def _shots(bot, obs, deadline=math.inf):
    """Bound candidate work to nearby shots that could reach us within .8 s."""
    me = obs['self']
    x, y, z = me['pos']
    nearby = []
    for shot in obs.get('projectiles', ()):
        px, py, pz = shot['pos']
        vx, vy, vz = shot['vel']
        dx, dy = px - x, py - y
        vv = vx * vx + vy * vy
        t = max(0., min(STEPS * DT, -(dx * vx + dy * vy) / max(vv, .001)))
        distance = math.hypot(dx + vx * t, dy + vy * t)
        margin = me['stats'].get('radius', 2.5) + (12 if shot['kind'] == 'mortar' else 1)
        if distance > margin + 20 * STEPS * DT:
            continue
        nearby.append((t, shot))
    # Closest-time threats take priority in pathological observation sizes.
    nearby.sort(key=lambda item: item[0])
    result = []
    grid = bot.grid
    for _, shot in nearby[:24]:
        if time.perf_counter() >= deadline:
            return None
        pos, vel = shot['pos'], shot['vel']
        mortar = shot['kind'] == 'mortar'
        gravity = bot.cfg['weapons']['mortar'].get('gravity', 22) if mortar else 0
        points = [tuple(pos)]
        impact = None
        end_fraction = 1.
        for step in range(1, STEPS + 1):
            t = step * DT
            point = (pos[0] + vel[0] * t, pos[1] + vel[1] * t,
                     pos[2] + vel[2] * t - .5 * gravity * t * t)
            if mortar and grid is not None and not hasattr(grid, 'segment_clear'):
                floor = grid.ground(point[0], point[1])
                if point[2] <= floor:
                    impact = (step, (point[0], point[1], floor), 1.)
                    points.append(point)
                    break
            points.append(point)
        if grid is not None and hasattr(grid, 'segment_clear'):
            blocked_step = None
            if mortar:
                # The shell bends; each .05 s chord must be tested separately.
                for i in range(1, len(points)):
                    if not grid.segment_clear(points[i-1], points[i]):
                        blocked_step = i
                        break
            elif not grid.segment_clear(points[0], points[-1]):
                # Gun/NPC bullets are straight. Find the first obstructed
                # interval logarithmically instead of tracing every interval.
                lo, hi = 1, len(points)-1
                while lo < hi:
                    mid = (lo + hi) // 2
                    if grid.segment_clear(points[0], points[mid]):
                        lo = mid + 1
                    else:
                        hi = mid
                blocked_step = lo
            if blocked_step is not None:
                a, b = points[blocked_step-1], points[blocked_step]
                lo, hi = 0., 1.
                for _ in range(8):
                    f = (lo + hi) * .5
                    p = tuple(a[j] + f * (b[j] - a[j]) for j in range(3))
                    if grid.segment_clear(a, p):
                        lo = f
                    else:
                        hi = f
                end_fraction = lo
                point = tuple(a[j] + lo * (b[j] - a[j]) for j in range(3))
                points = points[:blocked_step] + [point]
                if mortar:
                    impact = (blocked_step, point, lo)
        damage = bot.cfg['weapons'].get(shot['kind'], {}).get('damage', 25)
        result.append((points, damage, impact, end_fraction))
    return result


def _risk(path, shots, radius, height, splash, grid=None):
    total = 0.
    for points, damage, impact, end_fraction in shots:
        hit = False
        a = tuple(points[0][axis] - path[0][axis] for axis in range(3))
        for i in range(1, len(points)):
            position = path[i]
            if i == len(points)-1 and end_fraction < 1:
                position = tuple(path[i-1][axis] + end_fraction * (path[i][axis] - path[i-1][axis]) for axis in range(3))
            b = tuple(points[i][axis] - position[axis] for axis in range(3))
            if cylinder_hit(a, b, radius + .35, height):
                hit = True
                break
            a = b
        if hit:
            total += damage
        elif impact:
            i, point, fraction = impact
            position = tuple(path[i-1][axis] + fraction * (path[i][axis] - path[i-1][axis]) for axis in range(3))
            # Blast-to-body distance; conservative around a predicted landing.
            dz = max(position[2] - point[2], point[2] - position[2] - height, 0)
            gap = math.hypot(max(0, math.dist(position[:2], point[:2]) - radius), dz)
            if gap < splash:
                if grid is not None and hasattr(grid, 'segment_clear'):
                    center = (position[0], position[1], position[2] + height * .5)
                    distance = max(math.dist(point, center), .001)
                    start = tuple(point[j] + .05 * (center[j]-point[j]) / distance for j in range(3))
                    if not grid.segment_clear(start, center):
                        continue
                total += 160 * (1 - .7 * gap / splash)
    return total


def _safe(bot, obs, path, baseline, radius, height, zc, zr):
    grid = bot.grid
    step_up = obs['self']['stats'].get('step_up', bot.cfg['mech'].get('step_up', 1.2))
    # Stay at least as safe as our intended route if already outside the zone.
    for i in (4, 8, 12, 16):
        point = path[i]
        center, rad = bot.zone_now(obs['zone'], obs['t'] + i * DT) if hasattr(bot, 'zone_now') else (zc, zr)
        limit = max(0, math.dist(baseline[i][:2], center) - rad + radius)
        if math.dist(point[:2], center) - rad + radius > limit + .05:
            return False
        if grid is not None:
            if hasattr(grid, 'body_blocked'):
                if grid.body_blocked(*point, radius, height, step_up):
                    return False
            else:
                vx, vy = point[0] - path[max(0, i - 1)][0], point[1] - path[max(0, i - 1)][1]
                distance = max(math.hypot(vx, vy), .001)
                for side in (-radius, 0, radius):
                    if bot.blocked(point[0] - vy / distance * side,
                                   point[1] + vx / distance * side, point[2]):
                        return False
    return True


def evade(bot, obs, move_yaw, speed, boost, zc, zr):
    """Return (heading, boost); preserve aim, firing, and all non-threat motion."""
    if move_yaw is None or speed <= 0 or not obs.get('projectiles'):
        return move_yaw, boost
    deadline = time.perf_counter() + .008
    shots = _shots(bot, obs, deadline)
    if not shots:
        return move_yaw, boost
    me = obs['self']
    radius, height = me['stats'].get('radius', 2.5), me['stats'].get('height', 9)
    splash = bot.cfg['weapons']['mortar'].get('splash_radius', 7)
    baseline = _motion(bot, obs, move_yaw, speed, boost)
    risk = _risk(baseline, shots, radius, height, splash, bot.grid)
    if risk <= 0:
        return move_yaw, boost
    best = move_yaw, boost
    best_cost = risk
    # Nine distinct headings at most, with one extra boosted straight option.
    candidates = [(off, boost) for off in (.55, -.55, 1.1, -1.1, 1.65, -1.65)]
    if not boost and me.get('grounded', True) and me['energy'] > 25:
        candidates += [(0., True), (.8, True), (-.8, True)]
    for off, use_boost in candidates:
        if time.perf_counter() >= deadline:
            break
        path = _motion(bot, obs, move_yaw + off, speed, use_boost)
        cost = _risk(path, shots, radius, height, splash, bot.grid) + 2 * (1 - math.cos(off)) + (1 if use_boost != boost else 0)
        if cost + 5 < best_cost and _safe(bot, obs, path, baseline, radius, height, zc, zr):
            best_cost, best = cost, (move_yaw + off, use_boost)
    return best
