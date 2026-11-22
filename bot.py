"""Survivor bot for mechfight/1: placement is what scores, so stay alive.
- stay inside the zone with margin, prefer the outer part of the next circle (away from the crowd)
- face every threat (rear hits deal x1.5), strafe to dodge, shoot back with lead
- grab a second weapon early, pick defensive upgrades, take supply drops when it is safe
- steer around buildings with a precomputed 2 m height grid, climb with jets when needed
"""
import math, os, random, struct, array
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


UPGRADE_PRIO = ["armor", "plating", "nanites", "laser_damage", "laser_charge", "gun_damage", "gun_rate",
                "lightning_efficiency", "lightning_range", "sensors", "gun_mag", "gun_velocity", "servos",
                "capacitor", "reactor", "jets", "gyros", "mortar_reload", "mortar_splash", "mortar_velocity",
                "laser_pierce", "lightning_chain"]
WEAPON_PRIO = {"laser": 3, "lightning": 2, "mortar": 1, "gun": 0}


class Bot:
    name = "survivor"

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
            free = True
            for d in (3.5, 6.0, 9.0):
                if self.blocked(x + cs * d, y + sn * d, z):
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
        tz += 4.5 if tgt.get("kind") == "mech" else 1.75
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
        return math.atan((v2 + math.sqrt(disc)) / (g * dist))  # high arc: lobs over cover

    # ---------- main ----------
    def act(self, obs):
        me = obs["self"]
        t = obs["t"]
        x, y, z = me["pos"]
        act = {"tick": obs["tick"]}
        G = self.grid

        # upgrades
        if obs.get("offer"):
            ch = [c["id"] for c in obs["offer"]["choices"]]
            best = min(range(len(ch)), key=lambda i: UPGRADE_PRIO.index(ch[i]) if ch[i] in UPGRADE_PRIO else 99)
            act["pick"] = best

        # remember who hurt us
        for ev in obs.get("events", []):
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
        recent_hit = t - self.last_hit_t < 4
        for m in mechs:
            attacker = recent_hit and m["id"] == self.last_attacker
            close = m["_d"] < (70 if hp_frac > 0.5 else 45)
            late_game = obs["alive"] <= 3
            if m["_d"] > 230 or not (attacker or close or late_game or m.get("hp", 1400) < 250):
                continue
            vis = G is None or G.los(eye, [m["pos"][0], m["pos"][1], m["pos"][2] + 4.5])
            hpm = m.get("hp", 1400) / max(m.get("max_hp", 1400), 1)
            score = m["_d"] + 120 * hpm - (60 if m["id"] == self.last_attacker else 0) - (80 if not vis else 0) * -1
            cands.append((score, m, vis))
        npc_attacker = self.last_attacker is not None and self.last_attacker >= 100 and t - self.last_hit_t < 3
        for m in npcs:
            if npc_attacker and m["id"] == self.last_attacker and m["_d"] < 120:
                vis = G is None or G.los(eye, [m["pos"][0], m["pos"][1], m["pos"][2] + 1.75])
                cands.append((m["_d"] + 300, m, vis))
        if cands:
            cands.sort(key=lambda c: c[0])
            target, tvis = cands[0][1], cands[0][2]

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
            aim_yaw, aim_pitch = yaw, pitch
            ay, ap = me["aim"]
            yerr = abs(wrap(ay - yaw)); perr = abs(ap - pitch)
            tol = max(0.012, math.atan2(2.0, max(dist, 1)))
            aimed = yerr < tol and perr < 0.06
            for i, w in enumerate(weps):
                if not w:
                    continue
                k = w["kind"]
                if k == "gun":
                    if tvis and aimed and dist < w["stats"].get("range", 230) and w.get("ammo", 1) > 0:
                        fire[i] = True
                elif k == "laser":
                    full = w["stats"].get("charge", 0.75)
                    ch = w.get("charge", 0)
                    if w["cooldown"] <= 0 and tvis and dist < w["stats"].get("range", 280):
                        # hitscan: aim straight at the target (no lead)
                        if ch < full - 0.02:
                            fire[i] = True  # keep charging
                        else:
                            fire[i] = not (aimed or yerr < 0.02)  # release when on target
                    elif ch > 0 and ch < full:
                        fire[i] = True
                elif k == "lightning":
                    cone = math.radians(w["stats"].get("cone_deg", 30)) / 2
                    if dist < w["stats"].get("range", 45) * 0.95 and yerr < cone and me["energy"] > 10 and tvis:
                        fire[i] = True
                elif k == "mortar":
                    if 35 < dist < 220 and w["cooldown"] <= 0:
                        v = w["stats"].get("speed", 72); g = w["stats"].get("gravity", 22)
                        dz = target["pos"][2] - (z + me["stats"].get("height", 9) + 0.5)
                        p = self.mortar_pitch(target["_d"], dz, v, g)
                        if p is not None and p < self.cfg["mech"].get("max_pitch", 1.3):
                            if not any(fire):  # only lob when gun is not firing this tick
                                aim_pitch = p
                                if abs(ap - p) < 0.05 and yerr < 0.04:
                                    fire[i] = True
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
        elif d_next > nr * 0.72 and (shrink_soon or d_next > nr):
            mode = "to-next"
            move_yaw = math.atan2(nc[1] - y, nc[0] - x)
            # time pressure: boost if the circle will close on us
            boost = d_next - nr > 0 and t > obs["zone"]["shrink_start"] - 8
        elif late and d_next > nr * 0.35:
            mode = "center"
            move_yaw = math.atan2(nc[1] - y, nc[0] - x)

        # supply drop
        if mode == "hold" or mode == "to-next":
            for dr in obs.get("drops", []):
                dd = math.dist(dr["pos"][:2], (x, y))
                in_next = math.dist(dr["pos"][:2], nc) < nr
                if dd < 160 and dr["lands_in"] < dd / 12 + 2 and in_next and hp_frac > 0.45:
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
        if mode in ("hold", "to-next") and (target is None or target["_d"] > 120):
            have = {w["kind"] for w in weps if w}
            slot1 = weps[1]
            cur = WEAPON_PRIO.get(slot1["kind"], 0) if slot1 else -1
            best = None
            for p in obs.get("pickups", []):
                if p["kind"] in have:
                    continue
                pr = WEAPON_PRIO.get(p["kind"], 0)
                if pr <= cur:
                    continue
                dd = math.dist(p["pos"][:2], (x, y))
                if dd > 55 or abs(p["pos"][2] - z) > 15:
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

        # cubes nearby: free HP
        if mode == "hold" and obs.get("cubes"):
            cb = min(obs["cubes"], key=lambda c: math.dist(c["pos"][:2], (x, y)))
            dd = math.dist(cb["pos"][:2], (x, y))
            if dd < 40 and (target is None or target["_d"] > 100):
                mode = "cube"
                move_yaw = math.atan2(cb["pos"][1] - y, cb["pos"][0] - x)

        # ---------- evade: stay unseen, let others fight ----------
        threats = [m for m in mechs if m["_d"] < 220 and (target is None or m["id"] != target["id"])]
        tx_ = ty_ = 0.0
        for m in threats:
            w_ = (220 - m["_d"]) / 220 + 0.3
            tx_ += (x - m["pos"][0]) / max(m["_d"], 1) * w_
            ty_ += (y - m["pos"][1]) / max(m["_d"], 1) * w_
        for sd in obs.get("sounds", []):
            if sd["kind"] in ("gun", "laser", "lightning", "explosion", "mortar", "step", "boost", "jet", "laser_charge") and sd["dist"] in ("near", "mid") and sd["age"] < 0.6:
                w_ = 0.8 if sd["dist"] == "near" else 0.35
                tx_ -= math.cos(sd["bearing"]) * w_
                ty_ -= math.sin(sd["bearing"]) * w_
        if (tx_ or ty_) and target is None and mode in ("hold", "to-next", "cube", "loot", "center"):
            fy = math.atan2(ty_, tx_)
            if mode == "hold" or mode == "cube" or mode == "loot":
                move_yaw = fy
                mode = "evade"
            else:
                move_yaw = math.atan2(math.sin(move_yaw) * 1.5 + math.sin(fy), math.cos(move_yaw) * 1.5 + math.cos(fy))
            # never evade out of the safe circle: slide along it instead
            px, py = x + math.cos(move_yaw) * 25, y + math.sin(move_yaw) * 25
            lim = nr * (0.85 if not late else 0.6)
            if math.hypot(px - nc[0], py - nc[1]) > lim:
                inward = math.atan2(nc[1] - y, nc[0] - x)
                a1, a2 = inward + math.pi / 2, inward - math.pi / 2
                move_yaw = a1 if abs(wrap(a1 - move_yaw)) < abs(wrap(a2 - move_yaw)) else a2
                if math.hypot(x - nc[0], y - nc[1]) > lim:
                    move_yaw = math.atan2(math.sin(move_yaw) + math.sin(inward) * 1.5, math.cos(move_yaw) + math.cos(inward) * 1.5)
            boost = boost or (threats and min(m["_d"] for m in threats) < 90 and me["energy"] > 40)

        # ---------- combat movement ----------
        if target is not None and target["kind"] == "mech":
            ty = math.atan2(target["pos"][1] - y, target["pos"][0] - x)
            d = target["_d"]
            if t > self.strafe_until:
                self.strafe_dir = random.choice((-1, 1))
                self.strafe_until = t + random.uniform(0.5, 1.3)
            side = ty + self.strafe_dir * math.pi / 2
            want_far = hp_frac < 0.45 or (target.get("hp", 0) > me["hp"] * 1.3)
            pref = 150 if want_far else 90
            # short-range weapon users: keep away from lightning (45 m)
            if "lightning" in target.get("weapons", []):
                pref = max(pref, 70)
            radial = 0.0
            if want_far:
                radial = -1.0
            elif d < pref - 25:
                radial = -0.8
            elif d > pref + 60 and not want_far and mode == "hold":
                radial = 0.4
            mx = math.cos(side) + radial * math.cos(ty)
            my_ = math.sin(side) + radial * math.sin(ty)
            cyaw = math.atan2(my_, mx)
            if move_yaw is None or mode == "hold":
                move_yaw = cyaw
            else:
                # blend zone goal with dodging
                bx = math.cos(move_yaw) * 1.4 + math.cos(side) * 0.8
                by = math.sin(move_yaw) * 1.4 + math.sin(side) * 0.8
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
                elif mode in ("hold",):
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


mf.run(Bot)
