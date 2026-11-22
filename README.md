# Mech Royale survivor-v8

Unscored candidate based on survivor-v6 (practice score 4.46). V7 scored 4.35; its crossfire steering is not included. No claim of a score above 7 or a tournament win.

## Changes
- Native arena geometry: exact prism spans and polygon holes, bilinear terrain, full cylinder clearance using current body size. No lossy grid.bin dependency.
- Exposed-body targeting: choose a clear center, upper-body, or lower-body point; preserve that height in aiming and alignment.
- Equipped weapon ranges, with direct distance for laser/lightning and intercept distance for gun.
- Release unusable laser charges to remove the half-speed movement penalty.
- Mortar interception includes tube offset and observed vertical velocity; validate flight arcs against native geometry.
- Incoming projectile evasion: acceleration-aware short-horizon cylinder/trajectory prediction, cover clipping and zone constraints. Bounded 8 ms search, preserving original movement when no collision is predicted or the prediction budget expires.

## Runtime
The scorer supplies /arena/v2/world.json and terrain.i16 through hello.map.dir. Python standard library only. Build the root Dockerfile for linux/amd64. The image runs with no network, one CPU, 512 MB, read-only filesystem and uid 65534.

## Verification
68 scenario/regression tests pass. Official check/check.py: 300/300 actions on time, no malformed or wrong-tick outputs. Container-backed observed response p99 4.5 ms, max 5.6 ms. Artificial 400-decision stress (15 enemies, 24 bullets): p99 23.793 ms, max 30.107 ms. Full details in VALIDATION.json.

Tests establish selected behaviors and measured runtime, not placement. The official recorded checker is non-reactive. Geometry movement uses sampled terrain/body clearance, not a complete server physics replica; projectile prediction assumes short-horizon movement and observed target velocities. Match performance must be scored on the competition site.

## Submission
Upload all seven files in this folder to the root of abirthare-ab/mech-royale-bot and commit. Submit the repository on the Builders/Mech Royale track. Keep the existing 4.46 entry selected unless this candidate scores higher.
