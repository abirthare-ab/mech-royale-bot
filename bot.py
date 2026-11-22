"""Mechfight controller: weapon-specific aim, loot growth, and zone-aware fighting."""
import math, os, random, struct, array, time
import mechfight as mf

HERE = os.path.dirname(os.path.abspath(__file__))


class Grid:
    def __init__(self, path):
        with open(path, "rb") as f:
            magic, self.x0, self.y0, self.c, _, self.w, self.h = struct.unpack("<4sffffii", f.read(28))
            n = self.w * self.h
            self.g = array.array("h"); self.g.frombytes(f.read(2 * n))
            self.t = array.array("h"); self.t.frombytes(f.read(2 * n))

    def idx(self, x, y):
        i = int((x - self.x0) / self.c); j = int((y - self.y0) / self.c)
        if i < 0 or j < 0 or i >= self.w or j >= self.h:
            return -1
        return j * self.w + i

    def top(self, x, y):
        k = self.idx(x, y)
        return 999.0 if k < 0 else self.t[k] / 100.0

    def ground(self, x, y):
        k = self.idx(x, y)
        return 0.0 if k < 0 else self.g[k] / 100.0

    def los(self, a, b, step=2.0, skip=4.0):
        dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        d = math.hypot(dx, dy)
        if d < 1e-6:
            return True
        n = int(d / step)
        for s in range(n + 1):
            f = s * step / d
            if f * d < skip or (1 - f) * d < 3.0:
                continue
            x = a[0] + dx * f; y = a[1] + dy * f; z = a[2] + dz * f
            if self.top(x, y) > z:
                return False
        return True


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


UPGRADE_PRIO = ["laser_charge", "laser_damage", "armor", "gun_rate", "gun_damage", "plating", "nanites",
                "lightning_efficiency", "lightning_range", "sensors", "gun_mag", "gun_velocity", "servos",
                "capacitor", "reactor", "jets", "gyros", "mortar_reload", "mortar_splash", "mortar_velocity",
                "laser_pierce", "lightning_chain"]
WEAPON_PRIO = {"laser": 3, "lightning": 2, "mortar": 1, "gun": 0}


class Bot:
    name = "survivor-v6"

    def __init__(self, hello):
        self.cfg = hello["config"]
        random.seed(hello.get("seed", 1))
        try:
            self.grid = Grid(os.path.join(HERE, "grid.bin"))
        except Exception as e:
            mf.log("grid load failed", e)
            self.grid = None
        self.reach = self.cfg["loot"]["pickup_radius"]
        self.strafe_dir = 1
        self.strafe_until = 0.0
        self.stuck_t = 0.0
        self.unstick_until = 0.0
        self.unstick_dir = 0.0
        self.climb_until = 0.0
        self.last_hit_bearing = None
        self.last_hit_t = -99.0
        self.last_attacker = None
        self.scan_phase = 0.0
        self.laser_hold = 0.0
        self.target_id = None
        self.loot_memory = {}
        self.last_damage_t = -99.0
        self.last_combat_t = -99.0
        self.healing = False
        self.cover_goal = None
        self.cover_search_at = 0.0
        self.recent_threats = {}

    def choose_upgrade(self, choices, me):
        """Marginal idealized DPS/EHP, with modest utility fallback scores.

        These are a selection heuristic, not predictions of match win rate.
        All weapon gains use their current upgraded stats, so diminishing
        magazine and charge benefits are handled without a fixed ordering.
        """
        weapons = {w['kind']: w['stats'] for w in me['weapons'] if w}
        dps = {}
        for kind, stats in weapons.items():
            damage, cooldown = stats.get('damage', 0), stats.get('cooldown', 1)
            if kind == 'gun':
                dps[kind] = damage * stats.get('mag', 12) / max(.01, cooldown * stats.get('mag', 12) + stats.get('reload', 2.4))
            elif kind == 'laser':
                dps[kind] = damage / max(.01, cooldown + stats.get('charge', .75))
            elif kind == 'mortar':
                # Don't assume every shell earns direct hit plus splash.
                dps[kind] = damage / max(.01, cooldown)
            else:
                dps[kind] = damage / max(.01, cooldown)
        total = max(sum(dps.values()), .01)
        values = {'plating': .15, 'armor': .08 / max(.01, 1 - me['stats'].get('armor', 0) - .08),
                  'nanites': .055, 'servos': .05, 'gun_velocity': .05,
                  'lightning_range': .06, 'lightning_efficiency': .08,
                  'reactor': .035, 'capacitor': .035, 'jets': .03,
                  'sensors': .025, 'gyros': .025, 'mortar_splash': .04,
                  'mortar_velocity': .035, 'laser_pierce': .015, 'lightning_chain': .015}
        if 'gun' in weapons:
            s = weapons['gun']; m, c, r = s['mag'], s['cooldown'], s['reload']
            fraction = dps['gun'] / total
            values['gun_mag'] = fraction * ((m + 8) * (m * c + r) / (m * ((m + 8) * c + .8 * r)) - 1)
            values['gun_damage'] = fraction * .15
            values['gun_rate'] = fraction * ((m * c + r) / (m * c * .87 + r) - 1)
        if 'laser' in weapons:
            s = weapons['laser']; q, c = s['charge'], s['cooldown']
            fraction = dps['laser'] / total
            values['laser_damage'] = fraction * .18
            values['laser_charge'] = fraction * ((q + c) / (.75 * q + c) - 1)
        if 'mortar' in weapons:
            values['mortar_reload'] = dps['mortar'] / total * (1 / .82 - 1)
        return max(range(len(choices)), key=lambda i: (values.get(choices[i]['id'], 0), -i))

    def covered_at(self, position, me, threats):
        if self.grid is None:
            return False
        for enemy in threats:
            origin = list(enemy['pos'])
            origin[2] += self.target_height(enemy) * .8
            for height in (me['stats'].get('height', 9) / 2, me['stats'].get('eye_height', 8)):
                point = [position[0], position[1], position[2] + height]
                if self.grid.los(origin, point):
                    return False
        return True

    def recovery_zone_safe(self, point, obs, zc, zr):
        # Recovery takes at least five seconds: never choose a destination
        # already outside the announced next circle, even during its hold.
        margin = max(12, obs['self']['stats'].get('radius', 2.5) + 3)
        zone = obs['zone']
        return (math.dist(point[:2], zc) <= zr - margin and
                math.dist(point[:2], zone['next_center']) <= zone['next_radius'] - margin)

    def reachable_cover(self, point, obs, zc, zr, threats):
        me = obs['self']
        if self.grid is None or not self.recovery_zone_safe(point, obs, zc, zr):
            return False
        x, y, z = me['pos']
        distance = math.dist(point[:2], (x, y))
        if distance > 22 or abs(point[2] - z) > 1.2:
            return False
        dx, dy = (point[0] - x) / max(distance, .001), (point[1] - y) / max(distance, .001)
        radius = me['stats'].get('radius', 2.5)
        # Include the endpoint; spacing <= 1 m cannot skip a narrow obstacle
        # represented by the grid. Recheck after every movement/steering step.
        steps = max(1, math.ceil(distance))
        for i in range(1, steps + 1):
            d = distance * i / steps
            for side in (-radius, 0, radius):
                px, py = x + dx * d - dy * side, y + dy * d + dx * side
                if self.blocked(px, py, z) or abs(self.grid.ground(px, py) - z) > 1.2:
                    return False
        return self.covered_at(point, me, threats)

    def healing_goal(self, obs, zc, zr, mode):
        """Return (goal, protected) only when recovery is presently feasible."""
        me, now = obs['self'], obs['t']
        visible_ids = {actor['id'] for actor in obs['mechs']}
        for actor in obs['mechs']:
            self.recent_threats[actor['id']] = (now, dict(actor))
        delay = self.cfg['mech'].get('regen_delay', 5)
        self.recent_threats = {i: v for i, v in self.recent_threats.items() if now - v[0] <= delay}
        hp = me['hp'] / max(1, me['max_hp'])
        if hp >= .85 or not me.get('in_zone', True) or mode in ('zone!', 'to-next', 'center', 'drop'):
            self.healing = False
            self.cover_goal = None
            return None
        if hp < .50:
            self.healing = True
        if not self.healing:
            return None
        threats = list(obs['mechs'])
        # A close hunter can erase a five-second recovery window almost at
        # once. Do not abandon shooting or loot for speculative cover then.
        if any(math.dist(e['pos'][:2], me['pos'][:2]) < 65 for e in threats):
            self.cover_goal = None
            return None
        # Losing vision of a pursuing opponent is not evidence it left.
        # Veto recovery if an unseen recent actor could already be close.
        for actor_id, (seen_t, actor) in self.recent_threats.items():
            if actor_id not in visible_ids:
                possible_speed = max(self.cfg['mech']['boost_speed'], math.hypot(*actor.get('vel', [0, 0])[:2]))
                if math.dist(actor['pos'][:2], me['pos'][:2]) - possible_speed * (now - seen_t + delay) < 65:
                    self.cover_goal = None
                    return None
        near_sound = any(s.get('dist') == 'near' and s.get('kind') in
                         ('gun', 'laser', 'laser_charge', 'mortar', 'lightning', 'npc_gun', 'explosion')
                         for s in obs.get('sounds', []))
        if not threats:
            if not near_sound and now - self.last_damage_t > 1.5 and self.recovery_zone_safe(me['pos'], obs, zc, zr):
                return me['pos'], True
            return None
        if (self.covered_at(me['pos'], me, threats) and not near_sound
                and now - self.last_damage_t > 1.5 and self.recovery_zone_safe(me['pos'], obs, zc, zr)):
            return me['pos'], True
        if self.grid is None:
            return None
        if self.cover_goal is not None:
            if math.dist(self.cover_goal[:2], me['pos'][:2]) < 2 or not self.reachable_cover(self.cover_goal, obs, zc, zr, threats):
                self.cover_goal = None
            else:
                return self.cover_goal, False
        if now < self.cover_search_at:
            return None
        self.cover_search_at = now + .7
        x, y, z = me['pos']
        nearest = min(threats, key=lambda e: math.dist(e['pos'][:2], me['pos'][:2]))
        away = math.atan2(y - nearest['pos'][1], x - nearest['pos'][0])
        deadline = time.perf_counter() + .003
        for distance in (10, 18):
            for offset in (0, -.7, .7, -1.4, 1.4, math.pi):
                if time.perf_counter() > deadline:
                    return None
                yaw = away + offset
                dx, dy = math.cos(yaw), math.sin(yaw)
                px, py = x + dx * distance, y + dy * distance
                point = [px, py, self.grid.ground(px, py)]
                if self.reachable_cover(point, obs, zc, zr, threats):
                    self.cover_goal = point
                    return point, False
        return None

    def target_height(self, tgt):
        if tgt.get("kind") == "mech":
            return self.cfg["mech"].get("height", 9) * (1 + self.cfg["cubes"].get("size_per_cube", .05) * tgt.get("cubes", 0))
        return self.cfg.get("npc" if tgt.get("kind") == "npc" else "crates", {}).get("height", 3.5 if tgt.get("kind") == "npc" else 2.4)

    def aligned(self, me, command, desired, dist, tgt):
        # A command applies before the next 20 Hz physics tick. Evaluate the
        # actual attainable aim, separately for each weapon's trajectory.
        dt = 1 / self.cfg["match"].get("tick_hz", 20)
        ay, ap = me["aim"]
        turn = me["stats"].get("turn_rate", 4) * dt
        pitch_turn = me["stats"].get("pitch_rate", 3) * dt
        ay += max(-turn, min(turn, wrap(command[0] - ay)))
        ap += max(-pitch_turn, min(pitch_turn, command[1] - ap))
        radius = (self.cfg["mech"].get("radius", 2.5) * (1 + .05 * tgt.get("cubes", 0))
                  if tgt.get("kind") == "mech" else 1.4)
        return (abs(wrap(ay - desired[0])) < math.atan2(radius * .85, max(dist, 1))
                and abs(ap - desired[1]) < math.atan2(self.target_height(tgt) * .35, max(dist, 1)))

    # ---------- geometry ----------
    def zone_now(self, z, t):
        c0, r0 = z["center"], z["radius"]
        c1, r1 = z["next_center"], z["next_radius"]
        s0, s1 = z["shrink_start"], z["shrink_end"]
        if t <= s0 or s1 <= s0:
            return c0, r0
        f = min(1.0, (t - s0) / (s1 - s0))
        return [c0[0] + (c1[0] - c0[0]) * f, c0[1] + (c1[1] - c0[1]) * f], r0 + (r1 - r0) * f

    def blocked(self, x, y, z):
        if self.grid is None:
            return False
        return self.grid.top(x, y) - z > 1.3

    def steer(self, me, want_yaw, speed_frac=1.0):
        """Pick a free heading close to want_yaw. Returns (yaw, need_climb)."""
        x, y, z = me["pos"]
        if self.grid is None:
            return want_yaw, False
        best, bscore = want_yaw, -9
        for k in range(24):
            off = (k // 2 + (k % 2)) * (math.pi / 12) * (1 if k % 2 == 0 else -1)
            yaw = want_yaw + off
            cs, sn = math.cos(yaw), math.sin(yaw)
            radius = me['stats'].get('radius', self.cfg['mech'].get('radius', 2.5))
            free = True
            for d in (3.5, 6.0, 9.0):
                if any(self.blocked(x + cs * d - sn * side, y + sn * d + cs * side, z)
                       for side in (-radius, 0, radius)):
                    free = False
                    break
            if free:
                score = math.cos(off)
                if score > bscore:
                    bscore, best = score, yaw
                    if k == 0:
                        break
        if bscore < -1:  # boxed in
            return want_yaw, True
        if bscore < math.cos(1.9):  # only way is backwards: climb instead
            return want_yaw, True
        return best, False

    def wall_height_ahead(self, me, yaw):
        x, y, z = me["pos"]
        h = 0.0
        for d in (3.5, 6.0, 9.0, 14.0):
            h = max(h, self.grid.top(x + math.cos(yaw) * d, y + math.sin(yaw) * d) - z)
        return h

    # ---------- aiming ----------
    def lead(self, me, tgt, speed):
        mx, my, mz = me["pos"]
        mz += me["stats"].get("muzzle_height", 7)
        tx, ty, tz = tgt["pos"]
        tz += self.target_height(tgt) / 2
        vx, vy, vz = tgt.get("vel", [0, 0, 0])
        t = 0.0
        for _ in range(3):
            px, py, pz = tx + vx * t - mx, ty + vy * t - my, tz + vz * t - mz
            t = math.sqrt(px * px + py * py + pz * pz) / max(speed, 1)
        px, py, pz = tx + vx * t - mx, ty + vy * t - my, tz + vz * t - mz
        return math.atan2(py, px), math.atan2(pz, math.hypot(px, py)), math.sqrt(px * px + py * py + pz * pz)

    def mortar_pitch(self, dist, dz, v, g):
        v2 = v * v
        disc = v2 * v2 - g * (g * dist * dist + 2 * dz * v2)
        if disc < 0:
            return None
        return math.atan((v2 - math.sqrt(disc)) / (g * max(dist, .01)))

    def mortar_solution(self, me, target, weapon):
        """Predict moving-target intercept and reject arcs blocked by terrain."""
        v = weapon['stats'].get('speed', 72)
        g = weapon['stats'].get('gravity', 22)
        x, y, z = me['pos']
        z += me['stats'].get('height', 9) + .5
        tv = target.get('vel', [0, 0, 0])
        for high in (False, True):
            flight = target['_d'] / v
            for _ in range(5):
                dx = target['pos'][0] + tv[0] * flight - x
                dy = target['pos'][1] + tv[1] * flight - y
                dz = target['pos'][2] + self.target_height(target) / 2 - z
                d = math.hypot(dx, dy)
                disc = v ** 4 - g * (g * d * d + 2 * dz * v * v)
                if disc < 0 or d < 1:
                    break
                p = math.atan((v * v + (1 if high else -1) * math.sqrt(disc)) / (g * d))
                flight = d / max(v * math.cos(p), .01)
            else:
                if not me['stats'].get('min_pitch', -.7) <= p <= me['stats'].get('max_pitch', 1.3):
                    continue
                if flight >= weapon['stats'].get('ttl', 7):
                    continue
                yaw = math.atan2(dy, dx)
                blocked = False
                if self.grid:
                    # Ignore the first few metres occupied by our barrel and
                    # the target's final cylinder, as in straight-shot LOS.
                    for j in range(1, max(2, int(d / 4))):
                        tm = flight * j / max(2, int(d / 4))
                        dist = v * math.cos(p) * tm
                        if dist < 4 or d - dist < 3:
                            continue
                        px, py = x + math.cos(yaw) * dist, y + math.sin(yaw) * dist
                        pz = z + v * math.sin(p) * tm - .5 * g * tm * tm
                        if self.grid.top(px, py) > pz:
                            blocked = True
                            break
                if not blocked:
                    return yaw, p, math.hypot(d, dz)
        return None

    # ---------- main ----------
    def act(self, obs):
        me = obs["self"]
        t = obs["t"]
        x, y, z = me["pos"]
        act = {"tick": obs["tick"]}
        G = self.grid
        for p in obs.get("pickups", []):
            self.loot_memory[p["id"]] = (p, t)
        for ev in obs.get("events", []):
            if ev.get("type") in ("took", "take_failed"):
                self.loot_memory.pop(ev.get("id"), None)
        self.loot_memory = {pid: (p, seen) for pid, (p, seen) in self.loot_memory.items() if t - seen < 15}

        # upgrades
        if obs.get("offer"):
            act["pick"] = self.choose_upgrade(obs['offer']['choices'], me)

        # remember who hurt us
        for ev in obs.get("events", []):
            if ev.get('type') in ('hit', 'damaged'):
                self.last_combat_t = t
            if ev.get('type') == 'damaged':
                self.last_damage_t = t
            if ev.get("type") == "damaged" and ev.get("bearing") is not None:
                self.last_hit_bearing = ev["bearing"]; self.last_hit_t = t; self.last_attacker = ev.get("by")

        hp_frac = me["hp"] / max(me["max_hp"], 1)
        zc, zr = self.zone_now(obs["zone"], t)
        nc, nr = obs["zone"]["next_center"], obs["zone"]["next_radius"]
        d_zone = math.hypot(x - zc[0], y - zc[1])
        d_next = math.hypot(x - nc[0], y - nc[1])

        mechs = [m for m in obs["mechs"] if m["kind"] == "mech"]
        npcs = [m for m in obs["mechs"] if m["kind"] == "npc"]
        for m in mechs + npcs:
            m["_d"] = math.dist(m["pos"][:2], (x, y))

        # ---------- choose target ----------
        target = None
        eye = [x, y, z + me["stats"].get("muzzle_height", 7)]
        cands = []
        for m in mechs:
            if m["_d"] > 230:
                continue
            vis = G is None or G.los(eye, [m["pos"][0], m["pos"][1], m["pos"][2] + self.target_height(m) / 2])
            hpm = m.get("hp", 1400) / max(m.get("max_hp", 1400), 1)
            score = m["_d"] + 160 * hpm - (35 if m["id"] == self.last_attacker else 0) + (500 if not vis else 0) - (35 if m["id"] == self.target_id else 0)
            cands.append((score, m, vis))
        npc_attacker = self.last_attacker is not None and self.last_attacker >= 100 and t - self.last_hit_t < 3
        for m in npcs:
            if npc_attacker and m["id"] == self.last_attacker and m["_d"] < 120:
                vis = G is None or G.los(eye, [m["pos"][0], m["pos"][1], m["pos"][2] + 1.75])
                cands.append((m["_d"] + 300, m, vis))
        if not any(c[2] for c in cands) and hp_frac > 0.55:
            for m in npcs:
                if m["_d"] < 110:
                    vis = G is None or G.los(eye, [m["pos"][0], m["pos"][1], m["pos"][2] + 1.75])
                    if vis:
                        cands.append((m["_d"] + 500, m, vis))
            if not any(c[2] for c in cands):
                for c in obs.get("crates", []):
                    dd = math.dist(c["pos"][:2], (x, y))
                    if dd < 60 and abs(c["pos"][2] - z) < 12:
                        cc = {"id": c["id"], "kind": "crate", "pos": c["pos"], "vel": [0, 0, 0], "_d": dd}
                        vis = G is None or G.los(eye, [c["pos"][0], c["pos"][1], c["pos"][2] + 1.2])
                        if vis:
                            cands.append((dd + 800, cc, vis))
        if cands:
            cands.sort(key=lambda c: (not c[2], c[0]))
            target, tvis = cands[0][1], cands[0][2]
            self.target_id = target["id"]

        # ---------- weapons ----------
        weps = me["weapons"]
        fire = [False, False]
        aim_yaw, aim_pitch = None, 0.0
        if target is not None:
            gun_speed = 252
            for w in weps:
                if w and w["kind"] == "gun":
                    gun_speed = w["stats"].get("speed", 252)
            yaw, pitch, dist = self.lead(me, target, gun_speed)
            dyaw, dpitch, _ = self.lead(me, target, 1e9)
            gun_aim = (yaw, pitch)
            laser_ready = any(w and w["kind"] == "laser" and w.get("charge", 0) >= w["stats"].get("charge", 0.75) - 0.12 for w in weps)
            mortar_plan = None
            if not any(w and w['kind'] == 'laser' and w.get('charge', 0) > 0 for w in weps):
                for wi, w in enumerate(weps):
                    if w and w['kind'] == 'mortar' and w['cooldown'] <= 0 and target['_d'] > 35:
                        solution = self.mortar_solution(me, target, w)
                        if solution is not None:
                            mortar_plan = wi, solution
                            break
            if laser_ready:
                yaw, pitch = dyaw, dpitch
            elif mortar_plan is not None:
                yaw, pitch = mortar_plan[1][:2]
            aim_yaw, aim_pitch = yaw, pitch
            ay, ap = me["aim"]
            yerr = abs(wrap(ay - yaw)); perr = abs(ap - pitch)
            tol = max(0.012, math.atan2(2.0, max(dist, 1)))
            aimed = self.aligned(me, (yaw, pitch), (dyaw, dpitch), dist, target)
            for i, w in enumerate(weps):
                if not w:
                    continue
                k = w["kind"]
                if k == "gun":
                    if tvis and self.aligned(me, (yaw, pitch), gun_aim, dist, target) and dist < w["stats"].get("range", 230) and w.get("ammo", 1) > 0:
                        fire[i] = True
                elif k == "laser":
                    full = w["stats"].get("charge", 0.75)
                    ch = w.get("charge", 0)
                    if w["cooldown"] <= 0 and tvis and dist < w["stats"].get("range", 280):
                        # hitscan: aim straight at the target (no lead)
                        if ch < full - 0.02:
                            fire[i] = True  # keep charging
                        else:
                            fire[i] = not aimed  # both axes must intersect the target
                    elif ch > 0:
                        fire[i] = True
                elif k == "lightning":
                    cone = math.radians(w["stats"].get("cone_deg", 30)) / 2
                    if dist < w["stats"].get("range", 45) * 0.95 and abs(wrap(ay - dyaw)) < cone and me["energy"] > w["stats"].get("energy", 3) and tvis:
                        fire[i] = True
                elif k == "mortar":
                    if mortar_plan is not None and mortar_plan[0] == i and not laser_ready:
                        myaw, mpitch, mdist = mortar_plan[1]
                        fire[i] = self.aligned(me, (yaw, pitch), (myaw, mpitch), mdist, target)
            if me["weapons"][0] and me["weapons"][0]["kind"] == "gun" and me["weapons"][0].get("ammo", 12) == 0:
                act["reload"] = True
        else:
            for i, w in enumerate(weps):
                if w and w["kind"] == "laser" and w.get("charge", 0) > 0:
                    fire[i] = False
            g0 = weps[0]
            if g0 and g0["kind"] == "gun" and g0.get("ammo", 12) < g0["stats"].get("mag", 12) * 0.6 and g0.get("reload", 0) == 0:
                act["reload"] = True

        # ---------- movement goal ----------
        move_yaw = None
        speed = 1.0
        boost = False
        mode = "hold"
        late = nr <= 70
        # zone first
        margin_next = nr * (0.45 if not late else 0.25)
        shrink_soon = t > obs["zone"]["shrink_start"] - 25
        if d_zone > zr - 6:
            mode = "zone!"
            move_yaw = math.atan2(nc[1] - y, nc[0] - x) if d_next < d_zone else math.atan2(zc[1] - y, zc[0] - x)
            boost = True
        elif d_next > nr - margin_next * 0.2 and (shrink_soon or d_next > nr):
            mode = "to-next"
            move_yaw = math.atan2(nc[1] - y, nc[0] - x)
            # time pressure: boost if the circle will close on us
            boost = d_next - nr > 0 and t > obs["zone"]["shrink_start"] - 8
        elif late and d_next > nr * 0.35:
            mode = "center"
            move_yaw = math.atan2(nc[1] - y, nc[0] - x)

        # Once zone movement is required, loot must not overwrite its goal.
        # Supply drops, weapons and cubes are considered only in hold mode.
        # supply drop
        if mode == "hold":
            for dr in obs.get("drops", []):
                dd = math.dist(dr["pos"][:2], (x, y))
                in_next = math.dist(dr["pos"][:2], nc) < nr
                if dd < 160 and dr["lands_in"] < dd / 12 + 2 and in_next:
                    contested = any(math.dist(m["pos"][:2], dr["pos"][:2]) < 70 for m in mechs)
                    if not contested and (dr["holder"] in (None, me["id"])):
                        mode = "drop"
                        if dd > 3:
                            move_yaw = math.atan2(dr["pos"][1] - y, dr["pos"][0] - x)
                            boost = dd > 30
                        else:
                            move_yaw = None
                        break

        # loot: second weapon / better weapon
        if mode == "hold":
            have = {w["kind"] for w in weps if w}
            slot1 = weps[1]
            cur = WEAPON_PRIO.get(slot1["kind"], 0) if slot1 else -1
            best = None
            for p, seen in self.loot_memory.values():
                if p["kind"] in have:
                    continue
                pr = WEAPON_PRIO.get(p["kind"], 0)
                if pr <= cur:
                    continue
                dd = math.dist(p["pos"][:2], (x, y))
                if target is not None and target["kind"] == "mech" and target["_d"] < 70 and dd > 18:
                    continue
                if dd > (140 if slot1 is None else 70) or abs(p["pos"][2] - z) > 25:
                    continue
                if math.dist(p["pos"][:2], nc) > nr + 20 and mode != "hold":
                    continue
                key = dd - 40 * pr
                if best is None or key < best[0]:
                    best = (key, p, dd)
            if best:
                p, dd = best[1], best[2]
                if dd < self.reach - 0.3 and abs(p["pos"][2] - z) < 3.5:
                    act["take"] = p["id"]
                    if slot1 is not None:
                        act["drop"] = 1
                else:
                    mode = "loot"
                    move_yaw = math.atan2(p["pos"][1] - y, p["pos"][0] - x)
                    speed = 1.0 if dd > 8 else 0.5
                    boost = dd > 25 and me["energy"] > 65 and (target is None or target.get("kind") != "mech")

        # Cubes grant HP/damage below the stat cap and upgrade progress above it.
        if mode == "hold" and obs.get("cubes"):
            reachable_cubes = [c for c in obs["cubes"] if abs(c["pos"][2] - z) < 4 and math.dist(c["pos"][:2], zc) < zr - 5]
            cb = min(reachable_cubes, key=lambda c: math.dist(c["pos"][:2], (x, y)), default=None)
            if cb is None:
                cb = {"pos": [x + 10000, y, z]}
            dd = math.dist(cb["pos"][:2], (x, y))
            if dd < 70 and (dd < 18 or target is None or target["kind"] != "mech" or target["_d"] > 65):
                mode = "cube"
                move_yaw = math.atan2(cb["pos"][1] - y, cb["pos"][0] - x)

        # Search the arena instead of waiting at an empty spawn. A complete
        # sweep reveals loot outside the initial 120-degree vision wedge.
        if mode == "hold" and target is None:
            mode = "explore"
            phase = t * .22 + me["id"] * 1.7
            roam_radius = min(nr * .45, 90)
            gx = nc[0] + math.cos(phase * .18) * roam_radius
            gy = nc[1] + math.sin(phase * .18) * roam_radius
            move_yaw = math.atan2(gy - y, gx - x)
            aim_yaw = wrap(phase)

        # ---------- combat movement ----------
        if target is not None and target["kind"] == "mech":
            ty = math.atan2(target["pos"][1] - y, target["pos"][0] - x)
            d = target["_d"]
            if t > self.strafe_until:
                self.strafe_dir = random.choice((-1, 1))
                self.strafe_until = t + random.uniform(0.5, 1.3)
            side = ty + self.strafe_dir * math.pi / 2
            want_far = hp_frac < 0.35 or (target.get("hp", 0) > me["hp"] * 1.6)
            pref = 150 if want_far else 90
            using_lightning = not want_far and any(w and w["kind"] == "lightning" for w in weps)
            if using_lightning:
                pref = next(w["stats"].get("range", 45) * .70 for w in weps if w and w["kind"] == "lightning")
            # Keep laser builds outside an opponent's shorting range. A build
            # carrying lightning must enter its own range to use both weapons.
            if "lightning" in target.get("weapons", []) and not using_lightning:
                pref = max(pref, 70)
            radial_band = 5 if using_lightning else 15
            radial = 0.0
            if hp_frac < 0.3:
                radial = -1.0
                boost = me["energy"] > 25
            elif d < pref - radial_band:
                radial = -0.8
            elif d > pref + radial_band and not want_far and mode == "hold":
                radial = 1.1
            mx = math.cos(side) + radial * math.cos(ty)
            my_ = math.sin(side) + radial * math.sin(ty)
            cyaw = math.atan2(my_, mx)
            if move_yaw is None or mode == "hold":
                move_yaw = cyaw
            else:
                # blend zone goal with dodging
                dodge_weight = 0.15 if mode in ("cube", "loot", "drop") else 0.8
                bx = math.cos(move_yaw) * 1.4 + math.cos(side) * dodge_weight
                by = math.sin(move_yaw) * 1.4 + math.sin(side) * dodge_weight
                move_yaw = math.atan2(by, bx)
            # incoming laser telegraph: dodge hard
            if target.get("charging"):
                to_me = math.atan2(y - target["pos"][1], x - target["pos"][0])
                if abs(wrap(target["aim"][0] - to_me)) < 0.15:
                    boost = me["energy"] > 20
            if mode == "hold":
                mode = "fight"

        # evade NPCs we are not fighting
        if move_yaw is not None or mode == "hold":
            rx = ry = 0.0
            for n in npcs:
                if n["_d"] < 55 and not (target is not None and n["id"] == target["id"]):
                    w_ = (55 - n["_d"]) / 55
                    rx += (x - n["pos"][0]) / max(n["_d"], 1) * w_ * 2
                    ry += (y - n["pos"][1]) / max(n["_d"], 1) * w_ * 2
            if rx or ry:
                if move_yaw is None:
                    move_yaw = math.atan2(ry, rx)
                else:
                    move_yaw = math.atan2(math.sin(move_yaw) + ry, math.cos(move_yaw) + rx)

        # Disengage only toward reachable cover, and keep firing while crossing
        # open ground. Once protected, stop damage (including crate farming)
        # so the game's regeneration timer can actually complete.
        recovery = self.healing_goal(obs, zc, zr, mode)
        if recovery is not None:
            goal, protected = recovery
            if protected:
                mode = 'heal'
                move_yaw = None
                speed = 0.0
                boost = False
                fire = [False, False]
            else:
                mode = 'cover'
                move_yaw = math.atan2(goal[1] - y, goal[0] - x)
                speed = 1.0
                boost = me['energy'] > 40
                # Releasing fires the beam; it does NOT cancel the shot. A
                # discharge may reset regeneration via a later hit event,
                # but removes the charge's half-speed movement immediately.
                for slot, weapon in enumerate(weps):
                    if weapon and weapon['kind'] == 'laser':
                        fire[slot] = False

        # ---------- steering / stuck handling ----------
        jump = False
        vel = me["vel"]
        hspeed = math.hypot(vel[0], vel[1])
        if move_yaw is not None and G is not None:
            if t < self.unstick_until:
                move_yaw = self.unstick_dir
            syaw, climb = self.steer(me, move_yaw)
            if climb and self.wall_height_ahead(me, move_yaw) < 50 and me["energy"] > 35:
                self.climb_until = t + 2.5
            if t < self.climb_until:
                syaw = move_yaw
                if me["grounded"]:
                    jump = True
                boost = True  # jets in the air
            move_yaw = syaw
            if me["grounded"] and hspeed < 1.5 and t > 1.0:
                self.stuck_t += 0.1
            else:
                self.stuck_t = max(0.0, self.stuck_t - 0.05)
            if self.stuck_t > 0.8:
                jump = True
                if self.stuck_t > 2.0 and t > self.unstick_until:
                    self.unstick_dir = move_yaw + random.uniform(-2.2, 2.2)
                    self.unstick_until = t + 1.5
                    self.stuck_t = 0.0
        if boost and me["energy"] < 15 and d_zone < zr - 6:
            boost = False

        if move_yaw is not None:
            act["move"] = [math.cos(move_yaw) * speed, math.sin(move_yaw) * speed]
        else:
            act["move"] = [0, 0]
        act["boost"] = bool(boost)
        act["jump"] = jump

        # ---------- aim / look ----------
        if aim_yaw is None and mechs:
            nm = min(mechs, key=lambda m: m["_d"])
            aim_yaw = math.atan2(nm["pos"][1] - y, nm["pos"][0] - x)
            aim_pitch = 0.0
        if aim_yaw is None:
            if t - self.last_hit_t < 3 and self.last_hit_bearing is not None:
                aim_yaw = self.last_hit_bearing
            else:
                snd = [s for s in obs.get("sounds", []) if s["kind"] in ("gun", "laser", "laser_charge", "lightning", "step", "boost", "mortar", "jet")]
                if snd:
                    near = min(snd, key=lambda s: {"near": 0, "mid": 1, "far": 2}[s["dist"]])
                    aim_yaw = near["bearing"]
                elif mode in ("hold", "explore"):
                    self.scan_phase += 0.1
                    base = math.atan2(nc[1] - y, nc[0] - x) + math.pi if d_next > 20 else 0.0
                    aim_yaw = base + math.sin(self.scan_phase * 0.6) * 1.6
                elif move_yaw is not None:
                    aim_yaw = move_yaw
                else:
                    aim_yaw = me["aim"][0]
            aim_pitch = 0.0
        act["aim"] = [aim_yaw, aim_pitch]
        act["fire"] = fire
        mf.draw.label(f"{mode} hp{me['hp']:.0f}")
        return act


if __name__ == "__main__":
    mf.run(Bot)
