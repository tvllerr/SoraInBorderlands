# Per-frame Sora logic. Hot-reloaded by __init__.py whenever this file changes (dev convenience).
# Motion numbers and timings come from KH1's own motion event tracks (data/sora_events.json):
#   hit = hitbox on/off frames, combo = next-input window, release = spell release frame (60 fps).
import ctypes
import os
import json
import math
import random
import struct
import time
from pathlib import Path

import unrealsdk  # type: ignore

UU = math.pi / 32768.0  # unreal rotator units -> radians
HERE = Path(__file__).parent
# Debug traces/dumps (text files next to logic.py) are OFF for players. config.json turns them on: "dbg_files": true for
# all of them, or one key (e.g. "dbg_combo_trace": true). 1.0.3 and older wrote them for everyone, every fight.
DBG_FILES = {"dbg_juggle_trace": "juggle_trace.txt", "dbg_spawn_trace": "spawn_trace.txt",
             "dbg_combo_trace": "combo_trace.txt", "dbg_air_trace": "air_trace.txt",
             "dbg_lock_trace": "lock_cam_trace.txt", "dbg_hit_trace": "hit_trace.txt",
             "dbg_summon_trace": "summon_trace.txt", "dbg_ability_trace": "ability_trace.txt",
             "dbg_cam_log": "cam_log.txt", "dbg_party_hp_log": "party_hp_log.txt",
             "dbg_party_trace": "party_trace.txt", "dbg_glide_trace": "glide_trace.txt",
             "dbg_damage_probe": "damage_probe.txt", "dbg_fields_dump": "fields.txt"}


def dbg_on(S, key: str) -> bool:
    """True when config.json asks for this debug trace/dump (its own key wins over "dbg_files")."""
    return bool(S.CFG.get(key, S.CFG.get("dbg_files", False)))


def dbg_clean_old(S) -> None:
    """Once per session: delete the trace files old versions left behind (only the ones that are switched off)."""
    if S.state.get("dbg_cleaned"):
        return
    S.state["dbg_cleaned"] = True
    for key, name in DBG_FILES.items():
        if not dbg_on(S, key):
            try:
                (HERE / name).unlink(missing_ok=True)
            except OSError:
                pass
BLEND_TIME = 0.15


def ang(d: float) -> float:
    return (d + math.pi) % (2 * math.pi) - math.pi


def events(S):
    if not hasattr(S, "events"):
        try:
            S.events = json.loads((HERE / "data" / "sora_events.json").read_text())
        except Exception:  # noqa: BLE001
            S.events = []
    return S.events


def ev(S, motion: int) -> dict:
    E = events(S)
    return E[motion] if 0 <= motion < len(E) and E[motion] else {"frames": 60, "fps": 60.0}


# ---------------------------------------------------------------- animation state (with crossfade)
def play(S, motion: int, restart: bool = False) -> None:
    """Switch the displayed motion; the previous one fades out over BLEND_TIME."""
    st = S.state
    if st["anim"] == motion and not restart:
        return
    now = time.time()
    st["prev"] = (st["anim"], anim_time(S, now), now)
    st["anim"] = motion
    st["t0"] = now
    st["phase"] = 0.0


def anim_time(S, now: float) -> float:
    st = S.state
    if st.get("loco"):
        return st.get("phase", 0.0)
    return now - st["t0"]


def field_dump(objs, path) -> None:
    lines = []
    for label, obj in objs:
        lines.append(f"== {label}: {obj}")
        try:
            cls = obj.Class
            while cls is not None:
                for f in cls._fields():
                    lines.append(f"{cls.Name}.{f.Name}:{f.Class.Name}")
                cls = cls.SuperField
        except Exception as ex:  # noqa: BLE001
            lines.append(f"ERR {ex}")
    open(path, "w", encoding="utf-8").write(chr(10).join(lines))


# ---------------------------------------------------------------- enemies (cached: find_all is expensive)
_enemy_cache = {"t": 0.0, "list": []}


def all_enemies():
    """AI pawns from WorldInfo.PawnList (the game's own list of live pawns). unrealsdk.find_all walks every object
    in the game; doing that every half second crashed Borderlands (General protection fault) when it ran while the
    game was destroying dead enemies' corpses. Re-read every 0.1 s so no destroyed pawn is held for long."""
    now = time.time()
    if now - _enemy_cache["t"] > 0.1:
        _enemy_cache["t"] = now
        out = []
        try:
            from mods_base import get_pc  # type: ignore
            pc = get_pc()
            p = pc.WorldInfo.PawnList if pc is not None else None
            n = 0
            while p is not None and n < 512:
                n += 1
                try:
                    if p is not pc.Pawn and not bool(p.bDeleteMe) and "AIPawn" in str(p.Class.Name):
                        out.append(p)
                except Exception:  # noqa: BLE001
                    pass
                p = p.NextPawn
        except Exception as ex:  # noqa: BLE001
            _enemy_cache["error"] = str(ex)
        _enemy_cache["list"] = out
    return _enemy_cache["list"]


def barriers_tick(S, now: float) -> None:
    """CFG no_barriers (or the F6 keybind) switches off Borderlands' invisible walls (BlockingVolume collision).
    Original collision is remembered per volume and restored when switched back. Re-scans every 5 s for newly loaded
    areas. Real level geometry is untouched (and kill volumes still kill)."""
    st = S.state
    want = bool(st.get("barriers_override", S.CFG.get("no_barriers", False)))
    if not want and not st.get("barriers_off", False):
        return                                            # nothing to do (and no find_all: see all_enemies)
    if want == st.get("barriers_off", False) and now - st.get("barriers_t", 0) < 5.0:
        return
    st["barriers_t"] = now
    saved = st.setdefault("barriers_saved", {})            # volume name -> (collide, block)
    try:
        vols = [v for v in unrealsdk.find_all("BlockingVolume", exact=False) if not str(v.Name).startswith("Default__")]
    except Exception as ex:  # noqa: BLE001
        S.status["barriers_error"] = str(ex)
        return
    changed = 0
    for v in vols:
        try:
            key = str(v)
            if want and key not in saved and (bool(v.bBlockActors) or _comp_blocks(v)):
                cc = v.CollisionComponent
                saved[key] = (bool(v.bCollideActors), bool(v.bBlockActors),
                              (bool(cc.CollideActors), bool(cc.BlockActors)) if cc is not None else None)
                S.pending.append(lambda v=v: _set_volume_collision(v, False, False, (False, False)))
                changed += 1
            elif not want and key in saved:
                vals = saved.pop(key)                           # (older versions saved 2 values)
                c, b, comp = vals[0], vals[1], (vals[2] if len(vals) > 2 else None)
                S.pending.append(lambda v=v, c=c, b=b, comp=comp: _set_volume_collision(v, c, b, comp))
                changed += 1
        except Exception:  # noqa: BLE001
            continue
    if not want:
        saved.clear()
    st["barriers_off"] = want
    S.status["barriers"] = {"off": want, "volumes": len(vols), "changed": changed}


def barriers_probe(S, pc) -> None:
    """Debug (config {"dbg_block": N}): every colliding+blocking actor within 800 UU of the player -> blockers.txt."""
    me = vec3(pc.Pawn.Location)
    rows, classes = [], {}
    for a in unrealsdk.find_all("Actor", exact=False):
        try:
            if str(a.Name).startswith("Default__") or a is pc.Pawn or not (a.bCollideActors and a.bBlockActors):
                continue
            loc = a.Location
            d = math.dist(me, (float(loc.X), float(loc.Y), float(loc.Z)))
            if d > 800:
                continue
            cls = str(a.Class.Name)
            classes[cls] = classes.get(cls, 0) + 1
            cc = a.CollisionComponent
            flags = [bool(cc.CollideActors), bool(cc.BlockActors), bool(cc.BlockNonZeroExtent)] if cc is not None else None
            rows.append((round(d), cls, str(a.Name), flags, str(cc.Class.Name) if cc is not None else None))
        except Exception:  # noqa: BLE001
            continue
    rows.sort()
    (HERE / "blockers.txt").write_text(json.dumps({"classes": classes, "nearest": rows[:40]}, indent=1))


def _comp_blocks(v) -> bool:
    cc = v.CollisionComponent
    return cc is not None and bool(cc.BlockActors)


def _set_volume_collision(v, collide: bool, block: bool, comp) -> None:
    """Actor flags AND the brush component (the component is what actually stops the player)."""
    v.SetCollision(collide, block, False)
    cc = v.CollisionComponent
    if cc is not None and comp is not None:
        try:
            cc.SetActorCollision(comp[0], comp[1])
        except Exception:  # noqa: BLE001
            cc.CollideActors, cc.BlockActors = comp[0], comp[1]
        cc.BlockNonZeroExtent = comp[1]
        cc.BlockZeroExtent = comp[1]


def barriers_toggle(S) -> None:
    st = S.state
    st["probe_at"] = time.time() + 1.5                      # record what still blocks you here AFTER the toggle
    st["barriers_override"] = not bool(st.get("barriers_override", S.CFG.get("no_barriers", False)))
    st["barriers_t"] = 0
    say(S, "Barriers OFF" if st["barriers_override"] else "Barriers ON")


def enemies(S, pc, reach: float, origin=None):
    pawn = pc.Pawn
    me = origin or vec3(pawn.Location)
    out = []
    for p in all_enemies():
        try:
            if p is pawn or hp(p) <= 0:
                continue
            d = p.Location
            dx, dy, dz = float(d.X) - me[0], float(d.Y) - me[1], float(d.Z) - me[2]
            dist = math.sqrt(dx * dx + dy * dy)
            if dist < reach and abs(dz) < 300:
                out.append((dist, p, math.atan2(dy, dx)))
        except Exception:  # noqa: BLE001
            continue
    out.sort(key=lambda e: e[0])
    return out


def hp(p) -> float:
    """Health read from the HealthPool property (calling GetHealth() on BL1E pawns crashed the game)."""
    try:
        pool = p.HealthPool.Data
        return float(pool.CurrentValue) if pool is not None else 0.0
    except Exception:  # noqa: BLE001
        return 0.0      # no health pool -> dead / not damageable


def vec3(v):
    return [float(v.X), float(v.Y), float(v.Z)]


# ---------------------------------------------------------------- combat
def start_action(S, kind: str, motion: int, speed: float = 1.0, **extra) -> None:
    e = ev(S, motion)
    st = S.state
    fps = e.get("fps", 60.0) * speed
    st["action"] = {"kind": kind, "motion": motion, "t0": time.time(), "fps": fps,
                    "len": e["frames"] / fps, "hit_done": False, "ev": e, **extra}
    play(S, motion, restart=True)


def e_frames_buffer(a) -> float:
    return a["ev"]["frames"] * 0.2


def attack(S, pc) -> None:
    """Left click (the hook in __init__.py calls this). KH1 controls (CFG kh_click_menu, default on): left click USES
    the selected command - Attack swings, Magic/Items open, a spell casts - like X in KH1."""
    if S.CFG.get("kh_click_menu", True):
        kh_confirm(S, pc)
        return
    kh_attack(S, pc)


def menu_confirm(S, pc) -> None:
    """Right click (hook in __init__.py). KH1 controls: Guard. Old controls: confirm the menu."""
    if S.CFG.get("kh_click_menu", True):
        kh_guard(S, pc)
        return
    kh_confirm(S, pc)


def kh_guard(S, pc) -> None:
    """KH1 Guard: Square with the stick neutral (a roll needs the stick held)."""
    st, now = S.state, time.time()
    if not kh1_combat_on(S):
        return
    if now < st.get("kh_guard_until", 0) - 0.5:
        return
    # no Guard equipped (slot 7: not learned yet) -> KH1 rolls on Square: right click does nothing (user 2026-10-12)
    base = int.from_bytes(kh1_mem(S, KH_CHAR_TABLE, 8) or bytes(8), "little")
    ab = kh1_abs(S, base + 0x44, 0x30) if base else None
    if not ab or 0x15 not in ab:
        S.status["kh_guard"] = "no Guard ability equipped"
        return
    st["kh_guard_until"] = now + S.CFG.get("kh_guard_time", 0.9)
    kh1_press(S, PAD_SQUARE, 0.12, delay=0.03)


def kh_roll_begin(S, pawn) -> None:
    st = S.state
    try:
        if "kh_crouch_pct" not in st:
            st["kh_crouch_pct"] = float(pawn.CrouchedPct)
        pawn.CrouchedPct = 1.0                              # no crouch speed cap while rolling
    except Exception:  # noqa: BLE001
        pass


def _kh_crouch(S, pawn, on: bool) -> None:
    st = S.state
    st["kh_own_crouch"] = True                              # (let it past the ShouldCrouch block)
    try:
        pawn.ShouldCrouch(on)
    except Exception:  # noqa: BLE001
        pass
    finally:
        st["kh_own_crouch"] = False


def kh_auto_crouch_tick(S, pawn, now: float) -> None:
    """LOW CEILINGS (user 2026-10-12: Fyrestone's start has a wall you must crouch under; crouching on EVERY roll made
    roll spam crouch / stand all the time). While rolling, BL traces look ahead: blocked at standing head height but
    clear at crouch height -> crouch (BL collision 120 instead of 180). Stand up again once there's headroom overhead
    for kh_stand_clear_s (UE3 also refuses to stand while blocked)."""
    st, CFG = S.state, S.CFG
    if not CFG.get("kh_auto_crouch", True) or not bl_trace_ok(S):
        return
    try:
        L, c = pawn.Location, pawn.CylinderComponent
        hh, ch, r = float(c.CollisionHeight), float(pawn.CrouchHeight), float(c.CollisionRadius)
        crouched = bool(pawn.bIsCrouched)
    except Exception:  # noqa: BLE001
        return
    x, y, z = float(L.X), float(L.Y), float(L.Z)
    if not st.get("kh_auto_crouch"):
        if not st.get("kh_roll_active") or crouched:
            return
        d = st.get("kh_roll_dir", 0.0)
        reach = r + CFG.get("kh_crouch_look", 140.0)
        fx, fy = math.cos(d) * reach, math.sin(d) * reach
        feet = z - hh
        head = bl_trace(S, (x, y, feet + 2 * hh - 8), (x + fx, y + fy, feet + 2 * hh - 8))
        low = bl_trace(S, (x, y, feet + 2 * ch - 12), (x + fx, y + fy, feet + 2 * ch - 12))
        if head and not low:
            st["kh_auto_crouch"], st["kh_stand_ok_t"] = True, 0.0
            st["kh_auto_crouches"] = st.get("kh_auto_crouches", 0) + 1
            S.pending.append(lambda pawn=pawn: _kh_crouch(S, pawn, True))
        return
    if st.get("kh_roll_active") and now < st.get("kh_roll_until", 0) - 0.15:
        return                                              # mid-roll under the wall: stay low
    feet = z - (ch if crouched else hh)
    blocked = any(bl_trace(S, (x + ox, y + oy, feet + 2 * ch - 4), (x + ox, y + oy, feet + 2 * hh + 6))
                  for ox, oy in ((0, 0), (r, 0), (-r, 0), (0, r), (0, -r)))
    if blocked:
        st["kh_stand_ok_t"] = 0.0
        return
    if not st.get("kh_stand_ok_t"):
        st["kh_stand_ok_t"] = now
    if now - st["kh_stand_ok_t"] >= CFG.get("kh_stand_clear_s", 0.1):
        st["kh_auto_crouch"] = False
        S.pending.append(lambda pawn=pawn: _kh_crouch(S, pawn, False))


def kh_roll_push(pawn, vx, vy) -> None:
    v = pawn.Velocity
    pawn.Velocity = unrealsdk.make_struct("Vector", X=vx, Y=vy, Z=float(v.Z))


def kh_roll_end(S, pawn, pc) -> None:
    st = S.state
    try:
        pawn.CrouchedPct = st.pop("kh_crouch_pct", 0.5)
    except Exception:  # noqa: BLE001
        pass
    try:
        pc.bDuck = 0                                        # the roll used the crouch press (no lingering crouch)
    except Exception:  # noqa: BLE001
        pass


def kh_attack(S, pc) -> None:
    """Keyblade: next hit of the combo. Input inside KH1's combo window chains the next swing."""
    st, CFG = S.state, S.CFG
    kh1_press(S, PAD_CROSS)            # passthrough: KH1 Sora swings too (KH1's own combo timing)
    if kh1_combat_on(S):
        face_target(S, pc)             # the hit action starts when KH1 really swings (kh1_combat_tick)
        return
    pawn = pc.Pawn
    if pawn is None:
        return
    air = int(pawn.Physics) == 2
    chain = CFG["air_combo"] if air else CFG["combo"]
    a = st.get("action")
    now = time.time()
    if a and a["kind"] == "attack":
        frame = (now - a["t0"]) * a["fps"]
        win = a["ev"].get("combo")
        if frame < a["ev"]["frames"]:
            if frame >= e_frames_buffer(a):     # buffer the next hit from early in the swing
                st["queued"] = True
            return
    if a and a["kind"] == "attack" and now - (a["t0"] + a["len"]) < 0.4 and a.get("chain") == chain:
        st["combo_i"] = (st.get("combo_i", 0) + 1) % len(chain)
    else:
        st["combo_i"] = 0
    face_target(S, pc)
    start_action(S, "attack", chain[st["combo_i"]], CFG["atk_speed"], chain=chain)


# ---------------------------------------------------------------- KH1 passthrough input (flag 128)
# kh1_bridge.dll inside KH1 replaces controller 0 with what we send (PS2 layout, verified in game 2026-09-30).
PAD_TRIANGLE, PAD_CIRCLE, PAD_CROSS, PAD_SQUARE = 0x1000, 0x2000, 0x4000, 0x8000   # examine, jump, attack/confirm, dodge
PAD_UP, PAD_RIGHT, PAD_DOWN, PAD_LEFT = 0x10, 0x20, 0x40, 0x80


KH_SUBMENU_PTR = 0x2D539F8   # u64: KH1's open command SUBMENU descriptor (Magic / Items / Summon), 0 at the top level
                             # (found 2026-10-12 by memory diff over 3 open / close cycles; checked with Items too)


def kh1_submenu_open(S) -> bool:
    b = kh1_mem(S, KH_SUBMENU_PTR, 8)
    return bool(b) and int.from_bytes(b, "little") != 0


def kh1_press(S, bits: int, hold: float = 0.1, delay: float = 0.0) -> None:
    now = time.time()
    S.state.setdefault("kh_press", {})[bits] = (now + delay, now + delay + hold)


# ---------------------------------------------------------------- KH1 combat (passthrough)
KH_SHORTCUTS = 0x2DE9BA4    # 3 bytes: spell ids on L1+Triangle / Square / Cross
KH_MAGIC_LEVELS = 0x2DE97F2  # 7 bytes: level per spell id (0 = not learned)
KH_SPELLS = [("Fire", "Fira", "Firaga"), ("Blizzard", "Blizzara", "Blizzaga"), ("Thunder", "Thundara", "Thundaga"),
             ("Cure", "Cura", "Curaga"), ("Gravity", "Gravira", "Graviga"), ("Stop", "Stopra", "Stopga"), ("Aero", "Aerora", "Aeroga")]
KH_MP_COST = (1, 1, 1, 3, 2, 2, 2)
KH_PARTY_TARGET_SPELLS = (3, 6)
KH_CHAR_TABLE = 0x2868BA0   # -> character records (0x74 B); Sora's magic unlock bits at +0x74   # Cure, Aero: KH1 asks which party member (PARTY menu after the shortcut)   # MP bars per spell id (Fire Blizzard Thunder Cure Gravity Stop Aero), KH1 Final Mix
KH_CAST_SFX = {0: ((119, 0.0), (120, 0.43)),                 # Fire: cast + fireball (KH1 capture 2026-10-05)
               1: ((122, 0.0), (123, 0.45)),                 # Blizzard
               2: ((106, 0.0), (107, 0.45)),                 # Thunder
               3: ((109, 0.0), (110, 0.30)),                 # Cure
               4: ((111, 0.0), (112, 0.30), (113, 0.60)),    # Gravity
               5: ((128, 0.0),),                             # Stop
               6: ((95, 0.0), (96, 0.30), (97, 0.60))}       # Aero
KH_CAST_ANIM = {54 + i: i for i in range(7)}   # KH1 shortcut-cast animation (entity +0x164) = 54 + spell id (verified Fire/Blizzard/Thunder)
PAD_L1 = 0x400


# Borderlands' own HUD pieces KH1's HUD replaces (clip names read from the HUD movie GfxHUD.FlashMovie.HUD, tree in
# D:/SoraBL/hud/hud_tree.txt): health + shield bars, ammo / grenades / reload, XP bar + level, compass strip, the
# class action-skill widgets. Kept: minimap (map), enemy ring, Fight For Your Life (dying*, secondwind), level-up,
# mission / training / chat text, damage direction markers, credits.
BL_HUD_HIDE = ("health", "health_icon", "shield", "bullets", "bullet_c", "bullet_icon", "reload", "xpbar", "xp",
               "level", "lvdisp", "comm", "bearing", "character", "protean", "instinctbar", "instinct",
               "crouch",   # stance icon: stuck on since C = roll and BL crouch is blocked (user 2026-10-12)
               *("grenade%d" % i for i in range(1, 20)))


def bl_hud_tick(S, pc, now: float) -> None:
    """Hide those clips while KH1's HUD is drawn (config bl_hud_hide); re-applied every 0.5 s because the HUD's
    ActionScript can show them again. Turning it off shows them once."""
    st = S.state
    want = bool(S.CFG.get("bl_hud_hide", True)) and bool(S.CFG.get("kh1_ui_native", True)) and kh1_combat_on(S)
    menu = want and bool(st.get("kh_menu_open"))         # KH1's F9 menu: all of Borderlands' HUD goes
    if not want and not st.get("bl_hud_hidden"):
        return
    if want and now - st.get("bl_hud_t", 0) < 0.5 and menu == st.get("bl_hud_menu", False):
        return
    st["bl_hud_t"], st["bl_hud_hidden"], st["bl_hud_menu"] = now, want, menu
    names = tuple(S.CFG.get("bl_hud_hide_list", BL_HUD_HIDE))

    def _apply():
        try:
            mv = pc.myHUD.HUDMovie
            if mv is None:
                return
            for n in names:
                mv.SetVariableBool("_root.p1.%s._visible" % n, not want)
            mv.SetVariableBool("_root.p1._visible", not menu)
            if S.CFG.get("bl_hide_crosshair", True) or not want:
                pc.myHUD.bCrosshairShow = not want            # KH1 has no crosshair (user 2026-10-05)
                w = pc.Pawn.Weapon if pc.Pawn is not None else None   # the weapon draws it (DrawWeaponCrosshair)
                if w is not None:
                    w.bSuppressCrosshair = want
            S.status["bl_hud"] = "hidden" if want else "shown"
        except Exception as ex:  # noqa: BLE001
            S.status["bl_hud"] = "ERR " + str(ex)
    S.pending.append(_apply)


def kh1_combat_on(S) -> bool:
    return bool(S.CFG["flags"] & 128) and S.state.get("kh_on", False) and getattr(S, "kh", None) is not None and bool(S.kh.get("h"))


def kh1_mem(S, rva: int, size: int):
    kh = getattr(S, "kh", None)
    if not kh or not kh.get("h") or not kh.get("base"):    # KH1 (re)starting: no handle yet (was a KeyError)
        return None
    buf = ctypes.create_string_buffer(size)
    ok = ctypes.windll.kernel32.ReadProcessMemory(ctypes.c_void_p(S.kh["h"]), ctypes.c_void_p(S.kh["base"] + rva), buf, size, None)
    return buf.raw if ok else None


def kh1_poke(S, rva: int, data: bytes) -> None:
    ctypes.windll.kernel32.WriteProcessMemory(ctypes.c_void_p(S.kh["h"]), ctypes.c_void_p(S.kh["base"] + rva), data, len(data), None)


def kh1_stats(S):
    import struct
    b = kh1_mem(S, KH_HP, 16)
    return struct.unpack("<4i", b) if b else None      # hp, max hp, mp, max mp


def kh1_add_mp(S, amount: float) -> None:
    """KH1 MP is whole gauge segments; fractions accumulate (Keyblade hits / slow regen refill it)."""
    stt = kh1_stats(S)
    if not stt:
        return
    _, _, mp, mpmax = stt
    acc = S.state.get("kh_mp_acc", 0.0) + amount
    gain = int(acc)
    S.state["kh_mp_acc"] = acc - gain if mp < mpmax else 0.0
    if gain and mp < mpmax:
        kh1_poke(S, KH_HP + 8, int(min(mpmax, mp + gain)).to_bytes(4, "little", signed=True))


def kh1_spells(S):
    """[(display name, spell id, level)] for the spells KH1 Sora has learned (cached 2 s)."""
    st = S.state
    if time.time() - st.get("kh_spells_t", 0) > 2.0:
        lv = kh1_mem(S, KH_MAGIC_LEVELS, 7) or b""
        unlock = 0x7F
        try:                                              # KH1's can-cast check: bit per spell in Sora's character
            cb = int.from_bytes(kh1_mem(S, KH_CHAR_TABLE, 8) or bytes(8), "little")   # record (FUN_14028efd0)
            ub = kh1_abs(S, cb + 0x74, 1) if cb else None
            if ub:
                unlock = ub[0]
        except Exception:  # noqa: BLE001
            pass
        st["kh_spells"] = [(KH_SPELLS[i][min(l, 3) - 1], i, l) for i, l in enumerate(lv) if 1 <= l <= 3 and unlock >> i & 1]
        st["kh_spells_t"] = time.time()
        S.status["kh_spells"] = [n for n, _, _ in st["kh_spells"]]
    return st.get("kh_spells", [])


def spell_list(S):
    """Rows of the Magic submenu: KH1's learned spells in passthrough mode, else the mod's own."""
    if kh1_combat_on(S):
        return [(name, 1) for name, _, _ in kh1_spells(S)]
    return SPELLS


def kh1_cast(S, pc, idx: int) -> None:
    """Casts through KH1: put the spell in the L1+Triangle shortcut, press it; KH1 plays the cast and spends MP.
    The Borderlands effect fires when KH1's cast animation starts (kh1_body_sync)."""
    st, now = S.state, time.time()
    spells = kh1_spells(S)
    if not spells:
        return
    name, sid, _ = spells[idx % len(spells)]
    stt = kh1_stats(S)
    S.status["last_cast"] = [name, "requested", round(now % 1000, 2), stt[2] if stt else None]
    # KH1 takes no new command while Sora is still casting (cast motion 54-60 + recovery 61): the command word written
    # straight into KH1 skipped that, so spam-clicking restarted casts mid-motion, cast past the MP KH1 hadn't taken yet
    # and once crashed KH1's spell code (2026-10-05). One request at a time, like the menu.
    ks = kh1_state(S)
    anim = ks.get("anim") if ks else None
    if S.CFG.get("kh_cast_command", True) and (now < st.get("kh_cmd_pending", 0) or (anim is not None and 54 <= anim <= 61)):
        S.status["last_cast"][1] = "blocked: still casting"
        return
    tpawn, tent = (None, None) if sid in KH_PARTY_TARGET_SPELLS else kh1_spell_target(S, pc)
    S.status["last_cast_target"] = hex(tent) if tent else None
    st["kh_spell"] = {"pawn": tpawn, "ent": tent, "sid": sid, "until": now + S.CFG.get("kh_spell_window", 3.0),
                      "free": isinstance(tpawn, FreeTarget)} if tpawn is not None else None
    if khfx_command(S, 0x2000000 | sid << 8 | 2, 1 if sid in KH_PARTY_TARGET_SPELLS else 3, tent or 0):
        st["kh_cmd_pending"] = now + S.CFG.get("kh_cast_pending_s", 0.6)
        # KH1's own menu path: KH1 checks unlock + MP itself and casts (Cure / Aero on Sora, attack spells at KH1's lock)
        S.status["last_cast"][1] = "kh1 command"
        st["kh_cast_busy"] = now + 0.8
        st["kh_cast_wait"] = (now + S.CFG.get("kh_cast_timeout", 0.55), KH_SPELLS[sid][0], next(l for _, i, l in spells if i == sid))
        face_target(S, pc)
        return
    if stt and stt[2] < KH_MP_COST[sid]:
        say(S, "Not enough MP")                       # KH1 refuses the cast itself below the spell's cost
        S.status["last_cast"][1] = f"blocked: MP {stt[2]} < {KH_MP_COST[sid]}"
        return
    if now < st.get("kh_cast_busy", 0):
        S.status["last_cast"][1] = "blocked: busy"
        return
    if "kh_sc_orig" not in st:
        st["kh_sc_orig"] = kh1_mem(S, KH_SHORTCUTS, 3)
    kh1_poke(S, KH_SHORTCUTS, bytes([sid]))
    st["kh_sc_restore"] = now + 1.5
    st["kh_cast_busy"] = now + 0.8
    party_target = sid in KH_PARTY_TARGET_SPELLS
    wait = S.CFG.get("kh_cast_timeout", 0.55) + (S.CFG.get("kh_target_confirm_s", 0.45) + 0.4 if party_target else 0.0)
    st["kh_cast_wait"] = (now + wait, KH_SPELLS[sid][0], next(l for _, i, l in spells if i == sid))
    face_target(S, pc)
    kh1_press(S, PAD_L1, 0.32)
    kh1_press(S, PAD_TRIANGLE, 0.14, delay=0.1)
    if party_target:   # KH1 opens its PARTY menu (Sora / Donald / Goofy) for Cure and Aero: confirm Sora (first entry)
        kh1_press(S, PAD_CROSS, 0.1, delay=S.CFG.get("kh_target_confirm_s", 0.45))


# ---------------------------------------------------------------- KH1 combo dummy
# KH1 only continues a combo when the swing HITS a KH1 enemy. The dummy: the nearest Heartless is held ~1 m in front of KH1
# Sora (pulled out of reach while it attacks), every Heartless is kept at 999 HP, and the other Heartless plus Donald and
# Goofy are parked away so nothing knocks the dummy around. Verified 2026-09-30 in Wonderland Bizarre Room: swings become
# 201/207 (combo hits) and 203 (finisher) instead of the lone 200 whiff.
KH_POOL, KH_STRIDE, KH_PARTY = 0x2D372A0, 0x4B0, (0x2D37280, 0x2D37288, 0x2D37290)


def kh1_scan_foes(S, ks, max_dist: float = 4000.0):
    """KH1 Heartless near Sora: [(dist, entity, stats page, max HP)] nearest first, plus Donald/Goofy's entities.
    Enemies are pool actors with real stats (not the 18/18 prop default)."""
    import struct
    sora, sp = ks["addr"], ks["pos"]
    raw = kh1_mem(S, KH_POOL, 96 * KH_STRIDE) or b""
    S.state["kh_scan_ok"] = len(raw) == 96 * KH_STRIDE       # (session 9: a failed read must not mean "room empty")
    party = {int.from_bytes(kh1_mem(S, a, 8) or b"\x00" * 8, "little") for a in KH_PARTY}
    foes, mates = [], []
    for i in range(len(raw) // KH_STRIDE):
        e = raw[i * KH_STRIDE:(i + 1) * KH_STRIDE]
        addr = S.kh["base"] + KH_POOL + i * KH_STRIDE
        if addr == sora or not struct.unpack_from("<I", e, 0x374)[0] & 1:
            continue
        pos = struct.unpack_from("<3f", e, 0x10)
        dist = math.dist(pos, sp)
        if addr in party:
            mates.append(addr)
            continue
        if dist > max_dist:
            continue
        stp = kh1_resolve(S, struct.unpack_from("<I", e, 0x6C)[0])
        hpb = stp and kh1_abs(S, stp + 0x3C, 8)
        if not hpb:
            continue
        hp, mx = struct.unpack("<2i", hpb)
        if mx == 18 or mx <= 0 or mx > 5000 or hp <= 0:
            continue
        foes.append((dist, addr, stp, mx))
    foes.sort(key=lambda f: f[0])
    return foes, mates


def kh_park_xz(S, ks) -> tuple:
    """Where parked Heartless / party wait (x, z): around the room's ANCHOR, not KH1 Sora's current spot. Session 10
    diagnostic: right after a refill warp KH1 Sora stands at the room entrance (and glides / summons hold him elsewhere),
    so parking 'behind Sora' put them near the room's edge (z ~2110 vs anchor 648) and KH1 removed them at full HP
    within seconds -> room empty -> another refill reload (= Sora froze, D/G vanished)."""
    if S.CFG.get("kh_park_at_anchor", True):
        a = {**KH_ANCHORS, **S.CFG.get("kh_anchors", {})}.get(kh1_room(S))
        if a is not None:
            return a[0], a[1]
    return ks["pos"][0], ks["pos"][2]


def kh_park_y(S, sora_y: float, up: float) -> float:
    """KH1 y for parked Heartless / party (negative = up), capped at an ABSOLUTE height: with the map feed KH1 Sora can
    stand ~4 m above the room floor, and Sora y - 1500 (~-1965) made KH1 remove every parked Heartless after ~30 s with
    no death -> room empty, no lock-on / combos (2026-10-12). -1500 = the height that always worked from the floor."""
    return max(sora_y - up, S.CFG.get("kh_park_y_top", -1500.0))


def kh1_dummy_tick(S, ks, now: float) -> None:
    import struct
    st, CFG = S.state, S.CFG
    if not CFG.get("kh_dummy", True) or not ks:
        return
    k32 = ctypes.windll.kernel32
    h = ctypes.c_void_p(S.kh["h"])
    sora, sp = ks["addr"], ks["pos"]
    if now - st.get("kh_dummy_scan", 0) > 0.5:          # who is around (enemies = real stats, not the 18/18 prop default)
        st["kh_dummy_scan"] = now
        foes, mates = kh1_scan_foes(S, ks)
        st["kh_foes"], st["kh_mates"] = [(d, a, sp_) for d, a, sp_, _ in foes], mates
        cur = st.get("kh_dummy")
        banned = {a for a, until in st.get("kh_dummy_ban", {}).items() if until > now}
        if cur in banned or not any(f[1] == cur for f in foes):
            ok = [f for f in foes if f[1] not in banned] or foes
            # sturdiest first: Shadows (12 HP) sink into the floor and become untargetable, Soldiers (24) don't
            st["kh_dummy"] = max(ok, key=lambda f: (f[3], -f[0]))[1] if ok else None
            st["kh_whiffs"] = 0
    foes, dummy = st.get("kh_foes", []), st.get("kh_dummy")
    S.status["kh1_dummy"] = [len(foes), hex(dummy) if dummy else None]
    if not foes:
        return
    keep = struct.pack("<i", 999)
    for _, addr, stp in foes:                            # nobody dies (Donald/Goofy/finishers)
        k32.WriteProcessMemory(h, ctypes.c_void_p(stp + 0x3C), keep, 4, None)
    fx, fz = ks["fwd"]
    # park everything else just behind Sora, inside the room (2500 cm away dropped them out of the level)
    pb = CFG.get("kh_park_dist", 300.0)
    up = CFG.get("kh_park_up", 1500.0)               # held high above Sora: never the nearest target, never out of bounds
    cx_, cz_ = kh_park_xz(S, ks)
    park = struct.pack("<3f", cx_ - fx * pb + fz * 150.0, kh_park_y(S, sp[1], up), cz_ - fz * pb - fx * 150.0)
    for _, addr, _ in foes:
        if addr != dummy:
            k32.WriteProcessMemory(h, ctypes.c_void_p(addr + 0x10), park, 12, None)
    if CFG.get("kh_park_party", True):
        mpark = struct.pack("<3f", cx_ - fx * pb - fz * 150.0, kh_park_y(S, sp[1], up), cz_ - fz * pb + fx * 150.0)
        for addr in st.get("kh_mates", []):
            k32.WriteProcessMemory(h, ctypes.c_void_p(addr + 0x10), mpark, 12, None)
    if dummy:
        dstate = (kh1_abs(S, dummy + 0x160, 1) or b"\x00")[0]
        if ks["state"] == KH_STATE_ATTACK and st.get("kh_dummy_fix"):
            spot = st["kh_dummy_fix"]                 # hold still while Sora dashes / swings at it
        else:
            dist = st.get("kh_dummy_want")
            if dist is None:                          # no Borderlands target in reach: dummy waits up with the others
                spot = struct.unpack("<3f", park)
                st["kh_dummy_fix"] = None
                k32.WriteProcessMemory(h, ctypes.c_void_p(dummy + 0x10), park, 12, None)
                return
            if dstate in (3, KH_STATE_ATTACK):        # winding up / attacking: step back out of its reach
                dist = max(dist, CFG.get("kh_dummy_far", 450.0))
            spot = (sp[0] + fx * dist, sp[1], sp[2] + fz * dist)
            st["kh_dummy_fix"] = spot if ks["state"] != KH_STATE_ATTACK else st.get("kh_dummy_fix")
        k32.WriteProcessMemory(h, ctypes.c_void_p(dummy + 0x10), struct.pack("<3f", *spot), 12, None)


# ---------------------------------------------------------------- KH1 enemy proxies (mobs vs police)
# In GTA x Minecraft, GTA's cops exist in Minecraft as invisible proxy entities that Minecraft's mobs can target, and
# hits on them cross over as GTA damage. Here every Borderlands enemy near Sora gets a KH1 Heartless stand-in, placed
# where that enemy is relative to Sora (same distance and bearing, turned into KH1 Sora's frame). KH1's own targeting,
# Sliding Dash, real combos and finishers then play out on those stand-ins, and every KH1 hit on a stand-in (its HP
# drops below the 999 we keep it at) becomes Keyblade damage on the Borderlands enemy it stands for. The KH1 world is
# invisible in Borderlands, so the Heartless never show; only Sora's pose and the damage come through.
def kh1_proxies_on(S) -> bool:
    return bool(S.CFG.get("kh_proxies", True))


def bl_key(p) -> str:
    try:
        return str(p.Name)
    except Exception:  # noqa: BLE001
        return ""


def kh1_proxy_spot(S, pc, ks, p, sign: float):
    """KH1 position for Borderlands enemy p: same relative bearing/distance as in Borderlands (shrunk by the two
    bodies' radii so swings connect), in KH1 Sora's frame, on the Borderlands floor height."""
    st, CFG = S.state, S.CFG
    me, t = vec3(pc.Pawn.Location), vec3(p.Location)
    rx, ry = t[0] - me[0], t[1] - me[1]
    yb = (st.get("kh_yaw_target") if st.get("kh_move_on") and st.get("kh_yaw_target") is not None else st["yaw"]) \
        + math.radians(CFG["yaw_offset_deg"])
    fwd_b = rx * math.cos(yb) + ry * math.sin(yb)
    side_b = -rx * math.sin(yb) + ry * math.cos(yb)
    dist_b = math.hypot(rx, ry) or 1.0
    sc = max(S.frame.scale, 0.01)
    d_k = max(CFG.get("kh_proxy_min_cm", 90.0), (dist_b - CFG.get("kh_target_radius", 50.0)) / sc)
    now = time.time()
    sp_ = st.get("kh_spell")
    want = st.get("kh_lock_want")
    if want and now < want[1] and want[0] is p:
        # real KH1 lock-on (R1) is being pressed for this enemy: its stand-in right in front of KH1 Sora, so KH1's
        # own targeting picks it (KH1 locks the nearest Heartless in front)
        fx_, fz_ = ks["fwd"]
        y_ = st.get("kh_floor_ref") if st.get("kh_ground_active") and st.get("kh_floor_ref") is not None else ks["pos"][1]
        dk_ = CFG.get("kh_lock_place_cm", 200.0)
        y_ = mapfeed_ground_ky(S, ks["pos"][0] + fx_ * dk_, ks["pos"][2] + fz_ * dk_, y_)
        return (ks["pos"][0] + fx_ * dk_, y_, ks["pos"][2] + fz_ * dk_), dk_
    if sp_ and now < sp_["until"] and sp_.get("pawn") is p:
        # a spell at this enemy: its stand-in within that spell's real KH1 reach (KH1 Fira flies ~3.7 m, Blizzara ~8 m),
        # so KH1's spell really hits it; the renderer stretches the flight onto the real enemy (khfx_tick warp)
        reach = CFG.get("kh_spell_reach", {}).get(str(sp_["sid"]), KH_SPELL_REACH.get(sp_["sid"], 500.0))
        d_k = min(d_k, reach * CFG.get("kh_spell_reach_frac", 0.8))
    else:
        d_k = min(d_k, CFG.get("kh_proxy_max_cm", 600.0))
    k = d_k / (dist_b / sc) / sc
    fwd_k, side_k = fwd_b * k, side_b * k * sign
    fx, fz = ks["fwd"]
    sp = ks["pos"]
    y = st.get("kh_floor_ref") if st.get("kh_ground_active") and st.get("kh_floor_ref") is not None else sp[1]
    y = mapfeed_ground_ky(S, sp[0] + fwd_k * fx - side_k * fz, sp[2] + fwd_k * fz + side_k * fx, y)
    return (sp[0] + fwd_k * fx - side_k * fz, y, sp[2] + fwd_k * fz + side_k * fx), d_k


# ---------------------------------------------------------------- KH1 hit reactions -> Borderlands (launch / juggle)
# User's list (2026-10-05): KH1's own knockback / launch / air juggle on Borderlands enemies. When a KH1 hit lands on a
# stand-in, the stand-in is no longer pinned to its Borderlands enemy: KH1 moves it with its real hit reaction (the
# bridge's flat Borderlands floor under it), and the Borderlands enemy is moved by the same offset - turned back from
# KH1 Sora's frame into Borderlands - including the height, so KH1 finishers launch it and air combos keep it up.
# Normal enemies only (user): no bosses, no badasses / champions, nothing big (config kh_juggle_*).
def juggle_ok(S, p) -> bool:
    CFG = S.CFG
    if not CFG.get("kh_juggle", True):
        return False
    cache = S.state.setdefault("juggle_ok", {})
    k = bl_key(p)
    if k in cache:
        return cache[k]
    ok = True
    try:
        name = (k + " " + str(getattr(p, "AIClass", "") or "") + " " + str(getattr(p, "BalanceDefinitionState", "") or "")).lower()
        if any(w in name for w in CFG.get("kh_juggle_never", ["boss", "badass", "champion", "destroyer", "king", "queen", "mother", "big", "crawmerax", "tank"])):
            ok = False
        cyl = getattr(p, "CylinderComponent", None)
        if ok and cyl is not None:
            r, hh = float(cyl.CollisionRadius), float(cyl.CollisionHeight)
            if r > CFG.get("kh_juggle_max_radius", 70.0) or hh > CFG.get("kh_juggle_max_height", 110.0):
                ok = False
        if ok and CFG.get("kh_juggle_ask_boss", True):
            for fn in ("IsBoss", "IsChampion"):
                f = getattr(p, fn, None)
                if f is not None and f():
                    ok = False
    except Exception:  # noqa: BLE001
        ok = False
    cache[k] = ok
    return ok


def bar_hp(S, p) -> int:
    """KH1 max HP for a stand-in = the size of KH1's lock-on gauge (measured 2026-10-06: ~300 HP = one full-width bar,
    more stacks yellow / pink layers like KH1 bosses; 24 = the stub the user called "super tiny"). By enemy tier."""
    cache = S.state.setdefault("bar_tier", {})
    k = bl_key(p)
    tier = cache.get(k)
    if tier is None:
        tier = "normal"
        try:
            name = (k + " " + str(getattr(p, "AIClass", "") or "") + " " + str(getattr(p, "BalanceDefinitionState", "") or "")).lower()
            f = getattr(p, "IsBoss", None)
            if (f is not None and f()) or any(w in name for w in ("boss", "destroyer", "crawmerax", "queen", "king")):
                tier = "boss"
            else:
                f = getattr(p, "IsChampion", None)
                if (f is not None and f()) or any(w in name for w in ("badass", "champion", "alpha", "mother", "big")):
                    tier = "badass"
        except Exception:  # noqa: BLE001
            pass
        cache[k] = tier
    return int(S.CFG.get("kh_bar_hp", {}).get(tier, {"normal": 150, "badass": 450, "boss": 900}[tier]))


def kh1_react_start(S, a: int, p, now: float) -> None:
    st = S.state
    react = st.setdefault("kh_react", {})
    if a in react:
        react[a]["until"] = now + S.CFG.get("kh_juggle_min_s", 0.45)   # combo continues: same anchors
        return
    pos = kh1_abs(S, a + 0x10, 12)
    if not pos:
        return
    try:
        b0 = vec3(p.Location)
    except Exception:  # noqa: BLE001
        return
    react[a] = {"p": p, "t0": now, "until": now + S.CFG.get("kh_juggle_min_s", 0.45), "k0": struct.unpack("<3f", pos),
                "b0": b0, "last": None, "still_t": now, "air": False}
    st["juggles"] = st.get("juggles", 0) + 1


def kh1_react_step(S, ks, a: int, r: dict, now: float) -> bool:
    """One reacting stand-in: Borderlands enemy follows KH1's motion of it. False = reaction over."""
    st, CFG = S.state, S.CFG
    p = r["p"]
    if not bl_alive(p) or now - r["t0"] > CFG.get("kh_juggle_max_s", 4.0):
        return False
    pos = kh1_abs(S, a + 0x10, 12)
    if not pos:
        return False
    x, y, z = struct.unpack("<3f", pos)
    k0 = r["k0"]
    floor = st.get("kh_floor_ref") if st.get("kh_ground_active") and st.get("kh_floor_ref") is not None else k0[1]
    if mapfeed_active(S):
        # map feed: the stand-in stands on Borderlands' terrain, not the flat floor (a 20 cm mound read as a launch) ->
        # the floor KH1's own collision found under it (desc + 0x1a * 4 = entity + 0x308)
        fu = kh1_abs(S, a + 0x308, 4)
        fu = struct.unpack("<f", fu)[0] if fu else None
        if fu is not None and fu == fu and abs(fu - floor) < 5000:
            floor = fu
    up_k = max(0.0, floor - y)                                     # KH1 y grows downward
    ox, oz = x - k0[0], z - k0[2]
    fx, fz = ks["fwd"]
    fwd_k, side_k = ox * fx + oz * fz, -ox * fz + oz * fx
    sc = max(S.frame.scale, 0.01)
    sign = CFG.get("kh_turn_sign", -1.0)
    fwd_b, side_b = fwd_k * sc, side_k * sign * sc
    yb = (st.get("kh_yaw_target") if st.get("kh_move_on") and st.get("kh_yaw_target") is not None else st["yaw"]) \
        + math.radians(CFG["yaw_offset_deg"])
    rx, ry = fwd_b * math.cos(yb) - side_b * math.sin(yb), fwd_b * math.sin(yb) + side_b * math.cos(yb)
    b0 = r["b0"]
    n_ = math.hypot(rx, ry)                                # v2: cap the knockback (750 cm KH1 offsets read as teleports)
    cap = CFG.get("kh_juggle_max_push", 300.0)
    if n_ > cap:
        rx, ry = rx * cap / n_, ry * cap / n_
    tgt = (b0[0] + rx, b0[1] + ry, b0[2] + up_k * sc * CFG.get("kh_juggle_height", 1.0))
    moved = r["last"] is None or math.dist(r["last"], (x, y, z)) > 1.0
    r["last"] = (x, y, z)
    if moved:
        r["still_t"] = now
    air = up_k > CFG.get("kh_juggle_air_cm", 15.0)
    was_air = r["air"]
    r["air"] = air

    def _mv(p=p, tgt=tgt, air=air, was_air=was_air):
        if air and not was_air:
            p.SetPhysics(4)                                        # PHYS_Flying: KH1 decides the height, not gravity
        p.Velocity = unrealsdk.make_struct("Vector", X=0.0, Y=0.0, Z=0.0)
        c = p.Location                                     # v2: glide toward KH1's spot (per-tick teleports jittered)
        k = CFG.get("kh_juggle_smooth", 0.35)
        nx, ny, nz = float(c.X) + (tgt[0] - float(c.X)) * k, float(c.Y) + (tgt[1] - float(c.Y)) * k, float(c.Z) + (tgt[2] - float(c.Z)) * k
        # SetLocation returns False for EVERY Borderlands enemy (probed 2026-10-06: any direction, 5 cm, collision off,
        # AI unfrozen) -> the juggle never moved anything; writing Location directly works
        nv = unrealsdk.make_struct("Vector", X=nx, Y=ny, Z=nz)
        if not p.SetLocation(nv):
            p.Location = nv
        if was_air and not air:
            p.SetPhysics(2)                                        # PHYS_Falling: lands on the real ground
    S.pending.append(_mv)
    if dbg_on(S, "dbg_juggle_trace"):                  # juggle trace (next session)
        with open(HERE / "juggle_trace.txt", "a") as f_:
            f_.write(f"{now % 1000:8.2f} {hex(a)} kh_off {ox:6.0f} {oz:6.0f} up {up_k:5.0f} -> bl {rx:6.0f} {ry:6.0f} z+ {tgt[2] - b0[2]:5.0f} air {int(air)}" + chr(10))
    if now < r["until"] or air or now - r["still_t"] < CFG.get("kh_juggle_settle_s", 0.15):
        return True
    if r["air"] or was_air:
        S.pending.append(lambda p=p: p.SetPhysics(2))
    return False


def kh1_kill_standin(S, a: int) -> bool:
    """A lethal KH1 hit from Sora on stand-in a (template: a real Sora -> enemy record from this KH1 process)."""
    m = _hits_map(S)
    if m is None or struct.unpack_from("<I", m, 0)[0] != 0x53544948:
        return False
    req, done = struct.unpack_from("<2I", m, 8)
    if req != done:
        return False
    sora = kh1_state(S)["addr"] if kh1_state(S) else 0
    sh = kh1_handle(S, sora)
    w = struct.unpack_from("<I", m, 4)[0]
    party = set(a_ for a_ in party_addrs(S) if a_) | {sora}
    tmpl = None
    for i in range(w - 1, max(-1, w - 256), -1):
        o = 120 + (i % 256) * 80
        seq, _, ent = struct.unpack_from("<IIQ", m, o)
        if seq != i + 1 or ent in party:
            continue
        rec = bytearray(m[o + 16:o + 80])
        if rec[0] & 0x20 or struct.unpack_from("<I", rec, 4)[0] != sh:
            continue
        tmpl = rec
        break
    if tmpl is None:
        tmpl = S.state.get("kh_kill_tmpl")                 # remembered from earlier in this KH1 process
        if tmpl is None or S.state.get("kh_kill_tmpl_pid") != S.kh.get("pid"):
            S.status["kh1_drops"] = "no Sora->Heartless hit record yet (hit an enemy once)"
            return False
    else:
        S.state["kh_kill_tmpl"], S.state["kh_kill_tmpl_pid"] = bytes(tmpl), S.kh.get("pid")
    stand_release(S, a)                                    # kh1_fx v31 floors stand-ins at 1 HP: lift it for this one
    tmpl = bytearray(tmpl)
    struct.pack_into("<I", tmpl, 4, sh)
    tmpl[0x20:0x24] = bytes(4)
    tmpl[0x28:0x2C] = bytes(4)
    stp = kh1_resolve(S, struct.unpack("<I", kh1_abs(S, a + 0x6C, 4) or bytes(4))[0])
    hpb = stp and kh1_abs(S, stp + 0x3C, 4)
    hp_ = struct.unpack("<i", hpb)[0] if hpb else 999
    m[48:112] = bytes(tmpl)
    struct.pack_into("<I", m, 112, 1)
    struct.pack_into("<Q", m, 16, a)
    struct.pack_into("<i", m, 24, max(1, hp_) + 1)
    struct.pack_into("<4f", m, 32, 0.0, 0.0, 1.0, 1.0)
    struct.pack_into("<I", m, 8, req + 1)
    S.state["kh1_kills"] = S.state.get("kh1_kills", 0) + 1
    S.status["kh1_drops"] = {"kills": S.state["kh1_kills"]}
    return True


def kh1_spawn_watch(S, ks, now: float) -> None:
    """Every tick: a pool slot that just came alive with real enemy stats = KH1 spawned a Heartless -> mute its appear
    cloud (renderer sphere at the first-seen spot for kh1_spawn_fresh_s), wherever it is and whoever is near (user
    2026-10-06: clouds near Borderlands enemies, where the learned-spot spheres stand down for paired stand-ins)."""
    import struct
    st, CFG = S.state, S.CFG
    raw = kh1_mem(S, KH_POOL, 96 * KH_STRIDE)
    if not raw:
        return
    alive = {i for i in range(96) if struct.unpack_from("<I", raw, i * KH_STRIDE + 0x374)[0] & 1}
    prev = st.get("kh_alive_slots")
    st["kh_alive_slots"] = alive
    fresh = [f for f in st.get("kh_spawn_fresh", []) if now - f[1] < CFG.get("kh1_spawn_fresh_s", 2.5)]
    if prev is not None:
        party = {int.from_bytes(kh1_mem(S, a, 8) or b"\x00" * 8, "little") for a in KH_PARTY}
        for i in alive - prev:
            addr = S.kh["base"] + KH_POOL + i * KH_STRIDE
            if addr == ks["addr"] or addr in party:
                continue
            stp = kh1_resolve(S, struct.unpack_from("<I", raw, i * KH_STRIDE + 0x6C)[0])
            hpb = stp and kh1_abs(S, stp + 0x3C, 8)
            if not hpb:
                continue
            hp, mx = struct.unpack("<2i", hpb)
            if mx == 18 or mx <= 0 or mx > 5000 or hp <= 0:
                continue                                       # props / spell objects, not a Heartless
            pos = struct.unpack_from("<3f", raw, i * KH_STRIDE + 0x10)
            if math.dist(pos, ks["pos"]) < CFG.get("kh1_spawn_fresh_min", 150.0):
                continue                                       # never over KH1 Sora himself (his own effects)
            fresh.append([list(pos), now])
            st["kh_spawn_fresh_n"] = st.get("kh_spawn_fresh_n", 0) + 1
            if dbg_on(S, "dbg_spawn_trace"):
                try:
                    kind = struct.unpack_from("<I", raw, i * KH_STRIDE + 0x134)[0]
                    rel = [round(a_ - b_) for a_, b_ in zip(pos, ks["pos"])]
                    with open(HERE / "spawn_trace.txt", "a", encoding="utf-8") as fh:
                        fh.write(f"{time.strftime('%H:%M:%S')} slot {i} kind {kind:#x} hp {hp}/{mx} rel {rel} "
                                 f"sora anim {ks.get('anim')}" + chr(10))
                except Exception:  # noqa: BLE001
                    pass
    st["kh_spawn_fresh"] = fresh
    S.status["kh_spawn_fresh"] = [len(fresh), st.get("kh_spawn_fresh_n", 0)]


SPAWN_FILE = HERE / "data" / "kh1_spawn_points.json"


def kh1_spawn_learn(S, ks, foes, now: float) -> None:
    """Heartless spawn points next to pinned KH1 Sora (user 2026-10-06: "heartless spawn cloud every ~6 s"): a parked
    Heartless of some kinds leaves by itself after ~15 s and KH1 respawns it at its spawn point, ~3.5 m from Sora, so its
    appear cloud passed the kh1_fx radius. A Heartless first seen by this scan stands where KH1 spawned it (we park it
    later in the tick) -> remember that spot per room (data/kh1_spawn_points.json); the renderer (v66) skips KH1
    effects inside kh1_spawn_mute_r of each spot unless KH1 Sora or a paired stand-in is near it (kh_spawn_mute)."""
    import struct
    st, CFG = S.state, S.CFG
    room = kh1_room(S)
    alive = {a for _, a, _, _ in foes}
    prev, prev_room = st.get("kh_spawn_seen"), st.get("kh_spawn_room")
    st["kh_spawn_seen"], st["kh_spawn_room"] = alive, room
    pts_all = st.get("kh_spawn_pts")
    if pts_all is None:
        try:
            pts_all = json.loads(Path(SPAWN_FILE).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pts_all = {}
        st["kh_spawn_pts"] = pts_all
    pts = pts_all.setdefault(room or "?", [])
    sp = ks["pos"]
    if prev is not None and prev_room == room and now > st.get("kh_warping_until", 0) + 2.0 and room:
        learned = False
        for _, a, _, _ in foes:
            if a in prev:
                continue
            raw = kh1_abs(S, a + 0x10, 12)
            if not raw:
                continue
            pos = struct.unpack("<3f", raw)
            if math.dist(pos, sp) > CFG.get("kh1_fx_radius", 1600.0) + 400.0:
                continue                                   # too far to ever reach the effect radius
            near = next((q for q in pts if math.dist(q, pos) < 150.0), None)
            if near:
                near[:] = [(n_ + p_) / 2 for n_, p_ in zip(near, pos)]
            else:
                pts.append([round(v, 1) for v in pos])
                del pts[:-6]
                learned = True
                kh1_event(S, f"learned a Heartless spawn point in room {room}: {[round(v) for v in pos]} (its cloud is hidden)")
        if learned:
            try:
                Path(SPAWN_FILE).write_text(json.dumps(pts_all, indent=1), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
    # active mute spheres: not while KH1 Sora or a paired stand-in is close (their own effects must show)
    busy = [sp]
    for a in st.get("kh_pmap", {}):
        raw = kh1_abs(S, a + 0x10, 12)
        if raw:
            busy.append(struct.unpack("<3f", raw))
    free_r = CFG.get("kh1_spawn_mute_free", 250.0)
    st["kh_spawn_mute"] = [q for q in pts if all(math.dist(q, b) > free_r for b in busy)][:6]
    S.status["kh_spawn_mute"] = [len(pts), len(st["kh_spawn_mute"])]


def kh1_proxy_tick(S, pc, ks, now: float) -> None:
    import struct
    st, CFG = S.state, S.CFG
    kh1_spawn_watch(S, ks, now)
    if now < st.get("kh_menu_park_until", 0):              # KH1 menu: enemies are parked away (kh_menu_clear)
        return
    k32 = ctypes.windll.kernel32
    h = ctypes.c_void_p(S.kh["h"])
    if now - st.get("kh_proxy_scan", 0) > 0.5:
        st["kh_proxy_scan"] = now
        # (range covers the parked ones: kh_park_dist 6000 keeps them out of KH1's battle range, and missing them
        #  here made the "room empty" refill warp KH1 Sora out and back every 25 s - user: teleports, party blinking)
        foes, mates = kh1_scan_foes(S, ks, CFG.get("kh_proxy_scan_dist", 10000.0))
        if not st.get("kh_scan_ok", True):                # unreadable pool this tick: keep the last list
            foes = [(0.0, a, stp, st.get("kh_pmx", {}).get(a, 1)) for a, stp in st.get("kh_pfoes", [])]
            mates = st.get("kh_mates", mates)
        if not foes and st.get("kh_pfoes"):
            kh1_event(S, f"scan: 0 Heartless (was {len(st['kh_pfoes'])}, read ok {st.get('kh_scan_ok')})")
        # session 10 diagnostic: KH1 spawns a Heartless every ~13-18 s, one had spawned 5-11 s before nearly every
        # "0 Heartless" scan, yet none counted -> log what drops out of the list and what the pool holds when empty
        if CFG.get("kh_scan_diag", True) and st.get("kh_scan_ok", True):
            bb_ = _lo_u32(S, KH_BATTLE_BITS) & 1                # park distance vs KH1 battle mode (field music / idle)
            if bb_ != st.get("kh_diag_battle"):
                st["kh_diag_battle"] = bb_
                kh1_event(S, f"scan diag: KH1 battle mode {bb_} (Heartless {len(foes)}, park {CFG.get('kh_park_dist')})")
            now_ids = {a for _, a, _, _ in foes}
            for a, stp_ in st.get("kh_pfoes", []):
                if a not in now_ids:
                    e_ = kh1_abs(S, a + 0x10, 12)
                    fl_ = kh1_abs(S, a + 0x374, 4)
                    hp_ = kh1_abs(S, stp_ + 0x3C, 8) if stp_ else None
                    kh1_event(S, f"scan diag: {hex(a)} left the list: flag {fl_.hex() if fl_ else None} hp/mx "
                                 f"{struct.unpack('<2i', hp_) if hp_ else None} pos {[round(v) for v in struct.unpack('<3f', e_)] if e_ else None}")
            if not foes and now - st.get("kh_diag_dump_t", 0) > 5.0:
                st["kh_diag_dump_t"] = now
                raw_ = kh1_mem(S, KH_POOL, 96 * KH_STRIDE) or b""
                rows_ = []
                for i in range(len(raw_) // KH_STRIDE):
                    e_ = raw_[i * KH_STRIDE:(i + 1) * KH_STRIDE]
                    if not struct.unpack_from("<I", e_, 0x374)[0] & 1:
                        continue
                    p_ = struct.unpack_from("<3f", e_, 0x10)
                    stp_ = kh1_resolve(S, struct.unpack_from("<I", e_, 0x6C)[0])
                    hb_ = stp_ and kh1_abs(S, stp_ + 0x3C, 8)
                    rows_.append(f"{i}:d{round(math.dist(p_, ks['pos']))} hp{struct.unpack('<2i', hb_) if hb_ else '-'}")
                kh1_event(S, "scan diag: pool active = " + " ".join(rows_))
        # sturdiest first: Shadows (12 HP) sink into the floor between attacks, Soldiers (24) don't
        foes.sort(key=lambda f: (-f[3], f[0]))
        st["kh_pfoes"], st["kh_mates"] = [(a, stp) for _, a, stp, _ in foes], mates
        kh1_spawn_learn(S, ks, foes, now)
        pmx0 = st.setdefault("kh_pmx0", {})               # KH1's own max HP, before kh_standin_bar_max rewrote it
        for _, a, _, mx in foes:
            pmx0.setdefault(a, mx)
        st["kh_pmx"] = {a: pmx0[a] for _, a, _, _ in foes}
    foes = st.get("kh_pfoes", [])
    # NO FADE-OUT (session 10, user: "if heartless despawn don't reload Sora"): KH1 removes a Heartless by fading it out -
    # FUN_140297fe0 adds rate +0x64 x dt to alpha +0x60 and, below 0 with flag bit 3, calls FUN_140292390 (marks +0x374
    # bit 1; the pool sweep FUN_140292e40 frees it). Caught live on parked / just-unpaired stand-ins at full HP -> room
    # empty -> refill reload = Sora froze, D/G vanished. Every tick: a fading stand-in / parked Heartless gets alpha 1,
    # rate 0. Free Heartless (real fights, may die) and dying ones keep KH1's fade.
    if CFG.get("kh_no_fadeout", True) and foes:
        free_ = set(st.get("kh_free", [])) | set(st.get("kh_dying", {}))
        vetoed = st.setdefault("kh_fade_vetoes", 0)
        for a, _ in foes:
            if a in free_:
                continue
            # bug 10-18: KH1's "leave" fade (parked Heartless, FUN_1402a0f40 SetAlpha(0) + flags +0x0 |= 8) left bit 3
            # set after the alpha veto -> the stand-in stayed visible / lockable but took no hits (combos, magic, D/G)
            if CFG.get("kh_no_fade_clear_bit", True):
                f0_ = kh1_abs(S, a, 4)
                if f0_ and f0_[0] & 8:
                    k32.WriteProcessMemory(h, ctypes.c_void_p(a), bytes([f0_[0] & ~8 & 0xFF]), 1, None)
                    st["kh_fade_bit_clears"] = st.get("kh_fade_bit_clears", 0) + 1
            fb_ = kh1_abs(S, a + 0x60, 8)
            if fb_ and struct.unpack("<f", fb_[4:8])[0] < 0.0:
                k32.WriteProcessMemory(h, ctypes.c_void_p(a + 0x60), struct.pack("<2f", 1.0, 0.0), 8, None)
                st["kh_fade_vetoes"] = vetoed = vetoed + 1
                if now - st.setdefault("kh_fade_veto_log", {}).get(a, 0) > 10.0:
                    st["kh_fade_veto_log"][a] = now
                    kh1_event(S, f"no fade-out: KH1 started fading {hex(a)} (alpha {struct.unpack('<f', fb_[:4])[0]:.2f}) -> kept")
        S.status["kh_fade_vetoes"] = vetoed
        S.status["kh_fade_bit_clears"] = st.get("kh_fade_bit_clears", 0)
    # room refill (re-entering KH1's room respawns its Heartless) only when Borderlands enemies nearby actually outnumber
    # the stand-ins, for kh_refill_need_s in a row: the reload blanks KH1's HUD and spawns Heartless with their cloud and
    # sound, and while idling / jumping the scan sometimes saw 0-1 of them (user 2026-10-05)
    if len(foes) < CFG.get("kh_proxy_refill_below", 2) and len(enemies(S, pc, CFG.get("kh_proxy_range", 2200.0))) > len(foes):
        if st.get("kh_refill_short_t") is None:
            st["kh_refill_short_t"] = now
    else:
        st["kh_refill_short_t"] = None
    short_for = now - st["kh_refill_short_t"] if st.get("kh_refill_short_t") is not None else 0.0
    # how many Heartless a fresh room has (most seen since the last warp)
    if now > st.get("kh_warping_until", 0) + 2.0:
        st["kh_room_full"] = max(st.get("kh_room_full", 0), len(foes))
    # CALM REFILL (user 2026-10-06: the 1-2 s "disconnect" mid-fight = this room reload): a learning kill uses up a real
    # KH1 Heartless, so top the room up while no Borderlands enemy is near - fights then start with a full room.
    # Only after a REAL death since the last load (kh_room_deaths): the scan count flickers 5 <-> 6 (Shadows sinking),
    # and "fewer than the most seen" reloaded the room every 25-30 s forever (user: disconnects + clouds of Heartless)
    if enemies(S, pc, CFG.get("kh_calm_range", 4000.0)):
        st["kh_calm_t"] = now
    calm_for = now - st.get("kh_calm_t", now)
    deaths = st.get("kh_room_deaths", 0) + (1 if free_on(S) and len(foes) < st.get("kh_room_full", 0) else 0)
    # room EMPTY -> refill, but only after kh_empty_need_s of empty scans in a row (session 9: ONE empty scan right after
    # a fight - both parked Heartless alive at full HP in a recording - reloaded the room = Sora froze, D/G vanished)
    if foes:
        st["kh_empty_t"] = None
    elif st.get("kh_empty_t") is None:
        st["kh_empty_t"] = now
    empty_for = now - st["kh_empty_t"] if st.get("kh_empty_t") is not None else 0.0
    if not foes and st.get("kh_room_full", 0) > 0 and empty_for > CFG.get("kh_empty_need_s", 3.0):
        deaths = max(deaths, 1)                       # room EMPTY (no 5<->6 flicker possible): KH1 removed them -> refill
    if CFG.get("kh_calm_refill", True) and calm_for > CFG.get("kh_calm_need_s", 4.0)             and deaths > 0 and now - st.get("kh_refill_t", 0) > CFG.get("kh_refill_every", 25.0)             and now > st.get("kh_warping_until", 0) and ks["state"] == 2 and not st.get("kh_dying"):
        st["kh_refill_t"] = now
        if kh1_go_home(S, force=True):
            st["kh_pmap"], st["kh_proxy_scan"], st["kh_room_full"] = {}, 0, 0
            kh1_event(S, f"calm: {len(foes)} Heartless left: re-entering the room to refill it")
    if short_for > CFG.get("kh_refill_need_s", 3.0) and now - st.get("kh_refill_t", 0) > CFG.get("kh_refill_every", 25.0) \
            and deaths > 0 and now > st.get("kh_warping_until", 0) and ks["state"] == 2:
        st["kh_refill_t"] = now
        if kh1_go_home(S, force=True):
            st["kh_pmap"], st["kh_proxy_scan"] = {}, 0
            kh1_event(S, f"only {len(foes)} Heartless left: re-entering the room to respawn them")
    # free Heartless: real KH1 enemies of their own (KH1 AI, KH1 HP, they can die), drawn around Borderlands Sora
    # (heartless_tick) - not stand-ins for Borderlands enemies. The weakest go free, the sturdiest stay stand-ins.
    free = st.setdefault("kh_free", [])
    if free_on(S):
        alive = {a for a, _ in foes}
        free[:] = [a for a in free if a in alive]
        pm0 = st.get("kh_pmap", {})
        for a, _ in reversed(foes):
            if len(free) >= CFG.get("kh_free_max", 3):
                break
            if a not in free and a not in pm0:
                free.append(a)
    else:
        free.clear()
    foes = [(a, stp_) for a, stp_ in foes if a not in free]
    pmap = st.setdefault("kh_pmap", {})                  # KH1 entity -> Borderlands enemy name
    bl = {bl_key(p): (dist, p) for dist, p, _ in enemies(S, pc, CFG.get("kh_proxy_range", 2200.0))}
    st["kh_bl_by_key"] = {k: v[1] for k, v in bl.items()}
    lk = lock_target(S)
    if lk is not None and bl_key(lk) in bl:
        bl = {bl_key(lk): bl[bl_key(lk)]}             # locked on: the others' Heartless wait out of reach
    react = st.setdefault("kh_react", {})
    for a, r in list(react.items()):                      # a launched enemy may be > 3 m up: keep its pair
        if a in pmap and bl_alive(r["p"]):
            bl.setdefault(bl_key(r["p"]), (0.0, r["p"]))
        else:
            react.pop(a, None)
    stats = dict(foes)
    # KH1 ITEM DROPS (user 2026-10-05): a paired Borderlands enemy that DIED -> its stand-in gets a real KH1 killing blow
    # from Sora (kh1_fx v24+ hit injection), so KH1 itself plays the death and drops its own munny / HP / MP orbs and
    # items (into the real KH1 inventory). Kill EXP is dropped by kh1_fx v26 while the level link is on.
    prize_learn(S)
    dying = st.setdefault("kh_dying", {})
    prev = st.get("kh_bl_by_key_prev", {})
    for a, k in list(pmap.items()):
        if k in bl or a in dying:
            continue
        p_old = prev.get(k)
        # the kill uses up a KH1 Heartless: only when the room keeps one per Borderlands enemy still fighting (+1 spare),
        # else no drop for this one - running dry forces a room reload mid-fight (HUD + Sora blink 1-2 s)
        n_live = len([a_ for a_, _ in foes if a_ not in dying]) - 1
        spare_ok = n_live >= max(len(st.get("kh_bl_by_key", {})) + CFG.get("kh_drop_spare", 1), CFG.get("kh_proxy_refill_below", 2))
        if p_old is not None and not bl_alive(p_old) and CFG.get("kh1_drops", True) and prize_drop(S, a):
            kh1_event(S, f"BL enemy {k} died -> KH1 prizes at its stand-in {hex(a)} (no kill)")
        elif p_old is not None and not bl_alive(p_old) and CFG.get("kh1_drops", True) and not spare_ok:
            kh1_event(S, f"BL enemy {k} died -> no KH1 kill ({n_live} Heartless would be left, {len(st.get('kh_bl_by_key', {}))} enemies near)")
        elif p_old is not None and not bl_alive(p_old) and CFG.get("kh1_drops", True) and not CFG.get("kh1_learn_kill", False):
            kh1_event(S, f"BL enemy {k} died -> no KH1 drop (its Heartless kind isn't learned; learning kills off = no room reload)")
        elif p_old is not None and not bl_alive(p_old) and CFG.get("kh1_drops", True):
            if kh1_kill_standin(S, a):
                dying[a] = now
                st["kh_room_deaths"] = st.get("kh_room_deaths", 0) + 1   # a real KH1 Heartless gone until the next load
                kh1_event(S, f"BL enemy {k} died -> KH1 kill on its stand-in {hex(a)} (drops)")
    for a in [a for a, t in dying.items() if now - t > CFG.get("kh1_dying_s", 6.0)]:
        del dying[a]
    st["kh_bl_by_key_prev"] = dict(st.get("kh_bl_by_key", {}))
    foes = [(a, stp_) for a, stp_ in foes if a not in dying]
    stats = dict(foes)
    # forget pairs whose Heartless or Borderlands enemy is gone (dead / out of range)
    for a in [a for a, k in pmap.items() if a not in stats or k not in bl]:
        del pmap[a]
    taken = set(pmap.values())
    free = [a for a, _ in foes if a not in pmap]
    for key in sorted((k for k in bl if k not in taken), key=lambda k: bl[k][0]):   # nearest enemies first
        if not free:
            break
        pmap[free.pop(0)] = key
    # hits: a KH1 swing that connected lowered a stand-in's HP -> Keyblade damage on its Borderlands enemy
    swinging = ks["state"] == KH_STATE_ATTACK and ks["anim"] in KH_SWING_ANIMS
    if swinging:
        st["kh_swing_t"] = now
    melee = now - st.get("kh_swing_t", 0) < CFG.get("kh_proxy_hit_window", 0.4)
    keep = struct.pack("<i", 999)
    hits, magic_hits, party_hits = [], [], []
    try:
        kh1_hit_scan(S, now)                              # who hit which stand-in, with what KH1 element
    except Exception as ex:  # noqa: BLE001
        S.status["hit_scan_err"] = f"{type(ex).__name__}: {ex}"
    mg = st.get("kh_magic")                               # (spell, level, until): a KH1 cast is in flight
    magic = bool(mg and now < mg[2]) and not swinging
    # kh1_fx v31: stand-ins floor at 1 HP inside KH1 and every hit is counted there (damage KH1 meant to deal), so their
    # KH1 HP can MIRROR the Borderlands enemy's health -> KH1's lock-on HP bar shows the real enemy (it showed 999)
    mirror = stand_sync(S, [a for a, _ in foes]) and CFG.get("kh_standin_hp_mirror", True)
    pa_s = [x for x in party_addrs(S) if x]
    summon_out = bool(pa_s) and all(int.from_bytes(kh1_abs(S, x, 4) or b"\x00" * 4, "little") & 0x1000000 for x in pa_s)
    pmx = st.get("kh_pmx", {})
    for a, stp in foes:
        b = kh1_abs(S, stp + 0x3C, 4)
        hpk = struct.unpack("<i", b)[0] if b else 999
        if mirror:
            dk = stand_hits(S, a)
            p_ = bl[pmap[a]][1] if a in pmap and pmap[a] in bl else None
            mxk = int(CFG.get("kh_standin_bar_max", 0)) or (bar_hp(S, p_) if p_ is not None else 0) or pmx.get(a, 0) or 100
            cur_, mx_ = pool_values(p_) if p_ is not None else (1.0, 1.0)
            want = max(1, min(mxk, int(math.ceil(mxk * cur_ / max(mx_, 1.0)))))
            if b and hpk != want:
                k32.WriteProcessMemory(h, ctypes.c_void_p(stp + 0x3C), struct.pack("<i", want), 4, None)
            mb = kh1_abs(S, stp + 0x40, 4)                # KH1 draws the gauge's frame from max HP: keep it = mxk
            if mb and struct.unpack("<i", mb)[0] != mxk:
                k32.WriteProcessMemory(h, ctypes.c_void_p(stp + 0x40), struct.pack("<i", mxk), 4, None)
            if dk <= 0:
                continue
            hpk = 999 - dk                                # (the code below reads 999 - hpk as the hit's damage)
            keep = None
        if hpk < 999:
            if (magic or melee) and a in pmap and pmap[a] in bl and juggle_ok(S, bl[pmap[a]][1]):
                kh1_react_start(S, a, bl[pmap[a]][1], now)       # KH1's hit reaction moves it from now on
            who_, wsp_ = kh1_hit_attacker(S, a, now)
            if not who_ and not melee and not magic and st.get("kh_summon_ent") and summon_out:
                # summon attacks never reach KH1's hit queue (recorder: no hit record from any summon) -> by elimination
                who_ = 3
                wsp_ = KH1_SUMMON_SPELL.get(int.from_bytes(kh1_mem(S, 0x298C8F0, 4) or b"\x00" * 4, "little"))
            if who_ and a in pmap and pmap[a] in bl and (party_on(S) or who_ == 3):
                # KH1's hit log names Donald / Goofy / the summon: theirs, even inside Sora's swing / cast window
                party_hits.append((bl[pmap[a]][1], 999 - max(hpk, 0), wsp_, who_))
            elif magic and a in pmap and pmap[a] in bl:
                magic_hits.append((bl[pmap[a]][1], 999 - max(hpk, 0)))
                kh1_learn_elem(S, a, mg[0], now)
            elif melee and a in pmap and pmap[a] in bl and not blade_on(S):
                hits.append((bl[pmap[a]][1], 999 - max(hpk, 0)))
            elif not melee and not magic and a in pmap and pmap[a] in bl and party_on(S):
                party_hits.append((bl[pmap[a]][1], 999 - max(hpk, 0), kh1_party_spell(S, a, now), 0))   # Donald / Goofy hit it
            if keep is not None:
                k32.WriteProcessMemory(h, ctypes.c_void_p(stp + 0x3C), keep, 4, None)   # nobody dies
    last_hit = st.setdefault("kh_proxy_last_hit", {})
    for p, dk, pspell, pwho in party_hits:
        key = "p" + bl_key(p)
        if now - last_hit.get(key, 0) < 0.2 or not bl_alive(p):
            continue
        last_hit[key] = now
        # a share of the enemy's own max health per hit (flat KH1-point damage one-shot low-level enemies)
        if CFG.get("kb_kh1_damage", True):
            who_ = pwho or (1 if random.random() < 0.5 else 2)        # 0 = KH1 didn't say who: Donald or Goofy
            dmg_ = kb_damage(S, pc, p, 1.0, who_, "summon" if who_ == 3 else "party")
        else:
            mx_ = pool_values(p)[1] or hp(p) or 100.0
            dmg_ = mx_ * CFG.get("party_hit_frac", 0.07) * min(2.0, max(0.5, dk / 10.0))
        pel = KB_SPELL_ELEM.get(pspell) if pspell else None       # Donald's KH1 spell -> the same BL element as Sora's
        if pel:
            S.status["party_spell"] = [pspell, round(now % 1000, 1)]
        if hurt(S, pc, p, dmg_, push=CFG.get("kh_party_push", 6000.0), lvl_scale=not CFG.get("kb_kh1_damage", True),
                elem=pel, elem_chance=CFG.get("kb_elem_chance", {}).get(pspell, 0.35) if pel else 0.0):
            st["party_hits"] = st.get("party_hits", 0) + 1
            # session 10: attack shout on their REAL landed hits (KH1's hit log says who) - the state-6 trigger alone
            # fired 3x per fight (they swing in state 2); spells already shout at cast time
            n6 = S.status.setdefault("party_atk_trig", {})
            # (Donald's staff hits come with an element code too - status hitwho1 16 / hit1 0 - so his count; cd covers casts)
            if pwho == 1 or (pwho == 2 and not pspell):
                n6[f"hit{pwho}"] = n6.get(f"hit{pwho}", 0) + 1
                voice(S, ("datk", "gatk")[pwho - 1], chance=CFG.get("party_hit_voice_chance", 0.45),
                      who=("donald", "goofy")[pwho - 1], cd=CFG.get("party_voice_cd", 2.5))
            else:
                n6[f"hitwho{pwho}"] = n6.get(f"hitwho{pwho}", 0) + 1
            try:
                at = vec3(p.Location)
                if invented_ok(S): _part(S, pos=[at[0], at[1], at[2] + 40], size=70, color=(0.9, 0.95, 1.0), life=0.12, spr="burst", rot=random.uniform(0, 6.3))
            except Exception:  # noqa: BLE001
                pass
    for p, dk in hits:
        key = bl_key(p)
        if now - last_hit.get(key, 0) < CFG.get("kh_proxy_hit_gap", 0.15) or not bl_alive(p):
            continue
        last_hit[key] = now
        if CFG.get("kb_kh1_damage", True):
            dmg = kb_damage(S, pc, p, 1.0, 0, "proxy")
        else:
            dmg = max_hp_share(p, CFG.get("sora_hit_frac", 0.10)) * min(2.5, max(0.5, dk / 10.0))
        if hurt(S, pc, p, dmg, push=CFG.get("kh_proxy_push", 30000.0) if CFG.get("kh_knockback", False) else 0.0,
                lvl_scale=not CFG.get("kb_kh1_damage", True)):
            st["proxy_hits"] = st.get("proxy_hits", 0) + 1
            st["mp"] = min(MP_MAX, st.get("mp", MP_MAX) + 4.0)
            kh1_add_mp(S, CFG.get("kh_mp_per_hit", 0.34))
            try:
                if invented_ok(S): fx_flash(S, [c + (40.0 if i == 2 else 0.0) for i, c in enumerate(vec3(p.Location))], (1.0, 0.95, 0.7), 110, 0.12)
            except Exception:  # noqa: BLE001
                pass
    # KH1's real spell hit a stand-in (the GTA mod's "firework hit -> GTA explosion there"): that Borderlands enemy
    # takes the spell, with the spell's impact effect on it
    for p, dk in magic_hits:
        key = "m" + bl_key(p)
        if now - last_hit.get(key, 0) < CFG.get("kh_magic_hit_gap", 0.25) or not bl_alive(p):
            continue
        last_hit[key] = now
        name, lvl = mg[0], mg[1]
        if CFG.get("kb_kh1_damage", True):
            dmg = spell_damage(S, pc, p, name, lvl)
        else:
            base = {"Fire": 1.0, "Blizzard": 0.8, "Thunder": 1.3}.get(name, 1.0)
            dmg = max_hp_share(p, CFG.get("sora_magic_frac", 0.12)) * base * (1.0 + 0.5 * (lvl - 1))
        if hurt(S, pc, p, dmg, push=CFG.get("kh_magic_push", 12000.0) if CFG.get("kh_knockback", False) else 0.0,
                lvl_scale=not CFG.get("kb_kh1_damage", True), elem=KB_SPELL_ELEM.get(name), elem_chance=S.CFG.get("kb_elem_chance", {}).get(name, 0.35) * (1.0 + 0.25 * (max(1, lvl) - 1)), spread=(max(1, lvl) - 1) if S.CFG.get("kb_spread", True) else 0):
            st["magic_hits"] = st.get("magic_hits", 0) + 1
            try:
                at = vec3(p.Location)
                if khfx_live(S):
                    pass                                  # KH1's real impact is drawn (v35)
                elif name == "Thunder":
                    fx_thunder_hit(S, at)
                else:
                    fx_impact(S, {"kind": {"Fire": "fire", "Blizzard": "ice"}.get(name, "spark"), "pos": at,
                                  "color": (1.0, 0.8, 0.4)})
                for pr in st.get("proj", []):            # the Borderlands visual for this spell ends on the hit
                    if pr.get("target") is p and pr.get("visual"):
                        pr["life"] = 0.0
            except Exception:  # noqa: BLE001
                pass
    S.status["kh1_magic"] = {"window": round(mg[2] - now, 1) if magic else 0, "hits": st.get("magic_hits", 0)}
    # place every stand-in; unused Heartless and the party wait high above Sora (never targeted, never out of the level)
    sign = CFG.get("kh_turn_sign", -1.0)
    sp, (fx, fz) = ks["pos"], ks["fwd"]
    pb, up = CFG.get("kh_park_dist", 300.0), CFG.get("kh_park_up", 1500.0)
    cx_, cz_ = kh_park_xz(S, ks)
    park = struct.pack("<3f", cx_ - fx * pb + fz * 150.0, kh_park_y(S, sp[1], up), cz_ - fz * pb - fx * 150.0)
    # session 10: a stand-in that was just fighting (its BL enemy died / left) and is thrown 1500 + 1500 away at once got
    # REMOVED by KH1 at full HP seconds later (diag: cbb0 died 00:24:59 -> gone 00:25:00, b8f0 +15 s, bda0 +2 s) -> room
    # empty -> refill reload = Sora froze, D/G vanished. Idle-parked ones live for minutes. So: kh_park_cool_s near
    # (the pre-10-05 300 / 1500 spot, never removed) before the far park that lets KH1 leave battle mode.
    # Default 0 since the real cause (KH1's fade-out) is vetoed by kh_no_fadeout - the near wait only kept battle music on.
    pbn = CFG.get("kh_park_near_dist", 300.0)
    park_near = struct.pack("<3f", cx_ - fx * pbn + fz * 150.0, kh_park_y(S, sp[1], up), cz_ - fz * pbn - fx * 150.0)
    last_paired = st.setdefault("kh_last_paired", {})
    used = []
    for a, _ in foes:
        key = pmap.get(a)
        if key is None or key not in bl:
            near_ = now - last_paired.get(a, -1e9) < CFG.get("kh_park_cool_s", 0.0)
            k32.WriteProcessMemory(h, ctypes.c_void_p(a + 0x10), park_near if near_ else park, 12, None)
            continue
        last_paired[a] = now
        if a in react:                                    # KH1 knocks it back / launches it: leave it to KH1
            try:
                going = kh1_react_step(S, ks, a, react[a], now)
            except Exception as ex:  # noqa: BLE001
                S.status["juggle_err"] = f"{type(ex).__name__}: {ex}"
                going = False
            if going:
                used.append(a)
                continue
            react.pop(a, None)
        spot, d_k = kh1_proxy_spot(S, pc, ks, bl[key][1], sign)
        dstate = (kh1_abs(S, a + 0x160, 1) or b"\x00")[0]
        if dstate in (3, KH_STATE_ATTACK) and d_k < CFG.get("kh_dummy_far", 450.0) and ks["state"] != KH_STATE_ATTACK:
            # the Heartless winds up / attacks: back off along the same bearing so it never hits KH1 Sora
            # (Borderlands enemies already hurt Borderlands Sora; KH1 HP is mirrored from there)
            f = CFG.get("kh_dummy_far", 450.0) / d_k
            spot = (sp[0] + (spot[0] - sp[0]) * f, spot[1], sp[2] + (spot[2] - sp[2]) * f)
        k32.WriteProcessMemory(h, ctypes.c_void_p(a + 0x10), struct.pack("<3f", *spot), 12, None)
        used.append(a)
    if CFG.get("kh_park_party", True) and not party_on(S):
        mpark = struct.pack("<3f", cx_ - fx * pb - fz * 150.0, kh_park_y(S, sp[1], up), cz_ - fz * pb + fx * 150.0)
        for a in st.get("kh_mates", []):
            k32.WriteProcessMemory(h, ctypes.c_void_p(a + 0x10), mpark, 12, None)
    # an ability is starting: KH1 only offers its reaction command with a target close in front, so the target's
    # stand-in (or any Heartless, with no Borderlands enemy) is pulled kh_ability_reach in front of KH1 Sora until
    # the ability is under way, then goes back to its mapped spot
    if st.get("kh_abil") and not st.get("kh_abil_started"):
        if ks["state"] != 2:
            st["kh_abil_started"] = True
        elif now < st.get("kh_abil_t0", 0) + CFG.get("kh_ability_pull_s", 1.5) and isinstance(lock_target(S), FreeTarget):
            lk_hb = lock_target(S).hb
            ent_ = next((e for e, h in st.get("free_bl", {}).items() if h is lk_hb), None)
            if ent_:
                st["kh_abil_pull_ent"] = ent_                   # the free-Heartless tick leaves this one where we put it
                reach = CFG.get("kh_ability_reach", 350.0)
                y = mapfeed_ground_ky(S, sp[0] + fx * reach, sp[2] + fz * reach, st.get("kh_floor_ref") if st.get("kh_ground_active") and st.get("kh_floor_ref") is not None else sp[1])
                k32.WriteProcessMemory(h, ctypes.c_void_p(ent_ + 0x10), struct.pack("<3f", sp[0] + fx * reach, y, sp[2] + fz * reach), 12, None)
        elif now < st.get("kh_abil_t0", 0) + CFG.get("kh_ability_pull_s", 1.5) and foes and not isinstance(lock_target(S), FreeTarget):
            lkey = bl_key(lock_target(S)) if lock_target(S) is not None else None
            pick = next((a for a in used if pmap.get(a) == lkey), None) or (used[0] if used else foes[0][0])
            reach = CFG.get("kh_ability_reach", 350.0)   # (220 shoved KH1 Sora's capsule: pose broke for 1.5 s)
            y = mapfeed_ground_ky(S, sp[0] + fx * reach, sp[2] + fz * reach, st.get("kh_floor_ref") if st.get("kh_ground_active") and st.get("kh_floor_ref") is not None else sp[1])
            k32.WriteProcessMemory(h, ctypes.c_void_p(pick + 0x10), struct.pack("<3f", sp[0] + fx * reach, y, sp[2] + fz * reach), 12, None)
            if pick not in used:
                used.append(pick)
    # Donald / Goofy stand on the Borderlands floor too - except while it is off (Sora gliding / falling): then they
    # keep KH1's own floor instead of falling with him (they stood under the glide in their falling pose)
    sh_p = kh1_shared(S)
    party_floor = party_on(S) and (CFG.get("party_glide_follow", False) or
                                   not (sh_p and st.get("kh_ground_active") and not sh_u32(sh_p, SH_FLOOR_ON)))
    # ground summons (Simba, Bambi) walk KH1's ROOM floor, 300 below the bridge floor KH1 Sora stands on -> they were
    # drawn a few feet under Sora in BL (user 2026-10-06, summon_trace: Sora y -300, Simba / Bambi y 0). The bridge
    # floor holds them at Sora's height like Donald / Goofy (first in the list: it is capped at 8). Flyers / riders
    # (Genie, Tink, Dumbo, Mushu) keep KH1's own movement. Not while the floor is off (gliding): bottomless then.
    sm_ent = st.get("kh_summon_ent", 0)
    sm_floor = ([sm_ent] if sm_ent and st.get("kh_summon_mid") in CFG.get("kh1_summon_floor_models", [4070, 4101])
                and not (sh_p and st.get("kh_ground_active") and not sh_u32(sh_p, SH_FLOOR_ON)) else [])
    S.status["summon_floor"] = hex(sm_floor[0]) if sm_floor else None
    free_ = [a for a in st.get("kh_free", []) if not st.get("free_bl", {}).get(a, {}).get("dead_until")]
    if mapfeed_active(S):
        # MAP FEED: they collide with Borderlands' real ground (kh1_mapfeed.dll) instead of the bridge's flat floor -
        # the party too while Sora glides / falls (their ground is real now); Sora himself stays on the bridge
        # every summon on Borderlands' ground (flyers too: KH1 only lands them where they really come down), except
        # the one Sora rides (Dumbo: KH1's player pointer = the summon; it stays on the bridge with him)
        sm_all = [sm_ent] if sm_ent and sm_ent != ks["addr"] and st.get("kh_summon_mid") not in CFG.get("kh1_mapfeed_summon_skip", [4100])             and (CFG.get("kh1_mapfeed_all_summons", True)
                                                               or st.get("kh_summon_mid") in CFG.get("kh1_summon_floor_models", [4070, 4101])) else []
        # PRIZES (HP / MP / munny): KH1 drops them with the SAME collision query, through one shared record (pointer
        # at 0x2D64FD0, "entity" = record - 0x2A0) -> on Borderlands' ground instead of the room 300 below KH1 Sora,
        # where KH1's pickup (|dy| <= 30, within 80 of a party member) could never reach them (user 2026-10-06)
        pz_ = kh1_mapfeed_prize_ent(S) if CFG.get("kh1_mapfeed_prizes", True) else 0
        sora_ = ks["addr"] if mapfeed_sora_on(S) else 0
        mapfeed_sora_state(S, ks, sora_)
        mapfeed_set_ents(S, ([sora_] if sora_ else []) + ([pz_] if pz_ else []) + sm_all + used
                         + [a for a in party_addrs(S) if a] + free_)
        kh1_set_proxies(S, [])
    else:
        mapfeed_set_ents(S, [])
        kh1_set_proxies(S, sm_floor + used + ([a for a in party_addrs(S) if a] if party_floor else []) + free_)
    st["kh_proxied"] = {pmap[a] for a in used}
    S.status["kh1_proxies"] = {"heartless": len(foes), "bl_enemies": len(bl), "paired": len(used),
                               "hits": st.get("proxy_hits", 0)}
    S.status["kh_juggle"] = {"on": bool(CFG.get("kh_juggle", True)), "started": st.get("juggles", 0),
                             "active": {hex(a): {"air": r["air"], "s": round(now - r["t0"], 2)} for a, r in react.items()},
                             "blocked": [k for k, v in st.get("juggle_ok", {}).items() if not v][:8]}


def kh1_abs(S, addr: int, size: int):
    buf = ctypes.create_string_buffer(size)
    ok = ctypes.windll.kernel32.ReadProcessMemory(ctypes.c_void_p(S.kh["h"]), ctypes.c_void_p(addr), buf, size, None)
    return buf.raw if ok else None


def kh1_resolve(S, hnd: int) -> int:
    """32-bit PS2-style KH1 pointer -> address (bucket table RVA 0x2EE3980)."""
    hnd &= 0x7FFFFFFF
    if not hnd:
        return 0
    b = kh1_mem(S, 0x2EE3980 + (hnd >> 25) * 8, 8)
    base = int.from_bytes(b, "little") if b else 0
    return 0 if base in (0, 0xFFFFFFFFFFFFFFFF) else base | (hnd & 0x1FFFFFF)


def kh1_handle(S, addr: int) -> int:
    """Address -> KH1's 32-bit handle (inverse of kh1_resolve: the bucket whose base holds this address)."""
    import struct
    b = kh1_mem(S, 0x2EE3980, 64 * 8)
    if not b or not addr:
        return 0
    hi = addr & ~0x1FFFFFF
    for i, base in enumerate(struct.unpack("<64Q", b)):
        if base == hi:
            return 0x80000000 | (i << 25) | (addr & 0x1FFFFFF)
    return 0


KH_ROLL, KH_GUARD = 220, 212       # Square while moving / standing (verified)
KH_SWING_ANIMS = set(range(200, 240)) - {KH_ROLL, KH_GUARD}   # 200 whiff, 201/207 combo hits, 203/213 finishers


def _ct_speed(st, key, p, now):
    """Horizontal speed (KH1 cm/s) of point p since the last call with this key (combo_trace: Sliding Dash study -
    KH1's movement vector vs real entity / hip travel). Jumps > 60 cm (pin) read as 0."""
    last = st.get(key)
    st[key] = (now, p)
    if not last or not 0 < now - last[0] < 0.1:
        return 0.0
    d = math.hypot(p[0] - last[1][0], p[2] - last[1][2])
    return 0.0 if d > 60.0 else d / (now - last[0])


def combo_trace(S, pc, ks, now: float) -> None:
    """Diagnostics (user 2026-10-06: Sliding Dash + air combos "don't work that well", no juggle into the sky):
    combo_trace.txt per tick while KH1 attacks or BL is airborne: KH1 anim, KH1 Sora height above his floor, KH1
    forward speed vs BL pawn speed, nearest stand-in (KH1 horizontal distance, height difference, airborne), and every
    new KH1 hit on a stand-in (attacker S=Sora, reaction type, damage, element code)."""
    import struct
    st = S.state
    pawn = pc.Pawn
    sp = ks.get("body", ks["pos"])
    floor = st.get("kh_floor_ref") if st.get("kh_ground_active") and st.get("kh_floor_ref") is not None else sp[1]
    near = None
    for a, _ in st.get("kh_pfoes", []):
        raw = kh1_abs(S, a + 0x10, 12)
        if not raw:
            continue
        q = struct.unpack("<3f", raw)
        dxz = math.hypot(q[0] - sp[0], q[2] - sp[2])
        if near is None or dxz < near[0]:
            near = (dxz, sp[1] - q[1], a, (q[1] < floor - 30) if floor is not None else False)
    v = pawn.Velocity
    blv = math.hypot(float(v.X), float(v.Y))
    feed = st.get("kh_hit_feed", [])
    seen = st.get("combo_trace_hit_t", 0.0)
    hits = [f for f in feed if f[0] > seen]
    if hits:
        st["combo_trace_hit_t"] = hits[-1][0]
    hs = " ".join(f"HIT[{'S' if h[2] == ks['addr'] else 'P' if h[2] in party_addrs(S) else 'x'} t{h[3]} d{h[4]} e{h[5]}]" for h in hits
                  if h[1] in dict(st.get("kh_pfoes", [])))
    line = (f"{now % 1000:8.2f} an {ks['anim']:3d} st {ks['state']} khH {int(floor - sp[1]) if floor is not None else 0:4d} "
            f"khv {int(math.hypot(ks['vel'][0], ks['vel'][2]) * 60):5d} kpos {int(_ct_speed(st, 'ct_pos', ks['pos'], now)):5d} "
            f"kbody {int(_ct_speed(st, 'ct_body', ks.get('body', ks['pos']), now)):5d} mv {int(math.hypot(*st.get('kh_mv', (0, 0)))):5d} "
            f"blv {int(blv):5d} blair {int(int(pawn.Physics) == 2)} "
            + (f"tgt d{int(near[0]):4d} dy{int(near[1]):5d} air{int(near[3])}" if near else "tgt -") + (" " + hs if hs else ""))
    with open(HERE / "combo_trace.txt", "a", encoding="utf-8") as fh:
        fh.write(line + chr(10))


def kh1_body_velocity(S, pc, ks, now: float) -> None:
    """While KH1 Sora attacks / dashes / rolls (state 6), his real ground speed drives the Borderlands pawn's velocity
    (Borderlands physics keeps collision). Sliding Dash, combo lunges and the Dodge Roll all come from KH1 this way."""
    st, CFG = S.state, S.CFG
    pos = ks.get("body", ks["pos"])
    lp, lt = st.get("kh_vpos"), st.get("kh_vt")
    if pos != lp:
        if lp and lt and 0 < now - lt < 0.1:
            dx, dz = pos[0] - lp[0], pos[2] - lp[2]
            if math.hypot(dx, dz) < 40.0:                 # larger = pin teleport
                v = ((dx * ks["fwd"][0] + dz * ks["fwd"][1]) / (now - lt), math.hypot(dx, dz) / (now - lt))
                pv = st.get("kh_v", (0.0, 0.0))
                st["kh_v"] = (pv[0] * 0.4 + v[0] * 0.6, pv[1] * 0.4 + v[1] * 0.6)
        st["kh_vpos"], st["kh_vt"] = pos, now
    elif now - (lt or 0) > 0.1:
        st["kh_v"] = (0.0, 0.0)
    pawn = pc.Pawn
    if ks["state"] != KH_STATE_ATTACK or int(pawn.Physics) == 2:
        return
    fwd_v, planar_v = st.get("kh_v", (0.0, 0.0))
    steer = (0.0, 0.0)
    if ks["anim"] == KH_ROLL:
        d = st.get("kh_roll_dir", st["yaw"])
        speed = planar_v
    else:
        d = st["yaw"] + math.radians(CFG["yaw_offset_deg"])
        speed = max(0.0, fwd_v)
        try:                                              # the player can still steer a little while swinging
            acc = pawn.Acceleration
            ax, ay = float(acc.X), float(acc.Y)
            n = math.hypot(ax, ay)
            if n > 1.0:
                k = float(pawn.GroundSpeed) * CFG.get("kh_attack_move", 0.35) / n
                steer = (ax * k, ay * k)
        except Exception:  # noqa: BLE001
            pass
    speed *= S.frame.scale * CFG.get("kh_lunge_scale", 1.0)
    vx, vy = math.cos(d) * speed + steer[0], math.sin(d) * speed + steer[1]

    def _vel(pawn=pawn, vx=vx, vy=vy):
        v = pawn.Velocity
        pawn.Velocity = unrealsdk.make_struct("Vector", X=vx, Y=vy, Z=float(v.Z))
    S.pending.append(_vel)


# ---------------------------------------------------------------- KH1 drives movement
# Like Minecraft's physics in SkyCraft: KH1 Sora's own walk/run speed, acceleration, turning and air control move the
# Borderlands pawn. Borderlands only gives the raw input direction and its collision (when a wall stops the pawn,
# KH1 Sora is told to stop too). KH1's stick is camera-relative: verified facing = C - stick_angle (deg, 0 = up,
# 90 = right) with C constant while the KH1 camera is still; C is re-estimated whenever his facing settles.
def kh1_facing_deg(ks) -> float:
    return math.degrees(math.atan2(ks["fwd"][1], ks["fwd"][0]))


def kh1_move_input(S, pc):
    """Borderlands move input -> (world yaw rad, 0..1) or (None, 0). The input acceleration Borderlands computed this tick
    is captured in the game tick (kh1_move_drive's job) before KH1's velocity replaces it."""
    st = S.state
    dm = st.get("dir_move")
    if dm and time.time() < dm[2]:                        # director "move" step
        return dm[0], max(0.0, min(1.0, dm[1]))
    ax, ay, amax = st.get("kh_in_acc", (0.0, 0.0, 1.0))
    n = math.hypot(ax, ay)
    S.status["move_input"] = [round(ax), round(ay), round(amax)]
    if n < 0.05 * amax:
        return None, 0.0
    return math.atan2(ay, ax), min(1.0, n / amax)


def kh1_move_stick(S, pc, ks, now: float):
    """Stick (lx, ly) that makes KH1 Sora head where the player is steering, relative to how both Soras face now."""
    st, CFG = S.state, S.CFG
    d_bl, mag = kh1_move_input(S, pc)
    f_kh = kh1_facing_deg(ks)
    # C (KH1 camera) estimate: once his facing has settled under a held stick, C = facing + stick
    last = st.get("kh_stick_deg")
    prev_f = st.get("kh_face_prev", f_kh)
    prev_s = st.get("kh_stick_prev")
    if prev_s is None:
        prev_s = last
    if abs(((f_kh - prev_f + 180) % 360) - 180) > 0.5 or last is None or abs(((last - prev_s + 180) % 360) - 180) > 3:
        st["kh_settle_t"] = now                           # still turning (or the stick changed): not settled
    steer_ok = ks["state"] in CFG.get("kh_steer_states", [2, 34])   # 34 = riding Dumbo (KH1 swings its camera ~240 deg)
    if last is not None and st.get("kh_stick_mag", 0) > 0.3 and steer_ok and now - st.get("kh_settle_t", now) > 0.25:
        c = f_kh + last
        old = st.get("kh_cam_c")
        st["kh_cam_c"] = c if old is None else old + (((c - old + 180) % 360) - 180) * 0.3
    st["kh_face_prev"], st["kh_stick_prev"] = f_kh, last
    if d_bl is None or now < st.get("kh_blocked_until", 0):
        st["kh_stick_mag"] = 0.0
        return 0x80, 0x80
    sign = CFG.get("kh_turn_sign", -1.0)
    ref = st.get("kh_yaw_target")
    ref = st["yaw"] if ref is None else ref
    # integral correction: running steadily but heading somewhere else than steered (KH1's camera swung, so the
    # camera estimate C is off) -> nudge C until the heading matches. Removes the "W goes north-west" drift.
    dt = min(0.1, max(0.0, now - st.get("kh_cam_int_t", now)))
    st["kh_cam_int_t"] = now
    if mag > 0.3 and steer_ok and st.get("kh_cam_c") is not None and now >= st.get("kh_blocked_until", 0):
        err = math.degrees(ang(d_bl - ref))
        if abs(err) < 100:
            st["kh_cam_c"] -= sign * err * min(1.0, CFG.get("kh_cam_gain", 3.0) * dt)
            st["kh_cam_bad_t"] = None
        else:
            # far off (KH1's battle camera swung: "forward walks backwards"): if it stays that wrong for a moment while
            # the player keeps steering, snap the camera estimate by the whole error instead of ignoring it
            st["kh_cam_bad_t"] = st.get("kh_cam_bad_t") or now
            if now - st["kh_cam_bad_t"] > CFG.get("kh_cam_snap_s", 0.25):
                st["kh_cam_c"] -= sign * err
                st["kh_cam_bad_t"] = None
                st["kh_cam_snaps"] = st.get("kh_cam_snaps", 0) + 1
                S.status["kh_cam_snaps"] = st["kh_cam_snaps"]
    theta = math.degrees(ang(d_bl - ref))                 # turn wanted, relative to the KH1-matched facing
    f_target = f_kh + sign * theta                        # same relative turn for KH1 Sora
    c = st.get("kh_cam_c")
    if c is None:                                          # estimate reset (warp / menu): start from the default
        c = st["kh_cam_c"] = CFG.get("kh_cam_c", -171.2)
    sdeg = c - f_target
    st["kh_stick_deg"], st["kh_stick_mag"] = sdeg, mag
    k = min(1.0, mag * CFG.get("kh_stick_gain", 1.0))
    D = CFG.get("kh_stick_deadzone", 60.6)               # KH1 per-axis dead zone (measured): |raw| < D counts as 0

    def axis(u):                                           # wanted effective -1..1 -> raw offset that KH1 reads as u
        if abs(u) < 0.01:
            return 0
        return int(math.copysign(D + abs(u) * (127 - D), u))
    lx = 0x80 + axis(math.sin(math.radians(sdeg)) * k)
    ly = 0x80 - axis(math.cos(math.radians(sdeg)) * k)
    return max(0, min(255, lx)), max(0, min(255, ly))


def kh1_pace(S, pc, ks, dt: float) -> float:
    """Borderlands-sized pace that still plays like KH1. KH1 Sora runs 480 cm/s = 446 BL units/s, Borderlands' WALK
    speed (players sprint almost all the time), so in BL he felt slow. While he walks/runs (kh_pace_anims) his travel
    AND his KH1 motion speed (entity +0x284: clock += dt x speed, from the friend's RE journal) are scaled by the same
    factor: a quicker run cadence, no foot sliding. Borderlands' sprint key = a KH2 Quick Run-style dash (kh_dash_pace).
    Jumps keep the pace they started with (no braking at take-off); combat, rolls and glides keep KH1's exact timing.
    Config: kh_pace 1.2, kh_dash_pace 1.33 (= BL sprint 594, BL caps the dash there), kh_pace_anims [1, 2], kh_pace_ease 8 (per second)."""
    st, CFG = S.state, S.CFG
    anim = ks["anim"]
    loco = anim in CFG.get("kh_pace_anims", [1, 2])
    air = 8 <= anim <= 12
    try:
        dash = bool(pc.bIsSprinting)
    except Exception:  # noqa: BLE001
        dash = False
    u32 = ctypes.windll.user32                             # F = KH1 Triangle (reaction commands: Strike Raid...)
    pid0 = ctypes.c_ulong(); u32.GetWindowThreadProcessId(u32.GetForegroundWindow(), ctypes.byref(pid0))
    tri = pid0.value == os.getpid() and any(u32.GetAsyncKeyState(vk) & 0x8000 for vk in CFG.get("kh_triangle_keys", [0x46]))
    if tri and not st.get("kh_tri_down"):
        kh1_press(S, PAD_TRIANGLE, 0.1)
    st["kh_tri_down"] = bool(tri)
    if not dash:                                           # BL never sets it while we drive the pawn: read the key
        u32 = ctypes.windll.user32
        pid = ctypes.c_ulong()
        u32.GetWindowThreadProcessId(u32.GetForegroundWindow(), ctypes.byref(pid))
        if pid.value == os.getpid():
            dash = any(u32.GetAsyncKeyState(vk) & 0x8000 for vk in CFG.get("kh_dash_keys", [0xA0]))   # left Shift
    cur = st.get("kh_pace", 1.0)
    if loco:
        target = CFG.get("kh_dash_pace", 1.33) if dash else CFG.get("kh_pace", 1.2)
    elif air:
        target = cur
    else:
        target = 1.0
    cur += (target - cur) * min(1.0, CFG.get("kh_pace_ease", 8.0) * dt)
    st["kh_pace"] = cur
    # KH1 legs at the same pace (only during locomotion; put 1.0 back on leaving it unless KH1 already changed it)
    kh = getattr(S, "kh", None)
    if kh and kh.get("h") and ks.get("addr"):
        val = cur if loco else None
        if val is None and st.get("kh_pace_wrote") is not None and abs(ks.get("mspeed", 1.0) - st["kh_pace_wrote"]) < 1e-4:
            val = 1.0
        if val is not None:
            buf = ctypes.c_float(val)
            ctypes.windll.kernel32.WriteProcessMemory(ctypes.c_void_p(kh["h"]), ctypes.c_void_p(ks["addr"] + 0x284),
                                                      ctypes.byref(buf), 4, None)
            st["kh_pace_wrote"] = val if loco else None
    S.status["kh_pace"] = [round(cur, 3), anim, dash, round(ks.get("mspeed", 0), 3)]
    return cur


def kh1_move_drive(S, pc, pawn, ks, now: float) -> None:
    """KH1 Sora's real velocity and turning -> Borderlands pawn (velocity, so Borderlands collision still applies)."""
    st, CFG = S.state, S.CFG
    pos = ks.get("body", ks["pos"])
    # integrate KH1 travel into a "virtual" position: pin teleports (>40 cm jumps) count as zero travel
    lastp, virt = st.get("kh_mlast"), st.get("kh_mvirt", (0.0, 0.0))
    if lastp is not None and pos != lastp:
        dx, dz = pos[0] - lastp[0], pos[2] - lastp[2]
        if math.hypot(dx, dz) < 40.0:
            virt = (virt[0] + dx, virt[1] + dz)
    st["kh_mlast"], st["kh_mvirt"] = pos, virt
    hist = st.setdefault("kh_mhist", [])
    if not hist or virt != hist[-1][1] or (lastp is not None and pos != lastp):
        hist.append((now, (virt[0], 0.0, virt[1])))
    while len(hist) > 2 and now - hist[0][0] > CFG.get("kh_vel_window", 0.25):
        hist.pop(0)
    if len(hist) >= 2 and now - hist[-1][0] < 0.1 and hist[-1][0] > hist[0][0]:
        dt_w = hist[-1][0] - hist[0][0]
        v = ((hist[-1][1][0] - hist[0][1][0]) / dt_w, (hist[-1][1][2] - hist[0][1][2]) / dt_w)
        pv = st.get("kh_mv", v)
        st["kh_mv"] = (pv[0] * 0.5 + v[0] * 0.5, pv[1] * 0.5 + v[1] * 0.5)
    elif not hist or now - hist[-1][0] > 0.1:
        st["kh_mv"] = (0.0, 0.0)
    sign = CFG.get("kh_turn_sign", -1.0)
    # turning: Borderlands Sora turns exactly as much as KH1 Sora did; KH1 turns in 60 Hz steps, so the Borderlands
    # facing glides toward the accumulated target instead of stair-stepping at the higher Borderlands frame rate
    f_kh = kh1_facing_deg(ks)
    pf = st.get("kh_mface")
    if st.get("kh_yaw_target") is None:
        st["kh_yaw_target"] = st["yaw"]
    if pf is not None:
        dturn = ((f_kh - pf + 180) % 360) - 180
        if abs(dturn) < 60:
            st["kh_yaw_target"] = ang(st["kh_yaw_target"] + sign * math.radians(dturn))
    st["kh_mface"] = f_kh
    dt = min(0.1, max(0.0, now - st.get("kh_mdrive_t", now)))
    st["kh_mdrive_t"] = now
    st["yaw"] = ang(st["yaw"] + ang(st["kh_yaw_target"] - st["yaw"]) * min(1.0, CFG.get("kh_turn_smooth", 30.0) * dt))
    # velocity: KH1's own per-frame movement (x60 -> cm/s), else the measured estimate
    if ks.get("vel") is not None and CFG.get("kh_use_kh_vel", True):
        vx, vz = ks["vel"][0] * 60.0, ks["vel"][2] * 60.0
    else:
        vx, vz = st.get("kh_mv", (0.0, 0.0))
    fx, fz = ks["fwd"]
    fwd = vx * fx + vz * fz
    side = vx * -fz + vz * fx
    sc = S.frame.scale * CFG.get("kh_move_scale", 1.0) * kh1_pace(S, pc, ks, dt)
    if ks["anim"] in KH_GLIDE_ANIMS:
        # GLIDE RAMP (user 2026-10-12: slower than vanilla KH1): KH1's glide builds speed, but we fly KH1 with the stick
        # neutral (no turn-animation stutter), so KH1 stays at its start speed. Borderlands ramps instead: x kh_glide_speed
        # 1.5 at takeoff -> x kh_glide_speed_max 2.3 after kh_glide_ramp_s 1.2 s (measured before: ~670 UU/s flat).
        t0 = st.setdefault("kh_glide_t0", now)
        k = min(1.0, (now - t0) / max(0.05, CFG.get("kh_glide_ramp_s", 1.2)))
        g0, g1 = CFG.get("kh_glide_speed", 1.5), CFG.get("kh_glide_speed_max", 2.3)
        sc *= g0 + (g1 - g0) * k * k * (3 - 2 * k)               # smoothstep: no jolt at either end
    else:
        st.pop("kh_glide_t0", None)
    y = st["yaw"] + math.radians(CFG["yaw_offset_deg"])
    bx = (math.cos(y) * fwd - math.sin(y) * side * sign) * sc
    by = (math.sin(y) * fwd + math.cos(y) * side * sign) * sc
    if ks["anim"] in KH_GLIDE_ANIMS and st.get("kh_glide_on"):
        # gliding: KH1 Sora flies straight (no stick, so KH1 never switches to its turn animations - every switch froze
        # the pose ~0.1 s, "stop motion"); Borderlands turns him smoothly toward the input, at KH1's glide speed
        d_in, _mag = kh1_move_input(S, pc)
        if d_in is not None:
            turn = ang(d_in - st["yaw"])
            step = CFG.get("kh_glide_turn", 2.5) * dt
            st["yaw"] = ang(st["yaw"] + max(-step, min(step, turn)))
        st["kh_yaw_target"] = st["yaw"]
        gs = max(math.hypot(vx, vz), CFG.get("kh_glide_min_speed", 450.0)) * sc
        y = st["yaw"] + math.radians(CFG["yaw_offset_deg"])
        bx, by = math.cos(y) * gs, math.sin(y) * gs
    # Borderlands blocked us (wall): tell KH1 Sora to stop instead of running in place
    want = math.hypot(bx, by)
    v = pawn.Velocity
    got = math.hypot(float(v.X), float(v.Y))
    if want > 150 and got < want * 0.3 and int(pawn.Physics) != 2:
        st["kh_block_t"] = st.get("kh_block_t") or now
        if now - st["kh_block_t"] > 0.25:
            st["kh_blocked_until"] = now + 0.3
    else:
        st["kh_block_t"] = None
    S.status["kh_move"] = [round(fwd), round(side), round(want), round(got), round(st.get("kh_cam_c") or 0.0, 1)]
    if CFG.get("dbg_move_trace") and now - st.get("mtr_t", 0) > 0.1:
        # movement trace (Dumbo: "W sometimes goes left / backwards"): KH1 facing vs KH1 travel vs steer, per 0.1 s
        st["mtr_t"] = now
        d_in, mg_ = kh1_move_input(S, pc)
        with open(HERE / "move_trace.txt", "a") as fh_:
            fh_.write(f"{now % 10000:.2f} ent {ks['addr']:#x} summon {ks['addr'] == st.get('kh_summon_ent')} anim {ks['anim']} "
                      f"st {ks['state']} face {f_kh:.0f} kvel {math.degrees(math.atan2(vz, vx)):.0f} |{math.hypot(vx, vz):.0f}| "
                      f"mv {math.degrees(math.atan2(st.get('kh_mv', (0, 0))[1], st.get('kh_mv', (0, 0))[0])):.0f} "
                      f"|{math.hypot(*st.get('kh_mv', (0, 0))):.0f}| stick {st.get('kh_stick_deg') or 0:.0f} mag {st.get('kh_stick_mag', 0):.2f} "
                      f"camC {st.get('kh_cam_c') or 0:.0f} kh1cam {kh1_cam_yaw(S) or 0:.0f} in {math.degrees(d_in) if d_in is not None else -999:.0f} "
                      f"yaw {math.degrees(st['yaw']):.0f} ytgt {math.degrees(st.get('kh_yaw_target') or 0):.0f} fwd {fwd:.0f} side {side:.0f}" + chr(10))

    # SLIDING DASH (user 2026-10-06: "slow, only 1-2 ft in BL vs 5-8 ft in KH1"): combo_trace showed KH1's dash at
    # 1041 cm/s (vector and real travel agree) but the pawn never above 440 = Borderlands' GroundSpeed cap clipping the
    # velocity we set. While KH1 Sora attacks and needs more, the cap is raised to his speed; restored right after
    # (running / sprinting keep Borderlands' own cap). Config kh_attack_uncap.
    boost = ks["state"] == KH_STATE_ATTACK and CFG.get("kh_attack_uncap", True)
    need = math.hypot(bx, by)

    def _vel(pawn=pawn, bx=bx, by=by, boost=boost, need=need):
        try:
            if boost and need > float(st.get("bl_gs_saved") or pawn.GroundSpeed):
                if st.get("bl_gs_saved") is None:
                    st["bl_gs_saved"] = float(pawn.GroundSpeed)
                pawn.GroundSpeed = need * 1.02
            elif st.get("bl_gs_saved") is not None and not boost:
                pawn.GroundSpeed = st.pop("bl_gs_saved")
        except Exception as ex:  # noqa: BLE001
            S.status["gs_error"] = str(ex)
        acc = pawn.Acceleration                            # this tick's player input (Borderlands just computed it)
        try:
            amax = float(pawn.AccelRate) or 1.0
        except Exception:  # noqa: BLE001
            amax = max(1.0, math.hypot(float(acc.X), float(acc.Y)))
        st["kh_in_acc"] = (float(acc.X), float(acc.Y), amax)
        vv = pawn.Velocity
        pawn.Velocity = unrealsdk.make_struct("Vector", X=bx, Y=by, Z=float(vv.Z))
        n = math.hypot(bx, by)                             # tiny acceleration along the motion: no braking, no input drift
        pawn.Acceleration = unrealsdk.make_struct("Vector", X=bx / n if n > 1 else 0.0, Y=by / n if n > 1 else 0.0, Z=float(acc.Z))
    S.pending.append(_vel)


# Shared memory with the KH1 bridge (bridge/shared.h, packed, 256 bytes).
SH_WALLS_OFF, SH_WALLS_REMOVED, SH_WORLD_HOOKS = 32, 36, 40
SH_GROUND_MODE, SH_FLOOR_ON, SH_FLOOR_Y, SH_SNAP_SEQ, SH_LIFT_TOTAL, SH_FALL_LIMIT, SH_FALL_LIFT = 44, 48, 52, 56, 60, 64, 68
SH_GROUND_CLAMPS, SH_FLOOR_POLY, SH_SORA_AIR, SH_NPROXY, SH_PROXY, SH_BRIDGE_VERSION = 72, 76, 80, 84, 88, 152
SH_MAX_PROXIES = 8


def kh1_shared(S):
    sh = getattr(S, "kh_sh", None)
    if sh is None or getattr(S, "kh_sh_size", 0) < 256:      # (older logic.py mapped only 128 bytes)
        k32 = ctypes.windll.kernel32
        k32.OpenFileMappingW.restype = ctypes.c_void_p
        k32.MapViewOfFile.restype = ctypes.c_void_p
        h = k32.OpenFileMappingW(0xF001F, False, r"Local\SoraPassthrough")
        view = k32.MapViewOfFile(ctypes.c_void_p(h), 0xF001F, 0, 0, 256) if h else None
        sh = S.kh_sh = view or 0
        S.kh_sh_size = 256 if view else 0
    return sh


def sh_u32(sh, off, value=None):
    v = ctypes.c_uint32.from_address(sh + off)
    if value is not None:
        v.value = int(value) & 0xFFFFFFFF
    return v.value


def sh_f32(sh, off, value=None):
    v = ctypes.c_float.from_address(sh + off)
    if value is not None:
        v.value = float(value)
    return v.value


def kh1_bridge_v3(S) -> bool:
    sh = kh1_shared(S)
    return bool(sh) and sh_u32(sh, SH_BRIDGE_VERSION) >= 3


def kh1_set_proxies(S, addrs) -> None:
    """The KH1 entities standing in for Borderlands enemies: the bridge gives them the Borderlands floor too."""
    sh = kh1_shared(S)
    if not sh:
        return
    addrs = list(addrs)[:SH_MAX_PROXIES]
    sh_u32(sh, SH_NPROXY, 0)                              # list is rewritten while the count is 0
    for i, a in enumerate(addrs):
        ctypes.c_uint64.from_address(sh + SH_PROXY + 8 * i).value = a
    sh_u32(sh, SH_NPROXY, len(addrs))


# ---------------------------------------------------------------- MAP FEED: Borderlands ground -> KH1 collision
# (2026-10-06 night) Borderlands' own line check, called natively from here (logic runs on BL's game thread):
# Launcher.exe is SteamStub-encrypted on disk -> RE'd from a memory dump (D:/SoraBL/ghidra/dump_bl.py, project blproj):
# AActor::execTrace / execFastTrace (native table "AActorexecTrace") both call UWorld::SingleLineCheck at RVA 0x6D4370
# (GWorld, FCheckResult* hit, AActor* source, FVector* end, FVector* start, u32 flags, FVector* extent, 0, 0) ->
# TRUE = nothing hit; hit +8 actor, +0x10 location, +0x1C normal, +0x28 time. GWorld = *(base + 0x25EEB58).
# Flags 0x2086 = movers | level | level geometry | terrain-ignore-holes (Actor.Trace with bTraceActors False).
# ~6 us per trace. The ground grid around Sora goes to kh1_mapfeed.dll (in KH1) via "Local\SoraMapFeed", which turns
# it into KH1 collision polygons for the listed KH1 entities (party, stand-ins, ground summons, free Heartless).
BL_RVA_GWORLD, BL_RVA_SLC, BL_TRACE_WORLD = 0x25EEB58, 0x6D4370, 0x2086
MF_DLL = "kh1_mapfeed.dll"
MF_N_MAX = 96
MF_SIZE = 256 + 4 * MF_N_MAX * MF_N_MAX
MF_MAGIC = 0x31464D53


# BL build check (release R1): Launcher.exe is SteamStub-encrypted on disk, so the check is on the decrypted code in
# memory: the first 32 bytes of UWorld::SingleLineCheck. Mismatch -> never call it; the map feed stays off.
BL_SLC_BYTES = bytes.fromhex("488bc45541564157488bec4881ec8000000048c745d0feffffff488958084889")


def bl_trace_ok(S) -> bool:
    ok = getattr(S, "bl_slc_ok", None)
    if ok is None:
        k32 = ctypes.windll.kernel32
        k32.GetModuleHandleW.restype = ctypes.c_void_p
        base = k32.GetModuleHandleW(None)
        ok = S.bl_slc_ok = ctypes.string_at(base + BL_RVA_SLC, len(BL_SLC_BYTES)) == BL_SLC_BYTES
        if not ok:
            kh1_event(S, "Borderlands build NOT supported (line check bytes differ) -> map feed off")
            say(S, "Sora mod: this Borderlands version isn't supported for ground matching - map feed off")
    S.status["bl_build_ok"] = ok
    return ok


def bl_tracer(S):
    t = getattr(S, "bl_tracer", None)
    if t is None:
        k32 = ctypes.windll.kernel32
        k32.GetModuleHandleW.restype = ctypes.c_void_p
        base = k32.GetModuleHandleW(None)
        slc = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(base + BL_RVA_SLC)
        t = S.bl_tracer = {"base": base, "slc": slc, "hit": (ctypes.c_char * 0x80)(), "a": (ctypes.c_float * 3)(),
                           "b": (ctypes.c_float * 3)(), "ex": (ctypes.c_float * 3)()}
    return t


def bl_trace(S, a, b, flags=BL_TRACE_WORLD):
    """Borderlands' native line check a -> b (world geometry): None = clear, else (location, normal, time)."""
    if not bl_trace_ok(S):
        return None
    t = bl_tracer(S)
    gw = ctypes.c_uint64.from_address(t["base"] + BL_RVA_GWORLD).value
    if not gw:
        return None
    hit = t["hit"]
    ctypes.memset(hit, 0, 0x80)
    t["a"][:] = a
    t["b"][:] = b
    if t["slc"](gw, hit, None, t["b"], t["a"], flags, t["ex"], None, None):
        return None
    return struct.unpack_from("<3f", hit, 0x10), struct.unpack_from("<3f", hit, 0x1C), struct.unpack_from("<f", hit, 0x28)[0]


def mapfeed_map(S):
    """kh1_mapfeed.dll's block (Local SoraMapFeed) as a writable buffer, or None (DLL not loaded)."""
    import ctypes.wintypes as W
    m = getattr(S, "mf_map", None)
    if m is not None and struct.unpack_from("<I", m, 0)[0] == MF_MAGIC:
        return m
    if time.time() < S.state.get("mf_map_retry", 0):
        return None
    S.state["mf_map_retry"] = time.time() + 3.0
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenFileMappingW.restype = W.HANDLE
    k32.MapViewOfFile.restype = ctypes.c_void_p
    k32.MapViewOfFile.argtypes = [W.HANDLE, W.DWORD, W.DWORD, W.DWORD, ctypes.c_size_t]
    h = k32.OpenFileMappingW(0x0006, False, "Local" + chr(92) + "SoraMapFeed")
    p = h and k32.MapViewOfFile(h, 0x0006, 0, 0, MF_SIZE)
    if not p:
        return None
    m = (ctypes.c_char * MF_SIZE).from_address(p)
    if struct.unpack_from("<I", m, 0)[0] != MF_MAGIC:
        return None
    S.mf_map = m
    return m


def mapfeed_inject(S) -> None:
    """Loads data/<kh1_mapfeed_dll> into KH1 once per KH1 process, AFTER kh1_fx (hook chain order bridge -> kh1_fx ->
    mapfeed; unload in reverse). Any loaded kh1_mapfeed*.dll counts (hot swaps use versioned file names)."""
    st, CFG = S.state, S.CFG
    pid = (getattr(S, "kh", None) or {}).get("pid")
    if not CFG.get("kh1_mapfeed", False) or not bl_trace_ok(S) or not pid or st.get("mf_injected") == pid or st.get("khfx_injected") != pid             or time.time() < st.get("mf_inject_t", 0):
        return
    st["mf_inject_t"] = time.time() + 10.0
    have = kh1_has_module(pid, "kh1_mapfeed*")
    if have is None:
        return
    if not have:
        path = HERE / "data" / CFG.get("kh1_mapfeed_dll", "kh1_mapfeed_v3.dll")
        if not path.exists() or not _kh1_load(S, pid, str(path.resolve())):
            S.status["mapfeed_inject"] = f"failed ({path.name})"
            return
        S.mf_map = None                                              # re-open the shared view
        kh1_event(S, f"{path.name} injected into KH1 (pid {pid})")
    st["mf_injected"] = pid


def mapfeed_active(S) -> bool:
    m = mapfeed_map(S) if S.CFG.get("kh1_mapfeed", False) and bl_trace_ok(S) else None
    # (the shared block outlives a KH1 restart with hooked = 1: also require the DLL injected into THIS KH1 process)
    return bool(m) and struct.unpack_from("<I", m, 8)[0] == 1 and bool(S.state.get("mf_live"))         and S.state.get("mf_injected") == (getattr(S, "kh", None) or {}).get("pid")


def kh1_mapfeed_prize_ent(S) -> int:
    """KH1's shared prize collision record (FUN_1402ac4d0 runs FUN_140195180 on *(0x2D64FD0)) as a feed 'entity'."""
    d = int.from_bytes(kh1_mem(S, 0x2D64FD0, 8) or bytes(8), "little")
    S.state["mf_prize_ent"] = d - 0x2A0 if d else 0
    return S.state["mf_prize_ent"]


def mapfeed_sora_on(S) -> bool:
    """SORA ON THE BORDERLANDS GROUND (user 2026-10-06): KH1 Sora collides with the fed terrain himself (jumps onto
    rocks, walks off ledges by KH1's own physics) instead of the bridge's switched flat floor. Not while gliding (the
    glide needs the bridge's bottomless floor), during a summon (KH1's player is the summon then) or the F9 menu.
    Config kh1_mapfeed_sora."""
    st = S.state
    return bool(S.CFG.get("kh1_mapfeed_sora", True)) and mapfeed_active(S) and not st.get("kh_glide_on")         and kh1_player_is_sora(S) and not st.get("kh_intro_view") and not st.get("kh_menu_open")


def kh1_player_is_sora(S) -> bool:
    """KH1's player pointer (0x2537E48) is Sora's own party entity (not 0 = summon intro, not Dumbo = riding)."""
    p = int.from_bytes(kh1_mem(S, KH_SORA_PTR, 8) or bytes(8), "little")
    lead = int.from_bytes(kh1_mem(S, KH_PARTY[0], 8) or bytes(8), "little")
    return bool(p) and p == lead


def mapfeed_set_ents(S, ents) -> None:
    S.state["mf_ents"] = [e for e in ents if e][:16]


def mapfeed_tick(S, pc, ks, now: float) -> None:
    """Borderlands' ground on a grid around Sora (cell kh1_mapfeed_cell BL units, n x n, nearest cells sampled first,
    kh1_mapfeed_budget traces per tick) + the KH1 <-> BL transform (same mapping as kh1_point_to_bl) -> kh1_mapfeed.dll.
    Heights: KH1 y = KH1 Sora's floor (kh_floor_ref) - (BL z - BL Sora's grounded feet z) / scale."""
    st, CFG = S.state, S.CFG
    m = mapfeed_map(S)
    if not CFG.get("kh1_mapfeed", False) or not bl_trace_ok(S) or not m or not ks:
        if m and struct.unpack_from("<I", m, 16)[0]:
            struct.pack_into("<I", m, 16, 0)
        st["mf_live"] = False
        S.status["mapfeed"] = {"on": False, "dll": bool(m)}
        return
    f = S.frame
    pawn = pc.Pawn if pc else None
    cell = float(CFG.get("kh1_mapfeed_cell", 40.0))
    n = int(min(MF_N_MAX, CFG.get("kh1_mapfeed_n", 64)))
    sc = max(f.scale, 0.01)
    if pawn is not None and (int(pawn.Physics) != 2 or st.get("mf_ref_bz") is None):
        st["mf_ref_bz"] = f.pos[2]                                   # BL Sora's feet while on the ground
    ref_bz = st["mf_ref_bz"]
    ref_ky = st.get("kh_floor_ref") if st.get("kh_floor_ref") is not None else ks["pos"][1]
    # grid window (re-centred when Sora walks a quarter of it away)
    cx, cy = int(math.floor(f.pos[0] / cell)), int(math.floor(f.pos[1] / cell))
    g0 = st.get("mf_g0")
    if g0 is None or st.get("mf_g0_cell") != (cell, n) or abs(cx - (g0[0] + n // 2)) > n // 4 or abs(cy - (g0[1] + n // 2)) > n // 4:
        g0 = st["mf_g0"] = (cx - n // 2, cy - n // 2)
        st["mf_g0_cell"] = (cell, n)
        st["mf_dirty"] = True
    cache = st.setdefault("mf_cache", {})
    if st.get("mf_cache_cell") != cell:
        cache.clear()
        st["mf_cache_cell"] = cell
    order = getattr(S, "mf_order", None)
    if order is None or len(order) != n * n:
        order = S.mf_order = sorted(((i - n // 2, j - n // 2) for i in range(n) for j in range(n)),
                                    key=lambda o: o[0] * o[0] + o[1] * o[1])
    budget = int(CFG.get("kh1_mapfeed_budget", 300))
    up, down = CFG.get("kh1_mapfeed_up", 400.0), CFG.get("kh1_mapfeed_down", 1500.0)
    stale = CFG.get("kh1_mapfeed_stale", 400.0)
    zr = ref_bz                                       # (not f.pos: gliding 20 m up the traces missed the ground)
    sampled = 0
    for ox, oy in order:
        if sampled >= budget:
            break
        k = (cx + ox, cy + oy)
        c = cache.get(k)
        if c is not None and abs(c[1] - zr) < stale:
            continue
        x, y = k[0] * cell, k[1] * cell
        r = bl_trace(S, (x, y, zr + up), (x, y, zr - down))
        if r is None and c is not None and c[0] == c[0]:
            cache[k] = (c[0], zr)                                       # keep known ground over a miss
        else:
            cache[k] = (r[0][2] if r else float("nan"), zr)
        sampled += 1
        st["mf_dirty"] = True
    if len(cache) > 4 * n * n:                                           # forget far cells
        for k in [k for k in cache if abs(k[0] - cx) > n or abs(k[1] - cy) > n]:
            cache.pop(k, None)
    # transform: KH1 offset (x, z) from KH1 Sora -> BL offset from BL Sora (kh1_point_to_bl as a matrix)
    fx, fz = ks["fwd"]
    sign = CFG.get("kh_turn_sign", -1.0)
    yb = (st.get("kh_yaw_target") if st.get("kh_move_on") and st.get("kh_yaw_target") is not None else st["yaw"]) \
        + math.radians(CFG["yaw_offset_deg"])
    cb, sb = math.cos(yb), math.sin(yb)
    m00, m01 = sc * (cb * fx + sb * sign * fz), sc * (cb * fz - sb * sign * fx)
    m10, m11 = sc * (sb * fx - cb * sign * fz), sc * (sb * fz + cb * sign * fx)
    xf = (f.pos[0], f.pos[1], ks["pos"][0], ks["pos"][2], m00, m01, m10, m11, ref_bz, ref_ky, sc)
    # SMOOTHED (user 2026-10-06: zig-zagging A / D while running, Donald and Goofy moved "all weird, synced up to
    # Sora"): rebuilt from both Soras every tick, every small mismatch between BL Sora's and KH1 Sora's movement / turn
    # dragged the whole party rigidly with him. Now the previous mapping, re-anchored at KH1 Sora (minus the pin's
    # shift, which moves Sora and the party together), eases toward the live one over kh1_mapfeed_smooth_s.
    prev = st.get("mf_xf")
    pin_dx, pin_dz = 0.0, 0.0                       # (the pin shifts st["mf_xf"] itself: mapfeed_shift)
    tau = CFG.get("kh1_mapfeed_smooth_s", 0.6)
    dt_s = min(0.1, max(0.0, now - st.get("mf_t", now)))
    st["mf_t"] = now
    if prev is not None and tau > 0 and not st.get("mf_xf_held"):
        ox_, oz_ = ks["pos"][0] - pin_dx - prev[2], ks["pos"][2] - pin_dz - prev[3]
        fpx, fpy = prev[0] + prev[4] * ox_ + prev[5] * oz_, prev[1] + prev[6] * ox_ + prev[7] * oz_
        ph_p, ph_t = math.atan2(prev[6], prev[4]), math.atan2(m10, m00)
        dph = ((ph_t - ph_p + math.pi) % (2 * math.pi)) - math.pi
        drift = math.hypot(f.pos[0] - fpx, f.pos[1] - fpy)
        S.status["mapfeed_smooth"] = [round(drift, 1), round(math.degrees(dph), 1)]
        if drift < CFG.get("kh1_mapfeed_snap", 400.0) and abs(dph) < math.radians(CFG.get("kh1_mapfeed_snap_deg", 60.0)):
            k_ = 1.0 - math.exp(-dt_s / tau)
            ph = ph_p + dph * k_
            cph, sph = math.cos(ph), math.sin(ph)
            xf = (fpx + (f.pos[0] - fpx) * k_, fpy + (f.pos[1] - fpy) * k_, ks["pos"][0], ks["pos"][2],
                  sc * cph, -sc * sph * sign, sc * sph, sc * cph * sign, ref_bz, ref_ky, sc)
    # GLIDE (user 2026-10-06: Donald / Goofy fell through the map while gliding): KH1 Sora hangs over his anchor while
    # BL Sora flies on, so a Sora-to-Sora mapping slid the whole BL terrain under the party in KH1 at glide speed
    # (they ended up 250 below it). While the bridge floor is off the mapping is HELD: the terrain stays put in KH1,
    # the party keeps walking on it near the takeoff (drawn through the same held mapping), and catches up on landing.
    sh_ = kh1_shared(S)
    held = bool(sh_ and st.get("kh_ground_active") and not sh_u32(sh_, SH_FLOOR_ON))
    if held and st.get("mf_xf") is not None:
        xf = st["mf_xf"]
    elif st.get("mf_xf") is not None and CFG.get("kh1_mapfeed_keep", True):
        # HEIGHT every tick: landing lower / higher than the takeoff jumped the whole terrain in KH1 and they ended up
        # under it (16 rescues in the user's glide tests) -> they move up / down with it. SIDEWAYS only on the landing
        # tick after a glide (BL steered while KH1 flew straight). Not while walking: the mapping follows Sora by design
        # and the pin shifts them itself - keeping their BL spot there left them behind (user: "they stay where they
        # are until I stop, then jump high and glide over to me")
        mapfeed_remap(S, st["mf_xf"], xf, sideways=bool(st.get("mf_xf_held")))
    st["mf_xf"], st["mf_xf_held"] = xf, held
    ents = st.get("mf_ents", [])
    seq = struct.unpack_from("<I", m, 20)[0]
    seq = seq + 1 if not seq & 1 else seq
    struct.pack_into("<I", m, 20, seq)                                     # odd: writing
    struct.pack_into("<I", m, 24, len(ents))
    for i, e in enumerate(ents):
        struct.pack_into("<Q", m, 32 + 8 * i, e)
    struct.pack_into("<fiii2f2f4f3f", m, 160, cell, g0[0], g0[1], n, *xf)
    struct.pack_into("<I", m, 244, int(CFG.get("kh1_mapfeed_max_cells", 400)))
    if st.pop("mf_dirty", False):
        nan = float("nan")
        hs = [cache.get((g0[0] + i, g0[1] + j), (nan, 0))[0] for j in range(n) for i in range(n)]
        struct.pack_into(f"<{n * n}f", m, 256, *hs)
        st["mf_known"] = sum(1 for h in hs if h == h)
    struct.pack_into("<I", m, 16, 1)                                       # enabled
    struct.pack_into("<I", m, 20, seq + 1)                                 # even: done
    st["mf_live"] = True
    mapfeed_rescue(S, ents, now)
    hooked = struct.unpack_from("<I", m, 8)[0]
    q_fed, q_polys, q_empty, gathers, fpoly, last, _, faults = struct.unpack_from("<IIIIiIII", m, 220)
    S.status["mapfeed"] = {"on": True, "hooked": hooked, "ents": len(ents), "known": st.get("mf_known", 0),
                           "of": n * n, "sampled": sampled, "q_fed": q_fed, "gathers": gathers, "polys": q_polys,
                           "last": last, "empty": q_empty, "faults": faults, "floor_poly": fpoly,
                           "ref": [round(ref_bz, 1), round(ref_ky, 1)], "held": held,
                           "rescued": st.get("mf_rescued", 0), "glide_shifts": st.get("mf_glide_shifts", 0),
                           "remaps": st.get("mf_remaps", 0)}


def mapfeed_to_bl(S, pos):
    """KH1 point -> Borderlands point through the mapping kh1_mapfeed.dll uses right now (held during a glide)."""
    Fx, Fy, Kx, Kz, m00, m01, m10, m11, ref_bz, ref_ky, sc = S.state["mf_xf"]
    ox, oz = pos[0] - Kx, pos[2] - Kz
    return (Fx + m00 * ox + m01 * oz, Fy + m10 * ox + m11 * oz, ref_bz - (pos[1] - ref_ky) * sc)


def kh1_shift_prizes(S, dx: float, dz: float) -> None:
    """Live KH1 prizes (pool 0x2432140 + 0x20 * k: pos @0, flags @0x19 bit0) move with KH1 Sora when the pin /
    glide re-centre puts him back on the anchor. Left behind, every re-centre (each 2.5 m walked) carried them
    along with Sora in Borderlands -> "the orbs stay at the same distance, I can't walk to them" (user 2026-10-06)."""
    raw = kh1_mem(S, KH_PRIZE_OBJS, 0x20 * 256)
    if not raw or not getattr(S, "kh", None):
        return
    h = ctypes.c_void_p(S.kh["h"])
    base = S.kh["base"] + KH_PRIZE_OBJS
    n = 0
    for k in range(256):
        if raw[k * 0x20 + 0x19] & 1:
            x, _, z = struct.unpack_from("<3f", raw, k * 0x20)
            ctypes.windll.kernel32.WriteProcessMemory(h, ctypes.c_void_p(base + k * 0x20), struct.pack("<f", x + dx), 4, None)
            ctypes.windll.kernel32.WriteProcessMemory(h, ctypes.c_void_p(base + k * 0x20 + 8), struct.pack("<f", z + dz), 4, None)
            n += 1
    # ITEM DROPS too (session 9: the item balls slid with Sora because only the prizes were re-centred - each pin
    # step carried them ~2.5 m along with him). Real KH1 entities in KH_ITEM_SLOTS (entity +8, pos at ent +0x10;
    # KH1 copies +0x10 into its +0x50 / +0x100 copies itself)
    slots = kh1_mem(S, KH_ITEM_SLOTS, 0x50 * 8)
    for i in range(8) if slots else ():
        e = struct.unpack_from("<Q", slots, i * 0x50 + 8)[0]
        p = kh1_abs(S, e + 0x10, 12) if e else None
        if p:
            x, _, z = struct.unpack("<3f", p)
            ctypes.windll.kernel32.WriteProcessMemory(h, ctypes.c_void_p(e + 0x10), struct.pack("<f", x + dx), 4, None)
            ctypes.windll.kernel32.WriteProcessMemory(h, ctypes.c_void_p(e + 0x18), struct.pack("<f", z + dz), 4, None)
            n += 1
    if n:
        S.state["prize_shifts"] = S.state.get("prize_shifts", 0) + n


def mapfeed_shift(S, dx: float, dz: float) -> None:
    """KH1 Sora (and the party with him) was just moved by (dx, dz) in KH1 (pin re-centre): move the mapping's KH1
    anchor by the same amount at once and push it to the DLL. (Applied a tick later, the party was drawn through the
    pre-pin mapping for one frame = a ~2.5 m jerk back every half second while running, smoothed to "less than a foot
    of rubberband" - user 2026-10-06.)"""
    st = S.state
    xf = st.get("mf_xf")
    if xf is None:
        return
    xf = (xf[0], xf[1], xf[2] + dx, xf[3] + dz) + tuple(xf[4:])
    st["mf_xf"] = xf
    m = mapfeed_map(S)
    if m is not None and st.get("mf_live"):
        seq = struct.unpack_from("<I", m, 20)[0]
        seq = seq + 1 if not seq & 1 else seq
        struct.pack_into("<I", m, 20, seq)
        struct.pack_into("<2f", m, 184, xf[2], xf[3])
        struct.pack_into("<I", m, 20, seq + 1)


def kh1_sora_on_ground(S, ks):
    """KH1's own verdict while Sora is on the fed ground: True = standing (collision result bit0 at entity + 0x2D0 clear
    and not in the jump state), False = airborne, None = not fed / unreadable."""
    if not S.state.get("mf_sora_fed") or not ks:
        return None
    b = kh1_abs(S, ks["addr"] + 0x2D0, 4)
    if not b:
        return None
    return not (b[0] & 1) and ks["state"] != KH_STATE_JUMP


def mapfeed_sora_state(S, ks, sora: int) -> None:
    """Switching Sora between the feed and the bridge floor. Into the feed (start, glide end): KH1 Sora is put at his
    REAL height over the ground (BL feet through the mapping) - after a glide he was held 4 m over the takeoff level,
    and the bridge's long-fall lift total is cleared (the BL follow adds it to his height)."""
    st = S.state
    was = bool(st.get("mf_sora_fed"))
    st["mf_sora_fed"], st["mf_sora_ent"] = bool(sora), sora
    if sora and not was and st.get("mf_xf") is not None:
        sh = kh1_shared(S)
        if sh:
            sh_f32(sh, SH_LIFT_TOTAL, 0.0)
        Fx, Fy, Kx, Kz, m00, m01, m10, m11, ref_bz, ref_ky, sc = st["mf_xf"]
        y = ref_ky - (S.frame.pos[2] - ref_bz) / sc
        ctypes.windll.kernel32.WriteProcessMemory(ctypes.c_void_p(S.kh["h"]), ctypes.c_void_p(sora + 0x14),
                                                  struct.pack("<f", y), 4, None)
        kh1_event(S, f"map feed: Sora on Borderlands' ground (KH1 y {ks['pos'][1]:.0f} -> {y:.0f})")
    elif was and not sora:
        kh1_event(S, "map feed: Sora back on the bridge floor (glide / summon / menu)")
    S.status["mapfeed_sora"] = bool(sora)


def mapfeed_remap(S, old, new, sideways: bool = True) -> None:
    """Moves the party + summon in KH1 so their Borderlands spot under mapping `new` equals the one under `old`
    (x, z and height; writes only what moved > 0.5 KH1 units)."""
    if old == new:
        return
    Fx, Fy, Kx, Kz, m00, m01, m10, m11, ref_bz, ref_ky, sc = new
    det = m00 * m11 - m01 * m10
    if abs(det) < 1e-9 or not getattr(S, "kh", None):
        return
    h = ctypes.c_void_p(S.kh["h"])
    st = S.state
    sora_ = st.get("mf_sora_ent") if st.get("mf_sora_fed") else 0
    sm_ = st.get("kh_summon_ent") if st.get("kh_summon_mid") not in S.CFG.get("kh1_mapfeed_summon_skip", [4100]) else 0
    for e in [a for a in party_addrs(S) if a] + ([sm_] if sm_ else []) + ([sora_] if sora_ else []):
        raw = kh1_abs(S, e + 0x10, 12)
        if not raw:
            continue
        ex, ey, ez = struct.unpack("<3f", raw)
        if e == sora_:
            # Sora: HEIGHT only (his KH1 spot is the mapping's anchor; the pin / smoothing handle sideways)
            ny_ = ref_ky - ((old[8] - (ey - old[9]) * old[10]) - ref_bz) / sc
            if abs(ny_ - ey) > 0.5:
                ctypes.windll.kernel32.WriteProcessMemory(h, ctypes.c_void_p(e + 0x14), struct.pack("<f", ny_), 4, None)
            continue
        ox, oz = ex - old[2], ez - old[3]
        bx, by = old[0] + old[4] * ox + old[5] * oz, old[1] + old[6] * ox + old[7] * oz
        bz = old[8] - (ey - old[9]) * old[10]
        dx, dy = bx - Fx, by - Fy
        nx, nz = (Kx + (m11 * dx - m01 * dy) / det, Kz + (-m10 * dx + m00 * dy) / det) if sideways else (ex, ez)
        ny = ref_ky - (bz - ref_bz) / sc
        if abs(nx - ex) > 0.5 or abs(ny - ey) > 0.5 or abs(nz - ez) > 0.5:
            ctypes.windll.kernel32.WriteProcessMemory(h, ctypes.c_void_p(e + 0x10), struct.pack("<3f", nx, ny, nz), 12, None)
            st["mf_remaps"] = st.get("mf_remaps", 0) + 1
    # PRIZES the same way (K2 "prizes sometimes float", 2026-10-12): a prize lying on the fed ground kept its KH1 height
    # while the mapping's height reference (BL Sora's feet) moved -> walking 83 up a slope drew a resting HP orb 83 in
    # the air (prize_float trace), walking down buried it. Pool 0x2432140 + 0x20 * k (pos @0, live = flags @0x19 bit0).
    if S.CFG.get("kh1_mapfeed_prize_remap", True):
        raw = kh1_mem(S, KH_PRIZE_OBJS, 0x20 * 256)
        pst = kh1_mem(S, 0x2D60FD0, 0x40 * 256)      # prize state objects: +0x1E 0 falling, 1 resting
        for k in range(256) if raw and pst else ():
            # RESTING ones only: a falling / bouncing prize lands on the live feed anyway, and rewriting it every tick
            # raced KH1's own physics (it never landed, one was lost)
            if not raw[k * 0x20 + 0x19] & 1 or pst[k * 0x40 + 0x1E] != 1:
                continue
            ex, ey, ez = struct.unpack_from("<3f", raw, k * 0x20)
            ox, oz = ex - old[2], ez - old[3]
            bx, by = old[0] + old[4] * ox + old[5] * oz, old[1] + old[6] * ox + old[7] * oz
            bz = old[8] - (ey - old[9]) * old[10]
            dx, dy = bx - Fx, by - Fy
            # always sideways too: a prize is world-fixed (the walking mapping slid a resting orb ~370 BL units along with Sora)
            nx, nz = Kx + (m11 * dx - m01 * dy) / det, Kz + (-m10 * dx + m00 * dy) / det
            ny = ref_ky - (bz - ref_bz) / sc
            if abs(nx - ex) > 0.01 or abs(ny - ey) > 0.01 or abs(nz - ez) > 0.01:   # (0.5 skipped slow slopes: sank)
                ctypes.windll.kernel32.WriteProcessMemory(h, ctypes.c_void_p(S.kh["base"] + KH_PRIZE_OBJS + k * 0x20),
                                                          struct.pack("<3f", nx, ny, nz), 12, None)
                st["mf_prize_remaps"] = st.get("mf_prize_remaps", 0) + 1
    # ITEM DROPS too (user 2026-10-12: Potions stayed "the same distance away from Sora"): real KH1 entities in the 8 item
    # slots (KH_ITEM_SLOTS, entity +8); resting = their own velocity (+0x20) ~0, so falling ones keep KH1's physics
    if S.CFG.get("kh1_mapfeed_item_remap", True):    # (session 8's slide was the missing pin shift, kh1_shift_prizes)
        slots = kh1_mem(S, KH_ITEM_SLOTS, 0x50 * 8)
        for i in range(8) if slots else ():
            e = struct.unpack_from("<Q", slots, i * 0x50 + 8)[0]
            raw = kh1_abs(S, e + 0x10, 0x1C) if e else None
            if not raw:
                continue
            ex, ey, ez = struct.unpack_from("<3f", raw, 0)
            vx_, vy_, vz_ = struct.unpack_from("<3f", raw, 0x10)
            if abs(vx_) + abs(vz_) > 0.05 or abs(vy_) > 1.6:     # resting = (0, 1.5, 0): KH1's constant gravity step
                continue
            ox, oz = ex - old[2], ez - old[3]
            bx, by = old[0] + old[4] * ox + old[5] * oz, old[1] + old[6] * ox + old[7] * oz
            bz = old[8] - (ey - old[9]) * old[10]
            dx, dy = bx - Fx, by - Fy
            nx, nz = Kx + (m11 * dx - m01 * dy) / det, Kz + (-m10 * dx + m00 * dy) / det
            ny = ref_ky - (bz - ref_bz) / sc
            if abs(nx - ex) > 0.01 or abs(ny - ey) > 0.01 or abs(nz - ez) > 0.01:
                ctypes.windll.kernel32.WriteProcessMemory(h, ctypes.c_void_p(e + 0x10), struct.pack("<3f", nx, ny, nz), 12, None)
                st["mf_item_remaps"] = st.get("mf_item_remaps", 0) + 1


def mapfeed_glide_spot(S, anchor):
    """MAP FEED glide (user 2026-10-06: Donald / Goofy froze under the anchor while Sora glided, then teleported to
    him on landing): KH1 Sora is held 4 m up at the KH1 spot matching BL Sora under the held mapping, so the party
    chases him across the fed terrain like vanilla, and both Soras still coincide on landing (no teleport). Beyond
    kh_glide_feed_radius from the room's anchor, KH1 Sora, the party and the summon are shifted back together with
    the mapping (drawing unchanged), which keeps KH1 coordinates inside the room."""
    st, CFG = S.state, S.CFG
    Fx, Fy, Kx, Kz, m00, m01, m10, m11, ref_bz, ref_ky, sc = st["mf_xf"]
    det = m00 * m11 - m01 * m10
    if abs(det) < 1e-9:
        return anchor[0], anchor[1]
    dx, dy = S.frame.pos[0] - Fx, S.frame.pos[1] - Fy
    tx, tz = Kx + (m11 * dx - m01 * dy) / det, Kz + (-m10 * dx + m00 * dy) / det
    if math.hypot(tx - anchor[0], tz - anchor[1]) > CFG.get("kh_glide_feed_radius", 800.0):
        sx, sz = anchor[0] - tx, anchor[1] - tz
        st["mf_xf"] = (Fx, Fy, Kx + sx, Kz + sz, m00, m01, m10, m11, ref_bz, ref_ky, sc)
        h = ctypes.c_void_p(S.kh["h"])
        movers = [a for a in party_addrs(S) if a] + ([st["kh_summon_ent"]] if st.get("kh_summon_ent") else [])
        for e in movers:
            raw = kh1_abs(S, e + 0x10, 12)
            if raw:
                ex, ey, ez = struct.unpack("<3f", raw)
                ctypes.windll.kernel32.WriteProcessMemory(h, ctypes.c_void_p(e + 0x10), struct.pack("<f", ex + sx), 4, None)
                ctypes.windll.kernel32.WriteProcessMemory(h, ctypes.c_void_p(e + 0x18), struct.pack("<f", ez + sz), 4, None)
        kh1_shift_prizes(S, sx, sz)
        st["mf_glide_shifts"] = st.get("mf_glide_shifts", 0) + 1
        tx, tz = anchor[0], anchor[1]
    return tx, tz


def mapfeed_ground_ky(S, kx: float, kz: float, default):
    """KH1 height (y down) of Borderlands' ground at KH1 point (kx, kz) under the feed's mapping (one native trace);
    `default` when the feed is off or nothing is there. For stand-ins placed by the mod: at the flat floor height they
    sat up to 1.5 m UNDER the terrain and the rescue pulled them out 4x a second (user test 2026-10-06 22:59)."""
    st = S.state
    if st.get("mf_xf") is None or not mapfeed_active(S):
        return default
    Fx, Fy, Kx, Kz, m00, m01, m10, m11, ref_bz, ref_ky, sc = st["mf_xf"]
    ox, oz = kx - Kx, kz - Kz
    bx, by = Fx + m00 * ox + m01 * oz, Fy + m10 * ox + m11 * oz
    r = bl_trace(S, (bx, by, ref_bz + S.CFG.get("kh1_mapfeed_up", 400.0)), (bx, by, ref_bz - S.CFG.get("kh1_mapfeed_down", 1500.0)))
    return ref_ky - (r[0][2] - ref_bz) / sc if r else default


def mapfeed_rescue(S, ents, now: float) -> None:
    """A fed KH1 entity more than kh1_mapfeed_rescue_cm below Borderlands' ground under it (it fell through) is put
    back on top (entity +0x14 = y). Every 0.25 s, one native trace per entity."""
    st, CFG = S.state, S.CFG
    if now - st.get("mf_rescue_t", 0) < 0.25 or st.get("mf_xf") is None or not getattr(S, "kh", None):
        return
    st["mf_rescue_t"] = now
    Fx, Fy, Kx, Kz, m00, m01, m10, m11, ref_bz, ref_ky, sc = st["mf_xf"]
    for e in ents:
        if e == st.get("mf_prize_ent"):
            continue                                   # (not an entity: no position of its own)
        raw = kh1_abs(S, e + 0x10, 12)
        if not raw:
            continue
        pos = struct.unpack("<3f", raw)
        bx, by, _ = mapfeed_to_bl(S, pos)
        r = bl_trace(S, (bx, by, ref_bz + CFG.get("kh1_mapfeed_up", 400.0)), (bx, by, ref_bz - CFG.get("kh1_mapfeed_down", 1500.0)))
        if not r:
            continue
        ky = ref_ky - (r[0][2] - ref_bz) / sc                            # KH1 y of the ground there (y grows down)
        if pos[1] - ky > CFG.get("kh1_mapfeed_rescue_cm", 60.0):
            ctypes.windll.kernel32.WriteProcessMemory(ctypes.c_void_p(S.kh["h"]), ctypes.c_void_p(e + 0x14),
                                                      struct.pack("<f", ky - 2.0), 4, None)
            st["mf_rescued"] = st.get("mf_rescued", 0) + 1
            kh1_event(S, f"map feed: {e:#x} was {pos[1] - ky:.0f} below the ground -> put back on top")


def kh1_set_walls(S, on: bool) -> None:
    """KH1's own walls off for Sora (bridge v2 collision hook): Borderlands supplies the walls instead."""
    sh = kh1_shared(S)
    if not sh:
        return
    ctypes.c_uint32.from_address(sh + SH_WALLS_OFF).value = 1 if on else 0
    S.status["kh1_walls"] = {"off": bool(on), "hooks": ctypes.c_uint32.from_address(sh + SH_WORLD_HOOKS).value,
                             "skipped": ctypes.c_uint32.from_address(sh + SH_WALLS_REMOVED).value}


# ---------------------------------------------------------------- Borderlands ground -> KH1 (bridge v3)
# GTA x Minecraft turns GTA's ground into invisible barrier blocks so Minecraft's physics walk on GTA's world. Here the
# bridge makes KH1 Sora ignore every KH1 room triangle and stand on one flat floor at kh_floor_ref (where he stood when
# this started), and Borderlands decides when that floor is there:
#   Borderlands on the ground -> floor on (KH1 Sora walks, runs, jumps and lands on it as usual)
#   Borderlands drops more than kh_fall_drop below where it left the ground (walked off / jumped down) -> no floor:
#     KH1 Sora keeps falling with KH1's own fall (the bridge lifts him every kh_fall_lift cm so the loop never ends)
#   Borderlands lands -> floor on + snap: KH1 Sora lands that frame, even mid-arc (jumped up onto a ledge).
# So the KH1 room no longer matters at all (no walls, no edges, no void), only Borderlands' world does.
def kh1_ground_on(S) -> bool:
    return bool(S.CFG.get("kh_ground", True)) and kh1_bridge_v3(S)


def kh1_ground_off(S) -> None:
    sh = kh1_shared(S)
    if sh and kh1_bridge_v3(S):
        sh_u32(sh, SH_GROUND_MODE, 0)
        sh_u32(sh, SH_NPROXY, 0)
    S.state["kh_ground_active"] = False


def kh1_ground_tick(S, pawn, ks, now: float) -> bool:
    """Feeds the Borderlands ground state to the bridge. Returns True while ground mode is active."""
    st, CFG = S.state, S.CFG
    if not kh1_ground_on(S):
        if st.get("kh_ground_active"):
            kh1_ground_off(S)
        return False
    sh = kh1_shared(S)
    if st.get("kh_floor_ref") is None:
        # first time (or after a resync): the floor goes where KH1 Sora stands now - must be standing still on KH1 ground
        if ks["state"] != 2 or abs(ks["vel"][1]) > 0.01:
            return False
        st["kh_floor_ref"] = ks["pos"][1]
        sh_f32(sh, SH_LIFT_TOTAL, 0.0)
        kh1_event(S, f"Borderlands ground: KH1 floor at y {ks['pos'][1]:.1f}")
    fy = st["kh_floor_ref"]
    sh_f32(sh, SH_FLOOR_Y, fy)
    sh_f32(sh, SH_FALL_LIMIT, CFG.get("kh_fall_limit", 800.0))
    sh_f32(sh, SH_FALL_LIFT, CFG.get("kh_fall_lift_cm", 600.0))
    bl_air = int(pawn.Physics) == 2
    bl_z = float(pawn.Location.Z)
    if not bl_air:
        sh_u32(sh, SH_FLOOR_ON, 1)
        if st.get("gr_was_air"):
            sh_u32(sh, SH_SNAP_SEQ, sh_u32(sh, SH_SNAP_SEQ) + 1)      # Borderlands landed: KH1 lands now
            kh1_event(S, f"ground: BL landed (dropped {st.get('gr_drop', 0):.0f} UU) -> KH1 snap")
        st["gr_was_air"], st["gr_drop"] = False, 0.0
    else:
        st["gr_was_air"] = True
        drop = st.get("bl_ground_z", bl_z) - bl_z
        st["gr_drop"] = max(st.get("gr_drop", 0.0), drop)
        falling = drop > CFG.get("kh_fall_drop", 30.0) or bool(st.get("kh_glide_on"))
        if falling and sh_u32(sh, SH_FLOOR_ON):
            kh1_event(S, f"ground: BL {drop:.0f} UU below takeoff -> no KH1 floor")
        sh_u32(sh, SH_FLOOR_ON, 0 if falling else 1)
    # map feed: Sora collides with Borderlands' real ground in KH1 -> the bridge's flat floor off (proxies list is empty
    # then too); floor_on above still tells everyone else "Borderlands is airborne / falling"
    sh_u32(sh, SH_GROUND_MODE, 0 if st.get("mf_sora_fed") else 1)
    st["kh_ground_active"] = True
    S.status["kh_ground"] = {"floor_y": round(fy, 1), "on": sh_u32(sh, SH_FLOOR_ON), "air": sh_u32(sh, SH_SORA_AIR),
                             "clamps": sh_u32(sh, SH_GROUND_CLAMPS), "lift": round(sh_f32(sh, SH_LIFT_TOTAL)),
                             "poly": ctypes.c_int32.from_address(sh + SH_FLOOR_POLY).value}
    return True


def kh1_virtual_y(S, ks) -> float:
    """KH1 Sora's height without the bridge's long-fall lifts (continuous, so the Borderlands follow stays smooth)."""
    sh = kh1_shared(S)
    if S.state.get("kh_ground_active") and sh:
        return ks["pos"][1] + sh_f32(sh, SH_LIFT_TOTAL)
    return ks["pos"][1]


KH_ABILITY_WORD = 0x2D5EC10          # KH1 movement abilities in effect: 0x80 Glide, 0x100 Superglide
KH_GLIDE_ANIMS = range(113, 117)      # glide motions 0x71-0x74


def ks_anim(S):
    ks = kh1_state(S)
    return ks["anim"] if ks else None


def kh1_glide_abilities(S) -> None:
    """Keep Glide (and optionally Superglide) switched on in KH1's ability word (CFG kh_glide / kh_superglide)."""
    want = (0x80 if S.CFG.get("kh_glide", True) else 0) | (0x100 if S.CFG.get("kh_superglide", False) else 0)
    # Sliding Dash from the start, any save (user 2026-10-12): its in-effect bit = byte +3 bit 0 of the same word
    # (KH1-LUA-LIBRARY enable_ability: soraHP + 0x1FC7 bit 1; soraHP + 0x1FC4 = this word)
    want |= 0x01000000 if S.CFG.get("kh_sliding_dash", True) else 0
    if not want:
        return
    b = kh1_mem(S, KH_ABILITY_WORD, 4)
    if b:
        cur = int.from_bytes(b, "little")
        if cur & want != want:
            kh1_poke(S, KH_ABILITY_WORD, (cur | want).to_bytes(4, "little"))


def kh1_target_distance(S, pc) -> float:
    """How far the Borderlands target is, in KH cm, so the dummy stands that far from KH1 Sora (Sliding Dash range)."""
    lk = lock_target(S)
    me = vec3(pc.Pawn.Location)
    best = None
    if lk is not None:
        t = vec3(lk.Location)
        best = math.hypot(t[0] - me[0], t[1] - me[1])
    else:
        cam_yaw = int(pc.Rotation.Yaw) * UU
        for dist, p, bearing in enemies(S, pc, S.CFG.get("kh_dash_range", 1200.0)):
            if abs(ang(bearing - cam_yaw)) < math.radians(60):
                best = dist
                break
    if best is None:
        return None                                    # nothing to hit: KH1 swings in place (no dash)
    cm = (best - S.CFG.get("kh_target_radius", 50.0)) / max(S.frame.scale, 0.01)
    if cm < S.CFG.get("kh_dash_min_cm", 152.0):       # under 5 ft: regular combo in place
        return S.CFG.get("kh_dummy_dist", 100.0)
    if cm > S.CFG.get("kh_dash_max_cm", 305.0):       # over 10 ft: out of reach, no dash
        return None
    return min(S.CFG.get("kh_dummy_max", 280.0), cm)  # 5-10 ft: KH1 Sliding Dash covers the gap


def kh1_combat_tick(S, pc, ks, now: float) -> None:
    """Turns KH1 events into Borderlands ones: a new KH1 swing -> Keyblade hit action, a KH1 cast -> the spell."""
    st, CFG = S.state, S.CFG
    pawn = pc.Pawn
    try:
        mapfeed_tick(S, pc, ks, now)                         # Borderlands' ground -> KH1 collision (kh1_mapfeed.dll)
        S.status.pop("mapfeed_error", None)
    except Exception as ex:  # noqa: BLE001
        S.status["mapfeed_error"] = f"{type(ex).__name__}: {ex}"
    if kh1_proxies_on(S):
        kh1_proxy_tick(S, pc, ks, now)                       # every Borderlands enemy has a KH1 stand-in
    else:
        st["kh_proxied"] = set()
        st["kh_dummy_want"] = kh1_target_distance(S, pc)
        kh1_dummy_tick(S, ks, now)
    if st.pop("kh_move_locked", False):                       # v8 used IgnoreMoveInput: release it once
        S.pending.append(lambda: pc.IgnoreMoveInput(False))
    if S.CFG.get("kh_move", True):
        st["kh_move_on"] = True                               # KH1 moves the pawn (velocity overwritten every tick)
        kh1_set_walls(S, S.CFG.get("kh_walls_off", True))
        kh1_move_drive(S, pc, pc.Pawn, ks, now)
    else:
        st["kh_move_on"] = False
        kh1_body_velocity(S, pc, ks, now)
    if dbg_on(S, "dbg_combo_trace") and (ks["state"] == KH_STATE_ATTACK or int(pc.Pawn.Physics) == 2):
        try:
            combo_trace(S, pc, ks, now)
        except Exception as ex:  # noqa: BLE001
            S.status["combo_trace_err"] = f"{type(ex).__name__}: {ex}"
    swing = ks["anim"] if ks["state"] == KH_STATE_ATTACK and ks["anim"] in KH_SWING_ANIMS else None
    if swing is not None and swing != st.get("kh_swing"):
        # a new KH1 swing (each combo step has its own animation): Borderlands hit action for that step
        air = int(pawn.Physics) == 2
        chain = CFG["air_combo"] if air else CFG["combo"]
        step = st.get("kh_combo_step", -1) + 1 if st.get("kh_swing") is not None else 0
        st["kh_combo_step"] = step
        if swing == 200 and st.get("kh_dummy"):           # whiff with a dummy present: it burrowed / got knocked away
            st["kh_whiffs"] = st.get("kh_whiffs", 0) + 1
            if st["kh_whiffs"] >= 2:
                st.setdefault("kh_dummy_ban", {})[st["kh_dummy"]] = now + 6.0
                st["kh_dummy_scan"] = 0                   # pick another one right away
        elif swing != 200:
            st["kh_whiffs"] = 0
        face_target(S, pc)
        start_action(S, "attack", chain[min(step, len(chain) - 1)], CFG["atk_speed"], chain=chain, kh=True)
    st["kh_swing"] = swing
    anim = ks["anim"]
    wait = st.get("kh_cast_wait")
    if wait and anim in KH_CAST_ANIM:
        st["kh_cast_wait"] = None
    elif wait and now > wait[0] and not S.CFG.get("kh_cast_fallback", False):
        st["kh_cast_wait"] = None                       # KH1 refused (not enough MP / not learned): like vanilla
        S.status["last_cast"] = [wait[1], "KH1 refused", round(now % 1000, 2)]
    elif wait and now > wait[0]:
        # KH1 refused the spell (e.g. Cure/Aero outside battle, Gravity not really usable): play the extracted KH1 cast
        # animation and fire the Borderlands spell ourselves, paying 1 KH1 MP
        st["kh_cast_wait"] = None
        S.status["last_cast"] = [wait[1], "KH1 refused -> fallback", round(now % 1000, 2)]
        sid = next((i for i, names in enumerate(KH_SPELLS) if names[0] == wait[1]), 0)
        motion = 54 + sid     # KH1's shortcut-cast motion for this spell (entity anim id == motion index; 64-67 were hit reactions)
        # KH1 never casts Cure/Aero through shortcuts (verified 2026-10-01 with full MP: no animation at all), and the
        # baked 57 is a crouched pose, not KH1's Cure: use 56 (Keyblade raised straight overhead, like KH1's Cure)
        motion = S.CFG.get("kh_fallback_motion", {}).get(wait[1], {"Cure": 56}.get(wait[1], motion))
        start_action(S, "cast", motion, 1.0, spell=wait[1], level=wait[2], kh_fallback=True)
        st["kh_baked_until"] = now + st["action"]["len"] if st.get("action") else now + 1.2
        stt = kh1_stats(S)
        if stt and stt[2] > 0:
            kh1_poke(S, KH_HP + 8, int(stt[2] - 1).to_bytes(4, "little", signed=True))
    if anim in KH_CAST_ANIM and anim != st.get("kh_cast_anim"):
        sid = KH_CAST_ANIM[anim]           # KH1 started casting: the spell leaves his hand ~0.45 s later
        sp_ = st.get("kh_spell")
        if (not sp_ or now > sp_.get("until", 0)) and sid not in KH_PARTY_TARGET_SPELLS:   # cast from KH1's own menu
            tpawn, tent = kh1_spell_target(S, pc)
            st["kh_spell"] = {"pawn": tpawn, "ent": tent, "sid": sid, "until": now + CFG.get("kh_spell_window", 3.0),
                              "free": isinstance(tpawn, FreeTarget)} if tpawn is not None else None
        lvl = next((l for _, i, l in kh1_spells(S) if i == sid), 1)
        face_target(S, pc)
        st["kh_cast_release"] = (now + CFG.get("kh_cast_release", 0.45), KH_SPELLS[sid][0], lvl)
        st["kh_magic"] = (KH_SPELLS[sid][0], lvl, now + CFG.get("kh_magic_window", 3.0))
        voice(S, f"vspell{sid}", cd=0.3)
        sfx(S, "swing", CFG.get("sfx_cast_whoosh", 0.0), pitch_jitter=0.08)   # the Keyblade sweeping out the spell
        # KH1's own cast sounds (user 2026-10-06: only the voice line). KH1 no longer creates them in its sound list
        # when it casts (probe: only the voice entry, bank 0), so the relay (kh1_se_tick) has nothing to forward:
        # play KH1's real clips at KH1's cast start, KH1's mix (KH1_SE_GAIN). Skipped if the relay played one.
        if CFG.get("kh1_cast_sfx", True):
            q = st.setdefault("kh_cast_sfx_q", [])
            q.extend((now + d, f"kse000n{n}") for n, d in KH_CAST_SFX.get(sid, ()))
    st["kh_cast_anim"] = anim if anim in KH_CAST_ANIM else None
    q = st.get("kh_cast_sfx_q")
    if q:
        due = [g for t, g in q if now >= t]
        st["kh_cast_sfx_q"] = [(t, g) for t, g in q if now < t]
        for g in due:
            if now - st.get("kh1_se_cast_t", 0) < 1.0:
                continue                                  # KH1's relay delivered the real event: no double
            vol = CFG.get("sfx_voice", 0.9) * CFG.get("sfx_kh1_se", 1.0) * CFG.get("kh1_se_gain", {}).get(g, KH1_SE_GAIN.get(g, 0.5))
            if g in (getattr(S, "sfx_ids", None) or {}) or sfx_lazy(S, g):
                sfx(S, g, vol, pitch_jitter=0.0)
    rel = st.get("kh_cast_release")
    if rel and now >= rel[0]:
        st["kh_cast_release"] = None
        # with stand-ins paired, KH1's real spell decides who gets hit: Borderlands draws it (no damage of its own)
        launch_spell(S, pc, rel[1], rel[2], visual=bool(st.get("kh_proxied")) and kh1_proxies_on(S))
    if st.get("kh_sc_restore") and now > st["kh_sc_restore"]:      # give the player's own shortcuts back
        if st.get("kh_sc_orig"):
            kh1_poke(S, KH_SHORTCUTS, st["kh_sc_orig"])
        st["kh_sc_restore"] = None
    if CFG.get("kh_mp_regen", True):           # slow MP regen (not in KH1; user 2026-10-12: on, 1 MP every 3 s)
        kh1_add_mp(S, (now - st.get("kh_mp_t", now)) / CFG.get("kh_mp_regen_s", 4.0))   # user session 9: 3 s still a bit fast
    st["kh_mp_t"] = now


# KH1 BUILD CHECK (release R1, 2026-10-12): every KH1 address in this mod and its DLLs is for ONE build (Steam
# "KINGDOM HEARTS FINAL MIX.exe", PE stamp 0x669E37CE). Before connecting: same PE stamp + size, else all 27 code samples
# below (16 bytes at +0x10 of each function the DLLs hook or call - past MinHook's 5-byte patch, most hold RIP-relative
# data offsets, so a moved data layout fails too) must match. Mismatch -> KH1 passthrough stays OFF (never guess),
# logged + told on screen; Borderlands runs as if KH1 were closed.
KH1_BUILD = (0x669E37CE, 0x2F91000)
KH1_SIGS = (
    ("shape_draw", 0x31E930, "488bd94885d20f85b40000003915be98"), ("tex_lookup", 0x10D730, "895c24404889742448488bf9488b1d8d"),
    ("mdl_run", 0x328EB0, "897c242041564883ec40488bf9448bf2"), ("begin_prim", 0xF3050, "8bd88bfa8bf1e8c5a70000488bc8448b"),
    ("end_prim", 0xFB9D0, "8bd88bfa8bf1e8451e0000488bc8448b"), ("cmd_exec", 0x2A6230, "004833c44889442430488bfa488bd9e8"),
    ("to_handle", 0x38ADA0, "4883c4205bc3e865000000488bc8488b"), ("gather", 0x194730, "800000004863da4c8be9c70594e01b02"),
    ("colquery", 0x195190, "ec380a0000440f29bc24a0090000488b"), ("read_pad", 0x113D30, "8b3dc25f1b02488bf24863d985c9750c"),
    ("exp_party", 0x2849A0, "415641574883ec204c8b05f1415e024c"), ("equip", 0x286730, "8bd9e839920000440fb64002488b055d"),
    ("equip_cb", 0x307470, "c3cccccccccccccccccccccccccccccc"), ("party_refresh", 0x2A4E60, "ec86feffe817150000e812def2ff488d"),
    ("spell_set", 0x28F570, "d1496bd2384803d044390a75124d85c0"), ("hit_proc", 0x29BBE0, "81ec20010000488970d0488bf98b496c"),
    ("hit_link", 0x2BFE70, "8bf9488bf28b89ec030000e840af0c00"), ("prize", 0x2ABC50, "70833d5453ab0200458bf04c8be20f85"),
    ("hp_add", 0x2A4930, "488bf1448bf28b496ce882640e00488b"), ("hit_direct", 0x2B7BA0, "895c24584889742460488bfa488bd9f7"),
    ("spr_submit", 0x2764C0, "000085d20f8e4a010000488d4160448b"), ("skel_eval", 0x391C80, "05032e1c004833c4488944246049896b"),
    ("snd_play", 0x598D0, "ff0f297424600f297c2450488b05a6b1"), ("fade", 0x56E70, "895c245848897424600f29742430418b"),
    ("snd_resume", 0x56080, "ff48895c2458418bf08bea488bf9488d"), ("fx_pass", 0x1ECF40, "4154415541564157488da838fbffff48"),
    ("se_stop", 0xDEF60, "feffffff488958084889681048897018"))


def kh1_build_check(S, h, base) -> list:
    """[] = the supported KH1 build; else the mismatching sample names (logged + shown once per KH1 build)."""
    k32 = ctypes.windll.kernel32

    def rd(addr, size):
        buf = ctypes.create_string_buffer(size); n = ctypes.c_size_t()
        ok = k32.ReadProcessMemory(ctypes.c_void_p(h), ctypes.c_void_p(addr), buf, size, ctypes.byref(n))
        return buf.raw if ok and n.value == size else None
    hdr = rd(base, 0x400)
    stamp = None
    if hdr:
        pe = struct.unpack_from("<I", hdr, 0x3C)[0]
        if pe + 0x58 <= len(hdr):
            stamp = (struct.unpack_from("<I", hdr, pe + 8)[0], struct.unpack_from("<I", hdr, pe + 0x50)[0])
    bad = [] if stamp == KH1_BUILD else [n for n, rva, hx in KH1_SIGS if rd(base + rva, 16) != bytes.fromhex(hx)]
    shown = [hex(x) for x in stamp] if stamp else None
    S.status["kh1_build"] = {"stamp": shown, "ok": not bad, "bad": bad[:8]}
    key = (stamp, tuple(bad))
    if S.state.get("kh1_build_told") != key:
        S.state["kh1_build_told"] = key
        if bad:
            kh1_event(S, f"KH1 build NOT supported (PE {shown}, {len(bad)}/{len(KH1_SIGS)} code samples differ: "
                         f"{', '.join(bad[:6])}) -> KH1 passthrough off")
            say(S, "Sora mod: this Kingdom Hearts Final Mix version isn't supported - KH1 link turned off")
        elif stamp != KH1_BUILD:
            kh1_event(S, f"KH1 PE stamp {shown} differs but all {len(KH1_SIGS)} code samples match -> connecting")
    return bad


# Read-only view of KH1 Sora's entity (Steam 1.0.0.2 RVAs; see MODLOG). Handle lives on S so it survives hot reloads.
KH_SORA_PTR = 0x2537E48
KH_STATE_ATTACK, KH_STATE_JUMP = 6, 10      # entity +0x160 (2 = normal); +0x164 = current animation


def kh1_state(S):
    k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
    kh = getattr(S, "kh", None)
    if kh and kh.get("h") and not (kh.get("rw") and kh.get("pid")):   # handle from an older logic.py: reopen
        k32.CloseHandle(ctypes.c_void_p(kh["h"])); kh = None
    if kh is None or not kh.get("h"):
        if kh and time.time() < kh.get("retry", 0):
            return None
        S.kh = kh = {"retry": time.time() + 2.0}
        pids = (ctypes.c_ulong * 4096)(); got = ctypes.c_ulong()
        psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(got))
        for pid in pids[:got.value // 4]:
            h = k32.OpenProcess(0x0438, False, pid)   # query + VM read/write/operation (write: kh1_write_y)
            if not h:
                continue
            name = ctypes.create_unicode_buffer(260)
            psapi.GetModuleBaseNameW(ctypes.c_void_p(h), None, name, 260)
            if name.value.lower() == "kingdom hearts final mix.exe":
                mods = (ctypes.c_void_p * 1)(); n = ctypes.c_ulong()
                psapi.EnumProcessModulesEx(ctypes.c_void_p(h), mods, ctypes.sizeof(mods), ctypes.byref(n), 3)
                bad = kh1_build_check(S, h, mods[0])
                if bad:                                   # another KH1 build: our addresses would be wrong -> stay off
                    k32.CloseHandle(h)
                    kh["retry"] = time.time() + 30.0
                    return None
                kh.update(h=h, base=mods[0], rw=True, pid=pid)
                break
            k32.CloseHandle(h)
        if not kh.get("h"):
            return None

    def rd(addr, size):
        buf = ctypes.create_string_buffer(size); n = ctypes.c_size_t()
        ok = k32.ReadProcessMemory(ctypes.c_void_p(kh["h"]), ctypes.c_void_p(addr), buf, size, ctypes.byref(n))
        return buf.raw if ok and n.value == size else None
    p = rd(kh["base"] + KH_SORA_PTR, 8)
    # (NOT the party-leader fallback here: logic WRITES to Sora (ground snap, pin, pad) and doing that during a summon
    #  intro froze KH1's staged scene, 2026-10-06. Only the renderer falls back (read-only) so Sora stays drawn.)
    ent = rd(int.from_bytes(p, "little"), 0x2C0) if p and int.from_bytes(p, "little") else None
    if not ent:
        if p is None or int.from_bytes(p, "little"):   # read failed / bad entity = KH1 gone: drop the handle
            k32.CloseHandle(ctypes.c_void_p(kh["h"])); kh["h"] = None
        return None                                   # pointer 0 = Sora absent for a moment (summon intro): keep it open
    import struct
    m = struct.unpack_from("<16f", ent, 0x230)
    pos = struct.unpack_from("<3f", ent, 0x10)
    body = pos
    # Sora's real body position = his hips (joint 0). Some moves (Dodge Roll) carry him 3.5 m in the joints before the
    # entity catches up; scratch frames (bones scattered) are ignored.
    try:
        a = kh1_resolve(S, struct.unpack_from("<I", ent, 0x15C)[0])
        jb = kh1_abs(S, a + 0x1B4, 4) if a else None
        a = kh1_resolve(S, int.from_bytes(jb, "little")) if jb else 0
        J = kh1_abs(S, a, 13 * 64) if a else None
        if J:
            h0 = struct.unpack_from("<3f", J, 48)
            h12 = struct.unpack_from("<3f", J, 12 * 64 + 48)
            if math.dist(h0, h12) < 250 and math.hypot(h0[0] - pos[0], h0[2] - pos[2]) < 600:
                body = (h0[0], pos[1], h0[2])
    except Exception:  # noqa: BLE001
        pass
    vel = struct.unpack_from("<3f", ent, 0x2B0)                # KH1's own movement this frame (cm / 60 Hz frame)
    return {"m": m, "pos": pos, "body": body, "vel": vel, "state": ent[0x160], "anim": ent[0x164], "fwd": (-m[8], -m[10]),
            "addr": int.from_bytes(p, "little"), "mspeed": struct.unpack_from("<f", ent, 0x284)[0],
            "motion": struct.unpack_from("<I", ent, 0x168)[0], "clock": struct.unpack_from("<f", ent, 0x16C)[0]}   # real mset motion index + its clock (60 fps frames)   # +0x284 motion speed (clock += dt x speed)


def kh1_write_y(S, ks, y: float) -> None:
    """Sets KH1 Sora's height (KH cm, y down, virtual: the bridge's long-fall lifts are taken off). Only his pose
    reaches BL, so this is invisible there."""
    sh = kh1_shared(S)
    if S.state.get("kh_ground_active") and sh:
        y -= sh_f32(sh, SH_LIFT_TOTAL)
    buf = ctypes.c_float(y)
    ctypes.windll.kernel32.WriteProcessMemory(ctypes.c_void_p(S.kh["h"]), ctypes.c_void_p(ks["addr"] + 0x14),
                                              ctypes.byref(buf), 4, None)


def kh1_body_sync(S, pc, pawn, now: float) -> None:
    """KH1 owns Sora's body: no walking during KH1 swings, and the jump arc follows KH1 (High Jump etc.)."""
    st, CFG = S.state, S.CFG
    ks = kh1_state(S)
    S.status["kh1_state"] = [ks["state"], ks["anim"]] if ks else None
    attacking = bool(ks and (ks["state"] == KH_STATE_ATTACK or ks["anim"] in KH_CAST_ANIM))         or bool(st.get("action") and (st["action"]["kind"] == "attack" or st["action"].get("kh_fallback")))
    st["kh_attacking"] = attacking
    hard = CFG.get("kh_attack_move", 0.35) <= 0           # 0 = old behaviour: no movement at all during swings
    if attacking and hard and not st.get("kh_lock"):
        st["kh_lock"] = now
        S.pending.append(lambda: pc.IgnoreMoveInput(True))
    elif st.get("kh_lock") and (not attacking or not hard or now - st["kh_lock"] > 3.0):
        kh1_unlock(S, pc)
    if not ks:
        return
    kh1_combat_tick(S, pc, ks, now)
    try:
        prize_orbs_tick(S, ks)                             # KH1's prize orbs drawn in Borderlands
        item_drops_tick(S, ks)                             # KH1's item drops (Potion, Ether...) too
    except Exception as ex:  # noqa: BLE001
        S.status["prize_orbs_err"] = f"{type(ex).__name__}: {ex}"
    ground_mode = kh1_ground_tick(S, pawn, ks, now)        # Borderlands ground -> KH1 floor (bridge v3)
    lift_jump = False
    if ground_mode:
        lt = sh_f32(kh1_shared(S), SH_LIFT_TOTAL)
        lift_jump = lt != st.get("kh_lift_seen", lt)      # the bridge lifted him this frame: KH1's velocity spikes
        st["kh_lift_seen"] = lt
    # vertical speed from KH1's height samples (KH: cm, y down). KH1 updates at 60 Hz, BL ticks faster.
    y = kh1_virtual_y(S, ks)
    if y != st.get("kh_y"):
        if st.get("kh_yt") and 0 < now - st["kh_yt"] < 0.1:
            vy = (y - st["kh_y"]) / (now - st["kh_yt"])
            st["kh_vy"] = vy if st.get("kh_vy") is None else st["kh_vy"] * 0.4 + vy * 0.6
        st["kh_y"], st["kh_yt"] = y, now
    elif now - st.get("kh_yt", 0) > 0.1:
        st["kh_vy"] = None
    if ks.get("vel") is not None and S.CFG.get("kh_use_kh_vel", True) and not lift_jump:
        st["kh_vy"] = ks["vel"][1] * 60.0                 # KH1's own vertical movement (cm/s, y down): exact jump arc
    # Airborne is judged from heights, not KH1's state byte (KH1 reports "jump" only on the way up).
    bl_air = int(pawn.Physics) == 2
    bl_z = float(pawn.Location.Z)
    if st.get("kh_glide_on"):
        st["kh_postglide"] = True                          # until Borderlands lands
    if not bl_air:
        st["kh_postglide"] = False
        st["bl_ground_z"], st["bl_air_t"] = bl_z, None
        st["kh_followed"] = False
        if not st.get("kh_longfall") and ks["state"] != KH_STATE_JUMP and (st.get("kh_ground") is None or abs(y - st["kh_ground"]) < 30):
            st["kh_ground"] = y
    elif st.get("bl_air_t") is None:
        st["bl_air_t"] = now
        st["kh_takeoff_y"] = y                             # (map feed: the jump arc is measured from here)
    if ground_mode:
        st["kh_ground"] = st["kh_floor_ref"]               # the bridge's floor IS the KH1 ground now
        st["kh_longfall"] = False
    ground = st.get("kh_ground")
    kh_air = ground is not None and y < ground - 5
    on_ = kh1_sora_on_ground(S, ks)
    if on_ is not None:
        # SORA ON THE FED GROUND: KH1 knows whether he stands (on a rock / slope above the flat-floor level he is not
        # "in the air"), and a jump's arc counts from where it started (user 2026-10-06: steep slope -> no jump)
        kh_air = not on_
        if bl_air and st.get("kh_takeoff_y") is not None:
            ground = st["kh_takeoff_y"]
    if not bl_air:
        st["kh_takeoff_y"] = None
    lift = CFG.get("kh_fall_lift", 120.0)
    st["kh_pin_pause"] = bl_air or st.get("kh_longfall", False) or ks["state"] == KH_STATE_ATTACK   # (height / dashes)
    if bl_air != st.get("dbg_bl_air"):
        st["dbg_bl_air"] = bl_air
        kh1_event(S, f"BL {'air' if bl_air else 'ground'} vz {float(pawn.Velocity.Z):.0f} | KH y {y:.0f} ground {ground} state {ks['state']} anim {ks['anim']}")
    if st.get("kh_glide_on") and ks["anim"] in KH_GLIDE_ANIMS and not ground_mode:
        if st.get("kh_glide_y") is None:
            st["kh_glide_y"] = y                                        # where the KH1 glide started (y down)
        if y > st["kh_glide_y"] + 20:
            kh1_write_y(S, ks, st["kh_glide_y"] + 8)                   # hold him up: no KH1 landing, never above the start
    elif not st.get("kh_glide_on"):
        st["kh_glide_y"] = None
    # long falls: BL keeps falling after KH1 would land (jumped off something high, or walked off a ledge)
    if st.get("kh_longfall"):
        if not bl_air:
            st["kh_longfall"] = False
            kh1_event(S, "long fall end: land")
            if ground is not None and y < ground - 5:
                kh1_write_y(S, ks, ground - 3)           # BL touched down: land KH1 Sora now
        elif ground is not None and y > ground - 40:
            kh1_write_y(S, ks, ground - lift)          # nearly down in KH1: lift again, fall animation continues
        return
    if bl_air and ground is not None and not ground_mode:
        # BL kept falling below where it left the ground while KH1 Sora stands -> walked off / jumped down
        drop = st.get("bl_ground_z", bl_z) - bl_z
        walked_off = not kh_air and ks["state"] != KH_STATE_JUMP and now - st["bl_air_t"] > 0.15 \
            and drop > CFG.get("kh_fall_drop", 30.0) and now - st.get("kh_jump_t", 0) > 0.15
        if walked_off:
            st["kh_longfall"] = True
            kh1_event(S, f"long fall start, BL {drop:.0f} below takeoff, followed jump {st.get('kh_followed')}")
            kh1_write_y(S, ks, ground - lift)
            return
    if bl_air and ks["anim"] in KH_GLIDE_ANIMS and st.get("kh_glide_on"):
        vz = -(st.get("kh_vy") or 0.0) * S.frame.scale * CFG.get("kh_glide_sink", 0.6)   # KH1's glide sink rate (gentler)

        def _gvel(pawn=pawn, vz=vz):
            v = pawn.Velocity
            pawn.Velocity = unrealsdk.make_struct("Vector", X=float(v.X), Y=float(v.Y), Z=vz)
        S.pending.append(_gvel)
        return
    # AIR COMBO HANG (user 2026-10-05: "I drop to the ground in the middle of a combo"): an attack that started in the
    # air keeps Borderlands airborne on KH1's own vertical motion (KH1 holds Sora up between air swings) - BL gravity
    # landed him mid-combo and the landing snapped KH1 Sora down, ending the combo. Config kh_air_combo_hang.
    air_atk = ks["state"] == KH_STATE_ATTACK and (kh_air or st.get("kh_air_atk", False))
    st["kh_air_atk"] = bool(air_atk and bl_air)
    if dbg_on(S, "dbg_air_trace") and (bl_air or ks["state"] == KH_STATE_ATTACK):   # air combo trace (next session)
        tr = st.setdefault("air_trace", [])
        tr.append(f"{now % 1000:8.2f} blair {int(bl_air)} blz {bl_z:8.1f} vz {float(pawn.Velocity.Z):6.0f} | khy {y:6.0f} "
                  f"gnd {ground} khair {int(kh_air)} st {ks['state']} an {ks['anim']} airatk {int(air_atk)} gm {int(bool(ground_mode))}")
        if len(tr) >= 400:
            with open(HERE / "air_trace.txt", "a") as f_:
                f_.write(chr(10).join(tr) + chr(10))
            tr.clear()
    if bl_air and air_atk and CFG.get("kh_air_combo_hang", True) and not ground_mode_land_ok(S):
        vz = -(st.get("kh_vy") or 0.0) * S.frame.scale * CFG.get("kh_jump_scale", 1.2)
        vz = max(-CFG.get("kh_air_combo_maxfall", 150.0), min(1500.0, vz))

        def _hang(pawn=pawn, vz=vz):
            v = pawn.Velocity
            pawn.Velocity = unrealsdk.make_struct("Vector", X=float(v.X), Y=float(v.Y), Z=vz)
        S.pending.append(_hang)
        return
    if bl_air and kh_air:
        # whole KH1 jump arc drives BL height: target = takeoff + KH1 height, KH1 speed + correction toward it
        st["kh_followed"] = True
        target = st.get("bl_ground_z", bl_z) + (ground - y) * S.frame.scale * CFG.get("kh_jump_scale", 1.2)
        vz = -(st.get("kh_vy") or 0.0) * S.frame.scale * CFG.get("kh_jump_scale", 1.2) + (target - bl_z) * CFG.get("kh_jump_gain", 12.0)
        if st.get("kh_postglide"):
            # after a glide KH1 Sora's height is meaningless (he was held 4 m up in KH1): follow only his fall speed,
            # otherwise every glide-off pulled Borderlands up toward that height ("Space makes me jump higher")
            vz = -(st.get("kh_vy") or 0.0) * S.frame.scale * CFG.get("kh_jump_scale", 1.2)
        vz = max(-3000.0, min(3000.0, vz))

        def _vel():
            v = pawn.Velocity
            pawn.Velocity = unrealsdk.make_struct("Vector", X=float(v.X), Y=float(v.Y), Z=vz)
        S.pending.append(_vel)


def ground_mode_land_ok(S) -> bool:
    """True when KH1 Sora is back on his floor (his air attack ended low) - then Borderlands may land normally."""
    st = S.state
    g, y = st.get("kh_ground"), st.get("kh_y")
    return g is not None and y is not None and y >= g - 5


KH_SUMMONING = 0x2D60FAC     # KH1 summon in progress (ref/kh1lua SteamGlobal "summoning"); Sora pointer is 0 meanwhile


def kh1_summon_intro(S, now: float) -> bool:
    """True while KH1 plays a summon's scripted intro: world freeze on (0x2D60FAC) and the Sora pointer cleared.
    The renderer keeps drawing Sora (party-leader fallback, v68) so kh1_live stays 1 - but nothing may write into KH1
    then: our pad input during the intro froze it on the staged Sora + Simba shot twice (2026-10-06). Cached per tick."""
    c = S.state.get("kh_intro_c")
    if c and c[0] == now:
        return c[1]
    v = False
    if getattr(S, "kh", None) and S.kh.get("h"):
        fl = kh1_mem(S, KH_SUMMONING, 1)
        sp = kh1_mem(S, KH_SORA_PTR, 8)
        v = bool(fl and fl[0] and sp is not None and not int.from_bytes(sp, "little"))
        # v2 (user: "only some of the summon audio comes through"): the summon CAST (Keyblade raised, the shout) starts
        # before the freeze - KH1's summon phase (0x298C8F4) leaves its resting 4 at FUN_140289210 (cast start) ->
        # intro mode from there, so those first sounds aren't started muted
        ph = int.from_bytes(kh1_mem(S, 0x298C8F4, 4) or b"\x00" * 4, "little")
        # only a DROP from 4 counts (a fresh KH1 has phase 0 until its first summon), for at most 30 s
        # ...and KH1's summon-cast flag (0x2867364 |= 0x102000 in FUN_140289210): a ROOM RELOAD also drops the phase to 1
        # (party flagged out for a moment) - that put the player on KH1's screen for 30 s after every reload
        fl_ = int.from_bytes(kh1_mem(S, KH_BATTLE_BITS, 4) or b"\x00" * 4, "little")
        if S.state.get("kh_phase_prev") == 4 and ph < 4 and fl_ & 0x100000:
            S.state["kh_cast_t"] = now
        S.state["kh_phase_prev"] = ph
        v = v or (ph < 4 and now - S.state.get("kh_cast_t", -99.0) < 30.0)
    S.state["kh_intro_c"] = (now, v)
    if v:
        S.state.setdefault("kh_intro_t0", now)
    else:
        S.state.pop("kh_intro_t0", None)
    return v


def summon_intro_view(S, pc, now: float) -> None:
    """User idea 2026-10-06: a summon's intro is a KH1 CUTSCENE (Sora raises the Keyblade, Simba roars on Pride Rock,
    KH1's own camera in a staged scene) -> show KH1's screen full-screen in Borderlands while it plays (the F9 menu's
    live window grab, sr_kh1_view) and slow Borderlands' world to a near-freeze like KH1 freezes its own
    (kh_menu_dilation). Back to Borderlands the moment KH1 unfreezes. Config kh1_summon_view."""
    st, dll = S.state, S.dll
    if not hasattr(dll, "sr_kh1_view") or st.get("kh_menu_open"):
        return
    want = bool(S.CFG.get("kh1_summon_view", True)) and kh1_summon_intro(S, now)
    mode = S.CFG.get("kh1_summon_view_mode", "native")
    if hasattr(dll, "sr_intro"):
        # "native" (renderer v74, user 2026-10-07: "the casting animations the same way as the F9 menu"): KH1's intro
        # redrawn from KH1's live data through KH1's own camera - black stage, Sora (party leader entity) and the
        # summon (its mesh, posed from its world joints), KH1's effects. Tinker Bell: a real actor in her intro (draw
        # bit 0x8 set ~4 s in, recorded 2026-10-12) - she was skipped as "an effect" and the user saw no Tink.
        if want and mode == "native":
            CFG = S.CFG
            ents, mdls = [], []
            lead = int.from_bytes(kh1_mem(S, KH_PARTY[0], 8) or bytes(8), "little")
            if lead:
                ents.append(lead); mdls.append(0)
            se, sm = st.get("kh_summon_ent", 0), st.get("kh_summon_mid", 0)
            if se and sm and sm not in CFG.get("kh1_intro_skip_models", []) and dll.sr_enemy_model(sm) > 0:
                ents.append(se); mdls.append(sm)
            # KH1 hides an actor mid-intro (Simba's shot: Sora's +0x374 bit 0x8 cleared at the cut, back at the end -
            # recorded 2026-10-07; drawn anyway he stood inside Simba, user) -> only actors KH1 itself draws
            vis = [(e, m_) for e, m_ in zip(ents, mdls)
                   if int.from_bytes(kh1_abs(S, e + 0x374, 4) or bytes(4), "little") & CFG.get("kh1_intro_vis_bit", 0x8)]
            ents, mdls = [e for e, _ in vis], [m_ for _, m_ in vis]
            n_ = len(ents)
            ea, ma = (ctypes.c_uint64 * 4)(*(ents + [0] * (4 - n_))), (ctypes.c_int * 4)(*(mdls + [0] * (4 - n_)))
            pp = (ctypes.c_float * 9)(CFG.get("kh1_intro_backdrop", 1.0), CFG.get("kh1_intro_bright", 1.0),
                                      CFG.get("kh1_intro_mirror", 1.0), CFG.get("kh1_intro_focal", 1.0),
                                      CFG.get("kh1_intro_oy", 0.0), 0.0, 0.0, 0.0, 1.0 if CFG.get("kh1_intro_fx", True) else 0.0)
            dll.sr_intro(1, ea, ma, n_, pp, 9)
            S.status["kh1_intro"] = [n_, mdls, dll.sr_intro_stats() if hasattr(dll, "sr_intro_stats") else None]
            if hasattr(dll, "sr_khfx_frame_stats"):           # v77: [using latched frame, KH1 frame no, latched, held]
                fs_ = (ctypes.c_uint32 * 4)(); dll.sr_khfx_frame_stats(fs_)
                S.status["kh1_intro"].append(list(fs_))
        elif st.get("kh_intro_native"):
            dll.sr_intro(0, None, None, 0, None, 0)
        st["kh_intro_native"] = bool(want and mode == "native")
    if want == bool(st.get("kh_intro_view")):
        return
    st["kh_intro_view"] = want
    # kh1_summon_view_mode "world" (default since 2026-10-07, user: "passthrough the entire animation into working in
    # Borderlands"): no window grab - the intro plays IN Borderlands (renderer draws Sora's intro pose from the party
    # leader, the summon arrives around him, KH1's own effects / sounds) while Borderlands' world is frozen like
    # KH1's. KH1's camera cuts and black stage backdrop are not reproduced. "window" = KH1's screen grabbed as before.
    world = S.CFG.get("kh1_summon_view_mode", "native") in ("world", "native")
    dll.sr_kh1_view(1 if want and not world else 0, ctypes.c_float(1.0))
    dil = S.CFG.get("kh_menu_dilation", 0.02) if want else 1.0
    try:
        wi = pc.WorldInfo
        S.pending.append(lambda wi=wi, d=dil: setattr(wi, "TimeDilation", d))
    except Exception:  # noqa: BLE001
        pass
    S.status["summon_view"] = ["on" if want else "off", time.strftime("%H:%M:%S")]
    kh1_event(S, (f"summon intro: played in Borderlands ({mode})" if world else "summon intro: showing KH1's screen") if want
              else "summon intro over: back to Borderlands")


KH1_SUMMON_SPELL = {4: "Fire"}   # summon id -> element of its attacks (Mushu's fire breath = Borderlands incendiary)


def summon_spawn_watch(S, now: float) -> None:
    """Every KH1 summon entity comes alive during the frozen intro once the summon phase (0x298C8F4) reaches 3/4
    (recorder 2026-10-06: Genie kind class 1, Dumbo class 0, Mushu no kind - only Simba / Bambi had class 3). So: a pool
    slot that comes alive while the freeze is on, phase >= 3, and isn't a Heartless (model ex_2xxx) = the summon.
    Model from KH1's summon id (0x298C8F0)."""
    import struct
    st = S.state
    if not getattr(S, "kh", None) or not S.kh.get("h"):
        return
    fl = kh1_mem(S, KH_SUMMONING, 1)
    ph = int.from_bytes(kh1_mem(S, 0x298C8F4, 4) or b"\x00" * 4, "little")
    if not fl or not fl[0]:
        st.pop("kh_spawn_prev", None)
        return
    pool = kh1_mem(S, KH_POOL, 96 * KH_STRIDE) or b""
    alive = {i for i in range(len(pool) // KH_STRIDE) if struct.unpack_from("<I", pool, i * KH_STRIDE + 0x374)[0] & 1}
    prev = st.get("kh_spawn_prev")
    st["kh_spawn_prev"] = alive
    if prev is None or ph < 3:
        return
    sid = int.from_bytes(kh1_mem(S, 0x298C8F0, 4) or b"\x00" * 4, "little")
    model = KH1_SUMMON_MODEL.get(sid)
    for i in sorted(alive - prev):
        ent = S.kh["base"] + KH_POOL + i * KH_STRIDE
        mid_bd = kh1_model_id(S, ent)
        if 2000 <= mid_bd < 3000 or not model:
            continue                                      # a Heartless (or an unknown summon id)
        st["kh_summon_ent"], st["kh_summon_mid"], st["kh_summon_logged"] = ent, model, (ent, sid)
        kh1_event(S, f"summon spawned during its intro: id {sid} slot {i} ent {ent:#x} -> model {model}")
        break


def summon_protect(S, pc) -> None:
    """KH1 freezes its whole world for a summon's ~19 s intro (FUN_1402ab8e0, flag 0x2D60FAC) while Sora poses - but
    Borderlands enemies kept shooting and the user died mid-intro (2026-10-06). Vanilla-faithful: nothing can hurt Sora
    during the freeze -> hold the Borderlands health at its value when the freeze began (config kh1_summon_protect)."""
    st = S.state
    if not S.CFG.get("kh1_summon_protect", True) or not getattr(S, "kh", None) or not S.kh.get("h"):
        st.pop("kh_freeze_hp", None)
        return
    flag = kh1_mem(S, KH_SUMMONING, 1)
    pawn = pc.Pawn if pc is not None else None
    if not flag or not flag[0] or pawn is None:
        if st.pop("kh_freeze_hp", None) is not None:
            S.status["summon_protect"] = "off"
        return
    try:
        pool = pawn.HealthPool.Data
        cur = float(pool.CurrentValue)
    except Exception:  # noqa: BLE001
        return
    held = st.get("kh_freeze_hp")
    if held is None or held <= 0:
        st["kh_freeze_hp"] = held = cur
        S.status["summon_protect"] = ["on", round(cur)]
    if cur < held:
        S.pending.append(lambda pool=pool, v=held: pool.SetCurrentValue(v))


def summon_watchdog(S, now: float) -> None:
    """SAFETY NET (2026-10-06: the user's first Simba summon froze KH1 on its intro close-up - Sora's party entity held
    state 10 / anim 64, summoning = 1, Sora pointer 0, no summon entity, Donald/Goofy kept fighting; only a room
    re-entry recovered it). While summons aren't supported: summoning on + Sora's party entity frozen in anim 64 at the
    same spot for kh1_summon_stuck_s -> forced room re-entry (warp home). v68: world freeze on + summon phase < 4 for
    kh1_summon_stuck_s instead (the pose test could have cancelled a working summon's intro)."""
    st, CFG = S.state, S.CFG
    if not CFG.get("kh1_summon_watchdog", True) or not getattr(S, "kh", None) or not S.kh.get("h"):
        return
    flag = kh1_mem(S, KH_SUMMONING, 1)
    ent = int.from_bytes(kh1_mem(S, KH_PARTY[0], 8) or b"\x00" * 8, "little")
    if not flag or not flag[0] or not ent:
        st.pop("kh_summon_stuck", None)
        return
    # v68: judged by KH1's summon PHASE (0x298C8F4), not Sora's pose: a working summon reaches phase 4 within ~1 s
    # (the frozen one sat at 1 = voice-bank registration); Sora may hold his summon pose through a normal 19 s intro
    ph = int.from_bytes(kh1_mem(S, 0x298C8F4, 4) or b"\x00" * 4, "little")
    # second trigger: an intro (freeze + Sora pointer 0) that outlasts kh1_summon_intro_max_s (a normal one ~19 s)
    if kh1_summon_intro(S, now) and now - S.state.get("kh_intro_t0", now) > CFG.get("kh1_summon_intro_max_s", 35.0):
        ph = -1
    key = ph
    first = st.get("kh_summon_stuck")
    if (ph >= 4 and ph != -1) or first is None or first[0] != key:
        st["kh_summon_stuck"] = (key, now)
        return
    if now - first[1] > CFG.get("kh1_summon_stuck_s", 8.0) and now > st.get("kh_warping_until", 0):
        # diagnostics before recovering: KH1's summon phase (0x298C8F4: 1 -> 2 -> 3 = summon data load issued (FUN_14028a190)
        # -> 4 = loads done, FUN_140289a00 / 1402899b0; Simba also loads "simba.dpx"), summon id (0x298C8F0, 5 = Simba,
        # 2 = Genie), event flags 0x23AB2D0 (bit 2 checked by FUN_140289750)
        ph, sid_, evf = (int.from_bytes(kh1_mem(S, a_, 4) or b"\x00" * 4, "little") for a_ in (0x298C8F4, 0x298C8F0, 0x23AB2D0))
        kh1_event(S, f"summon stuck: phase {ph} summon id {sid_} event flags {evf:#x}")
        home = CFG.get("kh_home", KH_HOME_PROXY if kh1_proxies_on(S) else KH_HOME)
        for rva, v in ((KH_WARP_WORLD, home[0]), (KH_WARP_ENTRANCE, home[1]), (KH_WARP_TYPE, 10), (KH_WARP_TRIGGER, 1)):
            kh1_poke(S, rva, int(v).to_bytes(4, "little", signed=True))
        st["kh_warping_until"] = now + 3.0
        st.pop("kh_summon_stuck", None)
        S.status["summon_watchdog"] = ["recovered", time.strftime("%H:%M:%S")]
        kh1_event(S, "summon stuck on its intro (phase < 4 for too long) -> room re-entry to recover")


def kh1_event(S, text: str) -> None:
    """Small rolling log of airborne/long-fall transitions (kh1_events.txt next to logic.py)."""
    try:
        p = HERE / "kh1_events.txt"
        lines = p.read_text().splitlines()[-200:] if p.exists() else []
        lines.append(f"{time.strftime('%H:%M:%S')} {text}")
        p.write_text("\n".join(lines) + "\n")
    except Exception:  # noqa: BLE001
        pass


KH_WORLD, KH_ROOM = 0x233FE94, 0x233FE8C
# KH1 field warp (verified 2026-09-30): write world + ENTRANCE number, type 10, countdown 1 -> KH1 loads that spot.
KH_WARP_WORLD, KH_WARP_ENTRANCE, KH_WARP_TYPE, KH_WARP_TRIGGER, KH_GAME_MODE = 0x233FEB8, 0x233FEBC, 0x22EC0B0, 0x22EC0AC, 0x233FBC0
KH_HOME = (4, 7, 2)        # Wonderland entrance 7 -> room 2: ~10 m of open floor (room_scan.py); was (4, 29, 8) Bizarre Room
KH_HOME_PROXY = (3, 66, 24)  # Traverse Town Second District (entrance 66 -> room 24, tt_scan.py): open, picked by the player.
                             # (The Bizarre Room's walls made KH1 Sora grab ledges, which looked like climbing in Borderlands.)
KH_HOME_VERSION = 4        # bump when KH_HOME changes so a running session warps again


def kh1_go_home(S, force: bool = False) -> bool:
    """Warps KH1 Sora to the Bizarre Room (CFG kh_home = [world, entrance, room]) unless he is already there.
    Only from normal field play (game mode 5, Sora standing/moving) so menus, cutscenes and loads are left alone."""
    home = S.CFG.get("kh_home", KH_HOME_PROXY if kh1_proxies_on(S) else KH_HOME)
    if not home:
        return True
    ks = kh1_state(S)                                 # (also opens the KH1 handle)
    if not ks:
        return False
    w, r, mode = kh1_mem(S, KH_WORLD, 4), kh1_mem(S, KH_ROOM, 4), kh1_mem(S, KH_GAME_MODE, 4)
    if not (w and r and mode):
        return False
    if int.from_bytes(w, "little") == home[0] and int.from_bytes(r, "little") == home[2] and not force:
        return True                                   # already home
    if int.from_bytes(mode, "little") != 5 or ks["state"] not in (2,):
        return False                                  # not in plain field play right now: try again later
    for rva, v in ((KH_WARP_WORLD, home[0]), (KH_WARP_ENTRANCE, home[1]), (KH_WARP_TYPE, 10), (KH_WARP_TRIGGER, 1)):
        kh1_poke(S, rva, int(v).to_bytes(4, "little", signed=True))
    S.state["kh_warping_until"] = time.time() + 3.0
    S.state["kh_room_deaths"] = 0                     # the load respawns the room's Heartless
    if hasattr(getattr(S, "dll", None), "sr_hold"):     # renderer v84: keep the last HUD frame / poses through the load
        S.dll.sr_hold.argtypes = [ctypes.c_float]
        S.dll.sr_hold(float(S.CFG.get("kh_reload_hold_s", 3.0)))
    kh1_event(S, f"warped KH1 Sora home {home}")
    return True


KH_ANCHORS_VERSION = 4      # bump when KH_ANCHORS changes so a running session re-places Sora
KH_ANCHORS = {"3:0": [-343.0, -2168.0],      # Traverse Town First District, middle of the square
              "4:8": [969.0, 2.2],           # Wonderland Bizarre Room: spot picked by the player (combo dummy room)
              "4:2": [-222.0, 358.0],        # Wonderland room 2: most open floor point, ~9.9 m clear (room_scan.py)
              "3:24": [3.0, 648.1, 0.0]}     # Traverse Town Second District: middle, on the ground (player's spot)


def kh1_room(S):
    w, r = kh1_mem(S, KH_WORLD, 4), kh1_mem(S, KH_ROOM, 4)
    return f"{int.from_bytes(w, 'little')}:{int.from_bytes(r, 'little')}" if w and r else None


def kh1_go_to_anchor(S) -> None:
    """An open spot per KH1 room (CFG kh_anchors overrides KH_ANCHORS, "world:room" -> [x, z]). Swinging or dashing
    toward a wall makes KH1 Sora rebound (anim 110), so he is moved there and the pin is re-anchored."""
    anchors = {**KH_ANCHORS, **S.CFG.get("kh_anchors", {})}
    ks = kh1_state(S)
    room = kh1_room(S)
    if room not in anchors:
        return True                                   # no anchor for this room: nothing to do
    if not ks or ks["state"] != 2:
        return False                                  # Sora not loaded / busy yet (e.g. just warped): retry
    a = anchors[room]
    import struct
    ctypes.windll.kernel32.WriteProcessMemory(ctypes.c_void_p(S.kh["h"]), ctypes.c_void_p(ks["addr"] + 0x10),
                                              struct.pack("<3f", a[0], a[2] if len(a) > 2 else ks["pos"][1], a[1]), 12, None)
    S.state["kh_floor_ref"] = None              # re-measure the Borderlands floor where he stands now
    S.dll.sr_set_pin(0, ctypes.c_float(0))      # next pin call anchors at the new spot
    kh1_event(S, f"moved KH1 Sora to anchor {a} in room {room}")
    return True


def kh1_unlock(S, pc) -> None:
    if S.state.pop("kh_lock", None):
        S.pending.append(lambda: pc.IgnoreMoveInput(False))


def kh1_foreground(S) -> bool:
    """True while the KH1 window is in front (the player is playing KH1 directly): the mod then stays out of KH1."""
    st = S.state
    if time.time() - st.get("kh_fg_t", 0) > 0.25:
        st["kh_fg_t"] = time.time()
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(ctypes.windll.user32.GetForegroundWindow(), ctypes.byref(pid))
        kh = getattr(S, "kh", None)
        st["kh_fg"] = bool(kh and kh.get("pid") and pid.value == kh["pid"])
    return st.get("kh_fg", False)


def kh1_pad(S, pc, pawn, speed: float, now: float) -> None:
    st, CFG, dll = S.state, S.CFG, S.dll
    live = S.status.get("kh1_live") == 1
    if live:
        st["kh_live_t"] = now
    live = live or now - st.get("kh_live_t", 0) < 1.0          # ride out short pose dropouts (loading, menus)
    # KH1's main menu (F9) stops posing field Sora (only the 3 menu models are evaluated) -> no live pose while it is
    # open; that must not switch KH1 mode off (it froze BL with the menu open and F9 dead - user had to restart both)
    live = live or bool(st.get("kh_menu_open")) or now - st.get("kh_menu_shut_t", 0) < 2.0
    on = bool(CFG["flags"] & 128) and hasattr(dll, "sr_set_pad") and live and not kh1_foreground(S) \
        and not kh1_summon_intro(S, now)     # summon intro: KH1 plays a scripted scene - no input into it (froze it)
    if not on:
        if st.get("kh_on"):
            dll.sr_set_pad(0, 0, 0x80, 0x80, 0x80, 0x80)
            dll.sr_set_pin(0, ctypes.c_float(0))
            kh1_unlock(S, pc)
            if st.pop("kh_move_locked", False):
                S.pending.append(lambda: pc.IgnoreMoveInput(False))
            st["kh_move_on"] = False
            kh1_set_walls(S, False)
            kh1_ground_off(S)
            cp = st.pop("kh_crouch_pawn", None)
            if cp is not None:
                S.pending.append(lambda cp=cp: setattr(cp, "bCanCrouch", True))
            try:
                if not pawn.bCanCrouch:
                    S.pending.append(lambda pawn=pawn: setattr(pawn, "bCanCrouch", True))
            except Exception:  # noqa: BLE001
                pass
            if st.get("kh_sc_orig") and getattr(S, "kh", None) and S.kh.get("h"):
                kh1_poke(S, KH_SHORTCUTS, st["kh_sc_orig"])
            st["kh_on"] = False
        return
    kh1_body_sync(S, pc, pawn, now)
    if not st.get("kh_injected"):
        dll.sr_inject_bridge()
        st["kh_injected"] = True
    if st.get("kh_home_ver") != KH_HOME_VERSION:                # home changed: warp again this session
        st["kh_home_ver"], st["kh_home_done"] = KH_HOME_VERSION, False
    if not st.get("kh_home_done") and getattr(S, "kh", None) and S.kh.get("h"):
        if now - st.get("kh_home_try", 0) > 1.0:          # once per session, retried until KH1 is in field play
            st["kh_home_try"] = now
            st["kh_home_done"] = kh1_go_home(S)
    room = kh1_room(S) if getattr(S, "kh", None) and S.kh.get("h") else None
    if st.get("kh_anchor_ver") != KH_ANCHORS_VERSION:            # anchors edited: place him again (and re-pin)
        st["kh_anchor_ver"], st["kh_anchor_room"] = KH_ANCHORS_VERSION, None
    if room != st.get("kh_room_seen"):                          # room changed (door / warp / load): let it settle
        st["kh_room_seen"], st["kh_room_t"] = room, now
    if st.get("kh_warping_until", 0) > now:
        room = None                                             # our own warp is in flight
    if room and room != st.get("kh_anchor_room") and now - st.get("kh_room_t", now) > 2.5             and now - st.get("kh_anchor_try", 0) > 0.5:
        st["kh_anchor_try"] = now
        if kh1_go_to_anchor(S):                                 # once per room visit; KH1 Sora may be moved on purpose
            st["kh_anchor_room"] = room
    st["kh_on"] = True
    # left stick: BL ground speed -> KH1 walk (partial tilt) or run (full). Direction doesn't matter: only the pose is used.
    mag = 0.0
    if speed > 30 and int(pawn.Physics) != 2:
        mag = 1.0 if speed / max(S.frame.scale, 0.01) > CFG["walk_max"] else CFG.get("kh_walk_tilt", 0.4)
    # crouch -> KH1 Dodge Roll, moving OR standing still (J7, user 2026-10-12: standing C crouched Borderlands' pawn and
    # crouch-walked slowly = looked glitched). Standing: rolls the way Sora faces. Borderlands' own crouch is blocked
    # while Sora mode is on (bl_roll_hooks: Engine.Pawn:ShouldCrouch). Carried kh_roll_dist forward at full speed
    # (crouch speed cap lifted). KH1's roll is invincible early on -> no Borderlands damage for kh_roll_iframe_s.
    # ROLL SPAM (user 2026-10-12, session 8): BL's crouch key is a TOGGLE (pc.bDuck flipped per press) -> only every other
    # press was a rising edge, and a press during a roll was dropped. Now every press counts (bDuck is cleared right after
    # we see it, so BL never sits in a toggled crouch = no crouch icon), a press during a roll is buffered and rolls again
    # the moment the roll ends (KH1-style roll spam, faster than running), and the roll lasts KH1's real 0.52 s.
    try:
        if not pawn.bCanCrouch:                              # an older version switched Borderlands crouch off
            S.pending.append(lambda pawn=pawn: setattr(pawn, "bCanCrouch", True))
        # the INPUT only: pawn.bWantsToCrouch is also set by our own roll crouch, which kept you crouched after rolls
        # the crouch KEY itself (CFG kh_roll_vk, C): BL's toggle keeps its own state, so pc.bDuck only changed on every
        # other press even after clearing it. Only while Borderlands is the focused window.
        fg = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(ctypes.windll.user32.GetForegroundWindow(), ctypes.byref(fg))
        keyd = fg.value == os.getpid() and bool(ctypes.windll.user32.GetAsyncKeyState(CFG.get("kh_roll_vk", 0x43)) & 0x8000)
        duck = keyd or now < st.get("dir_duck", 0)
        if pc.bDuck:                                         # BL's own crouch toggle: never left on (no crouch icon)
            S.pending.append(lambda pc=pc: setattr(pc, "bDuck", 0))
    except Exception:  # noqa: BLE001
        duck = False
    press = duck and not st.get("kh_duck")
    if press and now < st.get("kh_roll_until", 0):
        st["kh_roll_buf"] = now                              # spam: roll again as soon as this one ends

    def _roll_start():
        kh1_press(S, PAD_SQUARE, 0.12)
        d_in, _m = kh1_move_input(S, pc)
        v = pawn.Velocity
        if d_in is not None:
            st["kh_roll_dir"] = d_in
        elif speed > 30:
            st["kh_roll_dir"] = math.atan2(float(v.Y), float(v.X))
        else:                                                # standing still: the way Sora faces
            st["kh_roll_dir"] = st.get("yaw", float(pawn.Rotation.Yaw) * math.pi / 32768.0)
        st["kh_iframe_until"] = now + CFG.get("kh_roll_iframe_s", 0.45)
        st["kh_roll_t0"] = now
        st["kh_roll_until"] = now + CFG.get("kh_roll_time", 0.52)
        st["yaw"] = st["face_want"] = st["kh_yaw_target"] = st["kh_roll_dir"]
        if not st.get("kh_roll_active"):
            S.pending.append(lambda pawn=pawn: kh_roll_begin(S, pawn))
        st["kh_roll_active"] = True
        st["kh_roll_buf"] = 0
        st["kh_rolls"] = st.get("kh_rolls", 0) + 1
    grounded = int(pawn.Physics) != 2
    if press and grounded and now >= st.get("kh_roll_until", 0) and (speed > 30 or CFG.get("kh_roll_standing", True)):
        _roll_start()
    if st.get("kh_roll_active"):
        if now < st["kh_roll_until"]:
            T = st["kh_roll_until"] - st["kh_roll_t0"]
            k = (now - st["kh_roll_t0"]) / T
            dist = CFG.get("kh_roll_dist", 360.0) * S.frame.scale       # KH1 roll ~3.6 m
            sp_ = dist / T * (1.6 - 1.2 * k)                          # fast out of the crouch, easing into the stand
            rd = st["kh_roll_dir"]
            S.pending.append(lambda pawn=pawn, vx=math.cos(rd) * sp_, vy=math.sin(rd) * sp_: kh_roll_push(pawn, vx, vy))
        elif grounded and now - st.get("kh_roll_buf", 0) < CFG.get("kh_roll_buffer_s", 0.6):
            _roll_start()                                    # chained roll (keeps the roll state: no stand-up between)
        else:
            st["kh_roll_active"] = False
            S.pending.append(lambda pawn=pawn, pc=pc: kh_roll_end(S, pawn, pc))
    kh_auto_crouch_tick(S, pawn, now)
    st["kh_duck"] = duck
    S.status["kh_duck"] = [duck, round(st.get("kh_roll_until", 0) - now, 2), st.get("kh_rolls", 0), bool(st.get("kh_auto_crouch"))]
    if now < st.get("kh_guard_until", 0):
        mag = 0.0                                   # guarding (right click): KH1 Square with the stick neutral
    if st.get("kh_attacking") and now >= st.get("kh_roll_until", 0):
        mag = 0.0                                   # swinging: no stick in KH1 (would cancel / redirect the combo)
    if now < st.get("kh_roll_until", 0):
        mag = 1.0                                   # roll needs the stick held
    # jump: Circle on takeoff
    air = int(pawn.Physics) == 2
    if air and not st.get("kh_air") and float(pawn.Velocity.Z) > 80:
        ks_ = kh1_state(S)
        # KH1 owns the body (user 2026-10-06: air combos never launched): KH1 can't jump out of a GROUND combo, but
        # Borderlands could -> BL flew while KH1 Sora kept swinging on his floor = ground hits (knock BACK, never up).
        # combo_trace / air_trace: 77/77 airborne attack frames had KH1 on the ground (anim 208). Vanilla: the jump is
        # ignored mid-combo -> cancel the Borderlands takeoff too, so BL is only airborne when KH1 Sora really is.
        if CFG.get("kh_jump_combo_lock", True) and ks_ and ks_["state"] == KH_STATE_ATTACK                 and ks_["anim"] in KH_SWING_ANIMS and ground_mode_land_ok(S):
            def _nojump(pawn=pawn):
                v = pawn.Velocity
                pawn.Velocity = unrealsdk.make_struct("Vector", X=float(v.X), Y=float(v.Y), Z=-200.0)
            S.pending.append(_nojump)
            st["kh_jump_blocked"] = st.get("kh_jump_blocked", 0) + 1
            S.status["kh_jump_blocked"] = [st["kh_jump_blocked"], ks_["anim"]]
            st["kh_jump_block_t"] = now
        elif kh1_submenu_open(S):
            # Magic / Items submenu open (user 2026-10-12: jumped "standing"): KH1's Circle backs out of a submenu
            # instead of jumping -> close it first, the real jump follows once KH1 shows the top level again
            kh1_press(S, PAD_CIRCLE, 0.06)
            st["kh_jump_after_menu"] = now + CFG.get("kh_jump_menu_wait", 0.6)
            st["kh_jump_t"] = now
        else:
            kh1_press(S, PAD_CIRCLE, CFG.get("kh_jump_hold", 0.3))   # held = full-height (High Jump) arc
            st["kh_jump_t"] = now
    if st.get("kh_jump_after_menu") and (PAD_CIRCLE not in st.get("kh_press", {})):
        if not kh1_submenu_open(S) and air:
            kh1_press(S, PAD_CIRCLE, CFG.get("kh_jump_hold", 0.3))
            st["kh_jump_after_menu"] = None
            st["kh_menu_jumps"] = st.get("kh_menu_jumps", 0) + 1
            S.status["kh_menu_jumps"] = st["kh_menu_jumps"]
        elif now > st["kh_jump_after_menu"] or not air:
            st["kh_jump_after_menu"] = None
    # Glide (KH1 style): press jump AGAIN in the air and hold it. The jump key is read directly (Space by default,
    # CFG kh_glide_vk) because Borderlands only reports the first press.
    kh1_glide_abilities(S)
    key = bool(ctypes.windll.user32.GetAsyncKeyState(CFG.get("kh_glide_vk", 0x20)) & 0x8000) or now < st.get("dir_space", 0)
    ks_g = kh1_state(S) if air and key and not st.get("kh_jumpkey") else None
    slide = bool(ks_g) and not st.get("kh_glide_on") and kh1_sora_on_ground(S, ks_g) is True         and abs(float(pawn.Velocity.Z)) < CFG.get("kh_slide_vz", 150.0) and now - st.get("kh_jump_t", 0) > 0.4
    if slide:
        # STEEP SLOPE (user 2026-10-06): Borderlands slides him (PHYS_Falling on a slope too steep for BL) while KH1
        # Sora stands on it -> BL refused the jump and Space started a glide. KH1 decides: he stands, so he JUMPS;
        # Borderlands follows the KH1 arc from here (body_sync: bl_air + KH1 airborne)
        kh1_press(S, PAD_CIRCLE, CFG.get("kh_jump_hold", 0.3))
        st["kh_jump_t"] = now
        st["kh_takeoff_y"], st["bl_ground_z"] = ks_g["pos"][1], float(pawn.Location.Z)
        st["kh_slide_jumps"] = st.get("kh_slide_jumps", 0) + 1
        S.status["kh_slide_jumps"] = st["kh_slide_jumps"]
    elif air and key and not st.get("kh_jumpkey") and now - st.get("kh_jump_t", 0) > 0.15             and now - st.get("kh_jump_block_t", 0) > 0.6:          # (not right after a cancelled mid-combo takeoff)
        if st.get("kh_glide_on"):
            st["kh_glide_on"] = False                          # Space again: stop gliding (drop)
        else:
            st["kh_glide_on"] = True                           # Space in the air: glide until Space again or landing
            st.get("kh_press", {}).pop(PAD_CIRCLE, None)       # release the takeoff press so KH1 sees a NEW press
            st["kh_glide_press_t"] = now + 0.05
    if st.get("kh_glide_on") and not air:
        st["kh_glide_on"] = False                              # landed in Borderlands
    st["kh_jumpkey"] = key
    if key or air:   # diagnostics: last time the jump key / airborne state were seen
        dbg = st.setdefault("kh_glide_dbg", {})
        if key: dbg["key_t"] = round(now % 1000, 2)
        if air: dbg["air_t"] = round(now % 1000, 2)
        if key and air: dbg["key_in_air_t"] = round(now % 1000, 2)
        dbg["since_takeoff"] = round(now - st.get("kh_jump_t", 0), 2)
        S.status["kh_glide_dbg"] = dbg
    if not st.get("kh_glide_on"):
        st["kh_glide_seen"] = False
        st["kh_glide_tries"] = 0
    kh1_glide_tick(S, kh1_state(S), now)
    kh1_ability_tick(S, now)
    st["kh_air"] = air
    buttons = 0
    for bits, win in list(st.get("kh_press", {}).items()):
        start, until = win if isinstance(win, tuple) else (0.0, win)
        if now >= until:
            del st["kh_press"][bits]
        elif now >= start:
            buttons |= bits
    lx, ly = 0x80, 0x80 - int(127 * mag)
    if CFG.get("kh_move", True):
        ks_now = kh1_state(S)
        if ks_now:
            lx, ly = kh1_move_stick(S, pc, ks_now, now)
            if st.get("kh_glide_on") and ks_now["anim"] in KH_GLIDE_ANIMS:
                lx, ly = 0x80, 0x80                                  # gliding straight in KH1 (Borderlands steers)
            if now < st.get("kh_guard_until", 0):
                lx, ly = 0x80, 0x80                                  # guard: stick neutral
            if st.get("kh_attacking") and now >= st.get("kh_roll_until", 0):
                lx, ly = 0x80, 0x80                                  # swinging: no stick (KH1 combo)
            elif now < st.get("kh_roll_until", 0) and (lx, ly) == (0x80, 0x80):
                lx, ly = 0x80, 0x00                                  # roll with no input: straight ahead
    if st.get("kh_abil") and not st.get("kh_abil_started"):
        lx, ly = 0x80, 0x80                                          # KH1 only takes the ability with Sora standing
    if st.get("kh_menu_open"):
        buttons, lx, ly = (buttons & PAD_START) | kh_menu_pad(S), 0x80, 0x80
    S.status["kh_pad"] = [buttons, lx, ly, bool(st.get("kh_menu_open")), bool(st.get("kh_glide_on")), round(st.get("kh_guard_until", 0) - now, 1), round(st.get("kh_blocked_until", 0) - now, 1), bool(st.get("kh_attacking")), bool(CFG.get("kh_move", True))]
    dll.sr_set_pad(1, buttons, 0x80, 0x80, lx, ly)
    # pin keeps KH1 Sora near his spot; paused while airborne because it rewrites his height too
    # KH1-driven movement needs a wide leash: every pin teleport kills KH1 Sora's momentum (40 cm: 255 cm/s, 300 cm: 398,
    # free: 429 measured), so he may roam kh_move_pin_radius around his spot (keep that spot >= this far from walls)
    radius = CFG.get("kh_move_pin_radius", 250.0) if CFG.get("kh_move", True) else CFG.get("kh_pin_radius", 40.0)
    if kh1_pin_tick(S, kh1_state(S), now, speed, radius):
        dll.sr_set_pin(0, ctypes.c_float(0))
    else:
        dll.sr_set_pin(1, ctypes.c_float(1e9 if st.get("kh_pin_pause") else radius))
    info = (ctypes.c_int * 5)()
    dll.sr_bridge_info(info)
    S.status["kh1_bridge"] = list(info)[:3]


# ---------------------------------------------------------------- lock-on (KH1 style, Borderlands targets)
class _Vec:
    __slots__ = ("X", "Y", "Z")


class FreeTarget:
    """A free KH1 Heartless (state free_bl entry) dressed as a Borderlands pawn - Location, Name, HealthPool, bDeleteMe -
    so lock-on, the reticle, auto-facing, abilities and spell aim treat it like any enemy. Holds the entry dict itself,
    so the lock survives the entry being rebound to a fresh KH1 actor. Damage comes from KH1 (its real hits on the
    Heartless' KH1 body), never from Borderlands' TakeDamage."""

    def __init__(self, S, hb, n):
        self.S, self.hb, self.Name = S, hb, f"Heartless_{n}"

    @property
    def alive(self) -> bool:
        hb = self.hb
        return (any(h is hb for h in self.S.state.get("free_bl", {}).values()) and not hb.get("dead_until")
                and hb.get("hp", 1) > 0)

    @property
    def bDeleteMe(self) -> bool:
        return not self.alive

    @property
    def Location(self):
        v = _Vec()
        v.X, v.Y, v.Z = float(self.hb["pos"][0]), float(self.hb["pos"][1]), float(self.hb.get("z", 0.0)) + 45.0
        return v

    @property
    def HealthPool(self):
        from types import SimpleNamespace
        return SimpleNamespace(Data=SimpleNamespace(CurrentValue=float(self.hb.get("hp", 1)) if self.alive else 0.0))


def free_targets(S, pc, reach: float):
    """Free Heartless within reach as (distance, FreeTarget, bearing), nearest first (one FreeTarget per entry)."""
    cache = S.state.setdefault("free_tgt", {})
    me = vec3(pc.Pawn.Location)
    out = []
    for n, hb in enumerate(S.state.get("free_bl", {}).values()):
        t = cache.get(id(hb))
        if t is None or t.hb is not hb:
            t = cache[id(hb)] = FreeTarget(S, hb, n)
        if not t.alive:
            continue
        dx, dy = hb["pos"][0] - me[0], hb["pos"][1] - me[1]
        d = math.hypot(dx, dy)
        if d <= reach:
            out.append((d, t, math.atan2(dy, dx)))
    return sorted(out, key=lambda e: e[0])


def all_targets(S, pc, reach: float):
    """Borderlands enemies + free KH1 Heartless, nearest first."""
    return sorted(list(enemies(S, pc, reach)) + free_targets(S, pc, reach), key=lambda e: e[0])


def lock_target(S):
    lk = S.state.get("lock")
    try:
        if lk is not None and hp(lk) > 0:
            return lk
    except Exception:  # noqa: BLE001
        pass
    S.state["lock"] = None
    return None


def lock_pick(S, pc, exclude=None):
    """Enemy closest to the crosshair (within 70 deg and lock_range), nearer ones preferred."""
    cam_yaw = int(pc.Rotation.Yaw) * UU
    best, score = None, 1e18
    for dist, p, bearing in all_targets(S, pc, S.CFG.get("lock_range", 3000.0)):
        off = abs(ang(bearing - cam_yaw))
        if p is exclude or off > math.radians(70):
            continue
        sc = off * 1500 + dist
        if sc < score:
            best, score = p, sc
    return best


KH_LOCK = 0x2DB3510      # KH1 lock-on: u32 mode (0 none, 2 soft target, 3 locked), +8 u32 target handle, +0x10 last
PAD_R1 = 0x800


def kh1_lock(S):
    """(mode, KH1 entity address) of KH1's own lock-on."""
    import struct
    b = kh1_mem(S, KH_LOCK, 0x10)
    if not b:
        return 0, 0
    mode, hnd = struct.unpack_from("<I", b, 0)[0], struct.unpack_from("<I", b, 8)[0]
    return mode, (kh1_resolve(S, hnd) if hnd else 0)


def bl_pawn_for_kh1(S, pc, ent: int):
    """The Borderlands enemy a KH1 entity stands for: its stand-in pairing, or the free Heartless itself."""
    st = S.state
    hb = st.get("free_bl", {}).get(ent)
    if hb is not None:
        return next((t for _, t, _ in free_targets(S, pc, 1e6) if t.hb is hb), None)
    key = st.get("kh_pmap", {}).get(ent)
    return st.get("kh_bl_by_key", {}).get(key) if key else None


KH_RETICLE = 0x284F900          # KH1's lock-on / target cursor sprite as built this frame (FUN_140274b20 ->
KH_RETICLE_STATE = 0x284FA08    # FUN_14027d470): +0x20 4 x (u, v) i32 (1/16 texel), +0x60 4 x RGBA i32 (128 = 1),
                                # +0xA0 4 x (x, y, z) i32 (1/16 px, PS2 640x448 + 2048 offset). State: 0 none,
                                # 1-3 locked (KH1 mode 3), 4 soft target (modes 1, 2, 4)


def kh1_ui_cursor(S):
    r"""KH1's target-cursor sprite as KH1 submitted it this frame (kh1_fx v10 copies lock-on UI sprites into
    Local\SoraUi with the submitting function). The sprite buffer 0x284F900 is shared with the blue "LOCK ON!" letters
    (colour 60,100,200), so reading it directly showed letter pieces over the target. Returns the 0xE0-byte sprite,
    b"" when KH1 drew no cursor lately, None when the mapping isn't there (older DLL)."""
    import ctypes.wintypes as W
    m = getattr(S, "kh1_ui_map", None)
    if m is None:
        if time.time() < S.state.get("kh1_ui_retry", 0):
            return None
        S.state["kh1_ui_retry"] = time.time() + 3.0
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenFileMappingW.restype = W.HANDLE
        k32.MapViewOfFile.restype = ctypes.c_void_p
        k32.MapViewOfFile.argtypes = [W.HANDLE, W.DWORD, W.DWORD, W.DWORD, ctypes.c_size_t]
        h = k32.OpenFileMappingW(0x0004, False, r"Local\SoraUi")
        p = h and k32.MapViewOfFile(h, 0x0004, 0, 0, 64 + 256 * 256)
        if not p:
            return None
        m = S.kh1_ui_map = (ctypes.c_char * (64 + 256 * 256)).from_address(p)
    w, frame = struct.unpack_from("<II", m, 12)
    for i in range(w - 1, max(-1, w - 256), -1):                     # newest first
        o = 64 + (i % 256) * 256
        seq, caller, _t, _n, fr = struct.unpack_from("<IIqII", m, o)
        if seq != i + 1:
            continue
        if frame - fr > 3:
            break
        if 0x274B20 <= caller < 0x274E70:                            # FUN_140274b20 = the target cursor
            return bytes(m[o + 32:o + 32 + 0xE0])
    return b""


def kh1_reticle_read(S, pc, mode: int, ent: int) -> None:
    """KH1's own target cursor: which enemy it marks and the sprite KH1 draws for it this frame (art, colour, size,
    animation) - kh_reticle draws exactly that over the Borderlands enemy the KH1 Heartless stands for."""
    import struct
    st = S.state
    stb = kh1_mem(S, KH_RETICLE_STATE, 4)
    state = struct.unpack("<i", stb)[0] if stb else 0
    pawn = bl_pawn_for_kh1(S, pc, ent) if (state and ent) else None
    st["kh_ret_pawn"] = pawn
    if pawn is None:
        st["kh_ret"] = None
        return
    b = kh1_ui_cursor(S)
    if b is None:                                # kh1_fx < v10: the shared buffer (also used by "LOCK ON!" letters)
        b = kh1_mem(S, KH_RETICLE, 0xE0)
    if not b:
        st["kh_ret"] = None
        return
    uv = [struct.unpack_from("<2i", b, 0x20 + 16 * k) for k in range(4)]
    col = struct.unpack_from("<4i", b, 0x60)
    xy = [struct.unpack_from("<2i", b, 0xA0 + 16 * k) for k in range(4)]
    us, vs = [u for u, _ in uv], [v for _, v in uv]
    xs, ys = [x for x, _ in xy], [y for _, y in xy]
    st["kh_ret"] = {"u0": min(us) / 16.0, "v0": min(vs) / 16.0, "u1": max(us) / 16.0, "v1": max(vs) / 16.0,
                    "w": (max(xs) - min(xs)) / 16.0, "h": (max(ys) - min(ys)) / 16.0,
                    "col": [c / 128.0 for c in col], "state": state}
    S.status["kh1_reticle"] = [state, round(st["kh_ret"]["w"], 1), [round(c, 2) for c in st["kh_ret"]["col"]]]


def kh_real_lock(S) -> bool:
    return bool(S.CFG.get("kh_real_lock", True)) and kh1_combat_on(S) and kh1_state(S) is not None


def kh1_lock_sync(S, pc, now: float) -> None:
    """KH1's own lock-on drives the mod's lock (camera, reticle, spell aim): whatever KH1 locked, Borderlands locks the
    enemy that KH1 Heartless stands for; KH1 releasing (target gone / out of reach) releases it here too."""
    st = S.state
    if not kh_real_lock(S):
        return
    mode, ent = kh1_lock(S)
    S.status["kh1_lock"] = [mode, hex(ent) if ent else None]
    kh1_reticle_read(S, pc, mode, ent)
    want = st.get("kh_lock_want")
    if want or now - st.get("lock_dbg_t", 0) < 3.0:
        if want:
            st["lock_dbg_t"] = now
        dbg = st.setdefault("lock_dbg", [])
        fk = [hex(e) for e in st.get("free_bl", {})][:4]
        dbg.append([round(now % 100, 2), mode, hex(ent) if ent else None, bool(want), fk])
        del dbg[:-12]
        S.status["lock_dbg"] = dbg
    if want and now < want[1]:
        if mode == 3:
            # KH1 locked (R1). If its own pick isn't the enemy you aimed at (a parked stand-in, a free Heartless far
            # off), hand KH1's lock the right KH1 entity, so KH1 is locked on what Borderlands shows
            wpawn = want[0]
            went = (next((e for e, h in st.get("free_bl", {}).items() if h is getattr(wpawn, "hb", None)), None)
                    if isinstance(wpawn, FreeTarget) else
                    next((a for a, k in st.get("kh_pmap", {}).items() if k == bl_key(wpawn)), None))
            if went and went != ent:
                hnd = kh1_handle(S, went)
                if hnd:
                    kh1_poke(S, KH_LOCK + 8, hnd.to_bytes(4, "little"))
                    S.status["lock_fix"] = [hex(ent) if ent else None, hex(went)]
            st["kh_lock_want"] = None                     # KH1 took it
        return
    if mode == 3 and ent:
        pawn = bl_pawn_for_kh1(S, pc, ent)
        st["kh_lock_match"] = pawn is not None
        if pawn is not None:
            if st.get("lock") is not pawn:
                st["lock"], st["lock_on"] = pawn, True
                S.status["lock"] = "kh1: " + bl_key(pawn)
            st["kh1_lock_lost_t"] = None
        elif now - (st.get("kh1_lock_lost_t") or now) > S.CFG.get("kh_lock_lost_s", 0.6):
            # KH1 is locked on a Heartless that stands for nobody (its enemy died): release it, like KH1 does
            kh1_press(S, PAD_R1, 0.12)
            st["kh1_lock_lost_t"] = None
            st["lock"], st["lock_on"] = None, False
            S.status["lock"] = "released (target gone)"
        else:
            st["kh1_lock_lost_t"] = st.get("kh1_lock_lost_t") or now
    elif st.get("lock_on") and st.get("kh_lock_by_kh1"):
        st["lock"], st["lock_on"] = None, False           # KH1 let go
        S.status["lock"] = "released by KH1"
    st["kh_lock_by_kh1"] = mode == 3
    if mode != 3:
        st["kh_lock_match"] = False


def lock_toggle(S, pc) -> None:
    """Middle mouse: lock onto the enemy nearest the crosshair; again to release (KH1 R1).
    With KH1 running (kh_real_lock): presses KH1's own R1 - the picked enemy's stand-in is put right in front of KH1
    Sora for that moment so KH1 locks it - and kh1_lock_sync follows whatever KH1 locks from then on."""
    if pc is None or pc.Pawn is None:
        return
    if kh_real_lock(S):
        st, now = S.state, time.time()
        mode, _ = kh1_lock(S)
        if mode == 3 or lock_target(S) is not None:
            kh1_press(S, PAD_R1, 0.12)                     # KH1 releases its lock
            st["lock"], st["lock_on"], st["kh_lock_want"] = None, False, None
            st["kh_lock_by_kh1"] = False
            S.status["lock"] = "released"
            return
        cam_yaw = int(pc.Rotation.Yaw) * UU
        S.status["lock_cands"] = [[bl_key(p_)[:14] if not isinstance(p_, FreeTarget) else p_.Name, round(d_),
                                   round(math.degrees(ang(b_ - cam_yaw)))] for d_, p_, b_ in all_targets(S, pc, 6000.0)][:6]
        # vanilla: R1 locks what KH1's target cursor is on (now drawn over that Borderlands enemy); with no cursor,
        # the enemy nearest the crosshair
        pawn = st.get("kh_ret_pawn") if st.get("kh_ret") and S.CFG.get("kh_lock_cursor_first", True) else None
        pawn = pawn if pawn is not None else lock_pick(S, pc)
        if pawn is None:
            S.status["lock"] = "no target in view"
            return
        st["lock"], st["lock_on"], st["lock_t"] = pawn, True, now      # mod side at once (pairs its stand-in)
        st["kh_lock_want"] = (pawn, now + S.CFG.get("kh_lock_press_window", 0.8))
        kh1_press(S, PAD_R1, 0.12, delay=S.CFG.get("kh_lock_press_delay", 0.12))
        S.status["lock"] = "kh1 R1 -> " + bl_key(pawn)
        return
    if lock_target(S) is not None or S.state.get("lock_on"):
        S.state["lock"] = None
        S.state["lock_on"] = False                       # released by the player
        S.status["lock"] = "released"
        return
    cam_yaw = int(pc.Rotation.Yaw) * UU
    S.status["lock_cands"] = [[bl_key(p_)[:14], round(d_), round(math.degrees(ang(b_ - cam_yaw)))]
                              for d_, p_, b_ in all_targets(S, pc, 6000.0)][:6]
    S.status["lock_free_raw"] = [[round(hb["pos"][0]), round(hb["pos"][1]), hb.get("hp"), hb.get("dead_until")]
                                 for hb in S.state.get("free_bl", {}).values()][:4]
    S.state["lock"] = lock_pick(S, pc)
    S.state["lock_on"] = S.state["lock"] is not None     # lock-on engaged: survives the target dying
    S.state["lock_t"] = time.time()
    S.status["lock"] = bl_key(S.state["lock"]) if S.state["lock"] is not None else "no target in view"


def lock_update(S, pc, dt: float) -> None:
    """While locked: camera and Sora track the target; a dead target hands over to the next nearest one."""
    st = S.state
    if st.get("lock_on") and lock_target(S) is None and not kh_real_lock(S):
        # the locked enemy died (whoever noticed first cleared it): lock onto the next closest, like KH1; with nobody
        # left in range, wait lock_grace seconds for one before releasing
        near = all_targets(S, pc, S.CFG.get("lock_range", 3000.0))
        if near:
            st["lock"] = near[0][1]
            st["lock_lost_t"] = None
            S.status["lock"] = "next: " + bl_key(near[0][1])
        else:
            st["lock_lost_t"] = st.get("lock_lost_t") or time.time()
            if time.time() - st["lock_lost_t"] > S.CFG.get("lock_grace", 1.5):
                st["lock_on"] = False
                S.status["lock"] = "released (no enemies left)"
    lk = lock_target(S)
    if lk is None:
        return
    me, t = vec3(pc.Pawn.Location), vec3(lk.Location)
    dx, dy, dz = t[0] - me[0], t[1] - me[1], t[2] - me[2]
    if math.hypot(dx, dy) > S.CFG.get("lock_range", 3000.0) * 1.4:
        st["lock"] = None
        return
    # KH1-style lock camera: it stays behind Sora at its normal height and turns to keep him and the target in frame.
    # The pitch is NOT aimed at the target (a close enemy pulled the view into a bird's-eye look): a fixed, slightly
    # downward base (lock_cam_pitch) plus half of the target's elevation seen from Sora's chest, clamped - tall or
    # flying enemies tilt it up a little, close ones don't tilt it down. Turning eases off when the target is very
    # close, so circling an enemy doesn't whip the camera around.
    CFG = S.CFG
    dist_h = math.hypot(dx, dy)
    want_yaw = math.atan2(dy, dx)
    elev = math.atan2(dz - CFG.get("lock_chest_z", 60.0), max(dist_h, 150.0))
    want_pitch = math.radians(CFG.get("lock_cam_pitch", -12.0)) + elev * CFG.get("lock_elev_follow", 0.5)
    want_pitch = max(math.radians(CFG.get("lock_pitch_min", -22.0)), min(math.radians(CFG.get("lock_pitch_max", 25.0)), want_pitch))
    foc = (kh1_lock_focus(S) if CFG.get("kh_lock_cam_kh1", True) and kh_real_lock(S) and st.get("kh_lock_match")
           and st.get("kh_K") else None)                  # KH1's own lock camera, rebuilt around the real enemy
    if foc is not None:
        f_, side, kpitch = foc
        K = st["kh_K"]
        sb = [me[0], me[1], me[2] + CFG.get("lock_chest_z", 60.0)]
        tb = [t[0], t[1], t[2] + CFG.get("lock_chest_z", 60.0) - 40.0]
        sdir = [side[0] * K[0] + side[1] * K[4] + side[2] * K[8], side[0] * K[1] + side[1] * K[5] + side[2] * K[9],
                side[0] * K[2] + side[1] * K[6] + side[2] * K[10]]                    # KH1 vector -> BL (mirror incl.)
        qb = [sb[i] + f_ * (tb[i] - sb[i]) + sdir[i] for i in range(3)]
        cp_ = S.frame.cam_pos
        want_yaw = math.atan2(qb[1] - cp_[1], qb[0] - cp_[0])
        want_pitch = kpitch + math.radians(CFG.get("kh_lock_cam_pitch_off", 0.0))
        S.status["kh_lock_cam"] = [round(f_, 2), [round(x) for x in sdir], round(math.degrees(kpitch), 1)]
    mir = (kh1_lock_cam(S) if CFG.get("kh_lock_cam_mirror", False) and kh_real_lock(S) and st.get("kh_lock_match")
           and foc is None else None)
    if mir is not None:
        # KH1's real lock camera: the same yaw relative to Sora -> target and the same pitch as KH1's camera has now
        want_yaw = want_yaw + CFG.get("kh_turn_sign", -1.0) * mir[0] + math.radians(CFG.get("kh_lock_cam_yaw_off", 0.0))
        want_pitch = mir[1] + math.radians(CFG.get("kh_lock_cam_pitch_off", 0.0))
        S.status["kh_lock_cam"] = [round(math.degrees(mir[0]), 1), round(math.degrees(mir[1]), 1)]
    rot = pc.Rotation
    yaw, pitch = int(rot.Yaw) * UU, ang(int(rot.Pitch) * UU)
    if dbg_on(S, "dbg_lock_trace"):
        # one line per frame while locked: why the KH1 lock camera swings (lock_cam_trace.txt)
        tr = st.setdefault("lock_trace", [])
        fd = st.get("kh_foc_dbg") if foc is not None else None
        tr.append("%.2f dbl %d wy %.1f y %.1f wp %.1f p %.1f foc %s mir %s cam %s" % (
            time.time() % 1000, round(dist_h), math.degrees(want_yaw), math.degrees(yaw), math.degrees(want_pitch),
            math.degrees(pitch), [round(foc[0], 2), [round(x) for x in sdir], round(math.degrees(foc[2]), 1), fd]
            if foc is not None else None, mir is not None, [round(x) for x in S.frame.cam_pos]))
        del tr[:-3000]
        if time.time() - st.get("lock_trace_t", 0) > 1.0:
            st["lock_trace_t"] = time.time()
            (HERE / "lock_cam_trace.txt").write_text(chr(10).join(tr) + chr(10))
    near_k = max(CFG.get("lock_close_turn", 0.35), min(1.0, dist_h / CFG.get("lock_close_dist", 400.0)))
    k = min(1.0, CFG.get("lock_turn", 6.0) * near_k * dt)
    kp = min(1.0, CFG.get("lock_pitch_turn", 3.0) * dt)
    ny, np_ = yaw + ang(want_yaw - yaw) * k, pitch + (want_pitch - pitch) * kp

    def _rot():
        pc.Rotation = unrealsdk.make_struct("Rotator", Pitch=int(np_ / UU) & 0xFFFF, Yaw=int(ny / UU), Roll=0)
    S.pending.append(_rot)


def kh1_lock_focus(S):
    """Where KH1's lock-on camera is looking, relative to KH1 Sora and his locked target: (f, side, pitch) - the point
    on KH1's camera ray closest to the line Sora -> target lies at Sora + f * (target - Sora) + side (KH1 world vector,
    cm), and the camera pitch (rad). None when KH1 isn't locked."""
    import struct
    mode, ent = kh1_lock(S)
    ks = kh1_state(S)
    if mode != 3 or not ent or not ks:
        return None
    b = kh1_mem(S, 0x2E9D480, 64)
    tp = kh1_abs(S, ent + 0x10, 12)
    if not b or not tp:
        return None
    m = struct.unpack("<16f", b)
    R = [[m[0], m[1], m[2]], [m[4], m[5], m[6]], [m[8], m[9], m[10]]]
    t = [m[12], m[13], m[14]]
    C = [-(t[0] * R[i][0] + t[1] * R[i][1] + t[2] * R[i][2]) for i in range(3)]   # camera position (row-vector view)
    F = [R[0][2], R[1][2], R[2][2]]
    fn = math.sqrt(sum(x * x for x in F)) or 1.0
    F = [x / fn for x in F]
    chest = S.CFG.get("kh_lock_focus_chest", 90.0)                       # KH1 y is down
    Sp = [ks["pos"][0], ks["pos"][1] - chest, ks["pos"][2]]
    tx, ty, tz = struct.unpack("<3f", tp)
    Tp = [tx, ty - chest, tz]
    D = [Tp[i] - Sp[i] for i in range(3)]
    dd = sum(x * x for x in D)
    if dd < 1.0:
        return None
    # closest points between the camera ray C + s F and the segment Sp + f D
    w0 = [C[i] - Sp[i] for i in range(3)]
    a, bb, c = 1.0, sum(F[i] * D[i] for i in range(3)), dd
    d, e = sum(F[i] * w0[i] for i in range(3)), sum(D[i] * w0[i] for i in range(3))
    den = a * c - bb * bb
    if abs(den) < 1e-6:
        return None
    sr = (bb * e - c * d) / den
    f = (a * e - bb * d) / den
    f = max(-0.5, min(1.5, f))
    Q = [C[i] + sr * F[i] for i in range(3)]
    side = [Q[i] - (Sp[i] + f * D[i]) for i in range(3)]
    S.state["kh_foc_dbg"] = [round((a * e - bb * d) / den, 2), round(bb / math.sqrt(dd), 3), round(math.sqrt(dd)),
                             round(sr), [round(x) for x in C], [round(x) for x in Sp], [round(x) for x in Tp]]
    return f, side, -math.asin(max(-1.0, min(1.0, F[1])))


def kh1_lock_cam(S):
    """KH1's lock-on camera, relative: (yaw of KH1's camera minus the bearing KH1 Sora -> his locked target, camera
    pitch), both radians, in the angle convention party_tick uses (atan2(-z, -x)); None when KH1 isn't locked."""
    import struct
    mode, ent = kh1_lock(S)
    ks = kh1_state(S)
    if mode != 3 or not ent or not ks:
        return None
    b = kh1_mem(S, 0x2E9D480, 64)
    tp = kh1_abs(S, ent + 0x10, 12)
    if not b or not tp:
        return None
    m = struct.unpack("<16f", b)
    fx, fy, fz = m[2], m[6], m[10]                      # camera forward in KH1 world (row-vector view: 3rd column)
    tx, ty, tz = struct.unpack("<3f", tp)
    sx, sy, sz = ks["pos"]
    if math.hypot(tx - sx, tz - sz) < 1.0:
        return None
    cam = math.atan2(-fz, -fx)
    bear = math.atan2(-(tz - sz), -(tx - sx))
    n = math.sqrt(fx * fx + fy * fy + fz * fz) or 1.0
    return ang(cam - bear), -math.asin(max(-1.0, min(1.0, fy / n)))


def lock_reproject(S, frame) -> None:
    """Re-sends the HUD with the lock-on reticle placed using exactly the camera the renderer draws Sora with
    (same pos/rot/fov, same maths as sora_render.cpp), so the reticle moves in lockstep with the scene."""
    lk = S.state.get("kh_ret_pawn") if S.state.get("kh_ret") else None
    lk = lk if lk is not None else lock_target(S)
    hf, wh = S.state.get("hud_hf"), S.state.get("canvas_wh")
    if lk is None or hf is None or not wh:
        return
    W, H = wh
    p, y, r = frame.cam_rot[0], frame.cam_rot[1], frame.cam_rot[2]
    SP, CP, SY, CY, SR, CR = math.sin(p), math.cos(p), math.sin(y), math.cos(y), math.sin(r), math.cos(r)
    X = (CP * CY, CP * SY, SP)
    Yv = (SR * SP * CY - CR * SY, SR * SP * SY + CR * CY, -SR * CP)
    Z = (-(CR * SP * CY + SR * SY), CY * SR - CR * SP * SY, CR * CP)
    loc = lk.Location
    d = (float(loc.X) - frame.cam_pos[0], float(loc.Y) - frame.cam_pos[1], float(loc.Z) + S.CFG.get("lock_z", 30.0) - frame.cam_pos[2])
    fwd = sum(a * b for a, b in zip(X, d))
    if fwd < 10:
        return
    xs = 1.0 / math.tan(math.radians(frame.fov) / 2); ys = xs * W / H
    sx = (sum(a * b for a, b in zip(Yv, d)) * xs / fwd + 1) * 0.5 * W
    sy = (1 - sum(a * b for a, b in zip(Z, d)) * ys / fwd) * 0.5 * H
    S.state["lock_screen"] = (sx, sy)
    out = HudFrame.from_buffer_copy(hf)
    kh_reticle(out, S, H)
    S.dll.sr_set_hud(ctypes.byref(out))


def lock_project(S, canvas) -> None:
    """Screen position of the locked target for the KH1 reticle (canvas.Project only works during PostRender)."""
    lk = S.state.get("kh_ret_pawn") if S.state.get("kh_ret") else None
    lk = lk if lk is not None else lock_target(S)
    S.state["lock_screen"] = None
    if lk is None or canvas is None:
        return
    try:
        loc = lk.Location
        v = canvas.Project(unrealsdk.make_struct("Vector", X=float(loc.X), Y=float(loc.Y), Z=float(loc.Z) + 40.0))
        S.state["lock_screen"] = (float(v.X), float(v.Y))
    except Exception as ex:  # noqa: BLE001
        S.status["lock_error"] = str(ex)


def face_target(S, pc) -> None:
    """KH-style auto-lock: turn toward the nearest enemy in range, else the camera direction."""
    lk = lock_target(S)
    if lk is not None:
        me, t = vec3(pc.Pawn.Location), vec3(lk.Location)
        S.state["face_want"] = math.atan2(t[1] - me[1], t[0] - me[0])
        return
    try:
        near = all_targets(S, pc, S.CFG["kb_reach"] * 1.8)
        S.state["face_want"] = near[0][2] if near else int(pc.Rotation.Yaw) * UU
    except Exception as ex:  # noqa: BLE001
        S.status["target_error"] = str(ex)
        S.state["face_want"] = int(pc.Rotation.Yaw) * UU


_dmg_type = {}


def damage_type():
    if "cls" not in _dmg_type:
        cls = None
        for name in ("WillowDamageType", "DamageType"):
            try:
                cls = unrealsdk.find_class(name)
                if cls is not None:
                    break
            except Exception:  # noqa: BLE001
                cls = None
        _dmg_type["cls"] = cls
    return _dmg_type["cls"]


def level(pc) -> int:
    try:
        return int(getattr(pc.PlayerReplicationInfo, "ExpLevel", 1) or 1)
    except Exception:  # noqa: BLE001
        return 1


def dmg_def(pc):
    """The equipped gun's damage-type definition (always valid for this game's damage pipeline)."""
    try:
        w = pc.Pawn.Weapon
        return w.StaticGetWeaponDamageType(w.DefinitionData)[0]
    except Exception:  # noqa: BLE001
        return None


def max_hp_share(p, frac: float) -> float:
    """Damage as a share of the enemy's own max health (flat KH1 numbers one-shot low-level enemies)."""
    return (pool_values(p)[1] or hp(p) or 100.0) * frac


def kh1_live_str(S, who: int = 0) -> int:
    """KH1's LIVE strength (stats page +0x4C: base STR + equipped weapon / accessories; +0x50 = defence) of Sora (0),
    Donald (1) or Goofy (2). Cached 0.5 s. 0 = unknown."""
    import struct
    c = S.state.setdefault("kh_str_cache", {})
    now = time.time()
    if who in c and now - c[who][1] < 0.5:
        return c[who][0]
    v = 0
    try:
        if who == 0:
            ks = kh1_state(S)
            ent = ks["addr"] if ks else 0
        elif who == 3:
            ent = S.state.get("kh_summon_ent", 0)       # the active summon (Simba ...) - its own KH1 strength
        else:
            ent = int.from_bytes(kh1_mem(S, KH_PARTY[who], 8) or b"\x00" * 8, "little")
        raw = ent and kh1_abs(S, ent + 0x6C, 4)
        stp = raw and kh1_resolve(S, struct.unpack("<I", raw)[0])
        sb = stp and kh1_abs(S, stp + 0x4C, 4)
        v = struct.unpack("<i", sb)[0] if sb else 0
        if not 0 < v < 1000:
            v = 0
    except Exception:  # noqa: BLE001
        v = 0
    c[who] = (v, now)
    return v


def bl_enemy_level(S, p, pc=None) -> int:
    cache = S.state.setdefault("bl_lvl", {})
    k = bl_key(p)
    if k not in cache:
        try:
            cache[k] = int(p.GetExpLevel())
        except Exception:  # noqa: BLE001
            cache[k] = level(pc) if pc is not None else 1
    return cache[k]


KB_SPELL_MULT = {"Fire": 1.0, "Blizzard": 0.9, "Thunder": 1.3, "Aero": 0.6, "Gravity": 1.0, "Stop": 0.0}


def kh1_sora_level(S) -> int:
    c = S.state.get("kh_lvl_cache")
    if c and time.time() - c[1] < 2.0:
        return c[0]
    b = kh1_mem(S, 0x2DE9360 + 4, 1)                 # KH1 party block, Sora +4 = level
    v = b[0] if b and 0 < b[0] <= 100 else 0
    S.state["kh_lvl_cache"] = (v, time.time())
    return v


def kb_early_boost(S, pc) -> float:
    """EARLY GAME (user 2026-10-12: Nine-Toes at BL 5 felt slightly weak, late game felt right): Sora's damage x
    (1 + kb_early_boost 0.15) up to BL level kb_early_full 5, fading linearly to x1 at kb_early_end 30."""
    CFG = S.CFG
    try:
        lv = float(level(pc))
    except Exception:  # noqa: BLE001
        return 1.0
    a, b = float(CFG.get("kb_early_full", 5)), float(CFG.get("kb_early_end", 30))
    k = max(0.0, min(1.0, (b - lv) / max(1.0, b - a)))
    return 1.0 + float(CFG.get("kb_early_boost", 0.15)) * k


def spell_damage(S, pc, p, name: str, level: int = 1, share: float = 1.0) -> float:
    """MAGIC slice (2026-10-06, same design as kb_damage): KH1 has no magic stat - spell power grows with Sora's LEVEL
    and the spell tier (-ra / -ga x1.5 / x2). ratio = KH1 level / the enemy's Borderlands level (clamped like the
    Keyblade); damage = enemy max health * kb_magic_frac * KB_SPELL_MULT * tier * share * ratio / enemy tier.
    Gravity = KH1's: a share of the target's CURRENT health (kb_gravity_frac per tier), less on badass / bosses.
    share = this projectile's part of the spell (Blizzard's 3 shards 0.5 each). Logged to hit_trace.txt."""
    CFG = S.CFG
    mx = pool_values(p)[1] or hp(p) or 100.0
    lvl = bl_enemy_level(S, p, pc)
    tier = bar_hp(S, p) / float(CFG.get("kh_bar_hp", {}).get("normal", 150))
    sp_tier = 1.0 + 0.5 * (max(1, level) - 1)
    kl = kh1_sora_level(S)
    ratio = kl / kh1_mapped_level(S, lvl) if kl else 1.0
    ratio = max(CFG.get("kb_ratio_min", 0.25), min(CFG.get("kb_ratio_max", 3.0), ratio))
    if name == "Gravity":
        dmg = max(1.0, hp(p) or mx) * CFG.get("kb_gravity_frac", 0.25) * sp_tier / max(tier, 0.1)
    else:
        dmg = mx * CFG.get("kb_magic_frac", 0.16) * CFG.get("kb_spell_mult", {}).get(name, KB_SPELL_MULT.get(name, 1.0)) \
            * sp_tier * share * ratio / max(tier, 0.1)
    dmg *= kb_early_boost(S, pc)
    if dbg_on(S, "dbg_hit_trace"):
        try:
            with open(HERE / "hit_trace.txt", "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%H:%M:%S')} magic {name} L{level} {bl_key(p)} lvl{lvl} tier{tier:.0f} maxHP {mx:.0f} "
                         f"KH1 lvl {kl} ratio {ratio:.2f} share {share:.2f} -> {dmg:.1f} ({dmg / mx * 100:.1f}% of max)" + chr(10))
        except Exception:  # noqa: BLE001
            pass
    return dmg


KB_START_WEAPON = (0x51, 0x67, 0x77)      # Kingdom Key, Mage's Staff, Knight's Shield: the "expected" weapon per character


def kb_norm_strength(S, who: int, enemy_bl_level: int):
    """STEP 2 (user 2026-10-12, "level + Keyblade only"): (strength, expected) for kb_damage independent of the KH1
    save: KH1 level-up STR (2 per STR level-up in the character's own growth tables, from level 1) at the character's
    loadout level + the equipped weapon's STR, vs the same at the enemy's BL level x 1.6 + the starting weapon.
    Power Ups and accessories don't count. None = KH1 not readable."""
    st = S.state
    base = int.from_bytes(kh1_mem(S, KH_CHAR_TABLE, 8) or bytes(8), "little")
    if not base:
        return None
    key = ("kb_tbl", who)
    tbl = st.get(key)
    if tbl is None:                                   # cumulative level-up STR per level 1..100 (KH1's own tables)
        tbl, idx, acc = [0, 0], 1, 0
        for L in range(1, 100):
            codes, idx = _lo_growth(S, base, who, L, L + 1, idx)
            acc += 2 * sum(1 for c in codes if c == 4)
            tbl.append(acc)
        st[key] = tbl
    blk = kh1_abs(S, base + 0x74 * who, 0x74)
    if not blk:
        return None
    b0 = float(S.CFG.get("kb_norm_base", 2.0))
    wpn = blk[0x36] if who == 0 or S.CFG.get("kh1_loadout_party_wpn", True) else KB_START_WEAPON[who]   # banded too
    own = b0 + tbl[max(1, min(100, blk[4]))] + _lo_wpn_bonus(S, who, wpn)[0]
    el = int(round(kh1_mapped_level(S, enemy_bl_level)))
    expect = b0 + tbl[max(1, min(100, el))] + _lo_wpn_bonus(S, who, KB_START_WEAPON[who])[0]
    return own, max(1.0, expect)


def kb_damage(S, pc, p, mult: float = 1.0, who: int = 0, kind: str = "melee") -> float:
    """KEYBLADE PROGRESSION slice 1 (user-approved design 2026-10-05): Borderlands damage from KH1's own stats, not
    guns. ratio = KH1 live strength (Keyblade included) / the strength a Kingdom-Key Sora of the ENEMY's Borderlands
    level would have (kb_str_base + kb_str_per_lvl * (lvl - 1); Sora's KH1 curve: base 19 at level 31 + Key 3).
    damage = enemy max health * kb_base_frac * ratio * mult / tier (normal 1, badass 3, boss 6 = the lock-on bar
    tiers). Stronger blade / more KH1 levels -> more; higher-level / tougher enemy -> less. Logged to hit_trace.txt."""
    CFG = S.CFG
    mx = pool_values(p)[1] or hp(p) or 100.0
    lvl = bl_enemy_level(S, p, pc)
    norm = kb_norm_strength(S, who, lvl) if who in (0, 1, 2) and CFG.get("kh1_loadout", True)         and CFG.get("kb_norm_str", True) else None
    if norm:
        strength, expect = norm
    else:
        strength = kh1_live_str(S, who)
        expect = CFG.get("kb_str_base", 5.0) + CFG.get("kb_str_per_lvl", 0.55) * max(0.0, kh1_mapped_level(S, lvl) - 1)
    ratio = strength / expect if strength else 1.0
    ratio = max(CFG.get("kb_ratio_min", 0.25), min(CFG.get("kb_ratio_max", 3.0), ratio)) ** CFG.get("kb_ratio_pow", 1.0)
    tier = bar_hp(S, p) / float(CFG.get("kh_bar_hp", {}).get("normal", 150))
    frac = CFG.get("kb_base_frac", 0.16) if who == 0 else CFG.get("kb_summon_frac", 0.16) if who == 3 \
        else CFG.get("kb_party_frac", 0.10)
    dmg = mx * frac * ratio * mult / max(tier, 0.1)
    if who in (0, 3):                                     # Sora + summons (Donald / Goofy unchanged)
        dmg *= kb_early_boost(S, pc)
    if dbg_on(S, "dbg_hit_trace"):
        try:
            with open(HERE / "hit_trace.txt", "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%H:%M:%S')} {kind} who{who} {bl_key(p)} lvl{lvl} tier{tier:.0f} maxHP {mx:.0f} "
                         f"STR {strength} expect {expect:.1f} ratio {ratio:.2f} mult {mult:.2f} -> {dmg:.1f} "
                         f"({dmg / mx * 100:.1f}% of max)" + chr(10))
        except Exception:  # noqa: BLE001
            pass
    return dmg


# BORDERLANDS ELEMENTS for KH1 spells (user 2026-10-06): the element lives in BL1's damage-type DEFINITION (flesh /
# armor / shield multipliers + coloured numbers; class = generic WillowDamageType); the status effect (burning, shock,
# slow) is applied separately through the enemy's StatusEffectComp, rolled like a BL elemental gun.
BL_ELEM_DEF = {"fire": "gd_Incendiary.DamageType.DmgType_Incendiary_Impact",
               "shock": "gd_Shock.DamageType.DmgType_Shock_Impact",
               "slow": "gd_Slow.DamageType.DmgType_Slow_Impact",
               "corrosive": "gd_Corrosive.DamageType.DmgType_Corrosive_Impact",
               "explosive": "gd_Explosive.DamageType.DmgType_Explosive"}
BL_ELEM_STATUS = {"fire": "gd_Incendiary.StatusEffect.Incendiary_Status",
                  "shock": "gd_Shock.StatusEffect.Shock_Status",
                  "slow": "gd_Slow.StatusEffect.Slow_Status"}
# user 2026-10-06: Fire burns, Thunder shocks, Blizzard slows; Gravity = corrosive DAMAGE TYPE only (no lingering corrosion
# status: "just corrosive damage so things weak to corrosive take more"); Aero = no element (it's KH1's air shield)
KB_SPELL_ELEM = {"Fire": "fire", "Thunder": "shock", "Blizzard": "slow", "Gravity": "corrosive"}


def _bl_obj(kind: str, path: str):
    c = _dmg_type.setdefault("objs", {})
    if path not in c:
        try:
            c[path] = unrealsdk.find_object(kind, path)
        except Exception:  # noqa: BLE001
            c[path] = None
    return c[path]


def hurt(S, pc, p, dmg: float, push=8000.0, lift=0.3, lvl_scale=True, elem=None, elem_chance=0.0, spread=0) -> bool:
    """Queues the damage for the game tick (see on_player_tick). Method chosen by CFG["dmg_mode"]: off/sethp/takedamage.
    lvl_scale False = no Borderlands player-level multiplier (kb_damage scales by the enemy's level itself).
    elem = "fire" / "shock" / "slow": BL1 elemental damage type + a status-effect roll (elem_chance)."""
    if S.CFG.get("dmg_mode", "takedamage") == "off":
        return False
    S.pending.append(lambda: _hurt_now(S, pc, p, dmg, push, lift, lvl_scale, elem, elem_chance, spread))
    return True


def bl_alive(p) -> bool:
    """Still a live, damageable pawn (dead or deleted pawns crash the game when damaged: General protection fault)."""
    try:
        if p is None or bool(getattr(p, "bDeleteMe", False)) or bool(getattr(p, "bTearOff", False)):
            return False
        return hp(p) > 0
    except Exception:  # noqa: BLE001
        return False


def _hurt_now(S, pc, p, dmg: float, push=8000.0, lift=0.3, lvl_scale=True, elem=None, elem_chance=0.0, spread=0) -> bool:
    if isinstance(p, FreeTarget):                         # its damage comes from KH1 hitting its KH1 body
        return False
    if not bl_alive(p) or pc.Pawn is None:
        S.status["hurt_skipped"] = S.status.get("hurt_skipped", 0) + 1
        return False
    if S.CFG.get("dmg_mode") == "sethp":
        before = hp(p)
        p.HealthPool.Data.SetCurrentValue(max(0.0, before - float(dmg)))
        S.status["dmg_result"] = [before, hp(p)]
        return True
    """BL1 TakeDamage(Damage, InstigatedBy, HitLocation, Momentum, DamageType, DamageTypeDefinition, ImpactDefinition,
    HitInfo, DamageCauser) - NOT the stock UE3 order (passing UE3 order crashed the game)."""
    if not S.CFG.get("magic_dmg", True):
        return False
    me = vec3(pc.Pawn.Location)
    there = vec3(p.Location)
    dx, dy = there[0] - me[0], there[1] - me[1]
    n = math.hypot(dx, dy) or 1.0
    mom = unrealsdk.make_struct("Vector", X=dx / n * push, Y=dy / n * push, Z=push * lift)
    try:
        cls = _dmg_type.get("melee")
        if cls is None:
            cls = _dmg_type["melee"] = unrealsdk.find_class("WillowDmgType_Melee")
        dtd = dmg_def(pc)
        edef = _bl_obj("WillowDamageTypeDefinition", BL_ELEM_DEF[elem]) if elem in BL_ELEM_DEF and S.CFG.get("bl_elements", True) else None
        if edef is not None:
            dtd = edef
            cls = _dmg_type.get("generic")
            if cls is None:
                cls = _dmg_type["generic"] = unrealsdk.find_class("WillowDamageType")
        p.TakeDamage(Damage=float(dmg * ((1.0 + 0.15 * (level(pc) - 1)) if lvl_scale else 1.0)), InstigatedBy=pc, HitLocation=p.Location,
                     Momentum=mom, DamageType=cls, DamageTypeDefinition=dtd, ImpactDefinition=None,
                     HitInfo=unrealsdk.make_struct("TraceHitInfo"), DamageCauser=pc.Pawn)
        if edef is not None and elem in BL_ELEM_STATUS and elem_chance > 0 and random.random() < elem_chance and bl_alive(p):
            sed = _bl_obj("StatusEffectDefinition", BL_ELEM_STATUS[elem])
            comp = getattr(p, "StatusEffectComp", None)
            if sed is not None and comp is not None:
                try:
                    comp.ApplyStatusEffect(pc, cls, sed, pc.Pawn, None, False)
                    S.status["elem_status"] = [elem, "apply", S.status.get("elem_status", [0, 0, 0])[2] + 1]
                    # -ra / -ga (user 2026-10-06): the status jumps to the nearest other enemies (a normal application each)
                    if spread > 0:
                        n_sp = 0
                        for _, q, _ in enemies(S, pc, S.CFG.get("kb_spread_radius", 450.0), origin=vec3(p.Location)):
                            if q is p or n_sp >= spread or not bl_alive(q) or getattr(q, "StatusEffectComp", None) is None:
                                continue
                            # (bIsFromSpreading MUST stay False: True without a SpreadSource crashed BL1 - GPF 2026-10-06)
                            q.StatusEffectComp.ApplyStatusEffect(pc, cls, sed, pc.Pawn, None, False)
                            n_sp += 1
                        S.status["elem_spread"] = [elem, n_sp]
                except Exception as ex:  # noqa: BLE001
                    try:
                        comp.CheatApplyStatusEffect(pc, sed)
                        S.status["elem_status"] = [elem, "cheat", f"{type(ex).__name__}: {ex}"[:80]]
                    except Exception as ex2:  # noqa: BLE001
                        S.status["elem_status"] = [elem, "ERR", f"{type(ex2).__name__}: {ex2}"[:80]]
        return True
    except Exception as ex:  # noqa: BLE001
        S.status["hit_error"] = f"{type(ex).__name__}: {ex}"
        return False


def do_hit(S, pc) -> None:
    """Keyblade hit frame: let the game's own melee attack find the target and deal damage (safe native path)."""
    CFG = S.CFG
    # pc.MeleeAttack() from a hook crashed the game (GPF) - not used.
    yaw = S.state["yaw"]
    hits = 0
    a = S.state.get("action") or {}
    if a.get("kh") and blade_on(S):
        return                # the real Keyblade decides (kh1_blade_tick)
    proxied = S.state.get("kh_proxied", set()) if a.get("kh") and kh1_combat_on(S) else set()
    for dist, p, bearing in enemies(S, pc, CFG["kb_reach"]):
        if abs(ang(bearing - yaw)) > math.radians(75) and dist > 90:
            continue          # only what is in front of the swing
        if bl_key(p) in proxied:
            continue          # its KH1 stand-in takes the real KH1 hit (kh1_proxy_tick turns that into damage)
        if hurt(S, pc, p, CFG["kb_damage"], push=30000.0):
            hits += 1
        S.state["mp"] = min(MP_MAX, S.state.get("mp", MP_MAX) + 4.0)   # KH: Keyblade hits restore MP
        if kh1_combat_on(S):
            kh1_add_mp(S, CFG.get("kh_mp_per_hit", 0.34))
    S.status["last_hits"] = hits


def update_action(S, pc, now: float) -> bool:
    """Plays the current attack/cast; returns True while an action owns the animation."""
    st = S.state
    a = st.get("action")
    if not a:
        return False
    frame = (now - a["t0"]) * a["fps"]
    e = a["ev"]
    apply_root_motion(S, pc, a, frame)
    if a["kind"] == "attack":
        hit = e.get("hit") or [e["frames"] * 0.35, e["frames"] * 0.5]
        if not a["hit_done"] and frame >= hit[0]:
            a["hit_done"] = True
            do_hit(S, pc)
        win = e.get("combo")
        if st.get("queued") and win and frame >= win[0]:     # chain at the start of KH1's combo window
            st.pop("queued", None)
            attack_next(S, pc, a)
            return True
    rel = e.get("release")
    if not rel or rel >= e["frames"]:
        rel = e["frames"] * 0.6
    if a["kind"] == "cast" and not a.get("released") and frame >= rel:
        a["released"] = True
        launch_spell(S, pc, a["spell"], a.get("level", 1))
    if frame >= e["frames"]:
        if a["kind"] == "attack" and st.pop("queued", False):
            attack_next(S, pc, a)
            return True
        st["action"] = None
        st["recover_until"] = now + 1.2
        return False
    return True


def apply_root_motion(S, pc, a, frame: float) -> None:
    """Moves the real character by the attack's root travel (stripped from the baked mesh), KH1-style lunge."""
    root = a["ev"].get("root")
    if not root or not S.CFG.get("root_motion", True) or (a.get("kh") and not S.CFG.get("kh_bl_lunge", False)):   # KH1 swings: KH1's own lunge
        return
    k = min(len(root) - 1, max(0, frame / 2.0))          # baked at 30 fps, motion frames are 60 fps
    i = int(k); f = k - i
    j = min(i + 1, len(root) - 1)
    cur = [root[i][0] + (root[j][0] - root[i][0]) * f, root[i][1] + (root[j][1] - root[i][1]) * f]
    last = a.get("root_last", [0.0, 0.0])
    a["root_last"] = cur
    dx, dz = cur[0] - last[0], cur[1] - last[1]          # KH cm: x side, z forward
    if abs(dx) + abs(dz) < 1e-3:
        return
    sc = S.frame.scale
    mir = -1.0 if (S.CFG["flags"] & 1) else 1.0
    fwd, side = dz * sc, dx * sc * mir
    yaw = S.state["yaw"] + math.radians(S.CFG["yaw_offset_deg"])
    # stop short of the target instead of running through it
    try:
        near = enemies(S, pc, 400)
        if near and near[0][0] < S.CFG.get("lunge_stop", 110) and abs(ang(near[0][2] - S.state["yaw"])) < 1.0:
            return
    except Exception:  # noqa: BLE001
        pass
    wx = math.cos(yaw) * fwd - math.sin(yaw) * side
    wy = math.sin(yaw) * fwd + math.cos(yaw) * side
    pawn = pc.Pawn

    def _move():
        loc = pawn.Location
        pawn.Location = unrealsdk.make_struct("Vector", X=float(loc.X) + wx, Y=float(loc.Y) + wy, Z=float(loc.Z))
    S.pending.append(_move)


def attack_next(S, pc, a) -> None:
    chain = a.get("chain") or S.CFG["combo"]
    S.state["combo_i"] = (S.state.get("combo_i", 0) + 1) % len(chain)
    if S.state["combo_i"] == 0:
        S.state["action"] = None
        return
    face_target(S, pc)
    start_action(S, "attack", chain[S.state["combo_i"]], S.CFG["atk_speed"], chain=chain)



# ---------------------------------------------------------------- KH1 abilities (HD 1.5: reaction commands on Triangle)
# KH1 HD 1.5 turned Sonic Blade, Strike Raid, Ars Arcanum, Ragnarok... into reaction commands (Triangle, near an enemy;
# Triangle again continues the chain, e.g. Sonic -> Rave x5 -> Blast). KH1's per-ability "active" flags (KH1-LUA-LIBRARY
# enable_ability: dialog 0x299BB08 + 0x738, bit 1 = Sonic Blade ... bit 5 = Trinity Limit) decide which one Triangle
# does: only the chosen one is switched on, Triangle is pressed while it runs, then the original flags come back.
# The real-blade hit detection turns whatever KH1 performs into Borderlands damage.
KH_ABILITY_FLAGS = 0x299BB08 + 0x738
KH_ABILITIES = {"Sonic Blade": 0x01, "Ars Arcanum": 0x02, "Strike Raid": 0x04, "Ragnarok": 0x08, "Trinity Limit": 0x10}


def ability_list(S):
    return [a for a in S.CFG.get("kh_abilities", ["Sonic Blade", "Strike Raid", "Ars Arcanum", "Ragnarok"]) if a in KH_ABILITIES]


def kh1_ability(S, pc, name: str) -> None:
    st, now = S.state, time.time()
    if not kh1_combat_on(S):
        say(S, "Abilities need KH1")
        return
    if now < st.get("kh_abil_until", 0) - S.CFG.get("kh_ability_time", 3.5) + 0.6:
        return                                            # just started one
    tgt = lock_target(S) or aim_target(S, pc, reach=S.CFG.get("kh_ability_range", 1500.0), cone=70.0)
    if tgt is None:
        say(S, "No target")                               # KH1 abilities are reaction commands: they need an enemy
        return
    if "kh_abil_orig" not in st:
        b = kh1_mem(S, KH_ABILITY_FLAGS, 1)
        st["kh_abil_orig"] = b[0] if b else 0
    st["kh_abil"] = (name, KH_ABILITIES[name])
    st["kh_abil_until"] = now + S.CFG.get("kh_ability_time", 3.5)
    st["kh_abil_next"] = now + 0.05
    st["kh_abil_t0"], st["kh_abil_started"] = now, False
    face_target(S, pc)
    S.status["last_ability"] = [name, round(now % 1000, 2)]


def kh1_ability_tick(S, now: float) -> None:
    st = S.state
    ab = st.get("kh_abil")
    if not ab:
        return
    if now >= st.get("kh_abil_until", 0):
        if "kh_abil_orig" in st:                          # give KH1 its own ability flags back
            kh1_poke(S, KH_ABILITY_FLAGS, bytes([st.pop("kh_abil_orig")]))
        st["kh_abil"] = None
        return
    ks_ = kh1_state(S)
    if ks_ and ks_["state"] not in (2, KH_STATE_JUMP, 130):   # jumping / landing is not the ability starting
        if not st.get("kh_abil_started"):
            voice(S, ABILITY_VOICE.get(ab[0], "vbig"), cd=0.3)
        st["kh_abil_started"] = True
    if not st.get("kh_abil_started") and now - st.get("kh_abil_t0", now) > S.CFG.get("kh_ability_start_timeout", 2.0):
        st["kh_abil_until"] = now                          # KH1 never took it: stop cleanly (no effects, flags back)
        S.status["last_ability"] = [ab[0], "not started"]
    orig = st.get("kh_abil_orig", 0)
    kh1_poke(S, KH_ABILITY_FLAGS, bytes([(orig & ~0x1F) | ab[1]]))   # only this one: Triangle does exactly it
    try:
        ks = kh1_state(S) if dbg_on(S, "dbg_ability_trace") else None
        if ks:
            tr = st.setdefault("abil_trace", [])
            tr.append(f"{now % 1000:8.3f} {ab[0]:12s} state {ks['state']:3d} anim {ks['anim']:3d} pos "
                      f"{[round(v) for v in ks['pos']]} vel {[round(v * 60) for v in ks['vel']]} live {S.status.get('kh1_live')} "
                      f"tri {int(bool(st.get('kh_press', {}).get(PAD_TRIANGLE)))}")
            if len(tr) % 20 == 0:
                del tr[:-2000]
                (HERE / "ability_trace.txt").write_text("\n".join(tr) + "\n")
    except Exception as ex:  # noqa: BLE001
        S.status["abil_trace_error"] = str(ex)
    if now >= st.get("kh_abil_next", 0):
        st["kh_abil_next"] = now + (S.CFG.get("kh_ability_repress", 0.3) if st.get("kh_abil_started") else 0.15)
        kh1_press(S, PAD_TRIANGLE, 0.08)                   # start, then every follow-up prompt (Rave, Blast...)


# ---------------------------------------------------------------- Keyblade trail + ability effects (renderer v16)
TRAIL_LOOK = {   # (r, g, b, a, trail seconds): KH1's pale swing streak, ability colours on top of KH1's trail texture
    None: (0.75, 0.9, 1.0, 0.85, 0.14),
    "Sonic Blade": (0.45, 0.75, 1.0, 1.0, 0.24),
    "Ars Arcanum": (1.0, 0.45, 0.25, 1.0, 0.2),
    "Strike Raid": (1.0, 0.85, 0.35, 1.0, 0.2),
    "Ragnarok": (1.0, 0.45, 0.85, 1.0, 0.22),
}


def trail_tick(S, pc, now: float) -> None:
    st, dll = S.state, S.dll
    if not hasattr(dll, "sr_trail") or not S.CFG.get("trail", not khfx_live(S)) or not kh1_combat_on(S):   # KH1's own trail shows (v35+)
        return
    ks = kh1_state(S)
    if not ks:
        return
    ab = st.get("kh_abil")
    name = ab[0] if ab and st.get("kh_abil_started") else None
    if not S.CFG.get("fx_invented", False):          # vanilla audit: ability auras/bursts/tints were made up
        name = None
    if kh1_attacking_now(S, ks):
        r, g, b, a, secs = TRAIL_LOOK.get(name, TRAIL_LOOK[None])
        dll.sr_trail(ctypes.c_float(r), ctypes.c_float(g), ctypes.c_float(b), ctypes.c_float(a), ctypes.c_float(secs), ctypes.c_float(0.12))
    if not name:
        st["abil_fx_anim"] = None
        return
    f = S.frame
    chest = [f.pos[0], f.pos[1], f.pos[2] + 90 * f.scale]
    moving = math.hypot(ks["vel"][0], ks["vel"][2]) * 60 > 500
    key = (ks["state"], ks["anim"], moving)
    if key != st.get("abil_fx_anim"):                  # a new step of the ability
        st["abil_fx_anim"] = key
        if name == "Sonic Blade" and moving:            # each dash launch: a flash and a starburst
            fx_flash(S, chest, (0.55, 0.8, 1.0), 220, 0.15)
            _part(S, pos=list(chest), size=200, color=(0.7, 0.9, 1.0), life=0.18, spr="ray8", rot=random.uniform(0, 1))
        elif name == "Ragnarok" and now > st.get("kh_abil_until", 0) - 1.0:
            fx_ragnarok_burst(S, pc, chest)
    if now - st.get("abil_fx_t", 0) > 0.05:             # running sparkle/aura while it lasts
        st["abil_fx_t"] = now
        if name == "Ars Arcanum":
            _part(S, pos=list(chest), size=220, color=(1.0, 0.35, 0.2), life=0.2, spr="bigglow", alpha=0.35)
        elif name == "Ragnarok":
            _part(S, pos=[c + random.uniform(-40, 40) for c in chest], vel=[0, 0, 120], size=24, color=(1.0, 0.55, 0.9),
                  life=0.5, spr="cross", rot=random.uniform(0, 1.6))
        elif name == "Strike Raid":
            _part(S, pos=[c + random.uniform(-30, 30) for c in chest], size=18, color=(1.0, 0.9, 0.5), life=0.3, spr="star",
                  rot=random.uniform(0, 6.3), spin=4.0)


def fx_ragnarok_burst(S, pc, chest):
    """Ragnarok's finale: a burst of pink lights flying out at the target (or ahead)."""
    tgt = lock_target(S) or aim_target(S, pc)
    t = vec3(tgt.Location) if tgt is not None else [chest[0] + math.cos(S.state["yaw"]) * 600, chest[1] + math.sin(S.state["yaw"]) * 600, chest[2]]
    fx_flash(S, chest, (1.0, 0.5, 0.9), 260, 0.2)
    for k in range(10):
        d = [t[i] - chest[i] + random.uniform(-80, 80) for i in range(3)]
        n = math.sqrt(sum(x * x for x in d)) or 1.0
        _part(S, pos=list(chest), vel=[x / n * random.uniform(900, 1300) for x in d], size=46, color=(1.0, 0.5, 0.9),
              life=0.55, spr="orb", t0=time.time() + k * 0.04, spin=6.0)


# ---------------------------------------------------------------- KH1's own effects in Borderlands (renderer v35 + kh1_fx.dll)
# kh1_fx.dll (in KH1) captures every effect sprite KH1 draws; the renderer draws them here. This sends the KH1 world ->
# Borderlands affine map (row vectors; the mapping party_tick uses for Donald and Goofy) and the filters.
KHFX_DLL = "kh1_fx.dll"


def invented_ok(S) -> bool:
    """Our own (made-up) hit sparks / flashes: only while KH1's real effects aren't reaching Borderlands."""
    return bool(S.CFG.get("fx_invented", False)) or not khfx_live(S)


def khfx_live(S) -> bool:
    """True while KH1's real effects reach Borderlands (then our made-up spell visuals stay off)."""
    return bool(S.CFG.get("kh1_fx", True)) and bool(S.state.get("khfx_ok"))


def kh1_has_module(pid: int, name: str):
    """True/False whether the KH1 process has a module of that file name loaded; None if it can't be read."""
    import ctypes.wintypes as W
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    k32.OpenProcess.restype = W.HANDLE
    hp = k32.OpenProcess(0x0410, False, pid)                         # QUERY_INFORMATION | VM_READ
    if not hp:
        return None
    try:
        mods = (ctypes.c_void_p * 1024)()
        need = W.DWORD()
        psapi.EnumProcessModulesEx.argtypes = [W.HANDLE, ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD), W.DWORD]
        if not psapi.EnumProcessModulesEx(hp, mods, ctypes.sizeof(mods), ctypes.byref(need), 3):
            return None
        psapi.GetModuleBaseNameW.argtypes = [W.HANDLE, ctypes.c_void_p, ctypes.c_wchar_p, W.DWORD]
        buf = ctypes.create_unicode_buffer(260)
        for i in range(min(1024, need.value // ctypes.sizeof(ctypes.c_void_p))):
            if mods[i] and psapi.GetModuleBaseNameW(hp, mods[i], buf, 260) and (
                    buf.value.lower().startswith(name.lower()[:-1]) if name.endswith("*") else buf.value.lower() == name.lower()):
                return True                                          # ("name*" = any file name starting with it)
        return False
    finally:
        k32.CloseHandle(hp)


def _kh1_load(S, pid: int, dll: str) -> bool:
    """Loads a DLL into KH1 through the renderer (sr_kh1_load, renderer v81+). Never in Python: Windows Defender flags
    the remote-thread LoadLibrary pattern in Python code (Exploit:Python/Leivion, MODLOG 2026-10-12)."""
    fn = getattr(S.dll, "sr_kh1_load", None) if getattr(S, "dll", None) is not None else None
    if fn is None:
        S.status["kh1_load"] = "renderer < v81 (no sr_kh1_load): restart Borderlands"
        return False
    fn.argtypes = [ctypes.c_int, ctypes.c_wchar_p]
    return bool(fn(int(pid), dll))


def khfx_inject(S) -> None:
    """Loads data/kh1_fx.dll into KH1 once (via the renderer, _kh1_load), unless it's already there."""
    st = S.state
    kh = getattr(S, "kh", None) or {}
    pid = kh.get("pid")
    # per KH1 process: the shared memory outlives a KH1 crash/restart (Borderlands keeps it open), so it can't tell
    # whether THIS KH1 has the DLL - ask the process for its module list instead
    if not pid or st.get("khfx_injected") == pid or time.time() < st.get("khfx_inject_t", 0):
        return
    st["khfx_inject_t"] = time.time() + 10.0
    dll = str((HERE / "data" / KHFX_DLL).resolve())
    if not (HERE / "data" / KHFX_DLL).exists():
        return
    # the input / ground bridge first (2026-10-05: after a KH1 crash the renderer's once-per-session bridge injection
    # never came back -> Sora couldn't move; and kh1_fx must load AFTER the bridge so its gather hook chains on it)
    bpath = HERE / "data" / "kh1_bridge.dll"
    hb = kh1_has_module(pid, "kh1_bridge.dll")
    if hb is False and bpath.exists():
        if _kh1_load(S, pid, str(bpath.resolve())):
            kh1_event(S, f"kh1_bridge.dll injected into KH1 (pid {pid}); kh1_fx follows")
            # a NEW KH1 process (restart / crash): the once-per-session home warp must run again (2026-10-06: KH1
            # restarted under the same BL session stayed in the save's room - Wonderland - instead of 2nd District)
            S.state["kh_home_done"] = False
            S.state["kh_anchor_room"] = None
        return
    have = kh1_has_module(pid, KHFX_DLL)
    if have is None:
        return
    if have:
        st["khfx_injected"] = pid
        return
    if not _kh1_load(S, pid, dll):
        S.status["khfx_inject"] = "load failed"
        return
    st["khfx_injected"] = pid
    S.khfx_sh = None                                                 # re-open the shared views
    kh1_event(S, f"kh1_fx.dll injected into KH1 (pid {pid})")


def khfx_shared(S):
    r"""kh1_fx.dll's shared block (Local\SoraFx) as a writable ctypes buffer, or None."""
    import ctypes.wintypes as W
    sh = getattr(S, "khfx_sh", None)
    if sh is not None:
        return sh
    if time.time() < S.state.get("khfx_sh_retry", 0):
        return None
    S.state["khfx_sh_retry"] = time.time() + 3.0
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenFileMappingW.restype = W.HANDLE
    k32.MapViewOfFile.restype = ctypes.c_void_p
    k32.MapViewOfFile.argtypes = [W.HANDLE, W.DWORD, W.DWORD, W.DWORD, ctypes.c_size_t]
    h = k32.OpenFileMappingW(0xF001F, False, r"Local\SoraFx")
    if not h:
        return None
    p = k32.MapViewOfFile(h, 0xF001F, 0, 0, 256)
    if not p:
        return None
    S.khfx_sh = (ctypes.c_char * 256).from_address(p)
    return S.khfx_sh


KH_SPELL_REACH = {0: 300.0, 1: 650.0, 2: 600.0, 4: 500.0, 5: 500.0}   # KH1 cm a spell travels (Fira ~3.7 m, Blizzara ~8 m measured)


def kh1_spell_target(S, pc):
    """KH1 entity for the spell's target: the stand-in Heartless of the Borderlands enemy you're locked on to (or
    aiming at), or the free Heartless itself. KH1's own lock-on isn't engaged (ours is the mod's), so the command names
    the target explicitly - KH1's spell then homes on it like in vanilla."""
    st = S.state
    tgt = lock_target(S) or aim_target(S, pc, reach=S.CFG.get("kh_spell_reach", 2500.0), cone=S.CFG.get("kh_spell_cone", 35.0))
    if tgt is None:
        return None, None
    if isinstance(tgt, FreeTarget):
        return tgt, next((e for e, h in st.get("free_bl", {}).items() if h is tgt.hb), None)
    key = bl_key(tgt)
    return tgt, next((a for a, k in st.get("kh_pmap", {}).items() if k == key), None)


def khfx_command(S, word: int, target: int, ent: int = 0) -> bool:
    """Hands KH1 Sora a command (kh1_fx v4: written into KH1's command word right before KH1 runs it), the way KH1's
    own menu does. target: 0 none, 1 Sora, 2 KH1's lock-on target. False when the DLL can't do it."""
    import struct
    sh = khfx_shared(S)
    if sh is None or not S.CFG.get("kh_cast_command", True):
        return False
    ver, = struct.unpack_from("<I", sh, 4)
    hooked, = struct.unpack_from("<I", sh, 20)
    if ver < 4 or not hooked & 2:
        return False
    req, = struct.unpack_from("<I", sh, 192)
    if target == 3:
        if ver < 7 or not ent:
            target = 2
        else:
            struct.pack_into("<Q", sh, 232, ent)
    struct.pack_into("<II", sh, 200, word, target)
    struct.pack_into("<I", sh, 192, (req + 1) & 0xFFFFFFFF)
    return True


def khfx_stretch(S, cfg, K, sc) -> None:
    """Spell stretch (renderer v42): KH1's spell flies to the target's stand-in (within KH1 reach); in Borderlands the
    part of the effect past Sora's hand is stretched so the stand-in's spot lands on the real enemy."""
    import struct
    st, CFG = S.state, S.CFG
    sp_ = st.get("kh_spell")
    if not sp_ or time.time() > sp_["until"] or not sp_.get("ent") or sp_.get("free") or not CFG.get("kh_spell_stretch", True):
        return
    try:
        if sp_["pawn"].bDeleteMe:
            return
        t = vec3(sp_["pawn"].Location)
    except Exception:  # noqa: BLE001
        return
    raw = kh1_abs(S, sp_["ent"], 0x1C)
    if not raw:
        return
    kx, ky, kz = struct.unpack_from("<3f", raw, 0x10)
    m = [kx * K[0] + ky * K[4] + kz * K[8] + K[12], kx * K[1] + ky * K[5] + kz * K[9] + K[13], kx * K[2] + ky * K[6] + kz * K[10] + K[14]]
    f = S.frame
    m[2] += CFG.get("kh_stretch_chest", 60.0) * sc        # both points at chest height
    cfg[28], cfg[29] = 1.0, CFG.get("kh_stretch_a0", 70.0)
    cfg[30], cfg[31], cfg[32] = f.pos[0], f.pos[1], f.pos[2]
    cfg[33], cfg[34], cfg[35] = m[0] - f.pos[0], m[1] - f.pos[1], m[2] - f.pos[2]
    cfg[36], cfg[37], cfg[38] = t[0] - m[0], t[1] - m[1], t[2] - m[2]
    S.status["kh_stretch"] = [round(math.hypot(cfg[33], cfg[34])), round(math.hypot(t[0] - f.pos[0], t[1] - f.pos[1]))]


def kh1_cam_yaw(S):
    """KH1's real camera heading (deg) from its view matrix (0x2E9D480, row vectors: forward = 3rd column)."""
    import struct
    b = kh1_mem(S, 0x2E9D480, 64)
    if not b:
        return None
    m = struct.unpack("<16f", b)
    return math.degrees(math.atan2(m[2], m[10]))


def cam_log_tick(S, now: float) -> None:
    """Logs (movement camera estimate, KH1's real camera yaw) pairs while steering, to fit how they relate (the real
    KH1 lock-on swings KH1's camera; movement should follow the real camera instead of a slow estimate)."""
    st = S.state
    if not dbg_on(S, "dbg_cam_log"):
        return
    if now - st.get("cam_log_t", 0) < 0.5 or st.get("kh_cam_c") is None or st.get("kh_stick_mag", 0) < 0.3:
        return
    st["cam_log_t"] = now
    cy = kh1_cam_yaw(S)
    if cy is None:
        return
    log = st.setdefault("cam_log", [])
    log.append(f"{now % 10000:.2f} c {st['kh_cam_c']:.1f} cam {cy:.1f} lock {kh1_lock(S)[0]}")
    del log[:-2000]
    if len(log) % 10 == 0:
        (HERE / "cam_log.txt").write_text("\n".join(log[-2000:]) + "\n")


def khfx_tick(S, pc, now: float) -> None:
    st, CFG, f, dll = S.state, S.CFG, S.frame, S.dll
    if not hasattr(dll, "sr_khfx"):
        st["khfx_ok"] = False
        return
    cfg = (ctypes.c_float * 96)()
    ks = kh1_state(S) if CFG.get("kh1_fx", True) and kh1_combat_on(S) else None
    if ks:
        khfx_inject(S)
        mapfeed_inject(S)
        sign = CFG.get("kh_turn_sign", -1.0)
        fx, fz = ks["fwd"]
        yb = (st.get("kh_yaw_target") if st.get("kh_yaw_target") is not None else st["yaw"]) + math.radians(CFG["yaw_offset_deg"])
        sc, c, s = f.scale, math.cos(yb), math.sin(yb)
        r0 = (sc * (c * fx + s * sign * fz), sc * (s * fx - c * sign * fz))
        r2 = (sc * (c * fz - s * sign * fx), sc * (s * fz + c * sign * fx))
        sx, sy, sz = ks["pos"]
        if st.get("kh_ground_active") and st.get("kh_floor_ref") is not None:
            y0, z0 = st["kh_floor_ref"], st.get("party_ground_feet", f.pos[2])
        else:
            y0, z0 = sy, f.pos[2]
        K = [r0[0], r0[1], 0.0, 0.0, 0.0, 0.0, -sc, 0.0, r2[0], r2[1], 0.0, 0.0,
             f.pos[0] - (sx * r0[0] + sz * r2[0]), f.pos[1] - (sx * r0[1] + sz * r2[1]), z0 + sc * y0, 1.0]
        # (default OFF since 2026-10-07: Tinker Bell is drawn as an EFFECT mesh right next to Sora; on the smoothed,
        # deliberately loose mapping she "teleported around" when walking - the Sora-anchored K kept her perfect)
        if mapfeed_active(S) and st.get("mf_xf") is not None and CFG.get("kh1_mapfeed_fx", False):
            # MAP FEED: KH1's effects through the same mapping as the ground / party / prizes (they hit that ground)
            Fx_, Fy_, Kx_, Kz_, m00_, m01_, m10_, m11_, rb_, rk_, sc_ = st["mf_xf"]
            K = [m00_, m10_, 0.0, 0.0, 0.0, 0.0, -sc_, 0.0, m01_, m11_, 0.0, 0.0,
                 Fx_ - (Kx_ * m00_ + Kz_ * m01_), Fy_ - (Kx_ * m10_ + Kz_ * m11_), rb_ + sc_ * rk_, 1.0]
        for i, v in enumerate(K):
            cfg[i] = v
        st["kh_K"] = K
        cfg[16] = 1.0
        cfg[17] = CFG.get("kh1_fx_radius", 1600.0)   # parked stand-ins sit ~2100 away: their sink / emerge dust stays hidden (user)
        cfg[18] = CFG.get("kh1_fx_ambient_s", 10.0)
        cfg[19] = CFG.get("kh1_fx_bright", 1.0)
        cfg[20] = CFG.get("kh1_fx_alpha", 1.0)
        cfg[21], cfg[22], cfg[23] = sx, sy, sz
        cfg[24] = sc
        cfg[25] = CFG.get("kh1_fx_max_age_ms", 70.0)
        cfg[26] = CFG.get("kh1_fx_gap_us", 5000.0)
        cfg[27] = 1.0 if CFG.get("kh1_fx_debug") else 0.0
        khfx_stretch(S, cfg, K, sc)
        cfg[39] = 0.0 if CFG.get("kh1_fx_models", True) else 1.0
        cfg[40] = 0.0 if CFG.get("kh1_fx_reface", True) else 1.0   # v48: KH1 camera-layered meshes face Borderlands' camera
        cfg[41] = 1.0 if CFG.get("kh1_fx_reface_all", False) else 0.0   # v49: all KH1 effect meshes as KH1's camera sees them
        cfg[42] = 1.0 if CFG.get("kh1_fx_mdl_uv_old", False) else 0.0   # v51: 1 = old TEX0-size UV scale
        # learned spots stay OFF by default: armed all the time 2-5 m around Sora they cut pieces out of spells flying
        # through (user 2026-10-06: "spells look buggy"); the fresh-spawn spheres (2.5 s after a real spawn) stay on
        learned = st.get("kh_spawn_mute", []) if CFG.get("kh1_spawn_learned", False) else []
        mute = ([f[0] for f in st.get("kh_spawn_fresh", [])] + learned)[:12] if CFG.get("kh1_spawn_mute", True) else []
        cfg[43] = float(len(mute))                                     # v66/v67: fresh spawns + learned spawn points
        for i, q in enumerate(mute):
            cfg[44 + 4 * i], cfg[46 + 4 * i] = q[0], q[2]
            cfg[45 + 4 * i] = q[1] - CFG.get("kh1_spawn_mute_up", 100.0)   # the cloud rises: centre above the spot (KH1 y down)
            cfg[47 + 4 * i] = CFG.get("kh1_spawn_mute_r", 220.0)
    elif st.get("kh_intro_native") and CFG.get("kh1_fx", True) and st.get("kh_K"):
        # NATIVE SUMMON INTRO (renderer v74): KH1's player pointer is empty during the intro, so combat reads "off" -
        # but the effects pass must keep running: kh1_fx only captures effects AND KH1's camera (view / proj) while it is
        # enabled. The intro draw uses K = identity itself; no distance filter (staged effects anywhere), no ambient
        # filter (the intro's star field runs ~19 s)
        for i, v in enumerate(st["kh_K"]):
            cfg[i] = v
        cfg[16], cfg[17], cfg[18] = 1.0, 0.0, 9999.0
        cfg[19], cfg[20] = CFG.get("kh1_fx_bright", 1.0), CFG.get("kh1_fx_alpha", 1.0)
        cfg[24] = f.scale
        # hold the last effects frame through KH1's effect gaps (fx_timing.py, Simba intro: 33 stalls of 80..870 ms
        # without any effect draw; at 70 ms the background blinked out and back - user: "flashy, popping in and out")
        cfg[25], cfg[26] = CFG.get("kh1_intro_fx_hold_ms", 250.0), CFG.get("kh1_fx_gap_us", 5000.0)
        cfg[39] = 0.0 if CFG.get("kh1_fx_models", True) else 1.0
        cfg[40], cfg[41] = 1.0, 1.0                     # meshes exactly as KH1's camera sees them (no re-facing)
    # renderer v76-v78 switches (1 = old behaviour): [92] KH1 depth state for intro effect meshes, [94] KH1 frame latch in
    # summon intros, [95] KH1 frame latch in gameplay (kh1_fx v40 "Local\SoraFxFrame")
    cfg[92] = 0.0 if CFG.get("kh1_intro_fx_depth", True) else 1.0
    cfg[94] = 0.0 if CFG.get("kh1_fx_frame_latch_intro", True) else 1.0
    cfg[95] = 0.0 if CFG.get("kh1_fx_frame_latch", True) else 1.0
    dll.sr_khfx(cfg, 96)
    if hasattr(dll, "sr_khfx_stats"):
        o = (ctypes.c_int * 6)()
        dll.sr_khfx_stats(o)
        S.status["kh1_fx"] = {"mapped": o[0], "drawn": o[1], "skipped": o[2], "textures": o[3], "frame_q": o[4], "captured": o[5]}
        if hasattr(dll, "sr_khfx_mdl_stats"):
            m4 = (ctypes.c_int * 8)()                       # renderer v48 writes 5 (refaced), room to grow
            dll.sr_khfx_mdl_stats(m4)
            S.status["kh1_fx"]["models"] = {"mapped": m4[0], "tris": m4[1], "batches": m4[2], "captured": m4[3], "refaced": m4[4]}
        if hasattr(dll, "sr_khfx_frame_stats2"):         # v78: gameplay frame latch [latched, held, KH1 frame no]
            f3 = (ctypes.c_uint32 * 3)(); dll.sr_khfx_frame_stats2(f3)
            S.status["kh1_fx"]["frames"] = list(f3)
        if hasattr(dll, "sr_khui"):                       # v52: KH1's own 2D interface drawn over Borderlands
            if kh1_state(S) is not None:                 # one failed KH1 read must not blank KH1's HUD for a tick
                st["kui_kh_ok"] = time.time()
            on = bool(CFG.get("kh1_ui_native", True)) and (time.time() - st.get("kui_kh_ok", 0) < 1.0
                                                           or time.time() < st.get("kh_warping_until", 0))   # v84 hold: our reload
            dll.sr_khui(1 if on else 0)
            if hasattr(dll, "sr_khui_party"):            # v53: party gauges left of Borderlands' minimap (PS2 px)
                dll.sr_khui_party.argtypes = [ctypes.c_float, ctypes.c_float]
                dx, dy = CFG.get("kh1_ui_party_offset", [-150.0, 0.0])
                dll.sr_khui_party(float(dx), float(dy))
            u4 = (ctypes.c_int * 4)()
            dll.sr_khui_stats(u4)
            S.status["kh1_ui"] = {"on": on, "mapped": u4[0], "tris": u4[1], "batches": u4[2], "captured": u4[3]}
        if hasattr(dll, "sr_khfx_stats2"):
            d2 = (ctypes.c_double * 6)()
            dll.sr_khfx_stats2(d2)
            S.status["kh1_fx"]["ambient"] = [int(d2[0]), int(d2[1]), round(d2[2] - d2[3], 2), round(d2[2] - d2[4], 2), d2[5]]
        st["khfx_ok"] = bool(ks) and o[0] == 1


# ---------------------------------------------------------------- Donald and Goofy in Borderlands (renderer v18 actors)
# They live in KH1 next to KH1 Sora (fighting the Heartless stand-ins); the renderer draws them from their live KH1
# joints, and this places them where they are relative to KH1 Sora, mirrored around Borderlands Sora (the same mapping
# that moves Sora). Their hits on stand-ins become Borderlands damage (kh1_proxy_tick); their HP stays full.
KH_PARTY_SLOTS = ((0, 0x2D37288), (1, 0x2D37290))   # renderer actor id, KH1 party pointer (Donald, Goofy)


class ActorFrame(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("pos", ctypes.c_float * 3), ("yaw", ctypes.c_float), ("scale", ctypes.c_float), ("visible", ctypes.c_int32),
                ("flags", ctypes.c_int32), ("pad", ctypes.c_float)]


def party_on(S) -> bool:
    return bool(S.CFG.get("kh_party", True)) and hasattr(S.dll, "sr_set_actor") and kh1_combat_on(S)


def party_hp_read(S):
    """[[hp, max hp, mp, max mp] or None] for Donald, Goofy (KH1 entity +0x6C -> stats +0x3C)."""
    out = []
    for ent in party_addrs(S):
        b = ent and kh1_abs(S, ent + 0x6C, 4)
        stp = kh1_resolve(S, int.from_bytes(b, "little")) if b else 0
        v = stp and kh1_abs(S, stp + 0x3C, 16)
        out.append(list(struct.unpack("<4i", v)) if v else None)
    return out


def party_addrs(S):
    out = []
    for _, slot in KH_PARTY_SLOTS:
        b = kh1_mem(S, slot, 8)
        a = int.from_bytes(b, "little") if b else 0
        out.append(a)
    return out


# ---------------------------------------------------------------- stray Borderlands shots -> Donald / Goofy (party step 3)
# Donald and Goofy have no Borderlands body, so Borderlands' bullets fly through where they are drawn. Runtime hooks
# (no restart): every enemy instant-hit shot (WillowWeapon.ProcessInstantHit: StartTrace -> HitLocation, BL's AI also
# fires "purposeful misses" this way) and every enemy projectile (WillowProjectile.Init / InitWithVelocity, Explode)
# is recorded; logic tests them against the two drawn bodies (vertical capsules at their Borderlands spot) and takes
# the same SHARE of their KH1 max HP that the shot would take of Sora's Borderlands max health (InstantHitDamage /
# Projectile.Damage, explosions with linear falloff over DamageRadius). KH1 owns the rest (KO, Cure, hurt voice).
PARTY_BODY = ((40.0, 100.0), (40.0, 175.0))           # (radius, height) Borderlands units: Donald, Goofy (KH1 cm x scale)


def _seg_dist(p0, p1, q0, q1) -> float:
    """Shortest distance between segments p0-p1 and q0-q1 (3D)."""
    d1 = [p1[i] - p0[i] for i in range(3)]
    d2 = [q1[i] - q0[i] for i in range(3)]
    r = [p0[i] - q0[i] for i in range(3)]
    a = sum(x * x for x in d1)
    e = sum(x * x for x in d2)
    f = sum(d2[i] * r[i] for i in range(3))
    if a < 1e-6 and e < 1e-6:
        return math.dist(p0, q0)
    if a < 1e-6:
        s, t = 0.0, min(1.0, max(0.0, f / e))
    else:
        c = sum(d1[i] * r[i] for i in range(3))
        if e < 1e-6:
            t, s = 0.0, min(1.0, max(0.0, -c / a))
        else:
            b = sum(d1[i] * d2[i] for i in range(3))
            den = a * e - b * b
            s = min(1.0, max(0.0, (b * f - c * e) / den)) if den > 1e-9 else 0.0
            t = (b * s + f) / e
            if t < 0.0:
                t, s = 0.0, min(1.0, max(0.0, -c / a))
            elif t > 1.0:
                t, s = 1.0, min(1.0, max(0.0, (b - c) / a))
    c1 = [p0[i] + d1[i] * s for i in range(3)]
    c2 = [q0[i] + d2[i] * t for i in range(3)]
    return math.dist(c1, c2)


def _v(v):
    return (float(v.X), float(v.Y), float(v.Z))


def party_shot_hooks(S, on: bool) -> None:
    """(Un)register the runtime hooks that record enemy shots into S.state["bl_shots"] / ["bl_projs"] / ["bl_booms"]."""
    from unrealsdk import hooks as _hk
    st = S.state
    ident = "sora_partyshots"
    names = {"WillowGame.WillowWeapon:ProcessInstantHit": "hit", "WillowGame.WillowProjectile:Init": "proj",
             "WillowGame.WillowProjectile:InitWithVelocity": "proj", "WillowGame.WillowProjectile:Explode": "boom"}
    for n in names:
        for tp in (_hk.Type.PRE, _hk.Type.POST):
            try:
                _hk.remove_hook(n, tp, ident)
            except Exception:  # noqa: BLE001
                pass
    st["shot_hooks"] = False
    if not on:
        return
    shots = st.setdefault("bl_shots", [])
    projs = st.setdefault("bl_projs", [])
    booms = st.setdefault("bl_booms", [])

    def enemy(inst) -> bool:
        try:
            pc = get_pc_safe()
            return inst is not None and pc is not None and inst != pc.Pawn and not getattr(inst.Controller, "bIsPlayer", False)
        except Exception:  # noqa: BLE001
            return False

    def on_hit(obj, args, _ret, _func):
        try:
            if not enemy(obj.Instigator) or len(shots) > 256:
                return
            imp = args.Impact
            a = _v(imp.StartTrace)
            b = _v(imp.HitLocation) if imp.HitActor is not None else _v(imp.EndTrace)
            if b == (0.0, 0.0, 0.0):
                b = _v(imp.EndTrace)
            shots.append((time.time(), a, b, float(obj.InstantHitDamage), bool(args.bIsPurposefulMiss)))
        except Exception as ex:  # noqa: BLE001
            st["shot_hook_err"] = f"hit {type(ex).__name__}: {ex}"

    def on_proj(obj, _args, _ret, _func):
        try:
            if not enemy(obj.Instigator) or len(projs) > 64:
                return
            from unrealsdk.unreal import WeakPointer  # type: ignore
            projs.append({"w": WeakPointer(obj), "t0": time.time(), "last": _v(obj.Location), "hit": set()})
        except Exception as ex:  # noqa: BLE001
            st["shot_hook_err"] = f"proj {type(ex).__name__}: {ex}"

    def on_boom(obj, args, _ret, _func):
        try:
            if not enemy(obj.Instigator) or len(booms) > 64:
                return
            booms.append((time.time(), _v(args.HitLocation), float(obj.Damage), float(obj.DamageRadius), id(obj)))
        except Exception as ex:  # noqa: BLE001
            st["shot_hook_err"] = f"boom {type(ex).__name__}: {ex}"

    cbs = {"hit": on_hit, "proj": on_proj, "boom": on_boom}
    ok = []
    for n, kind in names.items():
        try:
            _hk.add_hook(n, _hk.Type.POST if kind == "proj" else _hk.Type.PRE, ident, cbs[kind])
            ok.append(n.split(":")[1])
        except Exception as ex:  # noqa: BLE001
            st["shot_hook_err"] = f"add {n}: {ex}"
    st["shot_hooks"] = True
    st["shot_hook_names"] = ok


HITS_SIZE = 120 + 256 * 80


ELEM_CODE_FILE = HERE / "data" / "kh1_elem_codes.json"
# KH1 attack definitions (hit record +0x2C handle): dword 0 = 0 for Keyblade / physical hits, non-zero for spells -
# read as KH1's element code. Seeds (guessed 2026-10-06 from the hit log: Sora's 160-damage Fira = 1, Donald's spell 1,
# multi-hit 48s = 3); every hit of a KNOWN Sora cast relearns its code -> data/kh1_elem_codes.json.
KH1_ELEM_SEED = {1: "Fire", 2: "Blizzard", 3: "Thunder"}


def kh1_hit_scan(S, now: float) -> None:
    """New KH1 hit records (kh1_fx "Local\\SoraHits" log) -> st kh_hit_last[victim] = (attacker entity, element code, t)."""
    st = S.state
    m = _hits_map(S)
    if m is None or struct.unpack_from("<I", m, 0)[0] != 0x53544948:
        return
    w = struct.unpack_from("<I", m, 4)[0]
    r = st.get("kh_hit_r", w)
    if w < r or w - r > 256:
        r = max(0, w - 32)
    last = st.setdefault("kh_hit_last", {})
    for i in range(r, w):
        o = 120 + (i % 256) * 80
        seq, _, ent = struct.unpack_from("<IIQ", m, o)
        if seq != i + 1:
            continue
        att_h, adef_h = struct.unpack_from("<I", m, o + 16 + 4)[0], struct.unpack_from("<I", m, o + 16 + 0x2C)[0]
        att = kh1_resolve(S, att_h) if att_h else 0
        dp = kh1_resolve(S, adef_h) if adef_h else 0
        db = dp and kh1_abs(S, dp, 4)
        code = struct.unpack("<i", db)[0] if db else 0
        last[ent] = (att, code, now)
        dmg_, typ_ = struct.unpack_from("<iI", m, o + 16 + 8)
        st.setdefault("kh_hit_feed", []).append((now, ent, att, typ_ & 0x1F, dmg_, code))
        del st["kh_hit_feed"][:-64]
    st["kh_hit_r"] = w
    if len(last) > 64:
        st["kh_hit_last"] = {k: v for k, v in last.items() if now - v[2] < 5.0}


def kh1_elem_codes(S) -> dict:
    c = S.state.get("kh_elem_codes")
    if c is None:
        c = dict(KH1_ELEM_SEED)
        try:
            c.update({int(k): v for k, v in json.loads(Path(ELEM_CODE_FILE).read_text(encoding="utf-8")).items()})
        except Exception:  # noqa: BLE001
            pass
        S.state["kh_elem_codes"] = c
    return c


def kh1_learn_elem(S, a: int, spell: str, now: float) -> None:
    """A stand-in hit while Sora's known spell is in flight: remember that spell's KH1 element code (attacker = Sora)."""
    info = S.state.get("kh_hit_last", {}).get(a)
    ks = kh1_state(S)
    if not info or not ks or info[0] != ks["addr"] or not info[1] or now - info[2] > 1.0:
        return
    c = kh1_elem_codes(S)
    if c.get(info[1]) != spell:
        c[info[1]] = spell
        try:
            learned = {k: v for k, v in c.items() if KH1_ELEM_SEED.get(k) != v or k in (S.state.get("kh_elem_seen") or set())}
            learned[info[1]] = spell
            Path(ELEM_CODE_FILE).write_text(json.dumps({str(k): v for k, v in learned.items()}, indent=1), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        kh1_event(S, f"learned KH1 element code {info[1]} = {spell}")
    S.state.setdefault("kh_elem_seen", set()).add(info[1])


def kh1_hit_attacker(S, a: int, now: float):
    """Who KH1 says just hit stand-in a (hit log, < 1 s): (who, spell) with who 1 Donald, 2 Goofy, 3 the active SUMMON
    (st kh_summon_ent), None = Sora / unknown; spell = the element code's spell name (or None)."""
    info = S.state.get("kh_hit_last", {}).get(a)
    if not info or now - info[2] > 1.0:
        return None, None
    att = info[0]
    spell = kh1_elem_codes(S).get(info[1]) if info[1] else None
    pa = party_addrs(S)
    if pa and len(pa) > 0 and att and att == pa[0]:
        return 1, spell
    if pa and len(pa) > 1 and att and att == pa[1]:
        return 2, spell
    if att and att == S.state.get("kh_summon_ent"):
        return 3, spell
    return None, None


def kh1_party_spell(S, a: int, now: float):
    """The spell Donald / Goofy just hit stand-in a with (KH1 element code of their latest hit), else None."""
    info = S.state.get("kh_hit_last", {}).get(a)
    if not info or not info[1] or now - info[2] > 1.0:
        return None
    if info[0] not in [x for x in party_addrs(S) if x]:
        return None
    return kh1_elem_codes(S).get(info[1])


def _hits_map(S):
    r"""kh1_fx v24 "Local\SoraHits" (KH1 hit records: log + injection request; layout in kh1_fx.cpp HitShared)."""
    import ctypes.wintypes as W
    m = getattr(S, "hits_map", None)
    if m is None and time.time() > S.state.get("hits_map_retry", 0):
        S.state["hits_map_retry"] = time.time() + 3.0
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenFileMappingW.restype = W.HANDLE
        k32.MapViewOfFile.restype = ctypes.c_void_p
        k32.MapViewOfFile.argtypes = [W.HANDLE, W.DWORD, W.DWORD, W.DWORD, ctypes.c_size_t]
        h = k32.OpenFileMappingW(0x0006, False, r"Local\SoraHits")
        pm = h and k32.MapViewOfFile(h, 0x0006, 0, 0, HITS_SIZE)
        if pm:
            m = S.hits_map = (ctypes.c_char * HITS_SIZE).from_address(pm)
    return m


STAND_SIZE, STAND_N, STAND_MAGIC = 16 + 32 * 16, 32, 0x444E5453


def _stand_map(S):
    r"""kh1_fx v31 "Local\SoraStand": magic, on, n, pad, then 32 x {u64 KH1 entity, i32 damage total, i32 hits}. Entities
    listed there never drop below 1 HP in KH1, and every hit on them adds the damage KH1 meant to deal."""
    import ctypes.wintypes as W
    m = getattr(S, "stand_map", None)
    if m is None and time.time() > S.state.get("stand_map_retry", 0):
        S.state["stand_map_retry"] = time.time() + 3.0
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenFileMappingW.restype = W.HANDLE
        k32.MapViewOfFile.restype = ctypes.c_void_p
        k32.MapViewOfFile.argtypes = [W.HANDLE, W.DWORD, W.DWORD, W.DWORD, ctypes.c_size_t]
        h = k32.OpenFileMappingW(0x0006, False, r"Local\SoraStand")
        pm = h and k32.MapViewOfFile(h, 0x0006, 0, 0, STAND_SIZE)
        if pm:
            m = S.stand_map = (ctypes.c_char * STAND_SIZE).from_address(pm)
    if m is not None and struct.unpack_from("<I", m, 0)[0] != STAND_MAGIC:
        return None
    return m


def stand_sync(S, ents) -> bool:
    """Puts exactly these KH1 entities in kh1_fx's stand-in list (HP floor 1 + hit counter). False = no v31 kh1_fx."""
    m = _stand_map(S)
    if m is None:
        return False
    base = S.state.setdefault("stand_base", {})          # entity -> damage total when it got its slot
    want = set(ents)
    free = []
    for i in range(STAND_N):
        o = 16 + i * 16
        e = struct.unpack_from("<Q", m, o)[0]
        if e and (e not in want or e not in base):
            struct.pack_into("<Q", m, o, 0)
            base.pop(e, None)
            e = 0
        if e:
            want.discard(e)
        else:
            free.append(o)
    for e in list(want):
        if not free:
            break
        o = free.pop(0)
        struct.pack_into("<Q", m, o, e)
        base[e] = (o, struct.unpack_from("<i", m, o + 8)[0])
    struct.pack_into("<I", m, 4, 1)
    return True


def stand_hits(S, ent: int) -> int:
    """KH1 damage dealt to stand-in ent since the last call (0 = no hit)."""
    m = _stand_map(S)
    b = S.state.get("stand_base", {}).get(ent)
    if m is None or b is None:
        return 0
    o, last = b
    if struct.unpack_from("<Q", m, o)[0] != ent:
        return 0
    tot = struct.unpack_from("<i", m, o + 8)[0]
    S.state["stand_base"][ent] = (o, tot)
    return tot - last


def stand_release(S, ent: int) -> None:
    m = _stand_map(S)
    b = S.state.get("stand_base", {}).pop(ent, None)
    if m is not None and b is not None and struct.unpack_from("<Q", m, b[0])[0] == ent:
        struct.pack_into("<Q", m, b[0], 0)


PRIZE_SIZE, PRIZE_MAGIC = 96 + 64 * 88, 0x5A495250
PRIZE_FILE = HERE / "data" / "kh1_prize_tables.json"


def _prize_map(S):
    r"""kh1_fx v32 "Local\SoraPrize": log of KH1's prize spawns FUN_1402abc40 (table, pos, mode) + a spawn request
    (layout in kh1_fx.cpp PrizeShared)."""
    import ctypes.wintypes as W
    m = getattr(S, "prize_map", None)
    if m is None and time.time() > S.state.get("prize_map_retry", 0):
        S.state["prize_map_retry"] = time.time() + 3.0
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenFileMappingW.restype = W.HANDLE
        k32.MapViewOfFile.restype = ctypes.c_void_p
        k32.MapViewOfFile.argtypes = [W.HANDLE, W.DWORD, W.DWORD, W.DWORD, ctypes.c_size_t]
        h = k32.OpenFileMappingW(0x0006, False, r"Local\SoraPrize")
        pm = h and k32.MapViewOfFile(h, 0x0006, 0, 0, PRIZE_SIZE)
        if pm:
            m = S.prize_map = (ctypes.c_char * PRIZE_SIZE).from_address(pm)
    if m is not None and struct.unpack_from("<I", m, 0)[0] != PRIZE_MAGIC:
        return None
    return m


def prize_tables(S) -> dict:
    """Learned drop tables: KH1 model id (str) -> offset of the prize table inside that kind's ex_XXXX.bd block (ent+0x134)."""
    t = getattr(S, "prize_tables", None)
    if t is None:
        try:
            t = json.loads(PRIZE_FILE.read_text())
        except Exception:  # noqa: BLE001
            t = {}
        S.prize_tables = t
    return t


def prize_learn(S) -> None:
    """A real KH1 death spawned prizes: find whose .bd block holds that table -> remember (model id -> offset)."""
    m = _prize_map(S)
    if m is None:
        return
    w = struct.unpack_from("<I", m, 4)[0]
    seen = S.state.get("prize_seen", w if "prize_seen" not in S.state and w > 64 else 0)
    if w == seen:
        return
    S.state["prize_seen"] = w
    raw = kh1_mem(S, KH_POOL, 96 * KH_STRIDE) or b""
    blocks = []
    for i in range(len(raw) // KH_STRIDE):
        h = struct.unpack_from("<I", raw, i * KH_STRIDE + 0x134)[0]
        if h:
            r = kh1_resolve(S, h)
            if r:
                blocks.append((r, S.kh["base"] + KH_POOL + i * KH_STRIDE))
    tabs, changed = prize_tables(S), False
    for i in range(max(seen, w - 64), w):
        o = 96 + (i % 64) * 88
        seq, _, tbl = struct.unpack_from("<IiQ", m, o)
        if seq != i + 1 or struct.unpack_from("<I", m, o + 84)[0]:
            continue                                          # torn entry / our own request
        near = [(tbl - r, r, ent) for r, ent in blocks if 0 <= tbl - r < 0x8000]
        for _, r, ent in sorted(near)[:1]:                    # the block right before the table
            if True:
                mid = kh1_model_id(S, ent)
                if mid and tabs.get(str(mid)) != tbl - r:
                    tabs[str(mid)] = tbl - r
                    changed = True
                    kh1_event(S, f"learned KH1 drop table of ex_{mid:04d}: .bd + {hex(tbl - r)}")
                break
    if changed:
        try:
            PRIZE_FILE.write_text(json.dumps(tabs, indent=1))
        except Exception:  # noqa: BLE001
            pass


def prize_drop(S, a: int) -> bool:
    """KH1's own prizes (orbs + item rolls at KH1's odds) for stand-in a, at its spot, WITHOUT killing it. False = this
    Heartless kind's table isn't learned yet (or no kh1_fx v32)."""
    m = _prize_map(S)
    if m is None or not S.CFG.get("kh1_prize_drops", True):
        return False
    req, done = struct.unpack_from("<2I", m, 8)
    if req != done:
        return False
    off = prize_tables(S).get(str(kh1_model_id(S, a)))
    hb = kh1_abs(S, a + 0x134, 4)
    r = off is not None and hb and kh1_resolve(S, struct.unpack("<I", hb)[0])
    tb = r and kh1_abs(S, r + off, 52)
    pos = kh1_abs(S, a + 0x10, 12)
    if not tb or not pos:
        return False
    m[16:68] = tb
    struct.pack_into("<i", m, 68, -1)                      # mode: KH1's own rule (as script command 63)
    struct.pack_into("<4f", m, 72, *struct.unpack("<3f", pos), 1.0)
    struct.pack_into("<I", m, 8, req + 1)
    S.state["prize_drops"] = S.state.get("prize_drops", 0) + 1
    return True


def _kh1_ent_alive(S, addr: int) -> bool:
    b = addr and kh1_abs(S, addr + 0x374, 4)
    return bool(b) and (struct.unpack("<I", b)[0] & 3) == 1


def kh1_hit_template(S) -> bool:
    """Newest real KH1 hit record on a party member (not guarded: flags & 0x20 == 0) whose ATTACKER is still alive in this
    KH1 process -> the injection template (mask / +0x20 cleared). The lethal-hit crash (2026-10-05) had a despawned
    attacker in its template."""
    m = _hits_map(S)
    if m is None or struct.unpack_from("<I", m, 0)[0] != 0x53544948:
        return False
    w = struct.unpack_from("<I", m, 4)[0]
    if w == S.state.get("hit_tmpl_w") and S.state.get("hit_tmpl_ok") is not None:
        return S.state["hit_tmpl_ok"]
    party = set(a for a in party_addrs(S) if a)
    ok = False
    for i in range(w - 1, max(-1, w - 256), -1):
        o = 120 + (i % 256) * 80
        seq, _, ent = struct.unpack_from("<IIQ", m, o)
        if seq != i + 1 or ent not in party:
            continue
        rec = bytearray(m[o + 16:o + 80])
        if rec[0] & 0x20 or (struct.unpack_from("<I", rec, 0xC)[0] & 0x1F) != 1:
            continue
        att = kh1_resolve(S, struct.unpack_from("<I", rec, 4)[0])
        if not _kh1_ent_alive(S, att):
            continue
        rec[0x20:0x24] = bytes(4)
        rec[0x28:0x2C] = bytes(4)
        m[48:112] = bytes(rec)
        struct.pack_into("<I", m, 112, 1)
        ok = True
        break
    if not ok:
        struct.pack_into("<I", m, 112, 0)
    S.state["hit_tmpl_w"], S.state["hit_tmpl_ok"] = w, ok
    return ok


def party_hurt_kh1(S, aid: int, share: float, dir_bl=None) -> int:
    """Take `share` of party member aid's KH1 max HP. With a live hit template (kh1_fx v24) as a REAL KH1 hit through
    KH1's own hit queue (flinch / knockback by KH1); otherwise a plain HP write. Never below party_shot_floor (1) until
    KO through the queue is verified (config party_shot_ko)."""
    st, CFG = S.state, S.CFG
    ent = party_addrs(S)[aid]
    b = ent and kh1_abs(S, ent + 0x6C, 4)
    stp = kh1_resolve(S, int.from_bytes(b, "little")) if b else 0
    hpb = stp and kh1_abs(S, stp + 0x3C, 8)
    if not hpb:
        return 0
    hp_, mx = struct.unpack("<2i", hpb)
    if hp_ <= 0 or not 0 < mx < 5000:
        return 0
    take = max(1, int(round(share * mx)))
    floor = 0 if CFG.get("party_shot_ko", False) else int(CFG.get("party_shot_floor", 1))
    take = min(take, hp_ - floor)
    if take <= 0:
        return 0
    m = _hits_map(S)
    if CFG.get("party_shot_kh1_hit", True) and m is not None and kh1_hit_template(S):
        req, done = struct.unpack_from("<2I", m, 8)
        if req == done:
            ks = kh1_state(S)
            dx, dz = 1.0, 0.0
            if dir_bl and ks:
                yb = (st.get("kh_yaw_target") if st.get("kh_yaw_target") is not None else st["yaw"]) + math.radians(CFG["yaw_offset_deg"])
                fwd_b = dir_bl[0] * math.cos(yb) + dir_bl[1] * math.sin(yb)
                side_b = -dir_bl[0] * math.sin(yb) + dir_bl[1] * math.cos(yb)
                sign = CFG.get("kh_turn_sign", -1.0)
                fx, fz = ks["fwd"]
                fk, sk = fwd_b, side_b * sign
                dx, dz = fk * fx - sk * fz, fk * fz + sk * fx
                n = math.hypot(dx, dz) or 1.0
                dx, dz = dx / n, dz / n
            struct.pack_into("<Q", m, 16, ent)
            struct.pack_into("<i", m, 24, take)
            struct.pack_into("<4f", m, 32, dx, 0.0, dz, 1.0)
            struct.pack_into("<I", m, 8, req + 1)
            st["party_kh1_hits"] = st.get("party_kh1_hits", 0) + 1
            return take
        return 0                                           # one KH1 hit per KH1 frame; the next shot gets through
    ctypes.windll.kernel32.WriteProcessMemory(ctypes.c_void_p(S.kh["h"]), ctypes.c_void_p(stp + 0x3C),
                                              struct.pack("<i", hp_ - take), 4, None)
    return take


def party_shots_tick(S, pc, now: float) -> None:
    st, CFG = S.state, S.CFG
    on = bool(CFG.get("party_shots", True)) and party_on(S)
    if on != st.get("shot_hooks", False) or (on and st.get("shot_hooks_mod") != id(party_shot_hooks)):
        party_shot_hooks(S, on)                           # (re)register after a logic reload too
        st["shot_hooks_mod"] = id(party_shot_hooks)
    shots, projs, booms = st.setdefault("bl_shots", []), st.setdefault("bl_projs", []), st.setdefault("bl_booms", [])
    if not on:
        shots.clear(), projs.clear(), booms.clear()
        return
    sm = st.get("party_smooth", {})
    bodies = []
    for aid in (0, 1):
        p = sm.get(aid)
        if p is None or st.get("party_air"):
            continue
        r, h = PARTY_BODY[aid]
        sc = max(S.frame.scale, 0.01)
        bodies.append((aid, (p[0], p[1], p[2] + r), (p[0], p[1], p[2] + h * sc - r), r))
    hpv = S.status.get("hp") or [100, 100]
    bl_max = max(1.0, float(hpv[1] if len(hpv) > 1 else 100))
    scale = CFG.get("party_shot_scale", 1.0)
    dmg = {0: 0.0, 1: 0.0}
    hdir = {0: None, 1: None}
    n_hits = st.setdefault("party_shot_hits", [0, 0])
    batch, shots[:] = list(shots), []
    for t, a, b, d, miss in batch:
        for aid, q0, q1, r in bodies:
            if _seg_dist(a, b, q0, q1) < r:
                dmg[aid] += d
                hdir[aid] = (b[0] - a[0], b[1] - a[1])
                n_hits[aid] += 1
                break                                      # a bullet stops in the first one it meets
    bb, booms[:] = list(booms), []
    for t, at, d, rad, pid in bb:
        for aid, q0, q1, r in bodies:
            dd = _seg_dist(at, at, q0, q1) - r
            if rad > 0 and dd < rad:
                dmg[aid] += d * max(0.0, 1.0 - max(0.0, dd) / rad)
                hdir[aid] = (q0[0] - at[0], q0[1] - at[1])
                n_hits[aid] += 1

    def _proj_job():                                       # projectiles are UObjects: read them on the game tick
        keep = []
        for pj in projs:
            o = pj["w"]()
            if o is None or time.time() - pj["t0"] > 8.0:
                continue
            try:
                cur = _v(o.Location)
                d = float(o.Damage)
            except Exception:  # noqa: BLE001
                continue
            for aid, q0, q1, r in bodies:
                if aid not in pj["hit"] and _seg_dist(pj["last"], cur, q0, q1) < r + 10.0:
                    pj["hit"].add(aid)
                    st.setdefault("party_proj_dmg", []).append((aid, d))
                    n_hits[aid] += 1
            pj["last"] = cur
            keep.append(pj)
        projs[:] = keep
    if projs and not st.get("proj_job_queued"):
        st["proj_job_queued"] = True

        def _run():
            st["proj_job_queued"] = False
            _proj_job()
        S.pending.append(_run)
    for aid, d in st.pop("party_proj_dmg", []):
        dmg[aid] += d
    for aid, d in dmg.items():
        if d <= 0:
            continue
        taken = party_hurt_kh1(S, aid, d / bl_max * scale, hdir[aid])
        st.setdefault("party_shot_taken", [0, 0])[aid] += taken
        kh1_event(S, f"stray BL shot: {('Donald', 'Goofy')[aid]} -{taken} KH1 HP ({d:.0f} BL dmg / {bl_max:.0f})")
    S.status["party_shots"] = {"hooks": st.get("shot_hook_names"), "hits": n_hits, "taken": st.get("party_shot_taken", [0, 0]),
                               "kh1_hits": st.get("party_kh1_hits", 0), "template": st.get("hit_tmpl_ok"),
                               "tracking": len(projs), "err": st.get("shot_hook_err")}


DONALD_CAST_ANIMS = {60: "attack", 73: "cure"}      # KH1 Donald (party_trace): 60 = attack spell (MP drops), 73 = Cure


def party_spell(S, pc, src, kind: str) -> None:
    """Donald's KH1 cast, shown in Borderlands: a KH1-style spell from his staff at the enemy nearest him (visual; the
    damage comes from his real KH1 spell hitting the stand-in, kh1_proxy_tick)."""
    st = S.state
    if khfx_live(S):                                   # KH1's own spell effect is drawn (v35): voices only
        voice(S, "dheal" if kind == "cure" else "datk", chance=1.0 if kind == "cure" else 0.6, who="donald", cd=1.5)
        return
    if kind == "cure":
        fx_cure(S)
        voice(S, "dheal", who="donald", cd=1.5)
        return
    voice(S, "datk", chance=0.6, who="donald", cd=1.2)
    near = enemies(S, pc, S.CFG.get("donald_range", 1800.0), origin=list(src))
    if not near:
        return
    tgt = near[0][1]
    spell = random.choice(S.CFG.get("donald_spells", ["Fire", "Blizzard", "Thunder"]))
    t = vec3(tgt.Location)
    start = [src[0], src[1], src[2] + 70.0]
    d = [t[i] - start[i] for i in range(3)]; n = math.sqrt(sum(x * x for x in d)) or 1.0; d = [x / n for x in d]
    now = time.time()
    if spell == "Fire":
        fx_flash(S, start, (1.0, 0.55, 0.15), 120)
        st["proj"].append({"kind": "fire", "pos": start, "vel": [x * 1400 for x in d], "target": tgt, "t0": now, "life": 2.0,
                           "dmg": 0, "color": (1.0, 0.45, 0.1), "size": 60, "visual": True})
    elif spell == "Blizzard":
        fx_flash(S, start, (0.7, 0.9, 1.0), 100)
        st["proj"].append({"kind": "ice", "pos": list(start), "vel": [x * 1800 for x in d], "target": tgt, "t0": now, "life": 1.2,
                           "dmg": 0, "color": (0.55, 0.85, 1.0), "size": 34, "visual": True})
    else:
        st["proj"].append({"kind": "bolt", "pos": t, "vel": [0, 0, 0], "target": tgt, "t0": now + 0.15, "life": 0.35, "dmg": 0,
                           "color": (1.0, 0.95, 0.5), "size": 60})


# KH1 summons drawn in Borderlands (user 2026-10-06): KH1 pool kind -> baked model id (tools/make_summon.py writes
# simba_*.bin = data/ex_4070_*.bin, loaded through the renderer's enemy-model path sr_enemy_model / sr_set_enemy)
KH1_SUMMON_KINDS = {0x830666A0: 4070}   # Simba
SUMMON_SLOT = 15                        # renderer enemy slot reserved for the summon (skins / free Heartless fill from 0)


# KH1 summon id (kind data byte 5, = 0x298C8F0 while summoned) -> baked model (tools/make_summon.py: the xs_<name>
# packages; Simba = xa_ex_4070). Ids 0 Dumbo / 2 Genie / 5 Simba seen in game; 1 / 3 / 4 = Bambi / Tinker Bell / Mushu
# by KH1's order (logged "summon appeared: id ..." to confirm).
KH1_SUMMON_MODEL = {0: 4100, 1: 4101, 2: 4102, 3: 4103, 4: 4104, 5: 4070}


def kh1_summon_model(S, ent: int) -> int:
    """Model id of a KH1 SUMMON entity, 0 = not a summon. A summon's kind data reads 'CLS' 03, then class byte 3 and its
    summon id (Simba: 43 4c 53 03 | 03 05 ...); cached per (entity, kind)."""
    import struct
    kb = kh1_abs(S, ent + 0x134, 4)
    k = struct.unpack("<I", kb)[0] if kb else 0
    if not k:
        return 0
    cache = S.state.setdefault("kh_summon_kind_c", {})
    if (ent, k) in cache:
        return cache[(ent, k)]
    mid = KH1_SUMMON_KINDS.get(k, 0)
    kd = kh1_abs(S, kh1_resolve(S, k), 8) if not mid else None
    if kd and kd[:3] == b"CLS" and kd[4] == 3:
        mid = KH1_SUMMON_MODEL.get(kd[5], 0)
        kh1_event(S, f"summon appeared: id {kd[5]} kind {k:#x} -> model {mid or 'unknown'}")
    cache[(ent, k)] = mid
    return mid


def summon_draw_tick(S, pc, now: float) -> None:
    """The active summon (pool entity of a KH1_SUMMON_KINDS kind) drawn from its LIVE KH1 skeleton, placed like Donald /
    Goofy: its KH1 offset from KH1 Sora turned + scaled into Borderlands around Borderlands Sora. Hidden otherwise
    (the renderer hides an enemy slot ~300 ms after the last call). Config kh1_summon_draw."""
    import struct
    st, CFG, f, dll = S.state, S.CFG, S.frame, S.dll
    if not CFG.get("kh1_summon_draw", True) or not hasattr(dll, "sr_set_enemy") or not getattr(S, "kh", None) or not S.kh.get("h"):
        return
    ent, mid = st.get("kh_summon_ent", 0), st.get("kh_summon_mid", 0)
    raw = kh1_abs(S, ent, 0x380) if ent else None      # (0x380: the alive flag sits at +0x374)
    if raw and (not struct.unpack_from("<I", raw, 0x374)[0] & 1
                or (not kh1_summon_model(S, ent) and st.get("kh_summon_logged", (0,))[0] != ent)):
        raw = None
    if raw is None and now - st.get("kh_summon_scan", 0) > 0.5:
        st["kh_summon_scan"] = now
        ent = mid = 0
        # summons without a class-3 kind (Mushu has NO kind at all): while KH1 has Donald / Goofy out (summon), the
        # non-party entity KH1 put in an action slot is the summon; its model comes from KH1's summon id (0x298C8F0)
        pa_ = [a for a in party_addrs(S) if a]
        out_ = pa_ and all(int.from_bytes(kh1_abs(S, a, 4) or b"\x00" * 4, "little") & 0x1000000 for a in pa_)
        if out_:
            lead_ = int.from_bytes(kh1_mem(S, KH_PARTY[0], 8) or b"\x00" * 8, "little")
            sid_ = int.from_bytes(kh1_mem(S, 0x298C8F0, 4) or b"\x00" * 4, "little")
            for i in range(4):
                sb = kh1_mem(S, 0x2D371D0 + i * 0x28, 16)
                hold = int.from_bytes(sb[8:16], "little") if sb else 0
                if sb and sb[0] and hold and hold != lead_ and hold not in pa_ and KH1_SUMMON_MODEL.get(sid_):
                    ent, mid = hold, KH1_SUMMON_MODEL[sid_]
                    if st.get("kh_summon_logged") != (ent, sid_):
                        st["kh_summon_logged"] = (ent, sid_)
                        kh1_event(S, f"summon (no class-3 kind) found in action slot {i}: id {sid_} ent {ent:#x} -> model {mid}")
                    break
        pool = kh1_mem(S, KH_POOL, 96 * KH_STRIDE) or b""
        for i in range(len(pool) // KH_STRIDE):
            e = pool[i * KH_STRIDE:(i + 1) * KH_STRIDE]
            if ent or not struct.unpack_from("<I", e, 0x374)[0] & 1:
                continue
            a_ = S.kh["base"] + KH_POOL + i * KH_STRIDE
            m_ = kh1_summon_model(S, a_)
            if m_:
                ent, mid = a_, m_
                break
        st["kh_summon_ent"], st["kh_summon_mid"] = ent, mid
        raw = kh1_abs(S, ent, 0x380) if ent else None
    ks = kh1_state(S)
    if raw is None or not ks or not mid:
        S.status["summon_draw"] = None
        return
    # every tick (cheap once loaded): a renderer hot swap starts with an empty model cache, and a "loaded already"
    # flag here left the summon slot pointing at nothing - Dumbo invisible after renderer v70-v73 (2026-10-06 19:48)
    if dll.sr_enemy_model(mid) <= 0:
        S.status["summon_draw"] = f"model {mid} missing (data/ex_{mid:04d}_mesh.bin)"
        return
    sign = CFG.get("kh_turn_sign", -1.0)
    fx, fz = ks["fwd"]
    yb = (st.get("kh_yaw_target") if st.get("kh_yaw_target") is not None else st["yaw"]) + math.radians(CFG["yaw_offset_deg"])
    sc = f.scale
    pos = struct.unpack_from("<3f", raw, 0x10)
    m = struct.unpack_from("<16f", raw, 0x230)
    rx, ry, rz = pos[0] - ks["pos"][0], pos[1] - ks["pos"][1], pos[2] - ks["pos"][2]
    fwd_k, side_k = rx * fx + rz * fz, -rx * fz + rz * fx
    bx = (math.cos(yb) * fwd_k - math.sin(yb) * side_k * sign) * sc
    by = (math.sin(yb) * fwd_k + math.cos(yb) * side_k * sign) * sc
    if not st.get("kh_glide_on"):
        # height relative to KH1 SORA (not the floor): Dumbo carries Sora 27 cm up and Mushu rides his shoulder - the
        # floor-based height left Sora drawn 27 cm low, inside Dumbo (user 2026-10-06: "can't see Sora / smaller")
        zb = f.pos[2] - ry * sc
    elif st.get("kh_ground_active") and st.get("kh_floor_ref") is not None:
        zb = st.get("party_ground_feet", f.pos[2]) - (pos[1] - st["kh_floor_ref"]) * sc   # gliding: KH1 Sora held up
    else:
        zb = f.pos[2] - ry * sc
    want = (f.pos[0] + bx, f.pos[1] + by, zb)
    mf_on = mapfeed_active(S) and st.get("mf_xf") is not None and CFG.get("kh1_mapfeed_summons", True)         and mid not in CFG.get("kh1_mapfeed_summon_skip", [4100])     # Dumbo: ridden, flies at his own height
    if mf_on:
        # MAP FEED: the summon walks / hovers over Borderlands' real ground in KH1 -> drawn exactly there, through the
        # same (smoothed, pin-shifted) mapping as the party: no rigid drag with Sora, feet on the ground
        want = mapfeed_to_bl(S, pos)
    prev = st.get("kh_summon_sm")
    dtp = min(0.1, max(0.0, now - st.get("kh_summon_t", now)))
    st["kh_summon_t"] = now
    # ridden summons (Dumbo): no smoothing - Sora is drawn unsmoothed on top, so a lagging Dumbo slid Sora forward into
    # Dumbo's hat when turning / speeding up (user 2026-10-12); both come from the same KH1 frame, so rigid = KH1's seat
    if prev is None or mid in CFG.get("kh1_summon_ridden", [4100]) or math.dist(prev, want) > CFG.get("party_snap_dist", 400.0):
        cur = want
    else:
        k = min(1.0, CFG.get("party_smooth", 18.0) * dtp)
        cur = tuple(prev[i] + (want[i] - prev[i]) * k for i in range(3))
    st["kh_summon_sm"] = cur
    af = ActorFrame()
    af.pos[:] = cur
    pf = math.degrees(math.atan2(-m[10], -m[8]))
    d = ((pf - kh1_facing_deg(ks) + 180) % 360) - 180
    s_ent = math.sqrt(m[0] * m[0] + m[1] * m[1] + m[2] * m[2])
    s_ent = s_ent if 0.2 < s_ent < 5.0 else 1.0
    s_ent *= CFG.get("kh1_summon_scale", {}).get(str(mid), 1.0)
    af.yaw, af.scale, af.visible = st["yaw"] + sign * math.radians(d), sc * s_ent, 1
    if mf_on:
        xf_ = st["mf_xf"]
        af.yaw = math.atan2(xf_[6] * -m[8] + xf_[7] * -m[10], xf_[4] * -m[8] + xf_[5] * -m[10])             - math.radians(CFG["yaw_offset_deg"])
    dll.sr_set_enemy(SUMMON_SLOT, ctypes.c_uint64(ent), mid, ctypes.byref(af))
    S.status["summon_draw"] = [mid, hex(ent), dll.sr_enemy_state(SUMMON_SLOT) if hasattr(dll, "sr_enemy_state") else None,
                               round(s_ent, 3)]
    if dbg_on(S, "dbg_summon_trace") and now - st.get("summon_trace_t", 0) > 0.25:
        # (user 2026-10-06: Sora looks smaller and sits on Dumbo's HEAD instead of his neck / back) - scales + heights
        st["summon_trace_t"] = now
        sm = ks.get("m") or []
        s_sora = math.sqrt(sm[0] ** 2 + sm[1] ** 2 + sm[2] ** 2) if len(sm) >= 3 else -1
        floor = st.get("kh_floor_ref") if st.get("kh_ground_active") else None
        with open(HERE / "summon_trace.txt", "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} model {mid} s_ent {s_ent:.3f} s_sora {s_sora:.3f} | KH1 y: sora "
                     f"{ks['pos'][1]:.0f} summon {pos[1]:.0f} floor {floor} | dist_k {math.hypot(rx, rz):.0f} | BL z: sora "
                     f"{f.pos[2]:.0f} summon {cur[2]:.0f} | sora anim {ks.get('anim')} summon anim {raw[0x164]}" + chr(10))


def summon_unstick(S, now: float) -> None:
    """2026-10-06: Simba's summon never ended - his time ran out (0x298C7A4 < 0), his task went to 'leaving' (state
    100) but waited forever because he still held KH1 action slot 2 (0x2D371D0 + idx * 0x28, idx = stats +0xBD;
    FUN_14028ff80 == 2) for a move (anim 204) that never finished. Freeing the slot's active byte let KH1 run its own
    vanilla exit at once (Simba left, Donald / Goofy back). Automated: time over for kh1_summon_overtime_s + the
    summon still holds its slot -> free it (logs the stuck pose for the root cause)."""
    import struct
    st, CFG = S.state, S.CFG
    if not CFG.get("kh1_summon_unstick", True) or not getattr(S, "kh", None) or not S.kh.get("h"):
        st.pop("kh_summon_over_t", None)
        return
    tb = kh1_mem(S, 0x298C7A4, 4)
    if not tb or struct.unpack("<f", tb)[0] > 0:
        st.pop("kh_summon_over_t", None)
        return
    t0 = st.setdefault("kh_summon_over_t", now)
    if now - t0 < CFG.get("kh1_summon_overtime_s", 4.0):
        return
    # only while KH1's summon sequencer waits on its end gate (step index 2 of FUN_14028a410's table)
    if int.from_bytes(kh1_mem(S, 0x298C98C, 4) or b"\x00" * 4, "little") != 2:
        return
    # v2 (2026-10-06, Mushu: an entity WITHOUT a kind held slot 2, so the Simba-only version never fired): free every
    # action slot (0x2D371D0 + i * 0x28: active byte, entity at +8) held by a non-party entity
    party = set(a for a in [int.from_bytes(kh1_mem(S, KH_PARTY[0], 8) or b"\x00" * 8, "little")] + party_addrs(S) if a)
    freed = []
    for i in range(4):
        sb = kh1_mem(S, 0x2D371D0 + i * 0x28, 16)
        if not sb or not sb[0]:
            continue
        holder = int.from_bytes(sb[8:16], "little")
        if not holder or holder in party:
            continue
        hr = kh1_abs(S, holder, 0x168) or b""
        kh1_poke(S, 0x2D371D0 + i * 0x28, b"\x00")
        freed.append(f"slot {i} = {sb[0]} by {holder:#x} (state {hr[0x160] if len(hr) > 0x164 else '?'} "
                     f"anim {hr[0x164] if len(hr) > 0x164 else '?'})")
    st["kh_summon_over_t"] = now                                  # (give KH1's exit a moment before any retry)
    if freed:
        kh1_event(S, "summon over but stuck mid-move -> freed " + ", ".join(freed) + "; KH1's own exit runs")


def party_tick(S, pc, now: float) -> None:
    import struct
    st, CFG, f = S.state, S.CFG, S.frame
    if not party_on(S):
        return
    if now < st.get("kh_warping_until", 0) and hasattr(S.dll, "sr_hold"):
        return                                            # our room reload: KH1 respawns them at the entrance -> keep the last frames
    ks = kh1_state(S)
    if not ks:
        return
    sign = CFG.get("kh_turn_sign", -1.0)
    fx, fz = ks["fwd"]
    yb = (st.get("kh_yaw_target") if st.get("kh_yaw_target") is not None else st["yaw"]) + math.radians(CFG["yaw_offset_deg"])
    sc = f.scale
    for (aid, slot), ent in zip(KH_PARTY_SLOTS, party_addrs(S)):
        af = ActorFrame()
        raw = kh1_abs(S, ent, 0x2C0) if ent else None
        if raw and struct.unpack_from("<I", raw, 0)[0] & 0x1000000:
            raw = None                                    # a summon is out: KH1 sends Donald / Goofy away (vanilla)
        if raw:
            pos = struct.unpack_from("<3f", raw, 0x10)
            m = struct.unpack_from("<16f", raw, 0x230)
            rx, ry, rz = pos[0] - ks["pos"][0], pos[1] - ks["pos"][1], pos[2] - ks["pos"][2]
            fwd_k, side_k = rx * fx + rz * fz, -rx * fz + rz * fx
            dist = math.hypot(fwd_k, side_k)
            if dist < CFG.get("party_max_cm", 2500.0):
                bx = (math.cos(yb) * fwd_k - math.sin(yb) * side_k * sign) * sc
                by = (math.sin(yb) * fwd_k + math.cos(yb) * side_k * sign) * sc
                # height: above KH1's floor, from where Borderlands Sora last stood (KH1 Sora is held 4 m up while
                # gliding; relative to him they sank into the ground)
                if int(pc.Pawn.Physics) != 2:
                    st["party_ground_feet"] = f.pos[2]
                if st.get("kh_ground_active") and st.get("kh_floor_ref") is not None:
                    zb = st.get("party_ground_feet", f.pos[2]) - (pos[1] - st["kh_floor_ref"]) * sc
                else:
                    zb = f.pos[2] - ry * sc
                sh_ = kh1_shared(S)
                air = bool(sh_ and st.get("kh_ground_active") and not sh_u32(sh_, SH_FLOOR_ON))
                held = st.get("party_smooth", {}).get(aid)
                trail = st.setdefault("party_trail", [])
                if air and CFG.get("party_glide_follow", False):
                    # Sora gliding / falling: they fly after him (KH1 has them in the air too), each one on his
                    # path a moment behind, a little to the side and lower
                    if aid == 0:
                        trail.append((now, f.pos[0], f.pos[1], f.pos[2], st["yaw"]))
                        del trail[:max(0, len(trail) - 240)]
                    lag = CFG.get("party_glide_lag", [0.35, 0.55])[aid]
                    pt = next((e for e in reversed(trail) if e[0] <= now - lag), trail[0] if trail else None)
                    if pt is not None:
                        side = CFG.get("party_glide_side", 70.0) * (-1 if aid == 0 else 1)
                        yw = pt[4]
                        want = (pt[1] - math.sin(yw) * side, pt[2] + math.cos(yw) * side,
                                pt[3] - CFG.get("party_glide_drop", 40.0))
                        prev = held if held is not None else want
                        dtp = min(0.1, max(0.0, now - st.get("party_smooth_t", now)))
                        k = min(1.0, CFG.get("party_glide_smooth", 6.0) * dtp)
                        cur = tuple(prev[i] + (want[i] - prev[i]) * k for i in range(3))
                        st.setdefault("party_smooth", {})[aid] = cur
                        af.pos[:] = cur
                        af.yaw = yw + math.radians(CFG.get("party_fly_yaw_deg", [0.0, 0.0])[aid])
                        af.pad = math.radians(CFG.get("party_fly_pitch_deg", [0.0, 70.0])[aid])   # renderer v23: lean into the flight
                        af.flags = 0x100 | (int(CFG.get("party_fly_motion", [0, 0])[aid]) << 12)
                        st.setdefault("party_yaw", {})[aid] = yw
                        af.scale, af.visible = sc, 1
                        st["party_air"] = True
                        S.dll.sr_set_actor(aid, ctypes.byref(af))
                        continue
                elif trail:
                    trail.clear()
                # MAP FEED: they walk on Borderlands' real ground in KH1 -> drawn exactly there through the DLL's mapping
                # (held during a glide: they stay near the takeoff, alive, and catch up on landing) - no frozen snapshot
                mf_on = mapfeed_active(S) and st.get("mf_xf") is not None
                if air and held is not None and not mf_on:
                    # Sora gliding / falling: they stay on the ground where they were (KH1 Sora hangs over his
                    # anchor, so following him put them under the glide at takeoff height); they catch up on landing
                    af.pos[:] = held
                    st["party_air"] = True
                    pf = math.degrees(math.atan2(-m[10], -m[8]))
                    af.yaw = st.get("party_yaw", {}).get(aid, st["yaw"])
                    af.scale, af.visible = sc, 1
                    S.dll.sr_set_actor(aid, ctypes.byref(af))
                    continue
                if st.pop("party_air", False):
                    st["party_land_t"] = now
                base = mapfeed_to_bl(S, pos) if mf_on else (f.pos[0] + bx, f.pos[1] + by, zb)
                st.setdefault("party_pos", {})[aid] = base
                want = (base[0], base[1], base[2] + st.get("party_lift", [0.0, 0.0])[aid])
                # KH1 updates at 60 Hz, Borderlands draws faster: glide toward the target instead of stepping
                sm = st.setdefault("party_smooth", {})
                prev = sm.get(aid)
                dtp = min(0.1, max(0.0, now - st.get("party_smooth_t", now)))
                if prev is None or math.dist(prev, want) > CFG.get("party_snap_dist", 400.0):
                    cur = want
                else:
                    k = min(1.0, CFG.get("party_smooth", 18.0) * dtp)
                    cur = tuple(prev[i] + (want[i] - prev[i]) * k for i in range(3))
                sm[aid] = cur
                af.pos[:] = cur
                # facing relative to KH1 Sora's, turned the same way in Borderlands
                pf = math.degrees(math.atan2(-m[10], -m[8]))
                d = ((pf - kh1_facing_deg(ks) + 180) % 360) - 180
                af.yaw = st["yaw"] + sign * math.radians(d)
                if mf_on:                                       # KH1 facing (x, z) through the same mapping
                    xf_ = st["mf_xf"]
                    af.yaw = math.atan2(xf_[6] * -m[8] + xf_[7] * -m[10], xf_[4] * -m[8] + xf_[5] * -m[10]) \
                        - math.radians(CFG["yaw_offset_deg"])
                st.setdefault("party_yaw", {})[aid] = af.yaw
                af.scale, af.visible = sc, 1
                an = raw[0x164]
                if aid == 0 and an in DONALD_CAST_ANIMS and an != st.get("donald_anim"):
                    party_spell(S, pc, tuple(af.pos), DONALD_CAST_ANIMS[an])
                    st["donald_cast_t"] = now
                if aid == 0:                                    # any spell: his MP drops (Blizzard / Thunder use other anims)
                    stp_d = kh1_resolve(S, struct.unpack_from("<I", raw, 0x6C)[0])
                    mpb = stp_d and kh1_abs(S, stp_d + 0x44, 4)
                    if mpb:
                        mp_d = struct.unpack("<i", mpb)[0]
                        if 0 <= mp_d < st.get("donald_mp", mp_d) and now - st.get("donald_cast_t", 0) > 0.8:
                            party_spell(S, pc, tuple(af.pos), "cure" if an == 73 else "attack")
                            st["donald_cast_t"] = now
                        st["donald_mp"] = mp_d
                if aid == 0:
                    st["donald_anim"] = an
        if not af.visible and now < st.get("kh_warping_until", 0):
            continue                                      # our own room reload: renderer v84 keeps their last frame
        S.dll.sr_set_actor(aid, ctypes.byref(af))
    # vanilla (user 2026-10-05): Donald and Goofy take hits, get KO'd and come back by KH1's own rules. The old
    # full-HP refill and Donald's free MP were made up -> only with config party_keep_full / party_mp_regen.
    php = S.status["party_hp"] = party_hp_read(S)
    if php != st.get("party_hp_last") and dbg_on(S, "dbg_party_hp_log"):                   # every change of Donald's / Goofy's HP: party_hp_log.txt
        st["party_hp_last"] = php
        log = st.setdefault("party_hp_log", [])
        log.append("%s %s %s" % (time.strftime("%H:%M:%S"), php, [S.dll.sr_actor_state(0), S.dll.sr_actor_state(1)]))
        del log[:-500]
        (HERE / "party_hp_log.txt").write_text(chr(10).join(log) + chr(10))
    if (S.CFG.get("party_keep_full", False) or S.CFG.get("party_mp_regen", False)) and now - st.get("party_hp_t", 0) > 0.5:
        st["party_hp_t"] = now
        for ent in party_addrs(S):
            if not ent:
                continue
            b = kh1_abs(S, ent + 0x6C, 4)
            stp = kh1_resolve(S, int.from_bytes(b, "little")) if b else 0
            hpb = stp and kh1_abs(S, stp + 0x3C, 8)
            if hpb:
                hp_, mx = struct.unpack("<2i", hpb)
                if 0 < mx < 5000 and hp_ < mx and S.CFG.get("party_keep_full", False):
                    ctypes.windll.kernel32.WriteProcessMemory(ctypes.c_void_p(S.kh["h"]), ctypes.c_void_p(stp + 0x3C),
                                                              struct.pack("<i", mx), 4, None)
                mpb = kh1_abs(S, stp + 0x44, 8)
                if mpb:
                    mp_, mmx = struct.unpack("<2i", mpb)
                    if 0 < mmx < 100 and mp_ < mmx and S.CFG.get("party_mp_regen", False) and now - st.get("party_mp_t", 0) > S.CFG.get("party_mp_regen_s", 3.0):
                        st["party_mp_t"] = now          # one MP bar back every few seconds: Donald keeps casting
                        ctypes.windll.kernel32.WriteProcessMemory(ctypes.c_void_p(S.kh["h"]), ctypes.c_void_p(stp + 0x44),
                                                                  struct.pack("<i", mp_ + 1), 4, None)
    st["party_smooth_t"] = now
    S.status["party"] = [S.dll.sr_actor_state(0), S.dll.sr_actor_state(1)]
    # party trace (party_trace.txt) while Borderlands enemies are paired with stand-ins
    if st.get("kh_proxied") and now - st.get("ptrace_t", 0) > 0.1 and dbg_on(S, "dbg_party_trace"):
        st["ptrace_t"] = now
        try:
            line = [f"{now % 1000:8.2f}"]
            foes = [a_ for a_, _ in st.get("kh_pfoes", [])]
            fpos = [struct.unpack("<3f", kh1_abs(S, a_ + 0x10, 12)) for a_ in foes[:6] if kh1_abs(S, a_ + 0x10, 12)]
            for name, ent in zip(("donald", "goofy"), party_addrs(S)):
                raw = kh1_abs(S, ent, 0x170) if ent else None
                if not raw:
                    continue
                pos = struct.unpack_from("<3f", raw, 0x10)
                d = min((math.dist(pos, q) for q in fpos), default=-1)
                stp = kh1_resolve(S, struct.unpack_from("<I", raw, 0x6C)[0])
                mp = struct.unpack("<i", kh1_abs(S, stp + 0x44, 4))[0] if stp else -1
                line.append(f"{name} st {raw[0x160]:3d} an {raw[0x164]:3d} near {d:6.0f} mp {mp}")
            tr = st.setdefault("party_trace", []); tr.append(" | ".join(line)); del tr[:-3000]
            if len(tr) % 20 == 0:
                (HERE / "party_trace.txt").write_text(chr(10).join(tr[-3000:]) + chr(10))
        except Exception as ex:  # noqa: BLE001
            S.status["ptrace_error"] = str(ex)


# ---------------------------------------------------------------- Heartless in Borderlands (renderer v21 enemy slots)
# "Mobs vs police": each Borderlands enemy near Sora keeps its body as an invisible hitbox (AI, collision, damage, loot)
# and wears a KH1 Heartless drawn by the renderer, posed from a live KH1 Heartless. A paired enemy (it has a stand-in
# fighting KH1 Sora) moves with its own stand-in's animation; the others borrow a Heartless of the same kind.
# The model is whatever KH1 has loaded in the room (entity +0x134 -> resource "xa_ex_NNNN.bd"), baked by
# tools/make_heartless.py into data/ex_NNNN_mesh.bin + _live.bin.
SKIN_MAX = 16


def kh1_model_id(S, ent: int) -> int:
    import re
    cache = S.state.setdefault("kh_model_ids", {})
    hb = kh1_abs(S, ent + 0x134, 4)
    h = int.from_bytes(hb, "little") if hb else 0
    if (ent, h) in cache:
        return cache[(ent, h)]
    mid = 0
    r = kh1_resolve(S, h) if h else 0
    d = r and kh1_abs(S, r + 0xF0, 0x40)
    if d:
        m = re.search(rb"ex_(\d{4})\.bd", d)
        mid = int(m.group(1)) if m else 0
    cache[(ent, h)] = mid
    return mid


def skin_model(S, mid: int) -> bool:
    """Loads a KH1 enemy model into the renderer once; False when it isn't baked."""
    ok = S.state.setdefault("skin_models", {})
    if mid not in ok:
        ok[mid] = mid > 0 and S.dll.sr_enemy_model(mid) > 0
        kh1_event(S, f"Heartless skin model {mid}: {'loaded' if ok[mid] else 'not baked'}")
    return ok[mid]


def bl_hide(p, on: bool) -> None:
    for a in (p, getattr(p, "Weapon", None)):
        try:
            if a is not None:
                a.SetHidden(on)
        except Exception:  # noqa: BLE001
            pass


def skin_on(S) -> bool:
    return bool(S.CFG.get("kh_skins", False)) and hasattr(S.dll, "sr_set_enemy") and kh1_combat_on(S)


def skin_off(S) -> None:
    for key, p in list(S.state.get("skin_hidden", {}).items()):
        if bl_alive(p):
            bl_hide(p, False)
    S.state["skin_hidden"] = {}


def skin_tick(S, pc, now: float) -> None:
    st, CFG, f = S.state, S.CFG, S.frame
    if not skin_on(S):
        if st.get("skin_hidden"):
            skin_off(S)
        return
    dll = S.dll
    if not st.get("skin_argtypes"):
        dll.sr_set_enemy.argtypes = [ctypes.c_int, ctypes.c_uint64, ctypes.c_int, ctypes.c_void_p]
        st["skin_argtypes"] = True
    # KH1 Heartless by kind (model id) -> entities; paired ones by Borderlands enemy
    kinds = {}
    for a, _stp in st.get("kh_pfoes", []):
        mid = kh1_model_id(S, a)
        if mid and skin_model(S, mid):
            kinds.setdefault(mid, []).append(a)
    pair = {k: a for a, k in st.get("kh_pmap", {}).items()}
    skins = st.setdefault("skin_of", {})            # Borderlands enemy -> model id (sticky)
    hidden = st.setdefault("skin_hidden", {})
    me = f.pos
    near = []
    if kinds and now >= st.get("kh_warping_until", 0):
        for p in all_enemies():
            try:
                if p is pc.Pawn or not bl_alive(p):
                    continue
                L = p.Location
                d = math.hypot(float(L.X) - me[0], float(L.Y) - me[1])
                if d < CFG.get("skin_range", 6000.0) and abs(float(L.Z) - me[2]) < 2000:
                    near.append((d, p))
            except Exception:  # noqa: BLE001
                continue
        near.sort(key=lambda e: e[0])
    shown = {}
    order = sorted(kinds)
    for slot, (_d, p) in enumerate(near[:SKIN_MAX]):
        key = bl_key(p)
        ent = pair.get(key)
        mid = skins.get(key)
        if mid not in kinds:
            mid = kh1_model_id(S, ent) if ent else 0
            if mid not in kinds:
                mid = order[sum(map(ord, key)) % len(order)]
            skins[key] = mid
        if not ent or kh1_model_id(S, ent) != mid:
            pool = kinds[mid]
            ent = pool[sum(map(ord, key)) % len(pool)]
        try:
            L = p.Location
            ch = float(p.CylinderComponent.CollisionHeight) if p.CylinderComponent else 0.0
            yaw = float(p.Rotation.Yaw) * math.tau / 65536.0
        except Exception:  # noqa: BLE001
            continue
        af = ActorFrame()
        af.pos[:] = (float(L.X), float(L.Y), float(L.Z) - ch + CFG.get("skin_dz", 0.0))
        af.yaw, af.scale, af.visible = yaw - math.radians(CFG["yaw_offset_deg"]) + math.radians(CFG.get("skin_yaw_deg", 0.0)), \
            f.scale * CFG.get("skin_scale", 1.0), 1
        dll.sr_set_enemy(slot, ent, mid, ctypes.byref(af))
        shown[key] = (p, af.pos[:])
        if key not in hidden:
            bl_hide(p, True)
            hidden[key] = p
    for slot in range(len(shown), SKIN_MAX):
        if st.get("skin_slots", 0) > slot:
            af = ActorFrame()
            dll.sr_set_enemy(slot, 0, 0, ctypes.byref(af))
    st["skin_slots"] = len(shown)
    # enemies that were wearing a skin: dead -> KH1 Heartless defeat (dark burst, a heart floats up); left -> body back
    last = st.get("skin_last", {})
    for key, (p, pos) in last.items():
        if key in shown:
            continue
        if not bl_alive(p) and not S.CFG.get("fx_invented", False):
            pass                                          # vanilla audit: the purple death smoke was made up (off)
        elif not bl_alive(p):
            c = [pos[0], pos[1], pos[2] + 50]
            fx_smoke(S, c, color=(0.18, 0.08, 0.28), n=7, size=110, life=0.8, rise=90)
            fx_flash(S, c, (0.6, 0.3, 1.0), size=260, life=0.25)
            _part(S, pos=c, vel=[0.0, 0.0, 120.0], size=55, color=(1.0, 0.35, 0.55), life=1.4, spr="soft", fade_pow=1.5)
            if hasattr(S, "sfx"):
                pass
        elif key in hidden:
            bl_hide(p, False)
            hidden.pop(key, None)
    for key in [k for k, p in hidden.items() if k not in shown and not bl_alive(p)]:
        hidden.pop(key, None)
    st["skin_last"] = shown
    S.status["skins"] = {"kinds": {str(k): len(v) for k, v in kinds.items()}, "shown": len(shown),
                         "states": [dll.sr_enemy_state(i) for i in range(len(shown))]}



def free_on(S) -> bool:
    return bool(S.CFG.get("kh_free_heartless", True)) and not S.state.get("hl_off", True) and hasattr(S.dll, "sr_set_enemy") \
        and kh1_combat_on(S)


def hl_toggle_tick(S, now: float) -> None:
    """F10 (Borderlands in front): free Heartless on / off. Off: they vanish in their defeat burst."""
    st = S.state
    vk = S.CFG.get("hl_toggle_vk", 0x79)
    down = bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)
    was, st["hl_key"] = st.get("hl_key", False), down
    if not down or was:
        return
    pid = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(ctypes.windll.user32.GetForegroundWindow(), ctypes.byref(pid))
    if pid.value != os.getpid():
        return
    st["hl_off"] = not st.get("hl_off", True)
    if st["hl_off"]:
        for hb in st.get("free_bl", {}).values():
            if not hb.get("dead_until") and S.CFG.get("fx_invented", False):   # made-up vanish smoke (off)
                c = [hb["pos"][0], hb["pos"][1], hb["z"] + 45]
                fx_smoke(S, c, color=(0.18, 0.08, 0.28), n=6, size=110, life=0.8, rise=90)
        st["free_bl"], st["free_smooth"] = {}, {}
    sfx(S, "swing", 0.5)
    kh1_event(S, f"Heartless {'off' if st['hl_off'] else 'on'} (F10)")
    S.status["heartless_on"] = not st["hl_off"]


def _kh1_heal_now(S, pawn, frac: float) -> None:
    """A KH1 heal (Cure, Potion, HP orb) on KH1 Sora: the same share of Borderlands health - except while Borderlands
    has him down (Fight For Your Life): only Borderlands' Second Wind gets him up."""
    try:
        if str(pawn.GetStateName()).lower().startswith("injured"):
            S.status["kh1_heal"] = "skipped (injured)"
            return
    except Exception:  # noqa: BLE001
        pass
    _heal_now(S, pawn, frac)


def _hurt_player_now(S, pawn, frac: float) -> None:
    """A free Heartless hit KH1 Sora: the same share of Borderlands health (never below 1 here)."""
    try:
        pool = pawn.HealthPool.Data
        mx = float(pool.GetMaxValue()) if hasattr(pool, "GetMaxValue") else float(pawn.HealthMaxVar)
        pool.SetCurrentValue(max(1.0, float(pool.CurrentValue) - mx * frac))
    except Exception as ex:  # noqa: BLE001
        S.status["hurt_player_error"] = str(ex)


def heartless_tick(S, pc, now: float) -> None:
    """Free Heartless drawn where they are relative to KH1 Sora, around Borderlands Sora (as Donald and Goofy)."""
    import struct
    st, CFG, f, dll = S.state, S.CFG, S.frame, S.dll
    st["pawn_ref"] = pc.Pawn
    free = st.get("kh_free", [])
    first = st.get("skin_slots", 0)
    used_slots = 0
    ks = kh1_state(S) if free_on(S) else None
    last = st.setdefault("free_last", {})        # entity -> (BL pos, hp)
    if ks:
        if not st.get("skin_argtypes"):
            dll.sr_set_enemy.argtypes = [ctypes.c_int, ctypes.c_uint64, ctypes.c_int, ctypes.c_void_p]
            st["skin_argtypes"] = True
        sign = CFG.get("kh_turn_sign", -1.0)
        fx, fz = ks["fwd"]
        yb = (st.get("kh_yaw_target") if st.get("kh_yaw_target") is not None else st["yaw"]) + math.radians(CFG["yaw_offset_deg"])
        sc = f.scale
        sm = st.setdefault("free_smooth", {})
        fbl = st.setdefault("free_bl", {})
        # KH1's spawner keeps replacing Heartless (far ones despawn, new ones appear = new actors). A Borderlands
        # Heartless is a slot: when its KH1 actor is gone it moves to a new one and keeps its spot, HP and timer.
        orphans = [e for e in fbl if e not in free]
        fresh = [e for e in free if e not in fbl]
        for old, new in zip(orphans, fresh):
            hb = fbl.pop(old)
            hb["kh"] = None
            hb.pop("held", None)
            fbl[new] = hb
            if old in sm:
                sm[new] = sm.pop(old)
            st["free_rebinds"] = st.get("free_rebinds", 0) + 1
        for e in [e for e in fbl if e not in free]:
            fbl.pop(e, None)
            sm.pop(e, None)
        dtp = min(0.1, max(0.0, now - st.get("free_smooth_t", now)))
        st["free_smooth_t"] = now
        for ent in free:
            if first + used_slots >= SKIN_MAX:
                break
            raw = kh1_abs(S, ent, 0x380)
            mid = kh1_model_id(S, ent)
            if not raw or not mid or not skin_model(S, mid) or not struct.unpack_from("<I", raw, 0x374)[0] & 1:
                continue
            pos = struct.unpack_from("<3f", raw, 0x10)
            m = struct.unpack_from("<16f", raw, 0x230)
            # Each free Heartless lives at its own Borderlands position. Only its own KH1 movement (AI: chase, lunge,
            # knockback) moves it there; then its KH1 body is put back where that Borderlands spot is relative to
            # KH1 Sora - so Sora walking (or KH1 Sora being recentered) never drags it along.
            hb = fbl.get(ent)
            if hb is None:
                rx, rz = pos[0] - ks["pos"][0], pos[2] - ks["pos"][2]
                fwd_k, side_k = rx * fx + rz * fz, -rx * fz + rz * fx
                hb = fbl[ent] = {"pos": [f.pos[0] + (math.cos(yb) * fwd_k - math.sin(yb) * side_k * sign) * sc,
                                         f.pos[1] + (math.sin(yb) * fwd_k + math.cos(yb) * side_k * sign) * sc],
                                 "z": st.get("party_ground_feet", f.pos[2]), "kh": pos}
            elif hb["kh"] is not None:
                dx, dz = pos[0] - hb["kh"][0], pos[2] - hb["kh"][2]
                if math.hypot(dx, dz) < CFG.get("free_step_max", 250.0):
                    fwd_k, side_k = dx * fx + dz * fz, -dx * fz + dz * fx
                    hb["pos"][0] += (math.cos(yb) * fwd_k - math.sin(yb) * side_k * sign) * sc
                    hb["pos"][1] += (math.sin(yb) * fwd_k + math.cos(yb) * side_k * sign) * sc
            if hb.get("dead_until") or now < st.get("kh_menu_park_until", 0):
                if now < hb.get("dead_until", 0) or now < st.get("kh_menu_park_until", 0):   # dead, or KH1 menu open
                    park = (ks["pos"][0] + 400.0, ks["pos"][1] - 1500.0, ks["pos"][2] + 400.0)
                    ctypes.windll.kernel32.WriteProcessMemory(ctypes.c_void_p(S.kh["h"]), ctypes.c_void_p(ent + 0x10),
                                                              struct.pack("<3f", *park), 12, None)
                    hb["kh"] = park
                    continue
                # back: rises out of the dark somewhere around Sora
                ang_ = random.uniform(0, math.tau)
                r_ = random.uniform(CFG.get("free_spawn_min", 800.0), CFG.get("free_spawn_max", 1400.0))
                hb["pos"] = [f.pos[0] + math.cos(ang_) * r_, f.pos[1] + math.sin(ang_) * r_]
                hb["z"] = st.get("party_ground_feet", f.pos[2])
                hb["dead_until"] = 0
                sm.pop(ent, None)
                c = [hb["pos"][0], hb["pos"][1], hb["z"] + 40]
                fx_smoke(S, c, color=(0.12, 0.05, 0.2), n=8, size=120, life=1.0, rise=40)
            # leash: never left far behind
            rx, ry = hb["pos"][0] - f.pos[0], hb["pos"][1] - f.pos[1]
            dist_b = math.hypot(rx, ry)
            leash = CFG.get("free_leash", 2200.0)
            if dist_b > leash:
                hb["pos"][0], hb["pos"][1] = f.pos[0] + rx * leash / dist_b, f.pos[1] + ry * leash / dist_b
                rx, ry = hb["pos"][0] - f.pos[0], hb["pos"][1] - f.pos[1]
                hb["z"] = st.get("party_ground_feet", f.pos[2])
            # KH1 spot for that Borderlands spot
            fwd_b = rx * math.cos(yb) + ry * math.sin(yb)
            side_b = -rx * math.sin(yb) + ry * math.cos(yb)
            fwd_k, side_k = fwd_b / sc, side_b / sc * sign
            tgt = (ks["pos"][0] + fwd_k * fx - side_k * fz, pos[1], ks["pos"][2] + fwd_k * fz + side_k * fx)
            mf_free = mapfeed_active(S) and st.get("mf_xf") is not None
            if mf_free:
                # MAP FEED: its KH1 spot through the feed's mapping, so the ground under it in KH1 is the BL ground
                # under its Borderlands spot (KH1 height kept: it walks / jumps on that ground itself)
                Fx_, Fy_, Kx_, Kz_, m00_, m01_, m10_, m11_ = st["mf_xf"][:8]
                det_ = m00_ * m11_ - m01_ * m10_
                if abs(det_) > 1e-9:
                    dx_, dy_ = hb["pos"][0] - Fx_, hb["pos"][1] - Fy_
                    tgt = (Kx_ + (m11_ * dx_ - m01_ * dy_) / det_, pos[1], Kz_ + (-m10_ * dx_ + m00_ * dy_) / det_)
            pulled = ent == st.get("kh_abil_pull_ent") and st.get("kh_abil") and now < st.get("kh_abil_until", 0)
            if not pulled and math.hypot(tgt[0] - pos[0], tgt[2] - pos[2]) > 3.0:   # (ability target: left in front of Sora)
                ctypes.windll.kernel32.WriteProcessMemory(ctypes.c_void_p(S.kh["h"]), ctypes.c_void_p(ent + 0x10),
                                                          struct.pack("<3f", *tgt), 12, None)
            hb["kh"] = tgt
            lift = (pos[1] - st["kh_floor_ref"]) * sc if st.get("kh_ground_active") and st.get("kh_floor_ref") is not None else 0.0
            want = (hb["pos"][0], hb["pos"][1], hb["z"] + max(-60.0, lift))
            if mf_free:                                     # its real KH1 height over the fed ground
                want = (hb["pos"][0], hb["pos"][1], mapfeed_to_bl(S, pos)[2])
            prev = sm.get(ent)
            if prev is None or math.dist(prev, want) > 400.0:
                cur = want
            else:
                k = min(1.0, CFG.get("party_smooth", 18.0) * dtp)
                cur = tuple(prev[i] + (want[i] - prev[i]) * k for i in range(3))
            sm[ent] = cur
            af = ActorFrame()
            af.pos[:] = cur
            pf = math.degrees(math.atan2(-m[10], -m[8]))
            d = ((pf - kh1_facing_deg(ks) + 180) % 360) - 180
            af.yaw, af.scale, af.visible = st["yaw"] + sign * math.radians(d), sc, 1
            dll.sr_set_enemy(first + used_slots, ent, mid, ctypes.byref(af))
            used_slots += 1
            # HP lives here, not in KH1 (held at 999 there so KH1 never kills it - a KH1 death removes the actor and
            # only re-entering the room brought Heartless back). 999 - KH1 HP = damage from a hit that just landed.
            stp = kh1_resolve(S, struct.unpack_from("<I", raw, 0x6C)[0])
            hpb = stp and kh1_abs(S, stp + 0x3C, 8)
            if hpb:
                hp_, mx_ = struct.unpack("<2i", hpb)
                if "hp" not in hb and 0 < mx_ < 5000:
                    hb["mx"] = hb["hp"] = mx_ * CFG.get("free_hp_mult", 1.0)
                if hp_ != 999:
                    if 0 <= hp_ < 999 and "hp" in hb and (hp_ < mx_ or hb.get("held")):
                        hb["hp"] -= (999 - hp_) if hb.get("held") else (mx_ - hp_)
                        c = [cur[0], cur[1], cur[2] + 45]
                        if invented_ok(S): _part(S, pos=c, size=90, color=(1.0, 0.95, 0.8), life=0.13, spr="burst", rot=random.uniform(0, 6.3))
                        st["free_hits"] = st.get("free_hits", 0) + 1
                        sfx(S, f"h{mid:04d}b", S.CFG.get("sfx_heartless", 0.8))   # its own KH1 hurt sound
                    ctypes.windll.kernel32.WriteProcessMemory(ctypes.c_void_p(S.kh["h"]), ctypes.c_void_p(stp + 0x3C),
                                                              struct.pack("<i", 999), 4, None)
                    hb["held"] = True
                else:
                    hb["held"] = True
                if hb.get("hp", 1) <= 0:          # defeated: dark burst, a heart floats up; back in a few seconds
                    c = [cur[0], cur[1], cur[2] + 45]
                    fx_smoke(S, c, color=(0.18, 0.08, 0.28), n=7, size=110, life=0.8, rise=90)
                    fx_flash(S, c, (0.6, 0.3, 1.0), size=260, life=0.25)
                    _part(S, pos=c, vel=[0.0, 0.0, 120.0], size=55, color=(1.0, 0.35, 0.55), life=1.4, spr="soft", fade_pow=1.5)
                    st["free_kills"] = st.get("free_kills", 0) + 1
                    sfx(S, f"h{mid:04d}c", S.CFG.get("sfx_heartless", 0.8))      # its KH1 defeat sound
                    hb["hp"] = hb["mx"]
                    hb["dead_until"] = now + CFG.get("free_respawn_s", 4.0)
                    dll.sr_set_enemy(first + used_slots - 1, 0, 0, ctypes.byref(ActorFrame()))
            last[ent] = (cur, 0)
    # gone (defeated in KH1, or no longer free): KH1 Heartless defeat - dark burst, a heart floats up
    for ent in [e for e in last if e not in free]:
        pos, hp_ = last.pop(ent)
        if False:
            c = [pos[0], pos[1], pos[2] + 45]
            fx_smoke(S, c, color=(0.18, 0.08, 0.28), n=7, size=110, life=0.8, rise=90)
            fx_flash(S, c, (0.6, 0.3, 1.0), size=260, life=0.25)
            _part(S, pos=c, vel=[0.0, 0.0, 120.0], size=55, color=(1.0, 0.35, 0.55), life=1.4, spr="soft", fade_pow=1.5)
            st["free_kills"] = st.get("free_kills", 0) + 1
    for slot in range(first + used_slots, first + st.get("free_slots", 0)):
        if slot < SKIN_MAX:
            dll.sr_set_enemy(slot, 0, 0, ctypes.byref(ActorFrame()))
    st["free_slots"] = used_slots
    S.status["free_heartless"] = {"n": len(free), "drawn": used_slots, "hits": st.get("free_hits", 0),
                                  "rebinds": st.get("free_rebinds", 0),
                                  "kills": st.get("free_kills", 0),
                                  "states": [dll.sr_enemy_state(first + i) for i in range(used_slots)] if used_slots else []}

# ================================================================ command menu, MP, magic
MENU = ["Attack", "Magic", "Abilities", "Items"]
SPELLS = [("Fire", 10), ("Blizzard", 12), ("Thunder", 18), ("Cure", 30)]
ITEMS = [("Potion", 0), ("Ether", 0)]
MP_MAX = 100.0


def kh1_ui_native(S) -> bool:
    """KH1's own HUD / command menu is drawn natively (renderer v52 khui): the mod's copies and its menu input stand
    aside. Holds for 3 s after the last frame KH1 drew its interface (one empty frame made the old menu flash in)."""
    ui = S.status.get("kh1_ui") or {}
    if not ui.get("on"):
        return False
    now = time.time()
    if ui.get("tris", 0) > 0:
        S.state["kui_seen"] = now
    return now - S.state.get("kui_seen", 0) < 3.0


def menu_state(S):
    st = S.state
    if "menu" not in st:
        st["menu"] = {"level": 0, "cur": 0, "sub": 0}
        st["mp"] = MP_MAX
        st["items"] = {"Potion": 5, "Ether": 3}
        st["proj"], st["parts"], st["msg"] = [], [], ("", 0.0)
    return st["menu"]


def menu_move(S, d: int) -> None:
    if S.CFG.get("kh1_ui_native", True):
        if kh1_combat_on(S):                              # KH1's real command menu: its own d-pad
            kh1_press(S, PAD_DOWN if d > 0 else PAD_UP, 0.07)
        return                                            # no KH1: nothing (user: no fake menu after a KH1 crash)
    m = menu_state(S)
    if m["level"] == 0:
        m["cur"] = (m["cur"] + d) % len(MENU)
    else:
        n = {1: len(spell_list(S)) or 1, 2: len(ITEMS), 3: len(ability_list(S)) or 1}[m["level"]]
        m["sub"] = (m["sub"] + d) % n


def menu_back(S) -> None:
    if S.CFG.get("kh1_ui_native", True):
        # KH1: Circle backs out of a submenu but JUMPS at the top level (user session 9: Q with no submenu open made
        # Sora hop ~an inch and glitch) -> only while KH1 has a submenu open (KH_SUBMENU_PTR)
        if kh1_combat_on(S) and kh1_submenu_open(S):
            kh1_press(S, PAD_CIRCLE, 0.07)
        return
    menu_state(S)["level"] = 0


def say(S, text: str) -> None:
    S.state["msg"] = (text, time.time())


def kh_confirm(S, pc) -> None:
    if S.CFG.get("kh1_ui_native", True) and not kh1_combat_on(S):
        return                                            # no KH1: no fake attacks / spells (true passthrough only)
    if S.CFG.get("kh1_ui_native", True) and kh1_combat_on(S):                                  # KH1's real command menu: Cross on whatever it shows
        kh1_press(S, PAD_CROSS)                           # (Attack swings, Magic opens, a spell casts - KH1 checks MP)
        face_target(S, pc)
        return
    m = menu_state(S)
    if m["level"] == 0:
        choice = MENU[m["cur"]]
        if choice == "Attack":
            kh_attack(S, pc)
        elif choice == "Magic":
            m["level"], m["sub"] = 1, m.get("last_spell", 0)
        elif choice == "Abilities":
            m["level"], m["sub"] = 3, m.get("last_ability", 0)
        else:
            m["level"], m["sub"] = 2, 0
        return
    if m["level"] == 3:
        abil = ability_list(S)
        if abil:
            m["last_ability"] = m["sub"]
            kh1_ability(S, pc, abil[m["sub"] % len(abil)])
        return
    if m["level"] == 1:
        m["last_spell"] = m["sub"]
        if kh1_combat_on(S):
            kh1_cast(S, pc, m["sub"])
            return
        name, cost = SPELLS[m["sub"]]
        cast(S, pc, name, cost)
        return
    name, _ = ITEMS[m["sub"]]
    if S.state["items"].get(name, 0) <= 0:
        say(S, f"No {name}s left")
        return
    S.state["items"][name] -= 1
    pawn = pc.Pawn
    if name == "Potion":
        heal(S, pawn, 0.35)
    else:
        S.state["mp"] = min(MP_MAX, S.state["mp"] + 40)
        if kh1_combat_on(S):
            kh1_add_mp(S, 99)
        sparkle(S, pawn, (0.3, 0.5, 1.0))
    say(S, f"Used {name}")


def cast(S, pc, name: str = "Fire", cost: int = 10) -> None:
    a = S.state.get("action")
    if a and a["kind"] == "cast":
        return
    if S.state["mp"] < cost:
        say(S, "Not enough MP")
        return
    S.state["mp"] -= cost
    face_target(S, pc)
    motion = S.CFG["spell_motions"].get(name, 61)
    start_action(S, "cast", motion, S.CFG.get("cast_speed", 1.25), spell=name)


# ---------------------------------------------------------------- spells
def aim_target(S, pc, reach=2000.0, cone=35.0):
    lk = lock_target(S)
    if lk is not None:
        return lk
    try:
        cam_yaw = int(pc.Rotation.Yaw) * UU
        for dist, p, bearing in all_targets(S, pc, reach):
            if abs(ang(bearing - cam_yaw)) < math.radians(cone):
                return p
    except Exception:  # noqa: BLE001
        pass
    return None


def hand_pos(S, pc):
    p = vec3(pc.Pawn.Location)
    y = S.state["yaw"] or 0.0
    return [p[0] + math.cos(y) * 55, p[1] + math.sin(y) * 55, p[2] + 20]


def blocked(pc, a, b) -> bool:
    """Level-geometry traces return nothing through the SDK in BL1E (FastTrace/Trace tested), so this is off."""
    return False
    try:
        clear = pc.Pawn.FastTrace(unrealsdk.make_struct("Vector", X=b[0], Y=b[1], Z=b[2]),
                                  unrealsdk.make_struct("Vector", X=a[0], Y=a[1], Z=a[2]))
        return not clear
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- KH1 effect sprites (sora_fx.bin, renderer v10)
# KH1's own effect art from texcommon.dpd (passthrough/make_fx_bin.py): tex 0 = particle atlas (64 px cells),
# 1 = flame flipbook (4x4), 2 = smoke puffs (4x4), 3 = hit spark. Greyscale sprites tinted per particle, like KH1.
def _cell(cx, cy, w=1, h=1, size=512.0, px=64.0):
    return (cx * px / size, cy * px / size, (cx + w) * px / size, (cy + h) * px / size)


SPR = {   # name -> (texture, uv rect)
    "pin": (0, _cell(0, 0)), "soft": (0, _cell(1, 0)), "shards": (0, _cell(3, 0)), "orb": (0, _cell(4, 0, 2, 2)),
    "bigglow": (0, _cell(6, 0, 2, 2)), "diamond": (0, _cell(0, 1)), "star": (0, _cell(1, 1)), "spike": (0, _cell(2, 1)),
    "sphere": (0, _cell(3, 1)), "cross": (0, _cell(0, 2)), "comet": (0, _cell(1, 2)), "bubble": (0, _cell(3, 2)),
    "ray8": (0, _cell(4, 2)), "hex": (0, _cell(5, 2)), "bolt1": (0, _cell(6, 2)), "bolt2": (0, _cell(7, 2)),
    "burst": (3, (0.0, 0.0, 0.62, 1.0)),
}
FLIP = {   # flipbooks: name -> (texture, columns, frames, cell uv size, first row)
    "fire": (1, 4, 13, 0.25, 0),
    "smoke": (0, 8, 24, 0.125, 3),
    "puff": (2, 4, 16, 0.25, 0),
}


def sprite_uv(pa, age):
    """(tex, uv) for a particle: a fixed sprite ("spr") or a flipbook frame ("flip", looping with "fps" or spread
    over the particle's life)."""
    if "tex" in pa:                                   # explicit texture + uv rect (KH1 prize orbs)
        return pa["tex"], pa["uv"]
    fl = pa.get("flip")
    if fl:
        tex, cols, nfr, cs, row0 = FLIP[fl]
        if pa.get("fps"):
            k = int(age * pa["fps"] + pa.get("frame0", 0)) % nfr
        else:
            k = min(nfr - 1, int(age / max(pa["life"], 1e-3) * nfr))
        cx, cy = k % cols, row0 + k // cols
        return tex, (cx * cs, cy * cs, (cx + 1) * cs, (cy + 1) * cs)
    spr = pa.get("spr")
    if spr in SPR:
        return SPR[spr]
    return -1, (0.0, 0.0, 1.0, 1.0)


def burst(S, pos, color, n=14, speed=260.0, size=40.0, life=0.5):
    for _ in range(n):
        d = [random.uniform(-1, 1) for _ in range(3)]
        _part(S, pos=list(pos), vel=[x * speed for x in d], size=size * random.uniform(0.6, 1.2), color=color,
              life=life * random.uniform(0.7, 1.2), spr=random.choice(("cross", "pin", "star")), rot=random.uniform(0, 6.3),
              spin=random.uniform(-4, 4), drag=2.0)


def sparkle(S, pawn, color, n=24):
    if not S.CFG.get("fx_invented", False):          # vanilla audit: invented sparkle column
        return
    base = vec3(pawn.Location)
    for k in range(n):
        a = random.uniform(0, 2 * math.pi)
        r = random.uniform(25, 55)
        _part(S, pos=[base[0] + math.cos(a) * r, base[1] + math.sin(a) * r, base[2] - 70 + random.uniform(0, 40)],
              vel=[0, 0, random.uniform(90, 180)], size=random.uniform(14, 26), color=color, t0=time.time() + k * 0.03,
              life=1.0, spr=random.choice(("cross", "star")), rot=random.uniform(0, 6.3), spin=random.uniform(-3, 3))


def fx_centre(S, c):
    """Orbit centre: 'sora' (follows Sora's feet), an enemy object, or a fixed point."""
    if c == "sora":
        f = S.frame
        return [f.pos[0], f.pos[1], f.pos[2]]
    if isinstance(c, (list, tuple)):
        return c
    try:
        loc = c.Location
        return [float(loc.X), float(loc.Y), float(loc.Z) - 40.0]
    except Exception:  # noqa: BLE001
        return None


def _part(S, **k):
    # every particle the mod makes up (sparks, flashes, smoke, death bursts...) is off while KH1's real effects reach
    # Borderlands (vanilla audit; config fx_invented true brings them back)
    if S.state.get("fx_mute") or not invented_ok(S):
        return
    k.setdefault("t0", time.time()); k.setdefault("vel", [0.0, 0.0, 0.0])
    S.state["parts"].append(k)


def fx_flash(S, pos, color, size=200.0, life=0.18):
    _part(S, pos=list(pos), size=size, color=color, life=life, grow=1.6, fade_pow=2.0, spr="bigglow")


def fx_smoke(S, pos, color=(0.35, 0.33, 0.32), n=4, size=90.0, life=0.9, rise=60.0):
    for _ in range(n):
        _part(S, pos=[c + random.uniform(-25, 25) for c in pos], vel=[random.uniform(-40, 40), random.uniform(-40, 40), rise],
              size=size * random.uniform(0.8, 1.3), grow=1.8, color=color, life=life * random.uniform(0.8, 1.2), drag=1.5,
              flip="smoke", mode=1, alpha=0.7, rot=random.uniform(0, 6.3), spin=random.uniform(-1, 1))


def fx_impact(S, pr, small=False):
    pos = pr["pos"]
    if pr["kind"] == "fire":                      # KH1 Fire: flash, a ball of flames bursting outward, then smoke
        fx_flash(S, pos, (1.0, 0.6, 0.25), 200 if small else 340)
        for _ in range(5 if small else 12):
            d = [random.uniform(-1, 1) for _ in range(3)]
            _part(S, pos=list(pos), vel=[x * random.uniform(120, 320) for x in d], drag=3.5, grav=140.0,
                  size=random.uniform(60, 110), grow=0.6, color=(1.0, 0.85, 0.6), life=random.uniform(0.35, 0.6),
                  flip="fire", rot=random.uniform(0, 6.3), spin=random.uniform(-2, 2))
        fx_smoke(S, pos, n=2 if small else 4)
    elif pr["kind"] == "ice":                     # KH1 Blizzard: ice shards shatter outward, glints, cold mist
        fx_flash(S, pos, (0.7, 0.9, 1.0), 150 if small else 240)
        for _ in range(5 if small else 12):
            d = [random.uniform(-1, 1), random.uniform(-1, 1), random.uniform(0.2, 1)]
            _part(S, pos=list(pos), vel=[x * random.uniform(200, 420) for x in d], grav=-900.0, drag=1.0,
                  size=random.uniform(20, 40), color=random.choice(((0.75, 0.9, 1.0), (0.95, 1.0, 1.0))),
                  life=random.uniform(0.45, 0.75), spr=random.choice(("diamond", "shards")), rot=random.uniform(0, 6.3),
                  spin=random.uniform(-8, 8))
        for _ in range(4):
            _part(S, pos=[c + random.uniform(-40, 40) for c in pos], size=random.uniform(30, 50), color=(0.9, 0.97, 1.0),
                  life=0.4, spr="cross", rot=random.uniform(0, 1.6), fade_in=0.05)
        fx_smoke(S, pos, color=(0.8, 0.9, 1.0), n=2 if small else 3, size=70, life=0.7, rise=20)
    else:
        burst(S, pos, pr["color"], n=8 if small else 14)


def fx_thunder_hit(S, pos):
    """KH1 Thunder at the strike point: an electric orb flash, a starburst and sparks skittering on the ground."""
    fx_flash(S, pos, (1.0, 1.0, 0.75), 200, 0.2)
    _part(S, pos=[pos[0], pos[1], pos[2] + 40], size=150, color=(1.0, 0.95, 0.6), life=0.35, spr="orb", grow=1.4, fade_pow=1.5)
    _part(S, pos=[pos[0], pos[1], pos[2] + 40], size=220, color=(1.0, 1.0, 0.8), life=0.2, spr="ray8", rot=random.uniform(0, 1))
    for _ in range(14):
        a = random.uniform(0, 2 * math.pi)
        sp = random.uniform(250, 550)
        _part(S, pos=list(pos), vel=[math.cos(a) * sp, math.sin(a) * sp, random.uniform(50, 250)], grav=-700.0, drag=2.5,
              size=random.uniform(16, 30), color=(1.0, 0.95, 0.55), life=random.uniform(0.25, 0.45), spr="cross",
              rot=random.uniform(0, 1.6))


def fx_cure(S):
    """KH1 Cure: a green-gold bloom, then stars and sparkles spiralling up around Sora."""
    now = time.time()
    _part(S, pos=[S.frame.pos[0], S.frame.pos[1], S.frame.pos[2] + 90], size=260, color=(0.6, 1.0, 0.5), life=0.7,
          grow=1.8, fade_in=0.15, spr="bigglow")
    for k in range(32):
        _part(S, pos=[0, 0, 0], orbit=("sora", random.uniform(35, 70), random.choice((-1, 1)) * random.uniform(3, 6),
                                       random.uniform(110, 190), random.uniform(0, 2 * math.pi), random.uniform(-10, 40)),
              size=random.uniform(16, 30), color=random.choice(((0.45, 1.0, 0.5), (0.95, 1.0, 0.55), (0.7, 1.0, 0.8))),
              t0=now + k * 0.025, life=1.1, fade_in=0.1, spr=random.choice(("star", "cross")), spin=random.uniform(-4, 4))


def fx_gravity(S, centre):
    """KH1 Gravity: a dark violet orb gathers over the target and slams down into it."""
    now = time.time()
    top = [centre[0], centre[1], centre[2] + 160]
    for _ in range(18):                           # particles rush inward to the sphere
        d = [random.uniform(-1, 1) for _ in range(3)]
        n = math.sqrt(sum(x * x for x in d)) or 1
        d = [x / n for x in d]
        r = random.uniform(180, 260)
        _part(S, pos=[top[i] + d[i] * r for i in range(3)], vel=[-d[i] * r / 0.35 for i in range(3)], size=random.uniform(30, 50),
              color=random.choice(((0.55, 0.2, 1.0), (0.35, 0.1, 0.8))), life=0.35, fade_in=0.08, spr="soft")
    _part(S, pos=list(top), size=200, color=(0.5, 0.2, 1.0), life=0.5, grow=0.5, fade_in=0.2, t0=now + 0.1, spr="orb", spin=3.0)
    _part(S, pos=list(top), vel=[0, 0, -900], size=190, color=(0.6, 0.3, 1.0), life=0.22, grow=1.4, t0=now + 0.45, spr="sphere")
    for _ in range(14):                           # crush ring at the target
        a = random.uniform(0, 2 * math.pi)
        _part(S, pos=list(centre), vel=[math.cos(a) * 420, math.sin(a) * 420, 20], drag=4.0, size=random.uniform(40, 60),
              color=(0.55, 0.25, 1.0), life=0.5, t0=now + 0.62, flip="smoke", mode=1, alpha=0.6)


def fx_stop(S, enemy, duration: float):
    """KH1 Stop: a slow ring of pale blue-grey glints around each frozen enemy for as long as it lasts."""
    now = time.time()
    for k in range(10):
        _part(S, pos=[0, 0, 0], orbit=(enemy, 80.0, 0.9, 0.0, k * 2 * math.pi / 10, 60.0), size=26, color=(0.7, 0.8, 1.0),
              life=duration, fade_in=0.3, fade_pow=0.15, t0=now, spr="cross")
    fx_flash(S, fx_centre(S, enemy) or [0, 0, 0], (0.75, 0.85, 1.0), 240, 0.3)


def fx_aero(S, duration: float):
    """KH1 Aero: wind circling Sora in three bands (comet streaks pointing along the wind) inside a faint shell."""
    now = time.time()
    S.state["aero_until"] = now + duration
    # translucent green bubble around him (the shell) - invented, off unless fx_invented (vanilla audit)
    if S.CFG.get("fx_invented", False):
      _part(S, pos=[0, 0, 0], orbit=("sora", 0.0, 0.0, 0.0, 0.0, 85.0), size=230, color=(0.55, 1.0, 0.7), life=duration,
          fade_in=0.3, fade_pow=0.2, alpha=0.3, t0=now, spr="bubble", spin=0.6)
      _part(S, pos=[0, 0, 0], orbit=("sora", 0.0, 0.0, 0.0, 0.0, 85.0), size=260, color=(0.5, 1.0, 0.7), life=duration,
          fade_in=0.3, fade_pow=0.2, alpha=0.12, t0=now, spr="soft")
    # wind: thin streaks racing round the shell in four tilted bands, alternating direction
    for h, w in ((30.0, 10.0), (70.0, -8.5), (110.0, 9.0), (145.0, -7.5)):   # (height cm, rad/s)
        for k in range(8):
            _part(S, pos=[0, 0, 0], orbit=("sora", 78.0 + random.uniform(-6, 6), w, 0.0, k * 2 * math.pi / 8 + random.uniform(0, 0.4), h),
                  size=random.uniform(9, 13), color=random.choice(((0.7, 1.0, 0.8), (0.95, 1.0, 0.95))), life=duration,
                  fade_in=0.3, fade_pow=0.15, alpha=0.75, t0=now, spr="soft", stretch=random.uniform(5.0, 7.0), align=True)
    fx_flash(S, [S.frame.pos[0], S.frame.pos[1], S.frame.pos[2] + 85], (0.7, 1.0, 0.8), 260, 0.3)


def heal(S, pawn, frac: float) -> None:
    S.pending.append(lambda: _heal_now(S, pawn, frac))
    sparkle(S, pawn, (0.35, 1.0, 0.45))


def _heal_now(S, pawn, frac: float) -> None:
    try:
        pool = pawn.HealthPool.Data
        mx = float(pool.GetMaxValue()) if hasattr(pool, "GetMaxValue") else float(pawn.HealthMaxVar)
        pool.SetCurrentValue(min(mx, float(pool.CurrentValue) + mx * frac))
    except Exception as ex:  # noqa: BLE001
        S.status["heal_error"] = str(ex)


def CFG_THUNDER_BOLTS(S) -> int:
    return int(S.CFG.get("thunder_bolts", 3))


def launch_spell(S, pc, name: str, level: int = 1, visual: bool = False) -> None:
    """The Borderlands side of a spell. While KH1's real effect is drawn (khfx_live) everything we'd draw is muted;
    the spell's effect on Borderlands (damage without a paired stand-in, heal, Stop, Aero push) still happens."""
    st = S.state
    mute = khfx_live(S)
    n0 = len(st["proj"])
    st["fx_mute"] = mute
    try:
        _launch_spell(S, pc, name, level, visual)
    finally:
        st["fx_mute"] = False
        if mute:
            for pr in st["proj"][n0:]:
                pr["hidden"] = True


def _launch_spell(S, pc, name: str, level: int = 1, visual: bool = False) -> None:
    st = S.state
    S.status["last_spell"] = [name, level, round(time.time() % 1000, 2), "kh1 hits" if visual else "bl hits"]
    mult = 1.0 + 0.5 * (level - 1)          # Fira / Firaga etc. hit harder
    if visual and name in ("Fire", "Blizzard", "Thunder"):
        mult = 0.0                          # damage arrives when KH1's real spell hits a stand-in (kh1_proxy_tick)
        if khfx_live(S):
            return                          # and KH1's real spell effect is drawn by the renderer (v35)
    tgt = aim_target(S, pc)
    start = hand_pos(S, pc)
    yaw = math.atan2(vec3(tgt.Location)[1] - start[1], vec3(tgt.Location)[0] - start[0]) if tgt else int(pc.Rotation.Yaw) * UU
    d = [math.cos(yaw), math.sin(yaw), 0.0]        # spells fly level (KH style), homing handles height
    now = time.time()
    if name == "Fire":
        fx_flash(S, start, (1.0, 0.55, 0.15), 140)
        st["proj"].append({"kind": "fire", "pos": start, "vel": [x * 1500 for x in d], "target": tgt, "t0": now,
                           "life": 2.0, "dmg": 70 * mult, "color": (1.0, 0.45, 0.1), "size": 70, "visual": visual,
                           "spell": ("Fire", level, 1.0)})
    elif name == "Blizzard":
        fx_flash(S, start, (0.7, 0.9, 1.0), 120)
        for k in (-1, 0, 1):
            yy = yaw + math.radians(10 * k)
            st["proj"].append({"kind": "ice", "pos": list(start), "vel": [math.cos(yy) * 1900, math.sin(yy) * 1900, 0.0],
                               "target": tgt if k == 0 else None, "t0": now, "life": 1.2, "dmg": 35 * mult,
                               "color": (0.55, 0.85, 1.0), "size": 38, "visual": visual, "spell": ("Blizzard", level, 0.5)})
    elif name == "Thunder":
        near = [p for _, p, _ in enemies(S, pc, 1400)]
        hits = ([tgt] + [p for p in near if p is not tgt])[:3] if tgt is not None else near[:3]
        # KH1 Thunder always brings down a volley: bolts without a target strike the ground in front of Sora
        centre = vec3(hits[0].Location) if hits else [start[0] + d[0] * 550, start[1] + d[1] * 550, start[2] - 60]
        for k in range(len(hits), CFG_THUNDER_BOLTS(S)):
            a_ = random.uniform(0, 2 * math.pi); r_ = random.uniform(120, 320) if hits else random.uniform(0, 300)
            spot = [centre[0] + math.cos(a_) * r_, centre[1] + math.sin(a_) * r_, centre[2] if hits else start[2] - 60]
            t_ = now + 0.1 + 0.14 * k + random.uniform(0, 0.05)
            st["proj"].append({"kind": "bolt", "pos": spot, "vel": [0, 0, 0], "target": None, "t0": t_,
                               "life": 0.35, "dmg": 0, "color": (1.0, 0.95, 0.5), "size": 60})
            st.setdefault("thunder_ground", []).append((t_, spot))
        for k, p in enumerate(hits):
            st["proj"].append({"kind": "bolt", "pos": vec3(p.Location), "vel": [0, 0, 0], "target": p,
                               "t0": now + 0.1 + 0.12 * k, "life": 0.35, "dmg": 90 * mult, "color": (1.0, 0.95, 0.5), "size": 60,
                               "spell": ("Thunder", level, 1.0)})
    elif name == "Cure":
        heal(S, pc.Pawn, 0.4 * mult)
        fx_cure(S)
    elif name == "Gravity":
        # crushes everything around the target: a share of their CURRENT health (KH Gravity), min 30
        centre = vec3(tgt.Location) if tgt is not None else [start[0] + d[0] * 700, start[1] + d[1] * 700, start[2]]
        fx_gravity(S, centre)
        for _, p, _ in enemies(S, pc, 450, origin=centre):
            if S.CFG.get("kb_kh1_damage", True):
                hurt(S, pc, p, spell_damage(S, pc, p, "Gravity", level), push=0, lvl_scale=False, elem="corrosive",
                     elem_chance=S.CFG.get("kb_elem_chance", {}).get("Gravity", 0.35) * (1.0 + 0.25 * (level - 1)),
                     spread=(level - 1) if S.CFG.get("kb_spread", True) else 0)
            else:
                hurt(S, pc, p, max(30.0, hp(p) * 0.25) * mult, push=0)
    elif name == "Stop":
        # freezes enemies in front for a few seconds
        until = now + 3.0 + 1.5 * (level - 1)
        for dist, p, bearing in enemies(S, pc, 1100):
            if abs(ang(bearing - S.state["yaw"])) > math.radians(70):
                continue
            st.setdefault("stopped", []).append((p, until))
            S.pending.append(lambda p=p: setattr(p, "CustomTimeDilation", 0.0001))
            fx_stop(S, p, until - now)
    elif name == "Aero":
        # wind burst around Sora: knocks back and damages what is close
        fx_aero(S, 6.0 + 2.0 * (level - 1))
        st["aero_pawn"] = pc.Pawn
        for _, p, _ in enemies(S, pc, 320):
            if S.CFG.get("kb_kh1_damage", True):
                hurt(S, pc, p, spell_damage(S, pc, p, "Aero", level), push=60000.0, lvl_scale=False)
            else:
                hurt(S, pc, p, 30 * mult, push=60000.0)


# ---------------------------------------------------------------- the Borderlands world against the blade and spells
# The SDK's traces are dead in BL1E, so the renderer (v13) samples Borderlands' own depth buffer under 8 points along
# the Keyblade and under points we send (projectiles; probe 0 = the ground at Sora's feet, for calibration).
# Depth -> distance needs Borderlands' near plane: solved continuously from the feet probe, whose true distance we know.
PROBE_ZF = 200000.0


def probe_lin(S, d):
    zn, zf = S.state.get("probe_zn", 10.0), PROBE_ZF
    den = zf - d * (zf - zn)
    return zn * zf / den if den > 1e-3 else 1e9


def probe_tick(S, pc, now: float) -> None:
    st, CFG, dll = S.state, S.CFG, S.dll
    if not hasattr(dll, "sr_get_probes") or not CFG.get("world_probes", True):
        return
    f = S.frame
    pts = [(f.pos[0], f.pos[1], f.pos[2] - 3.0)]                    # 0: ground just under his feet (calibration)
    lifts = st.setdefault("party_lift", [0.0, 0.0])
    for aid in (0, 1):                                              # 1-4: just above / just below each party member's feet
        pp = st.get("party_pos", {}).get(aid)
        z = (pp[2] + lifts[aid]) if pp else 0.0
        pts += [(pp[0], pp[1], z + 8.0), (pp[0], pp[1], z - 12.0)] if pp else [(0.0, 0.0, -1e6), (0.0, 0.0, -1e6)]
    hl_probe = list(st.get("free_bl", {}).items())[:3]               # 5-10: the same for free Heartless
    st["hl_probe"] = [e for e, _ in hl_probe]
    for k in range(3):
        if k < len(hl_probe):
            hb = hl_probe[k][1]
            pts += [(hb["pos"][0], hb["pos"][1], hb["z"] + 8.0), (hb["pos"][0], hb["pos"][1], hb["z"] - 12.0)]
        else:
            pts += [(0.0, 0.0, -1e6), (0.0, 0.0, -1e6)]
    projs = [pr for pr in st.get("proj", []) if pr["kind"] in ("fire", "ice") and now >= pr["t0"]][:5]
    pts += [tuple(pr["pos"]) for pr in projs]
    buf = (ctypes.c_float * 48)(*[c for p_ in pts for c in p_][:48])
    dll.sr_set_probes(buf, len(pts))
    st["probe_projs"] = projs
    out = (ctypes.c_float * 96)()
    fid = dll.sr_get_probes(out)
    if fid == st.get("probe_fid"):
        return
    st["probe_fid"] = fid
    P = [tuple(out[k * 4:k * 4 + 4]) for k in range(24)]          # scene NDC z, point NDC z, point distance, valid
    # calibrate the near plane on the feet probe (standing on the ground, sample in front of nothing else)
    ds, _dp, w, ok = P[8]
    if ok and 0.0 < ds < 1.0 and int(pc.Pawn.Physics) != 2:
        zn = w * PROBE_ZF * (1.0 - ds) / max(1e-3, PROBE_ZF - w * ds)
        if 0.5 < zn < 200.0:
            st["probe_zn"] = st.get("probe_zn", zn) * 0.9 + zn * 0.1
    # blade against the world: a surface just in front of a blade point = the blade is touching it
    ks = kh1_state(S) if kh1_combat_on(S) else None
    swinging = bool(ks and kh1_attacking_now(S, ks))
    contacts = []
    bl = (ctypes.c_float * 8)()
    if swinging and dll.sr_get_blade(bl):
        hilt, tip = tuple(bl[0:3]), tuple(bl[3:6])
        for k in range(8):
            ds, dp, w, ok = P[k]
            if not ok or ds < 0:
                continue
            gap = w - probe_lin(S, ds)
            if 0.0 < gap < CFG.get("wall_gap", 60.0):
                t = (k + 1) / 8 * 1.15
                pt = [hilt[i] + (tip[i] - hilt[i]) * t for i in range(3)]
                if pt[2] > f.pos[2] + CFG.get("wall_min_z", 25.0):      # not the floor under him
                    contacts.append(pt)
    if contacts and ks["anim"] != st.get("wall_swing"):
        st["wall_swing"] = ks["anim"]
        pt = contacts[len(contacts) // 2]
        near_enemy = any(math.dist(pt, vec3(p_.Location)) < 120 for _, p_, _ in enemies(S, pc, 400))
        if not near_enemy:                                            # enemies have their own hit (kh1_blade_tick)
            if CFG.get("hitstop", False):           # vanilla audit: KH1 has no hit-freeze (off)
                dll.sr_hitstop(ctypes.c_float(CFG.get("wall_hitstop", 0.09)))
            st["wall_stop_until"] = now + CFG.get("wall_hitstop", 0.09) + 0.06
            if invented_ok(S): fx_wall_spark(S, pt)
            sfx(S, "clang")
            st["wall_hits"] = st.get("wall_hits", 0) + 1
    if not swinging:
        st["wall_swing"] = None
    if now < st.get("wall_stop_until", 0):                            # the blade bit the wall: no lunge through it
        pawn = pc.Pawn
        S.pending.append(lambda pawn=pawn: setattr(pawn, "Velocity", unrealsdk.make_struct(
            "Vector", X=0.0, Y=0.0, Z=float(pawn.Velocity.Z))))
    # spells against the world: a surface just in front of a flying Fire/Blizzard = it hit the wall
    walled = set()
    # party on the Borderlands ground: feet buried -> up, nothing under the feet -> down (like GTA's ground probes)
    for aid in (0, 1):
        if not st.get("party_pos", {}).get(aid):
            continue
        da, _, wa, oka = P[9 + aid * 2]
        db, _, wb, okb = P[10 + aid * 2]
        buried = oka and da >= 0 and 0.0 < wa - probe_lin(S, da) < 150.0
        # floating = ground a little below the feet (a big gap = looking past an edge / at the sky: no evidence)
        floating = okb and db >= 0 and 10.0 < probe_lin(S, db) - wb < CFG.get("party_float_max", 120.0)
        if buried:
            lifts[aid] = min(200.0, lifts[aid] + 2.5)
        elif floating:
            lifts[aid] = max(CFG.get("party_lift_min", -80.0), lifts[aid] - 2.0)
        elif not (oka and okb):
            lifts[aid] *= 0.98                     # no reading (off screen, hidden): back toward KH1's own height
    for k, ent in enumerate(st.get("hl_probe", [])):               # free Heartless on the Borderlands ground
        hb = st.get("free_bl", {}).get(ent)
        if not hb:
            continue
        da, _, wa, oka = P[13 + k * 2]
        db, _, wb, okb = P[14 + k * 2]
        if oka and da >= 0 and 0.0 < wa - probe_lin(S, da) < 200.0:
            hb["z"] += CFG.get("free_lift_up", 6.0)
        elif okb and db >= 0 and probe_lin(S, db) - wb > 10.0:
            hb["z"] -= CFG.get("free_lift_down", 5.0)
    for k, pr in enumerate(st.get("probe_projs", [])):
        if 19 + k >= 24:
            break
        ds, dp, w, ok = P[19 + k]
        if ok and ds >= 0 and -20.0 < w - probe_lin(S, ds) < CFG.get("spell_wall_gap", 120.0):
            walled.add(id(pr))
    st["proj_walled"] = walled
    S.status["probes"] = {"zn": round(st.get("probe_zn", 10.0), 2), "wall_hits": st.get("wall_hits", 0)}


# ---------------------------------------------------------------- KH1 sound effects (renderer v14 XAudio2)
# ---------------------------------------------------------------- sound pass (passthrough/make_sfx2.py)
# Sora's own KH1 sound effects played on KH1's own motion cues (footsteps, jump, land, roll... at the exact frames), and
# Traverse Town battle voices: Sora (swings, finishers, spells, abilities, hurt), Donald (spells, Cure), Goofy.
# Config: sfx_steps 0.45 (cue volume), sfx_voice 0.9, voice_atk_chance 0.4, sfx_cues_off [sound indices to skip].
ABILITY_VOICE = {"Sonic Blade": "vbig", "Strike Raid": "vthrow", "Ars Arcanum": "vfin", "Ragnarok": "vthisisit"}


def voice(S, group: str, chance: float = 1.0, cd: float = 0.7, who: str = "sora", vol: float = 1.0) -> None:
    """One voice line per speaker at a time (cd seconds apart), so lines never pile up."""
    st, now = S.state, time.time()
    if random.random() > chance or now < st.setdefault("voice_until", {}).get(who, 0):
        return
    st["voice_until"][who] = now + cd
    sfx(S, group, S.CFG.get("sfx_voice", 0.9) * vol, pitch_jitter=0.015)


# KH1's level for its spell sounds relative to Sora's spell voice line, measured from KH1's own output with the music
# silenced (2026-10-05): first sound of each cast as recorded; the flying/impact sounds are 3D in KH1 (quiet when they
# fly off with no target, ~0.07) -> 0.3 as a mid-distance level.
KH1_SE_GAIN = {"kse000n119": 1.14, "kse000n120": 0.3, "kse000n122": 0.5, "kse000n123": 0.3, "kse000n106": 0.82,
               "kse000n107": 0.3, "kse000n109": 0.3, "kse000n110": 1.04, "kse000n128": 0.52, "kse000n95": 0.53,
               "kse000n96": 0.3, "kse000n97": 0.3, "kse000n112": 0.8, "kse000n111": 0.96, "kse000n113": 0.32}   # Gravity


def kh1_se_tick(S) -> None:
    r"""KH1's own sound effects, played in Borderlands as KH1 starts them (kh1_fx v9 logs every sound KH1's sound
    library starts into Local\SoraSe: bank, sound number, bank file). KH1 itself stays muted. A sound plays when its
    clip exists: data/sfx/kh1/k<bank file>n<sound number>.wav, e.g. kse000n119 = Fire cast (se000 sound N = vgmstream
    stream N-15, verified against KH1's own output).
    Config: kh1_se (on), sfx_kh1_se 1.0 (volume), kh1_se_vol {"se022": 0.8} per file prefix (se022 = field footsteps,
    se011 = Sora, se012 Donald, se013 Goofy), kh1_se_any_emitter [prefixes also played when not from the party]."""
    import ctypes.wintypes as W
    st, CFG = S.state, S.CFG
    if not CFG.get("kh1_se", True):
        return
    m = getattr(S, "kh1_se_map", None)
    if m is None:
        if time.time() < st.get("kh1_se_retry", 0):
            return
        st["kh1_se_retry"] = time.time() + 3.0
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenFileMappingW.restype = W.HANDLE
        k32.MapViewOfFile.restype = ctypes.c_void_p
        k32.MapViewOfFile.argtypes = [W.HANDLE, W.DWORD, W.DWORD, W.DWORD, ctypes.c_size_t]
        h = k32.OpenFileMappingW(0x0004, False, r"Local\SoraSe")
        p = h and k32.MapViewOfFile(h, 0x0004, 0, 0, 64 + 1024 * 96)
        if not p:
            return
        m = S.kh1_se_map = (ctypes.c_char * (64 + 1024 * 96)).from_address(p)
        st["kh1_se_r"] = struct.unpack_from("<I", m, 12)[0]          # start at "now": no backlog
    if getattr(S, "sfx_ids", None) is None:
        sfx(S, "")                                                   # builds the clip cache
    w = struct.unpack_from("<I", m, 12)[0]
    r = st.get("kh1_se_r", w)
    if w < r or w - r > 1024:
        r = max(0, w - 64)
    played, seen = st.setdefault("kh1_se_ids", {}), S.status.setdefault("kh1_se", {"played": 0, "unmapped": {}})
    now = time.time()
    for i in range(r, w):
        o = 64 + (i % 1024) * 96
        seq, kind, bank, no, sid = struct.unpack_from("<IIiii", m, o)
        if seq != i + 1 or kind not in (1, 2, 3, 4, 5, 11):          # starts that carry the bank file name
            continue                                                 # (11 = kh1_fx v33 restart of a loaded entry: spell casts)
        name = bytes(m[o + 40:o + 96]).split(b"\0")[0].decode("latin1").split(".")[0]
        if now - played.get((name, sid), 0) < 0.3:
            continue
        played[(name, sid)] = now
        party = sid in (0x30001, 0x30002, 0x30003)                   # emitter: Sora, Donald, Goofy
        group = f"k{name}n{no}" if bank >= 0 else f"k{name}"
        if bank < 0 and not party and name[:5] not in CFG.get("kh1_se_any_emitter", ["se010"]):   # se010 = summon SE
            continue                                                 # KH1-room sounds (parked Heartless, props)
        if group in CFG.get("kh1_se_off", []):
            continue
        if st.get("kh_menu_open") and CFG.get("kh1_menu_se", True):
            continue                                                 # F9 menu: KH1 plays its own menu sounds (no doubles)
        if not party and not free_on(S) and group in CFG.get("kh1_se_heartless", ["kse000n164", "kse000n165"]):
            continue                                                 # Heartless appear / vanish (parked stand-ins) - user
        if group in (getattr(S, "sfx_ids", None) or {}) or sfx_lazy(S, group):
            vol = CFG.get("sfx_kh1_se", 1.0) * CFG.get("kh1_se_vol", {}).get(name[:5], 1.0)
            if group in KH1_SE_GAIN:                                 # KH1's own mix, relative to the voice line
                st["kh1_se_cast_t"] = now                            # (the cast-start fallback then stays quiet)
                vol = CFG.get("sfx_voice", 0.9) * CFG.get("sfx_kh1_se", 1.0) * CFG.get("kh1_se_gain", {}).get(group, KH1_SE_GAIN[group])
            sfx(S, group, vol, pitch_jitter=0.0)
            seen["played"] += 1
        else:
            um = seen["unmapped"]
            if len(um) < 40 or group in um:
                um[group] = um.get(group, 0) + 1
    st["kh1_se_r"] = w
    if len(played) > 256:
        st["kh1_se_ids"] = {k: t for k, t in played.items() if now - t < 2.0}


def sound_tick(S, pc, pawn, ks, now: float) -> None:
    st, CFG = S.state, S.CFG
    try:
        kh1_se_tick(S)
    except Exception as ex:  # noqa: BLE001
        S.status["kh1_se_error"] = str(ex)
    # KH1's own audio doubled ours (it really plays the same steps / voices): keep it muted (config kh1_mute, default on)
    want = bool(CFG.get("kh1_mute", True)) and not kh1_music_mode(S) \
        and not st.get("kh_intro_view")                 # summon intro on KH1's screen: KH1 audible (its own sound)
    try:
        kh1_music_tick(S, pc, now)
    except Exception as ex:  # noqa: BLE001
        S.status["kh1_music"] = f"ERR {type(ex).__name__}: {ex}"
    if hasattr(S.dll, "sr_mute_kh1") and (st.get("kh1_muted") != want or now - st.get("kh1_mute_t", 0) > 10.0):
        st["kh1_mute_t"] = now
        r = S.dll.sr_mute_kh1(1 if want else 0)
        if r > 0 or not want:
            st["kh1_muted"] = want
        S.status["kh1_mute"] = [want, r]
    if getattr(S, "sfx_ver", 0) != 8:                     # sound pass files arrived after the first load: reload once
        S.sfx_ids, S.sfx_ver, S.sfx_kh1_files = None, 8, None
    # 1) KH1 motion cues: every cue whose frame the clock passed since the last tick
    cues = getattr(S, "sfx_cues", None)
    if cues is None:
        try:
            cues = {int(k): v for k, v in json.loads((HERE / "data" / "sora_sfx_cues.json").read_text(encoding="utf-8")).items()}
        except Exception:  # noqa: BLE001
            cues = {}
        S.sfx_cues = cues
    if ks and ks.get("motion") is not None:
        m, c = ks["motion"], ks["clock"]
        pm, pc_ = st.get("cue_m"), st.get("cue_c", 0.0)
        st["cue_m"], st["cue_c"] = m, c
        if m != pm and pm is not None:
            if m == 8:                                      # jump takeoff
                voice(S, "vatk", chance=CFG.get("voice_jump_chance", 0.6), cd=0.4, vol=0.7)
            elif m == 12:                                   # dodge roll
                voice(S, "vatk", chance=0.7, cd=0.4, vol=0.8)
        e = cues.get(m)
        if e and pm is not None:
            if m != pm:
                spans = [(-0.01, c)]
            elif c >= pc_:
                spans = [(pc_, c)] if c - pc_ < 30 else []
            else:                                           # the motion looped
                spans = [(pc_, float(e["len"]) + 1), (-0.01, c)]
            off, played = set(CFG.get("sfx_cues_off", [])), st.setdefault("cue_played", {})
            for fr, idx, kvol in e["ev"]:                  # kvol = KH1's own volume for this cue (0..127)
                if idx in off or not any(a < fr <= b for a, b in spans):
                    continue
                if now - played.get(idx, 0) < 0.06:        # the same sound on both cue tracks in the same frame
                    continue
                played[idx] = now
                if m in CFG.get("step_motions", [2, 3, 4, 5, 6, 7]) and idx not in CFG.get("step_skip_idx", [4, 18]):
                    if now - st.get("step_t", 0) > 0.12:            # one step per foot plant (cues come in pairs)
                        st["step_t"] = now
                        sfx(S, "step", CFG.get("sfx_footsteps", 0.9), pitch_jitter=0.08)
                if CFG.get("sfx_steps", 1.0) > 0:
                    sfx(S, f"sse{idx:02d}", CFG.get("sfx_steps", 1.0) * kvol / 127.0, pitch_jitter=0.04)
    # 1b) Donald / Goofy: their own KH1 motion cues (steps, staff, shield), only while they are drawn in Borderlands
    if party_on(S):
        for aid, (prefix, addr) in enumerate(zip(("dse", "gse"), party_addrs(S))):
            pc_cues = getattr(S, f"{prefix}_cues", None)
            if pc_cues is None:
                try:
                    pc_cues = {int(k): v for k, v in json.loads((HERE / "data" / f"{prefix}_sfx_cues.json").read_text(encoding="utf-8")).items()}
                except Exception:  # noqa: BLE001
                    pc_cues = {}
                setattr(S, f"{prefix}_cues", pc_cues)
            raw = kh1_abs(S, addr + 0x168, 8) if addr else None
            if not raw:
                continue
            m, c = struct.unpack("<If", raw)
            who = ("donald", "goofy")[aid]
            # attack shout on ENTERING KH1's action state 6 (session 9: the guessed motions 41-45 never matched - Goofy's
            # attack voice played 0 times all session; party_trace shows both in state 6 while swinging / casting)
            sb_ = kh1_abs(S, addr + 0x160, 1)
            pst = sb_[0] if sb_ else 0
            # session 10: also each NEW motion while staying in state 6 (a combo keeps them in 6, so entry-only shouted
            # once per combo); the per-speaker cooldown keeps wind-up -> swing motion pairs to one line
            if pst == 6 and (st.get(f"pst_{prefix}") != 6 or m != st.get(f"pm_{prefix}")):
                voice(S, ("datk", "gatk")[aid], chance=CFG.get("party_attack_voice_chance", 0.5), who=who, cd=1.2)
                n6 = S.status.setdefault("party_atk_trig", {})
                n6[who] = n6.get(who, 0) + 1
            st[f"pst_{prefix}"] = pst
            st[f"pm_{prefix}"] = m
            stp_ = kh1_resolve(S, struct.unpack("<I", kh1_abs(S, addr + 0x6C, 4) or bytes(4))[0])
            hpb = stp_ and kh1_abs(S, stp_ + 0x3C, 4)
            if hpb:
                php = struct.unpack("<i", hpb)[0]
                if 0 <= php < st.get(f"php_{prefix}", php):
                    voice(S, ("dhurt", "ghurt")[aid], who=who, cd=1.5)
                st[f"php_{prefix}"] = php
            key = f"cue_{prefix}"
            pm, pcl = st.get(key, (None, 0.0))
            st[key] = (m, c)
            e = pc_cues.get(m)
            if not e or pm is None:
                continue
            spans = [(-0.01, c)] if m != pm else ([(pcl, c)] if 0 <= c - pcl < 30 else ([(pcl, float(e["len"]) + 1), (-0.01, c)] if c < pcl else []))
            for fr, idx, kvol in e["ev"]:
                if any(a < fr <= b for a, b in spans):
                    sfx(S, f"{prefix}{idx:02d}", CFG.get("sfx_steps", 1.0) * min(1.0, kvol / 127.0) * CFG.get("sfx_party", 0.8), pitch_jitter=0.04)
    # 1c) the hidden Borderlands character stays silent: no jump / land / pain grunts, footsteps or hit sounds
    if CFG.get("bl_sounds_off", True) and pawn is not None:
        if st.get("bl_mute_pawn2") is not pawn:
            st["bl_mute_pawn2"] = pawn
            steps = None
            if CFG.get("bl_footsteps", True):                   # Sora's steps = Borderlands' own surface footsteps
                try:
                    steps = float(pawn.Class.ClassDefaultObject.MaxFootstepEffectDistance) or 4000.0
                except Exception:  # noqa: BLE001
                    steps = 4000.0
            for name, val in (("PlayerSounds", None), ("bMuteHitSound", True), ("MaxFootstepEffectDistance", steps if steps else 0.0)):
                try:
                    setattr(pawn, name, val)
                except Exception as ex:  # noqa: BLE001
                    S.status["bl_mute_error"] = f"{name}: {ex}"
        try:
            pawn.LastPainSoundTime = float(pc.WorldInfo.TimeSeconds) + 3600.0
        except Exception:  # noqa: BLE001
            pass
    # 2) hurt: Borderlands health dropped
    try:
        hp, hpmax = pool_values(pawn)[0], max(1.0, float(pool_values(pawn)[1]))
    except Exception:  # noqa: BLE001
        return
    last = st.get("snd_hp")
    st["snd_hp"] = hp
    if last is not None and last - hp > 0.02 * hpmax and hp > 0:
        if hp < 0.2 * hpmax:
            voice(S, "vko", cd=2.5)
        else:
            voice(S, "vhurt", cd=0.8)


def sfx_lazy(S, group: str) -> bool:
    """Loads data/sfx/kh1/<group>.wav on first use (KH1's one-sound files, ~950 of them: too many to load up front)."""
    have = getattr(S, "sfx_kh1_files", None)
    if have is None or (group not in have and time.time() - getattr(S, "sfx_kh1_scan_t", 0) > 60.0):
        d = HERE / "data" / "sfx" / "kh1"                           # (re-scanned for new clips at most once a minute)
        have = S.sfx_kh1_files = {f.stem for f in d.glob("*.wav")} if d.exists() else set()
        S.sfx_kh1_scan_t = time.time()
    if group not in have or getattr(S, "sfx_ids", None) is None or not hasattr(S.dll, "sr_sfx_load"):
        return False
    i = S.dll.sr_sfx_load(str(HERE / "data" / "sfx" / "kh1" / f"{group}.wav").encode("mbcs"))
    if i < 0:
        have.discard(group)                                          # unloadable: don't retry every time
        return False
    S.sfx_ids[group] = [i]
    return True


# data/sfx/*.wav decoded from the player's own KH1 install (passthrough/make_sfx.py): clang, hit_*, heavy_*, swing_*.
def sfx(S, group: str, volume: float = None, pitch_jitter: float = 0.05, pitch: float = 1.0) -> None:
    dll = S.dll
    if not hasattr(dll, "sr_sfx_play") or not S.CFG.get("sfx", True):
        return
    cache = getattr(S, "sfx_ids", None)
    if cache is None:
        cache = S.sfx_ids = {}
        dll.sr_sfx_load.argtypes = [ctypes.c_char_p]
        for f in sorted((HERE / "data" / "sfx").glob("*.wav")):
            i = dll.sr_sfx_load(str(f).encode("mbcs"))
            if i >= 0:
                cache.setdefault(f.stem.split("_")[0], []).append(i)
    ids = cache.get(group)
    if not ids:
        return
    vol = S.CFG.get("sfx_volume", 0.7) * (volume if volume is not None else 1.0)
    # session 9 (user: Sora's swings / jumps / magic "way too loud"): the mod's own audio voice ignored Borderlands'
    # sliders -> follow master x sfx like KH1's window does (kh1_volume_tick reads them: st blvol, 0..10 each)
    bv = S.state.get("blvol")
    if bv and S.CFG.get("kh1_follow_bl_volume", True):
        vol *= min(1.0, bv["master"] / 10.0) * min(1.0, bv["sfx"] / 10.0)
    ok = dll.sr_sfx_play(random.choice(ids), ctypes.c_float(vol), ctypes.c_float(pitch + random.uniform(-pitch_jitter, pitch_jitter)))
    n = S.status.setdefault("sfx_played" if ok else "sfx_dropped", {})   # dropped = no free XAudio2 voice
    n[group] = n.get(group, 0) + 1


def fx_wall_spark(S, pt):
    """Keyblade on stone: a white-gold flash, the KH1 hit-spark burst and sparks flying off the wall."""
    fx_flash(S, pt, (1.0, 0.95, 0.8), 120, 0.12)
    _part(S, pos=list(pt), size=90, color=(1.0, 0.95, 0.75), life=0.14, spr="burst", rot=random.uniform(0, 6.3))
    for _ in range(10):
        d = [random.uniform(-1, 1) for _ in range(3)]
        _part(S, pos=list(pt), vel=[x * random.uniform(250, 520) for x in d], grav=-900.0, drag=1.5, size=random.uniform(8, 16),
              color=(1.0, 0.85, 0.5), life=random.uniform(0.2, 0.4), spr="cross", rot=random.uniform(0, 1.6))


def update_spells(S, pc, dt: float, now: float) -> None:
    st = S.state
    keep = []
    for pr in st["proj"]:
        st["fx_mute"] = bool(pr.get("hidden"))
        if now < pr["t0"]:
            keep.append(pr)
            continue
        age = now - pr["t0"]
        if pr["kind"] == "bolt":
            if pr["target"] is not None:
                try:
                    pr["pos"] = vec3(pr["target"].Location)
                except Exception:  # noqa: BLE001
                    pass
            if not pr.get("struck"):
                pr["struck"] = True
                if pr["target"] is not None and pr["dmg"]:
                    if pr.get("spell") and S.CFG.get("kb_kh1_damage", True):
                        hurt(S, pc, pr["target"], spell_damage(S, pc, pr["target"], *pr["spell"]), push=3000, lvl_scale=False,
                             elem=KB_SPELL_ELEM.get(pr["spell"][0]), elem_chance=S.CFG.get("kb_elem_chance", {}).get(pr["spell"][0], 0.35) * (1.0 + 0.25 * (max(1, pr["spell"][1]) - 1)), spread=(max(1, pr["spell"][1]) - 1) if S.CFG.get("kb_spread", True) else 0)
                    else:
                        hurt(S, pc, pr["target"], pr["dmg"], push=3000)
                fx_thunder_hit(S, pr["pos"])
            if age < pr["life"]:
                keep.append(pr)
            continue
        tgt = pr["target"]
        hit = None
        if tgt is not None:
            try:
                if hp(tgt) <= 0:
                    raise ValueError("dead")
                t = vec3(tgt.Location)
                v = pr["vel"]
                sp = math.sqrt(sum(x * x for x in v)) or 1.0
                want = [t[i] - pr["pos"][i] for i in range(3)]
                wn = math.sqrt(sum(x * x for x in want)) or 1.0
                turn = min(1.0, 6.0 * dt)
                nv = [v[i] / sp * (1 - turn) + want[i] / wn * turn for i in range(3)]
                nn = math.sqrt(sum(x * x for x in nv)) or 1.0
                pr["vel"] = [x / nn * sp for x in nv]
                if wn < 90:
                    hit = tgt
            except Exception:  # noqa: BLE001
                pr["target"] = None
        else:
            for dist, p, _ in enemies(S, pc, 120, origin=pr["pos"]):
                hit = p
                break
        if hit is not None:
            if pr["dmg"]:
                if pr.get("spell") and S.CFG.get("kb_kh1_damage", True):
                    hurt(S, pc, hit, spell_damage(S, pc, hit, *pr["spell"]), lvl_scale=False,
                         elem=KB_SPELL_ELEM.get(pr["spell"][0]), elem_chance=S.CFG.get("kb_elem_chance", {}).get(pr["spell"][0], 0.35) * (1.0 + 0.25 * (max(1, pr["spell"][1]) - 1)), spread=(max(1, pr["spell"][1]) - 1) if S.CFG.get("kb_spread", True) else 0)
                else:
                    hurt(S, pc, hit, pr["dmg"])
            fx_impact(S, pr)
            continue
        new = [pr["pos"][i] + pr["vel"][i] * dt for i in range(3)]
        if id(pr) in st.get("proj_walled", ()) or blocked(pc, pr["pos"], new):   # hit a wall (depth probe): burst there
            S.status["blocked_at"] = [round(x) for x in pr["pos"]]
            fx_impact(S, pr)
            continue
        pr["pos"] = new
        if age < pr["life"]:
            keep.append(pr)
            if pr["kind"] == "fire":   # flame trail: KH1 flame flipbook puffs that rise and fade, a little smoke
                for _ in range(2):
                    _part(S, pos=[c + random.uniform(-12, 12) for c in pr["pos"]],
                          vel=[random.uniform(-40, 40), random.uniform(-40, 40), random.uniform(40, 110)],
                          size=random.uniform(45, 70), grow=0.5, drag=2.0, color=(1.0, 0.8, 0.55), t0=now,
                          life=random.uniform(0.25, 0.4), flip="fire", rot=random.uniform(0, 6.3), spin=random.uniform(-2, 2))
                if random.random() < 0.15:
                    fx_smoke(S, pr["pos"], n=1, size=50, life=0.6)
            elif pr["kind"] == "ice":   # glittering ice trail: sparkles and tiny crystals
                _part(S, pos=list(pr["pos"]), vel=[random.uniform(-30, 30) for _ in range(3)], size=random.uniform(14, 24),
                      color=(0.85, 0.95, 1.0), t0=now, life=0.35, spr=random.choice(("cross", "diamond")),
                      rot=random.uniform(0, 6.3), spin=random.uniform(-6, 6))
        else:
            fx_impact(S, pr, small=True)
    st["fx_mute"] = False
    st["proj"] = keep
    if st.get("stopped"):                    # Stop wears off
        still = []
        for p, until in st["stopped"]:
            if now >= until:
                S.pending.append(lambda p=p: setattr(p, "CustomTimeDilation", 1.0))
            else:
                still.append((p, until))
        st["stopped"] = still
    parts = []
    for pa in st["parts"]:
        age = now - pa["t0"]
        if age < 0:
            parts.append(pa)
            continue
        if age >= pa["life"]:
            continue
        orb = pa.get("orbit")
        if orb:                                   # (centre, radius, rad/s, rise cm/s, phase, height)
            c = fx_centre(S, orb[0])
            if c is None:
                continue
            a = orb[4] + orb[2] * age
            r = orb[1] * pa.get("r_grow", 1.0) ** (age / pa["life"])
            pa["pos"] = [c[0] + math.cos(a) * r, c[1] + math.sin(a) * r, c[2] + orb[5] + orb[3] * age]
        else:
            v = pa["vel"]
            g, dr = pa.get("grav", 0.0), pa.get("drag", 0.0)
            if g:
                v[2] += g * dt
            if dr:
                k = max(0.0, 1.0 - dr * dt)
                v = [x * k for x in v]
                pa["vel"] = v
            pa["pos"] = [pa["pos"][i] + v[i] * dt for i in range(3)]
        parts.append(pa)
    keep_orb = [pa for pa in parts if pa.get("orbit")]
    rest = [pa for pa in parts if not pa.get("orbit")]
    st["parts"] = keep_orb[-120:] + rest[-(220 - min(120, len(keep_orb))):]
    st["mp"] = min(MP_MAX, st["mp"] + 2.0 * dt)


def cam_axes(S):
    """Camera right (Y) and up (Z) axes, same maths as the renderer."""
    p, y, r = S.frame.cam_rot[0], S.frame.cam_rot[1], S.frame.cam_rot[2]
    SP, CP, SY, CY, SR, CR = math.sin(p), math.cos(p), math.sin(y), math.cos(y), math.sin(r), math.cos(r)
    return (SR * SP * CY - CR * SY, SR * SP * SY + CR * CY, -SR * CP), (-(CR * SP * CY + SR * SY), CY * SR - CR * SP * SY, CR * CP)


def screen_rot(S, d):
    """Sprite rotation that lines a stretched sprite's long axis up with world direction d on screen."""
    Y, Z = cam_axes(S)
    dy, dz = sum(a * b for a, b in zip(Y, d)), sum(a * b for a, b in zip(Z, d))
    return math.atan2(-dy, dz)


def bolt_path(top, bottom, n=9, jag=45.0):
    """KH1 Thunder: a crooked line from the sky down to the target (new shape every frame = flicker)."""
    pts = [list(top)]
    for k in range(1, n):
        t = k / n
        pts.append([top[0] + (bottom[0] - top[0]) * t + random.uniform(-jag, jag),
                    top[1] + (bottom[1] - top[1]) * t + random.uniform(-jag, jag),
                    top[2] + (bottom[2] - top[2]) * t + random.uniform(-jag * 0.3, jag * 0.3)])
    pts.append(list(bottom))
    return pts


# KH1 PRIZE ORBS in Borderlands (2026-10-06): KH1 draws its prizes as normal objects (kh1_fx captures none of them), so
# their live state is read from KH1's prize objects (0x2432140 + 0x20 * k: vec pos @0, kind @0x18, flags @0x19 bit0 =
# live) and drawn with KH1's own prize.dpx textures (sora_fx.bin 9-11). Kinds = drop-table slots, identified by
# picking each up: 0/1/2 munny 1/5/20, 3/4 HP small/big, 5/6 MP small/big (3-6 by KH1's usual order, not measured).
KH_PRIZE_OBJS = 0x2432140
PRIZE_LOOK = {   # kind -> (texture, sphere tint, glow tint, KH1 diameter)
    # session 9 (user: Bambi's BIG orbs see-through + glitched, munny see-through): the atlases were mis-read. tex 9
    # (-320) = small green sphere + glow squeezed in its LEFT half and the faceted gold MUNNY orb in its RIGHT half;
    # tex 10 (-1740) = sphere | glow; tex 11 (-3b60) = clear bubble | empty. Big HP / MP = tex 10 bigger; munny = tex 9
    # right half (solid) inside tex 11's bubble (fill_fx)
    0: (9, (1.0, 1.0, 1.0), None, 14.0), 1: (9, (1.0, 1.0, 1.0), None, 20.0), 2: (9, (1.0, 1.0, 1.0), None, 28.0),
    3: (10, (1.0, 1.0, 1.0), (0.6, 1.0, 0.6), 18.0), 4: (10, (1.0, 1.0, 1.0), (0.6, 1.0, 0.6), 30.0),
    5: (10, (0.55, 0.7, 1.0), (0.6, 0.75, 1.0), 18.0), 6: (10, (0.55, 0.7, 1.0), (0.6, 0.75, 1.0), 30.0),
}


def kh1_point_to_bl(S, ks, p):
    """A KH1 world point -> Borderlands world point (offset from KH1 Sora turned into BL Sora's frame, as the juggle)."""
    st, CFG = S.state, S.CFG
    ox, oz = p[0] - ks["pos"][0], p[2] - ks["pos"][2]
    fx, fz = ks["fwd"]
    fwd_k, side_k = ox * fx + oz * fz, -ox * fz + oz * fx
    sc = max(S.frame.scale, 0.01)
    fwd_b, side_b = fwd_k * sc, side_k * CFG.get("kh_turn_sign", -1.0) * sc
    yb = (st.get("kh_yaw_target") if st.get("kh_move_on") and st.get("kh_yaw_target") is not None else st["yaw"])         + math.radians(CFG["yaw_offset_deg"])
    floor = st.get("kh_floor_ref") if st.get("kh_ground_active") and st.get("kh_floor_ref") is not None else ks["pos"][1]
    return [S.frame.pos[0] + fwd_b * math.cos(yb) - side_b * math.sin(yb),
            S.frame.pos[1] + fwd_b * math.sin(yb) + side_b * math.cos(yb),
            S.frame.pos[2] + (floor - p[1]) * sc]


# ITEM DROPS (user 2026-10-12: Potions / Ethers never showed in Borderlands). KH1 drops items through FUN_1402bff00
# (item, pos, mode), not the orb pool: up to 8 slots at 0x2DE8E20 (0x50 B: +8 entity ptr, +0x10 item id, +0x30 scale
# 1.0 / 1.4 / 2.0 from the item's size class, table 0x529FF0) and each item is a REAL KH1 entity (purple + gold item
# ball model, position at ent +0x10). They land on the fed Borderlands ground and KH1 picks them up itself (its own
# "obtained" popup, real inventory) - only the drawing was missing: same sprite route as the prize orbs.
KH_ITEM_SLOTS = 0x2DE8E20


def item_drops_tick(S, ks) -> None:
    st = S.state
    st["kh_item_drops"] = []
    if not ks or not S.CFG.get("kh1_item_drops_draw", True):
        return
    raw = kh1_mem(S, KH_ITEM_SLOTS, 0x50 * 8)
    if not raw:
        return
    feed = mapfeed_active(S) and st.get("mf_xf") is not None and S.CFG.get("kh1_mapfeed_prizes", True)
    sc = max(S.frame.scale, 0.01)
    for i in range(8):
        ent = struct.unpack_from("<Q", raw, i * 0x50 + 8)[0]
        e = kh1_abs(S, ent, 0x170) if ent else None
        if not e:
            continue
        p = struct.unpack_from("<3f", e, 0x10)
        size = struct.unpack_from("<f", raw, i * 0x50 + 0x30)[0]
        size = size if 0.5 <= size <= 3.0 else 1.0
        d = S.CFG.get("kh_item_size", 34.0) * size * sc       # KH1 item ball ~34 cm at size 1.0 (KH1 screenshot)
        if feed:
            q = list(mapfeed_to_bl(S, p))
        else:
            q = kh1_point_to_bl(S, ks, p)
            q[2] = st.get("party_ground_feet", S.frame.pos[2])
        q[2] += d * 0.5                                        # entity origin = the ball's bottom
        # ball colour = KH1's own pick: item record (*(0x2D22D38) + (item - 1) * 0x14) byte 3 = class 0..8 ->
        # model 0x80000 + class // 3 (prize.ard: gold / purple, red, blue = sora_fx.bin tex 12 / 13 / 14)
        item = struct.unpack_from("<I", raw, i * 0x50 + 0x10)[0]
        rb = kh1_mem(S, 0x2D22D38, 8)
        rec = kh1_abs(S, int.from_bytes(rb, "little") + (item - 1) * 0x14, 4) if rb and 0 < item < 0x200 else None
        cls = rec[3] if rec and rec[3] < 9 else 0
        # 3D (user chose real models, session 9): KH1's own item ball mesh (tools/make_items.py -> data/ex_600x),
        # renderer v85 static model in enemy slot 16 + i; slow spin from the entity's frame counter (+0x16c, 60/s)
        drawn = False
        mid = 6000 + cls // 3
        if S.CFG.get("kh_item_3d", True) and hasattr(S.dll, "sr_set_enemy") and S.dll.sr_enemy_model(mid) > 0:
            af = ActorFrame()
            af.pos[:] = (q[0], q[1], q[2] - d * 0.5)               # the model's origin is its bottom too
            tick_ = struct.unpack_from("<f", e, 0x16C)[0]
            af.yaw = (tick_ / 60.0 * S.CFG.get("kh_item_spin", 1.0) if math.isfinite(tick_) else 0.0) + i
            af.scale, af.visible = sc * size, 1
            S.dll.sr_set_enemy(16 + i, ctypes.c_uint64(0), mid, ctypes.byref(af))
            drawn = True
        st["kh_item_drops"].append((q, d, i, 12 + cls // 3, drawn))
    S.status["kh_item_drops"] = len(st["kh_item_drops"])


def prize_orbs_tick(S, ks) -> None:
    """Live KH1 prizes -> st kh_prize_orbs [(BL pos, kind, slot)] for fill_fx."""
    st = S.state
    st["kh_prize_orbs"] = []
    if not ks or not S.CFG.get("kh1_prize_orbs", True):
        return
    raw = kh1_mem(S, KH_PRIZE_OBJS, 0x20 * 256)
    if not raw:
        return
    # height: KH1 prizes fall to the KH1 ROOM's floor, not the Borderlands floor KH1 Sora stands on (bridge) -> only
    # their bounce above their own resting point (lowest point seen, KH1 y grows down) counts, on BL Sora's ground
    # like Donald / Goofy (BL1E traces return nothing)
    rest = st.setdefault("kh_prize_rest", {})
    sc = max(S.frame.scale, 0.01)
    for k in range(256):
        if raw[k * 0x20 + 0x19] & 1 and raw[k * 0x20 + 0x18] in PRIZE_LOOK:
            p = struct.unpack_from("<3f", raw, k * 0x20)
            r = rest.get(k)
            if r is None or r[1] != raw[k * 0x20 + 0x18] or abs(p[0] - r[2]) + abs(p[2] - r[3]) > 600.0:
                r = [p[1], raw[k * 0x20 + 0x18], p[0], p[2]]       # new prize in this slot
            r[0], r[2], r[3] = max(r[0], p[1]), p[0], p[2]
            rest[k] = r
            if mapfeed_active(S) and st.get("mf_xf") is not None and S.CFG.get("kh1_mapfeed_prizes", True):
                # map feed: KH1's prizes lie / bounce on Borderlands' ground -> drawn exactly where KH1 has them
                st["kh_prize_orbs"].append((list(mapfeed_to_bl(S, p)), raw[k * 0x20 + 0x18], k))
                continue
            q = kh1_point_to_bl(S, ks, p)
            d = PRIZE_LOOK[raw[k * 0x20 + 0x18]][3] * sc * S.CFG.get("kh_prize_scale", 1.0)
            h = r[0] - p[1]                                  # bounce above its resting point
            # homing in on KH1 Sora (he stands on the bridge floor, above the room floor): height above HIS floor
            near = math.hypot(p[0] - ks["pos"][0], p[2] - ks["pos"][2])
            fr = st.get("kh_floor_ref") if st.get("kh_ground_active") and st.get("kh_floor_ref") is not None else ks["pos"][1]
            w = max(0.0, 1.0 - near / S.CFG.get("kh_prize_home_r", 350.0))
            h = h * (1.0 - w) + max(0.0, fr - p[1]) * w
            q[2] = st.get("party_ground_feet", S.frame.pos[2]) + min(250.0, h * sc) + d * 0.5
            st["kh_prize_orbs"].append((q, raw[k * 0x20 + 0x18], k))
        else:
            rest.pop(k, None)
    S.status["kh_prize_orbs"] = len(st["kh_prize_orbs"])
    if S.CFG.get("dbg_prize_float"):                       # test probe (game thread): each orb's height over BL ground
        rows = []
        for q_, kind_, k_ in st["kh_prize_orbs"][:6]:
            r_ = bl_trace(S, (q_[0], q_[1], q_[2] + 150), (q_[0], q_[1], q_[2] - 600))
            rows.append([k_, kind_, round(q_[0]), round(q_[1]), round(q_[2]), round(q_[2] - r_[0][2]) if r_ else None])
        S.status["prize_float"] = rows


def fill_fx(S, now: float) -> None:
    f = S.frame
    if not hasattr(f, "fx"):
        return
    textured = hasattr(f.fx[0], "tex")                # renderer v10 + new __init__ (else: old soft dots)
    n = 0

    def put(pos, size, color, a, pa=None, age=0.0, spr=None, rot=0.0, stretch=1.0, mode=0, flip=None):
        nonlocal n
        if n >= 256:
            return
        e = f.fx[n]
        e.pos[:] = pos
        e.size = size
        e.color[:] = (color[0], color[1], color[2], a)
        if textured:
            src = pa if pa is not None else {"spr": spr, "flip": flip, "fps": 24.0, "life": 1.0}
            tex, uv = sprite_uv(src, age)
            e.tex, e.uv[:] = tex, uv
            e.rot = (pa.get("rot", 0.0) + pa.get("spin", 0.0) * age) if pa is not None else rot
            e.stretch = pa.get("stretch", 1.0) if pa is not None else stretch
            e.mode = pa.get("mode", 0) if pa is not None else mode
        n += 1

    for pr in S.state["proj"]:
        if pr.get("hidden"):
            continue
        if now < pr["t0"]:
            continue
        age = now - pr["t0"]
        if pr["kind"] == "bolt":                  # KH1 Thunder: one crooked, flickering bolt from the sky + branches
            a = max(0.0, 1.0 - age / pr["life"])
            a *= 0.65 + 0.35 * random.random()
            x, y, z = pr["pos"]
            main = bolt_path((x + random.uniform(-50, 50), y + random.uniform(-50, 50), z + 600), (x, y, z + 30), n=16, jag=30.0)
            paths = [(main, 1.0)]
            for _ in range(2):                    # side branches off the main bolt
                k = random.randint(2, len(main) - 3)
                s0 = main[k]
                end = [s0[0] + random.uniform(-160, 160), s0[1] + random.uniform(-160, 160), s0[2] - random.uniform(120, 260)]
                paths.append((bolt_path(s0, end, n=5, jag=22.0), 0.55))
            if textured:
                for pts, wgt in paths:
                    for p0, p1 in zip(pts, pts[1:]):
                        d = [p1[i] - p0[i] for i in range(3)]
                        L = math.sqrt(sum(c * c for c in d)) or 1.0
                        mid = [(p0[i] + p1[i]) * 0.5 for i in range(3)]
                        rot = screen_rot(S, d)
                        put(mid, 60 * wgt, (1.0, 0.9, 0.45), a * 0.45, spr="soft", rot=rot, stretch=L / (60 * wgt) * 2.0)
                        put(mid, 16 * wgt, (1.0, 1.0, 0.95), a, spr="soft", rot=rot, stretch=L / (16 * wgt) * 2.2)
                        put(p1, 22 * wgt, (1.0, 1.0, 0.9), a, spr="soft")      # joints: no gaps between segments
            else:
                for p0 in main:
                    put(p0, 60, (1.0, 0.95, 0.5), a)
            put([x, y, z + 40], 130, (1.0, 0.95, 0.6), a * 0.35, spr="bigglow")
        elif pr["kind"] == "fire":                # KH1 Fire: a flickering ball of flame with a hot glow
            put(pr["pos"], pr["size"] * 2.0, (1.0, 0.55, 0.2), 0.6, spr="bigglow")
            put(pr["pos"], pr["size"] * 1.5, (1.0, 0.9, 0.7), 1.0, flip="fire", rot=age * 3.0)
        elif pr["kind"] == "ice":                 # KH1 Blizzard: a spinning ice crystal with a cold glint
            put(pr["pos"], pr["size"] * 2.2, (0.6, 0.85, 1.0), 0.5, spr="soft")
            put(pr["pos"], pr["size"] * 1.4, (0.9, 0.97, 1.0), 1.0, spr="diamond", rot=age * 9.0)
            put(pr["pos"], pr["size"] * 1.2, (1.0, 1.0, 1.0), 0.7, spr="cross", rot=age * 2.0)
        else:
            put(pr["pos"], pr["size"], pr["color"], 1.0, spr="soft")
    sc_ = max(getattr(f, "scale", 1.0), 0.01)
    for pos_, kind_, slot_ in S.state.get("kh_prize_orbs", []) if textured else []:
        tex_, tint_, glow_, d_ = PRIZE_LOOK[kind_]
        if n >= 252:
            break
        sz = d_ * sc_ * S.CFG.get("kh_prize_scale", 1.0)
        # mode 0x10 (renderer v82): drawn as a SPHERE (card pulled toward the camera) - no half orbs on slopes
        if glow_ is not None:                     # white halo (right half of the texture), tinted, additive
            put(pos_, sz * 2.2, glow_, 0.55, pa={"tex": tex_, "uv": (0.5, 0.0, 1.0, 1.0), "mode": 0x10 | 0})
        core_uv = (0.0, 0.0, 0.5, 1.0) if glow_ is not None else (0.5, 0.0, 1.0, 1.0)   # munny: faceted orb (tex 9 right)
        put(pos_, sz, tint_, 1.0, pa={"tex": tex_, "uv": core_uv, "mode": 0x10 | 1, "rot": now * 2.0 + slot_})
        if glow_ is None:                         # munny: clear bubble shell around the coin orb (tex 11 left half)
            put(pos_, sz * 1.5, (1.0, 1.0, 1.0), S.CFG.get("kh_munny_shell_a", 0.6), pa={"tex": 11, "uv": (0.0, 0.0, 0.5, 1.0), "mode": 0x10 | 1})
    for it_ in S.state.get("kh_item_drops", []) if textured else []:
        pos_, d_, slot_, tex_, drawn_ = (tuple(it_) + (12, False))[:5]   # (tolerates entries from an older logic reload)
        if drawn_:                                             # the real 3D model is drawn (renderer v85)
            continue
        if n >= 252:
            break
        # KH1's item ball (its own panel art from prize.ard as a shaded disc; sphere mode = no half balls on slopes)
        put(pos_, d_ * 1.5, S.CFG.get("kh_item_glow", (1.0, 0.85, 0.5)), 0.25, pa={"tex": 10, "uv": (0.5, 0.0, 1.0, 1.0), "mode": 0x10 | 0})
        put(pos_, d_, (1.0, 1.0, 1.0), 1.0, pa={"tex": tex_, "uv": (0.0, 0.0, 1.0, 1.0), "mode": 0x10 | 1, "rot": math.sin(now * 2.0 + slot_) * 0.25})
    for pa in S.state["parts"]:
        age = now - pa["t0"]
        if age >= 0 and textured and pa.get("align") and pa.get("orbit"):
            orb = pa["orbit"]                     # streak points along its circular path
            ang_ = orb[4] + orb[2] * age
            pa["rot"], pa["spin"] = screen_rot(S, (-math.sin(ang_) * orb[2], math.cos(ang_) * orb[2], orb[3])), 0.0
        if age >= 0:
            t = min(1.0, age / pa["life"])
            a = (1.0 - t) ** pa.get("fade_pow", 1.0)
            fin = pa.get("fade_in", 0.0)
            if fin and age < fin:
                a *= age / fin
            put(pa["pos"], pa["size"] * (1.0 + (pa.get("grow", 1.0) - 1.0) * t), pa["color"], a * pa.get("alpha", 1.0),
                pa=pa if (pa.get("spr") or pa.get("flip")) else None, age=age, spr="soft")
    f.nfx = n


# ---------------------------------------------------------------- HUD (KH1-style command menu)
_hud = {}


def _color(r, g, b, a=255):
    return unrealsdk.make_struct("Color", R=r, G=g, B=b, A=a)


def _rect(canvas, x, y, w, h, col):
    tex = _hud.get("white")
    if tex is None:
        try:
            tex = unrealsdk.find_object("Texture2D", "EngineResources.WhiteSquareTexture")
        except Exception:  # noqa: BLE001
            tex = False
        _hud["white"] = tex
    canvas.DrawColor = col
    if tex:
        canvas.SetPos(x, y)
        canvas.DrawTile(tex, w, h, 0, 0, 2, 2)
    else:
        for yy in range(int(y), int(y + h)):
            canvas.Draw2DLine(x, yy, x + w, yy, col)


def _text(canvas, x, y, s, col, scale):
    if "font" not in _hud:
        _hud["font"] = unrealsdk.find_object("Font", "ui_fonts.font_willowbody_18pt")
    canvas.Font = _hud["font"]
    canvas.DrawColor = col
    canvas.SetPos(x, y)
    canvas.DrawText(s, False, scale, scale)


def pool_values(pawn):
    """(current, max) health from the HealthPool property (no game function calls)."""
    try:
        pool = pawn.HealthPool.Data
        cur = float(pool.CurrentValue)
        mx = 0.0
        for name in ("MaxValue", "BaseMaxValue"):
            try:
                mx = float(getattr(pool, name))
                if mx > 0:
                    break
            except Exception:  # noqa: BLE001
                continue
        if mx <= 0:
            mx = float(pawn.HealthMaxVar) or max(cur, 1.0)
        return cur, mx
    except Exception:  # noqa: BLE001
        return 1.0, 1.0


def _disc(canvas, cx, cy, r, col):
    canvas.DrawColor = col
    ir = int(r)
    for dy in range(-ir, ir + 1):
        w = math.sqrt(max(0.0, r * r - dy * dy))
        canvas.Draw2DLine(cx - w, cy + dy, cx + w, cy + dy, col)


# ---------------------------------------------------------------- KH1 HUD (drawn by sora_render.dll with KH1's own art)
class HudSprite(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("x", ctypes.c_float), ("y", ctypes.c_float), ("w", ctypes.c_float), ("h", ctypes.c_float),
                ("u0", ctypes.c_float), ("v0", ctypes.c_float), ("u1", ctypes.c_float), ("v1", ctypes.c_float),
                ("color", ctypes.c_float * 4), ("tex", ctypes.c_int32), ("mode", ctypes.c_int32)]


class HudFrame(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("n", ctypes.c_int32), ("gauge", ctypes.c_float * 8), ("s", HudSprite * 160)]   # renderer v12: 160


KH_HP = 0x2D5CC4C          # KH1 Sora: int32 HP, max HP, MP, max MP (MP in gauge segments)


# ---------------------------------------------------------------- KH1 command menu (drawn by the renderer, KH1's own UI art)
# sora_hud.bin tex 3 = KH1's command-menu frame pieces (command2 uitex -4140), tex 4 = KH1 title words (COMMAND, MAGIC,
# ITEMS...), tex 5 = KH1's system font (menu/sysfont US, 36 px glyph pitch, 48 px rows). Pixel rects measured from the
# textures (passthrough notes in MODLOG).
KM_TEX_FRAME, KM_TEX_TITLE, KM_TEX_FONT = 3, 4, 5
KM_FRAME = {"tab": (3, 3, 253, 45), "row": (3, 47, 253, 93), "row_b": (3, 95, 253, 141),
            "fill_hi": (6, 150, 234, 167), "fill_mid": (3, 169, 235, 197), "fill": (3, 199, 251, 249),
            "sel_hi": (8, 252, 226, 266), "sel_mid": (7, 269, 233, 297), "sel": (5, 299, 248, 349), "slash": (6, 351, 37, 377)}
KM_FRAME_WH = (256.0, 384.0)
KM_TITLES = {"COMMAND": (5, 3, 156, 22), "MAGIC": (4, 25, 98, 44), "ITEMS": (4, 69, 84, 88)}
KM_FONT_ROWS = [" 0123456789ABC", "DEFGHIJKLMNOPQ", "RSTUVWXYZabcde", "fghijklmnopqrs", "tuvwxyz!?&%+-x"]
KM_GLYPHS = {'0': (40, 58), '1': (78, 86), '2': (112, 128), '3': (148, 164), '4': (184, 202), '5': (220, 236), '6': (256, 272),
             '7': (292, 308), '8': (328, 344), '9': (364, 380), 'A': (400, 418), 'B': (436, 454), 'C': (472, 488), 'D': (4, 22),
             'E': (40, 56), 'F': (76, 92), 'G': (112, 130), 'H': (148, 166), 'I': (186, 190), 'J': (220, 236), 'K': (256, 274),
             'L': (292, 308), 'M': (328, 350), 'N': (364, 382), 'O': (400, 418), 'P': (436, 454), 'Q': (472, 492), 'R': (4, 24),
             'S': (40, 56), 'T': (74, 90), 'U': (112, 130), 'V': (148, 166), 'W': (184, 210), 'X': (220, 238), 'Y': (256, 276),
             'Z': (292, 310), 'a': (328, 344), 'b': (364, 380), 'c': (400, 414), 'd': (436, 452), 'e': (472, 488), 'f': (4, 18),
             'g': (40, 56), 'h': (76, 92), 'i': (114, 118), 'j': (148, 158), 'k': (184, 199), 'l': (222, 226), 'm': (256, 280),
             'n': (292, 308), 'o': (328, 344), 'p': (364, 380), 'q': (400, 416), 'r': (436, 452), 's': (472, 488), 't': (4, 18),
             'u': (40, 56), 'v': (75, 93), 'w': (112, 136), 'x': (473, 489), 'y': (184, 204), 'z': (220, 236), '!': (258, 262),
             '?': (292, 308), '&': (328, 349), '%': (362, 386), '+': (400, 420), '-': (436, 456)}
KM_GLYPH_ROW = {ch: r for r, row in enumerate(KM_FONT_ROWS) for ch in row}


def km_sprite(hf, tex, rect, tw, th, x, y, w, h, color):
    if hf.n >= len(hf.s):
        return
    sp = hf.s[hf.n]
    sp.x, sp.y, sp.w, sp.h = x, y, w, h
    sp.u0, sp.v0, sp.u1, sp.v1 = rect[0] / tw, rect[1] / th, rect[2] / tw, rect[3] / th
    sp.color[:] = color
    sp.tex, sp.mode = tex, 0
    hf.n += 1


def km_piece(hf, name, x, y, w, h, color):
    km_sprite(hf, KM_TEX_FRAME, KM_FRAME[name], KM_FRAME_WH[0], KM_FRAME_WH[1], x, y, w, h, color)


def km_text(hf, text, x, y, height, color):
    """KH1 system-font text; returns its width. height = the 48 px glyph row on screen."""
    k = height / 48.0
    for ch in text:
        if ch == " ":
            x += 12 * k
            continue
        g, r = KM_GLYPHS.get(ch), KM_GLYPH_ROW.get(ch)
        if g is None or r is None:
            continue
        km_sprite(hf, KM_TEX_FONT, (g[0], r * 48, g[1], r * 48 + 48), 1024.0, 1024.0, x, y, (g[1] - g[0]) * k, height, color)
        x += (g[1] - g[0] + 3) * k
    return x


def kh_menu_sprites(S, hf, W, H) -> None:
    """KH1's command menu, bottom-left: the title tab (COMMAND / MAGIC / ITEMS in KH1's lettering) over navy row
    slots with a bright arrow-ended highlight on the selected row and KH1-font labels, like KH1's battle menu."""
    CFG = S.CFG
    m = menu_state(S)
    u = H / 1080.0 * CFG.get("kh_menu_scale", 1.0)
    rows = {0: MENU, 1: [n for n, _ in spell_list(S)] or ["(no magic)"], 2: [f"{n}  x{S.state['items'].get(n, 0)}" for n, _ in ITEMS],
            3: ability_list(S) or ["(none)"]}[m["level"]]
    title = {0: "COMMAND", 1: "MAGIC", 2: "ITEMS", 3: "COMMAND"}[m["level"]]
    sel = m["cur"] if m["level"] == 0 else m["sub"]
    rw, rh = 330 * u, 50 * u
    x0 = CFG.get("kh_menu_x", 46) * u + (24 * u if m["level"] else 0)   # submenus step out to the right, like KH1
    y_bottom = H - CFG.get("kh_menu_bottom", 250) * u
    y0 = y_bottom - rh * len(rows)
    navy, blue, light = (0.10, 0.16, 0.48, 0.95), (0.20, 0.36, 0.86, 0.85), (0.55, 0.72, 1.0, 0.55)
    mp_now = None
    if m["level"] == 1 and kh1_combat_on(S):
        stt = kh1_stats(S)
        mp_now = stt[2] if stt else None
    # title tab
    th = 44 * u
    km_piece(hf, "fill", x0, y0 - th, rw * 0.74, th, (0.06, 0.10, 0.34, 0.95))
    km_piece(hf, "tab", x0, y0 - th, rw * 0.74, th, (1, 1, 1, 1))
    t = KM_TITLES[title]
    tw = (t[2] - t[0]) / (t[3] - t[1]) * 22 * u
    km_sprite(hf, KM_TEX_TITLE, t, 256.0, 256.0, x0 + 18 * u, y0 - th + 11 * u, tw, 22 * u, (1.0, 1.0, 1.0, 1.0))
    for i, label in enumerate(rows):
        y = y0 + i * rh
        last = i == len(rows) - 1
        km_piece(hf, "row_b" if last else "row", x0, y, rw, rh, (1, 1, 1, 0.92))
        km_piece(hf, "fill", x0 + 16 * u, y + 6 * u, rw - 26 * u, rh - 12 * u, (0.13, 0.22, 0.62, 0.62))   # navy glass
        km_piece(hf, "fill_hi", x0 + 20 * u, y + 7 * u, rw - 50 * u, (rh - 12 * u) * 0.32, (0.45, 0.6, 1.0, 0.28))
        on = i == sel
        if on:   # selected: KH1's arrow-ended highlight, brighter on top
            pulse = 0.85 + 0.15 * math.sin(time.time() * 6.0)
            km_piece(hf, "sel", x0 + 8 * u, y + 5 * u, rw - 4 * u, rh - 10 * u, (blue[0] * pulse, blue[1] * pulse, blue[2], 0.95))
            km_piece(hf, "sel_hi", x0 + 14 * u, y + 7 * u, rw - 30 * u, (rh - 10 * u) * 0.3, light)
        col = (1.0, 1.0, 1.0, 1.0)
        if m["level"] == 1 and kh1_combat_on(S):
            spells_ = kh1_spells(S)
            if i < len(spells_) and mp_now is not None and mp_now < KH_MP_COST[spells_[i][1]]:
                col = (0.55, 0.55, 0.62, 1.0)
        km_text(hf, label, x0 + 34 * u, y + (rh - 30 * u) * 0.5 - 1 * u, 30 * u, col)
        if on:   # cursor: KH1's slash marker, bobbing left of the row
            bob = 4 * u * math.sin(time.time() * 8.0)
            km_piece(hf, "slash", x0 - 26 * u + bob, y + rh * 0.25, 22 * u, rh * 0.5, (1.0, 0.92, 0.55, 1.0))


def km_on(S) -> bool:
    """KH1 menu art needs renderer v12 (160 HUD sprites, sr_hud_max) and the KH1 HUD layer."""
    return bool(S.CFG.get("kh_menu", True)) and S.CFG.get("kh1_hud", True) and hasattr(S.dll, "sr_hud_max")         and S.dll.sr_hud_max() >= 160


def kh_hud_frame(W, H, hp, trail, mp_fill, mp_segs, face, low, cfg, alpha=1.0):
    """Layout in units of R (gauge outer radius), measured from KH1's own HUD. Keep in sync with passthrough/kh_hud_layout.py."""
    hf = HudFrame()
    R = cfg.get("hud_r", 0.13) * H
    cx = W - cfg.get("hud_dx", 1.45) * R
    cy = H - cfg.get("hud_dy", 1.3) * R
    hf.gauge[:] = (hp, max(hp, trail), mp_fill, float(mp_segs), low, 0, float(W), float(H))
    q = 1.06 * R
    k = 0.0089 * R; lx, ly = cx - 1.31 * R, cy + 0.07 * R
    ps = 1.18 * R
    sprites = [(cx - q, cy - q, 2 * q, 2 * q, 0, 0, 1, 1, -1, 1),                                        # rings
               (lx, ly, 45 * k, 19 * k, 8 / 256, 12 / 256, 53 / 256, 31 / 256, 1, 0),                      # "mp"
               (lx + 45 * k, ly, 46 * k, 19 * k, 72 / 256, 12 / 256, 118 / 256, 31 / 256, 1, 0),           # "HP"
               (cx - 0.02 * R - ps / 2, cy - 0.04 * R - ps / 2, ps, ps, face / 3, 0, (face + 1) / 3, 1, 0, 0)]  # portrait
    for i, (x, y, w, h, u0, v0, u1, v1, tex, mode) in enumerate(sprites):
        s = hf.s[i]
        s.x, s.y, s.w, s.h, s.u0, s.v0, s.u1, s.v1 = x, y, w, h, u0, v0, u1, v1
        s.color[:] = (1, 1, 1, alpha); s.tex, s.mode = tex, mode
    hf.n = len(sprites)
    return hf


def kh_reticle(hf, S, H) -> None:
    """KH1 lock-on cursor (ring with four arrows) on the locked target, gently pulsing."""
    pos = S.state.get("lock_screen")
    if not pos or hf.n >= len(hf.s):            # (was 16: the KH1 command menu fills more than that now)
        return
    if S.state.get("kh_intro_view"):             # user 2026-10-12: KH1 shows no lock-on cursor in a summon's cutscene
        return
    r = S.state.get("kh_ret")
    if r and r["w"] > 0:                         # KH1's own cursor sprite this frame (art, colour, size, animation)
        k = H / 448.0 * S.CFG.get("kh_ret_scale", 1.0)
        w, h = r["w"] * k, r["h"] * k
        s = hf.s[hf.n]
        s.x, s.y, s.w, s.h = pos[0] - w / 2, pos[1] - h / 2, w, h
        s.u0, s.v0, s.u1, s.v1 = r["u0"] / 128.0, r["v0"] / 128.0, r["u1"] / 128.0, r["v1"] / 128.0
        s.color[:] = tuple(min(2.0, c) for c in r["col"]); s.tex, s.mode = 2, 0
        hf.n += 1
        return
    if kh_real_lock(S):                          # KH1 marks nobody: no made-up cursor either
        return
    size = S.CFG.get("lock_size", 0.075) * H * (1.0 + 0.08 * math.sin(time.time() * 6))
    s = hf.s[hf.n]
    s.x, s.y, s.w, s.h = pos[0] - size / 2, pos[1] - size / 2, size, size
    s.u0, s.v0, s.u1, s.v1 = 66 / 256, 2 / 256, 126 / 256, 62 / 256
    s.color[:] = (1, 1, 1, 0.95); s.tex, s.mode = 2, 0
    hf.n += 1


def kh1_hud(S, cur: float, mx: float, W: float, H: float) -> None:
    """HP = Borderlands health (also mirrored into KH1 Sora's HP), MP segments = KH1 max MP."""
    st, now = S.state, time.time()
    hp = max(0.0, min(1.0, cur / mx if mx else 0.0))
    prev = st.get("hud_hp", hp)
    if hp < prev - 1e-4 and now < st.get("aero_until", 0) and st.get("aero_pawn") is not None:
        back = (prev - hp) * S.CFG.get("aero_guard", 0.4)          # KH1 Aero lowers damage taken
        S.pending.append(lambda pawn=st["aero_pawn"], back=back: _heal_now(S, pawn, back))
    if hp < prev - 1e-4:
        st["hud_hurt_t"] = now
        st["hud_trail_t"] = now
        st["hud_trail"] = max(st.get("hud_trail", prev), prev)
    trail = st.get("hud_trail", hp)
    if now - st.get("hud_trail_t", 0) > 0.6:                       # red "just lost" section drains after a moment
        trail = max(hp, trail - (now - st.get("hud_last", now)) * 0.35)
    st["hud_trail"], st["hud_hp"], st["hud_last"] = trail, hp, now
    face = 2 if cur <= 0 else (1 if now - st.get("hud_hurt_t", 0) < 0.6 else 0)
    low = (0.5 + 0.5 * math.sin(now * 7)) if hp < 0.25 else 0.0
    segs, kh_cur = 5, None
    ks = kh1_state(S) if S.CFG["flags"] & 64 else None
    if ks:
        import struct
        h, base = S.kh["h"], S.kh["base"]
        buf = ctypes.create_string_buffer(16)
        if ctypes.windll.kernel32.ReadProcessMemory(ctypes.c_void_p(h), ctypes.c_void_p(base + KH_HP), buf, 16, None):
            kh_cur, kh_max, kh_mp, kh_mpmax = struct.unpack("<4i", buf.raw)
            if 1 <= kh_mpmax <= 20:
                segs = kh_mpmax
            # never 0: KH1 Sora must not die. Rounded DOWN (full only when Borderlands is full): rounded to nearest,
            # 99 % read as 45 / 45 and an HP orb (+1 / +5 KH1 HP) healed nothing (user 2026-10-06: orbs must heal
            # "at the accurate level" = KH1's own amount as a share of max HP, via kh_hp_heal_sync)
            want = (kh_max if hp >= 0.9999 else max(1, min(kh_max - 1, int(hp * kh_max)))) if kh_max > 0 else None
            if kh_max > 0:
                # HP both ways (user 2026-10-05): KH1's gauge shows Borderlands health (floor 1 - Borderlands owns
                # death and its Fight For Your Life knockdown); anything that changes KH1 HP on KH1's side since our
                # last write crosses over: heals (Donald's Cure, Cure, Potions, HP orbs) heal Borderlands Sora,
                # free-Heartless hits hurt him. kh_hp_seen = the value we last wrote / accepted.
                seen = st.get("kh_hp_seen")
                st["kh_hp_seen"] = kh_cur
                pawn = st.get("pawn_ref")
                if seen is not None and kh_cur > seen and pawn is not None and S.CFG.get("kh_hp_heal_sync", True):
                    frac = (kh_cur - seen) / kh_max
                    S.pending.append(lambda pawn=pawn, frac=frac: _kh1_heal_now(S, pawn, frac))
                    st["kh_hp_hold"] = now + 0.3          # let the heal land before writing Borderlands HP back
                    S.status["kh1_heal"] = [seen, kh_cur, round(frac, 3)]
                elif seen is not None and kh_cur < seen and st.get("kh_free") and pawn is not None:
                    frac = (seen - kh_cur) / kh_max * S.CFG.get("kh_free_dmg", 0.6)
                    S.pending.append(lambda pawn=pawn, frac=frac: _hurt_player_now(S, pawn, frac))
                    st["hud_hurt_t"] = now
                    st["kh_hp_hold"] = now + 0.3
                if not S.CFG.get("kh_hp_mirror", True):
                    want = kh_max   # old behaviour: KH1 held at full HP
                if now < st.get("kh_hp_hold", 0):
                    want = None
            if want is not None and want != kh_cur and S.CFG.get("kh_hp_sync", True) and not kh1_foreground(S):
                v = ctypes.c_int32(want)
                st["kh_hp_seen"] = want
                ctypes.windll.kernel32.WriteProcessMemory(ctypes.c_void_p(h), ctypes.c_void_p(base + KH_HP), ctypes.byref(v), 4, None)
    mp_fill = max(0.0, min(1.0, st.get("mp", 0) / MP_MAX)) * segs
    if ks and kh_cur is not None and kh1_combat_on(S):
        mp_fill = float(kh_mp)
    if S.CFG.get("kh1_ui_native", True):   # user 2026-10-05: never the mod's own gauge / menu (it flashed up during
        # casts / glides whenever kh1_combat_on blinked off) - only KH1's own HUD / command menu exist
        hf = HudFrame()                                  # v52: KH1's own HUD + command menu are drawn natively;
        hf.gauge[6], hf.gauge[7] = float(W), float(H)    # only the lock reticle (added in tick) uses this frame
    else:
        hf = kh_hud_frame(W, H, hp, trail, mp_fill, segs, face, low, S.CFG)
        if km_on(S):
            kh_menu_sprites(S, hf, W, H)
    st["hud_hf"], st["canvas_wh"] = HudFrame.from_buffer_copy(hf), (W, H)   # reticle is added in tick (lock_reproject)
    if lock_target(S) is None:
        S.dll.sr_set_hud(ctypes.byref(hf))


def draw_kh_gauge(S, pc, canvas, W, H, u) -> None:
    """KH1-style HP/MP: round portrait frame bottom-right, green HP bar and blue MP bar extending left."""
    cur, mx = pool_values(pc.Pawn)
    S.status["hp"] = [round(cur), round(mx)]
    if hasattr(S.dll, "sr_set_hud") and S.CFG.get("kh1_hud", True):
        kh1_hud(S, cur, mx, W, H)
        return
    cx, cy, r = W - 130 * u, H - 250 * u, 72 * u
    hp_len = (260 + min(mx, 600) * 0.35) * u            # bar grows with max HP like KH
    mp_len = 240 * u
    by = cy + 22 * u                                      # HP bar sits along the lower half of the circle
    # HP bar: black frame, dark track, green fill (yellow/red when low)
    _rect(canvas, cx - hp_len - 6 * u, by - 3 * u, hp_len + 10 * u, 26 * u, _color(0, 0, 0, 220))
    _rect(canvas, cx - hp_len - 3 * u, by, hp_len, 20 * u, _color(30, 40, 30, 230))
    frac = max(0.0, min(1.0, cur / mx if mx else 0.0))
    col = _color(70, 230, 60) if frac > 0.35 else (_color(240, 200, 40) if frac > 0.15 else _color(240, 60, 40))
    _rect(canvas, cx - 3 * u - hp_len * frac, by + 2 * u, hp_len * frac, 16 * u, col)
    _rect(canvas, cx - 3 * u - hp_len * frac, by + 2 * u, hp_len * frac, 4 * u, _color(200, 255, 190, 160))
    # MP bar under it
    my = by + 30 * u
    _rect(canvas, cx - mp_len - 6 * u, my - 3 * u, mp_len + 10 * u, 20 * u, _color(0, 0, 0, 220))
    _rect(canvas, cx - mp_len - 3 * u, my, mp_len, 14 * u, _color(20, 25, 50, 230))
    mfrac = max(0.0, min(1.0, S.state.get("mp", 0) / MP_MAX))
    _rect(canvas, cx - 3 * u - mp_len * mfrac, my + 2 * u, mp_len * mfrac, 10 * u, _color(60, 140, 255))
    _text(canvas, cx - mp_len - 50 * u, my - 6 * u, "MP", _color(150, 200, 255), 1.0 * u)
    # portrait frame: gold ring, blue disc, green HP ring segment
    _disc(canvas, cx, cy, r + 6 * u, _color(230, 190, 70))
    _disc(canvas, cx, cy, r, _color(25, 45, 120))
    _disc(canvas, cx, cy, r * 0.82, _color(60, 100, 200))
    _text(canvas, cx - 40 * u, cy - 22 * u, "SORA", _color(255, 255, 255), 1.35 * u)
    _text(canvas, cx - 30 * u, cy + 14 * u, f"{int(cur)}", _color(200, 255, 190), 1.0 * u)


class _WorldJobs(list):
    """S.pending with every job tied to the world + pawn it was queued in (session 9: 'General protection fault' when
    backing out to the main menu). __init__'s PlayerTick hook runs the jobs BEFORE the next PostRender can notice the
    map change, on the menu's / new map's controller - and the jobs still held the old map's destroyed pawn. A job now
    runs only while the player's world and pawn are the ones it was queued for; otherwise it is dropped."""

    def append(self, job):
        pc = get_pc_safe()
        try:
            key = (str(pc.WorldInfo), pc.Pawn) if pc is not None else None
        except Exception:  # noqa: BLE001
            key = None

        def run(job=job, key=key):
            if key is None or key[1] is None:
                return
            pc_ = get_pc_safe()
            try:
                ok = pc_ is not None and pc_.Pawn is not None and pc_.Pawn == key[1] and str(pc_.WorldInfo) == key[0]
            except Exception:  # noqa: BLE001
                ok = False
            if ok:
                job()
            else:
                S_ = _S_ref.get("S")
                if S_ is not None:
                    S_.status["jobs_dropped"] = S_.status.get("jobs_dropped", 0) + 1
        super().append(run)


_S_ref = {}


def _guard_jobs(S) -> None:
    _S_ref["S"] = S
    if type(S.pending) is not _WorldJobs:                     # PlayerTick swaps in a plain [] after each run
        S.pending = _WorldJobs(S.pending)


def draw_hud(S, pc, canvas) -> None:
    _guard_jobs(S)
    if not S.CFG.get("hud", True):
        return
    if canvas is None or pc is None or pc.Pawn is None:
        return
    try:
        if pc.IsPaused() or pc.bStatusMenuOpen:
            return
    except Exception:  # noqa: BLE001
        pass
    lock_project(S, canvas)
    if S.CFG.get("kh1_ui_native", True):
        # vanilla only (no kh1_combat_on condition: its blinks showed the old mod HUD - user 2026-10-05): KH1 draws its own HUD / command menu (renderer khui); the mod adds nothing but the KH1 lock
        # reticle. kh1_hud still mirrors Borderlands' health into KH1 Sora's HP and sends the reticle-only frame.
        cur, mx = pool_values(pc.Pawn)
        # (MENU CRASH 2026-10-12: kh1_hud heals / hurts st pawn_ref, which only heartless_tick refreshed - after the main
        #  menu -> load save it was the OLD map's destroyed pawn -> pyunrealsdk access violation. Always the live one.)
        S.state["pawn_ref"] = pc.Pawn
        kh1_hud(S, cur, mx, float(canvas.ClipX), float(canvas.ClipY))
        return
    m = menu_state(S)
    H = float(canvas.ClipY)
    u = H / 1080.0
    x0, row, pw = 40 * u, 46 * u, 300 * u
    if km_on(S):                                   # KH1's own menu is drawn by the renderer (kh_menu_sprites)
        if S.CFG.get("kh_gauge", True):
            draw_kh_gauge(S, pc, canvas, float(canvas.ClipX), H, u)
        msg, t = S.state["msg"]
        if msg and time.time() - t < 1.5:
            _text(canvas, x0, H - 470 * u, msg, _color(255, 220, 120), 1.2 * u)
        return
    lists = {0: MENU, 1: [n for n, _ in spell_list(S)] or ["(no magic)"], 2: [n for n, _ in ITEMS], 3: ability_list(S) or ["(none)"]}
    rows = lists[m["level"]]
    title = {0: "COMMAND", 1: "MAGIC", 2: "ITEMS", 3: "ABILITIES"}[m["level"]]
    y0 = H - 150 * u - row * len(rows) - 60 * u
    _rect(canvas, x0, y0 - 34 * u, 150 * u, 34 * u, _color(30, 60, 170, 230))
    _text(canvas, x0 + 14 * u, y0 - 31 * u, title, _color(255, 255, 255), 1.1 * u)
    _rect(canvas, x0, y0, pw, row * len(rows) + 12 * u, _color(5, 10, 40, 200))
    _rect(canvas, x0, y0, pw, 3 * u, _color(120, 170, 255, 255))
    _rect(canvas, x0, y0 + row * len(rows) + 9 * u, pw, 3 * u, _color(120, 170, 255, 255))
    sel = m["cur"] if m["level"] == 0 else m["sub"]
    for i, name in enumerate(rows):
        yy = y0 + 6 * u + i * row
        if i == sel:
            _rect(canvas, x0 + 6 * u, yy + 4 * u, pw - 12 * u, row - 8 * u, _color(70, 130, 255, 210))
            _text(canvas, x0 - 26 * u, yy + 6 * u, ">", _color(255, 230, 120), 1.4 * u)
        label, col = name, _color(255, 255, 255)
        if m["level"] == 1 and kh1_combat_on(S):
            stt = kh1_stats(S)
            if stt and stt[2] < 1:
                col = _color(140, 140, 160)
        elif m["level"] == 1 and i < len(SPELLS):
            cost = SPELLS[i][1]
            if S.state["mp"] < cost:
                col = _color(140, 140, 160)
            _text(canvas, x0 + pw - 70 * u, yy + 10 * u, f"{cost}", _color(150, 200, 255), 1.0 * u)
        elif m["level"] == 2:
            label = f"{name}  x{S.state['items'].get(name, 0)}"
        _text(canvas, x0 + 22 * u, yy + 6 * u, label, col, 1.25 * u)
    if S.CFG.get("kh_gauge", True):
        draw_kh_gauge(S, pc, canvas, float(canvas.ClipX), H, u)
    msg, t = S.state["msg"]
    if msg and time.time() - t < 1.5:
        _text(canvas, x0, y0 - 70 * u, msg, _color(255, 220, 120), 1.2 * u)


# ================================================================ resync (GTA mod's F8 "re-level") and director
def kh1_resync(S) -> None:
    """F5 (was F9): puts KH1 back in order - re-anchor Sora, re-measure the Borderlands floor, land him, re-pair the
    enemy stand-ins, warp home if he wandered off, forget the KH1 camera estimate."""
    st = S.state
    if _key(kh_menu_vk(S)):
        S.status["resync"] = "ignored (F9 = KH1 menu)"   # the old keybind still says F9 until Borderlands restarts
        return
    st["kh_anchor_room"] = None
    st["kh_floor_ref"] = None
    st["kh_pmap"] = {}
    st["kh_proxy_scan"] = st["kh_dummy_scan"] = 0
    st["kh_home_done"] = False
    st["kh_cam_c"] = None
    st["kh_longfall"] = False
    sh = kh1_shared(S)
    if sh and kh1_bridge_v3(S):
        sh_u32(sh, SH_FLOOR_ON, 1)
        sh_u32(sh, SH_SNAP_SEQ, sh_u32(sh, SH_SNAP_SEQ) + 1)
    try:
        S.dll.sr_set_pin(0, ctypes.c_float(0))
    except Exception:  # noqa: BLE001
        pass
    kh1_event(S, "resync (F9)")
    say(S, "KH1 resynced")


# Director: repeatable scripted scenes for testing and video takes (the GTA mod's director.py, run inside the mod).
# config.json: {"director": "shots/name.json", "director_nonce": N} starts that shot list (path relative to this mod).
# A shot list is a JSON list of steps; every step waits "t" seconds after it starts (default 0):
#   {"op": "say", "text": "..."}                    on-screen message
#   {"op": "move", "yaw": 90, "mag": 1, "t": 2}     hold movement toward a world yaw (deg, or "cam") for t seconds
#   {"op": "stop"}                                  release movement
#   {"op": "attack", "n": 3, "gap": 0.35}           left-click presses
#   {"op": "cast", "spell": "Fire"}                 cast through the command menu logic
#   {"op": "jump"} / {"op": "glide"}                jump / press jump again in the air (glide toggle)
#   {"op": "dodge"}                                 crouch press (KH1 Dodge Roll / Guard)
#   {"op": "lock"}                                  lock-on toggle
#   {"op": "face", "yaw": 0}                        camera yaw (deg)
#   {"op": "teleport", "x": 0, "y": 0, "z": 0}      move the Borderlands pawn
#   {"op": "cfg", "set": {"relight": 0}}            change config values for the rest of the session
#   {"op": "resync"}                                F9
#   {"op": "mark", "name": "hit test"}              marker in director_log.txt
# Every step is written to director_log.txt with the time and a status snapshot (evidence per run).
def director_log(S, text: str) -> None:
    try:
        p = HERE / "director_log.txt"
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {time.time() % 1000:8.2f} {text}\n")
    except Exception:  # noqa: BLE001
        pass


def director_tick(S, pc, now: float) -> None:
    st, CFG = S.state, S.CFG
    name, nonce = CFG.get("director"), CFG.get("director_nonce")
    if name and nonce is not None and st.get("dir_nonce") != nonce:
        st["dir_nonce"] = nonce
        try:
            path = Path(name) if Path(name).is_absolute() else HERE / name
            st["dir_steps"], st["dir_i"], st["dir_next"] = json.loads(path.read_text()), 0, now
            director_log(S, f"=== start {name} ({len(st['dir_steps'])} steps)")
        except Exception as ex:  # noqa: BLE001
            S.status["director"] = f"ERR {ex}"
            return
    steps = st.get("dir_steps")
    if not steps:
        return
    if st.get("dir_move") and now > st["dir_move"][2]:
        st["dir_move"] = None
    for t, kind in list(st.get("dir_clicks", [])):      # scheduled attack presses
        if now >= t:
            st["dir_clicks"].remove((t, kind))
            kh_attack(S, pc)
    if now < st.get("dir_next", 0):
        return
    i = st.get("dir_i", 0)
    if i >= len(steps):
        director_log(S, "=== done " + json.dumps({k: S.status.get(k) for k in ("kh1_proxies", "kh_ground", "kh1_state", "hp")}))
        st["dir_steps"] = None
        S.status["director"] = "done"
        return
    step = steps[i]
    st["dir_i"], st["dir_next"] = i + 1, now + float(step.get("t", 0.0))
    op = step.get("op")
    S.status["director"] = [i, op]
    director_log(S, f"#{i} {json.dumps(step)} | state {S.status.get('kh1_state')} proxies {S.status.get('kh1_proxies')}")
    pawn = pc.Pawn
    try:
        if op == "say":
            say(S, step.get("text", ""))
        elif op == "move":
            y = step.get("yaw", "cam")
            yaw = int(pc.Rotation.Yaw) * UU if y == "cam" else math.radians(float(y))
            st["dir_move"] = (yaw, float(step.get("mag", 1.0)), now + float(step.get("t", 1.0)))
        elif op == "stop":
            st["dir_move"] = None
        elif op == "attack":
            gap = float(step.get("gap", 0.35))
            st.setdefault("dir_clicks", []).extend((now + k * gap, "atk") for k in range(int(step.get("n", 1))))
        elif op == "cast":
            want = step.get("spell", "Fire")
            if kh1_combat_on(S):
                names = [n for n, _, _ in kh1_spells(S)]
                hit = [k for k, n in enumerate(names) if n.lower().startswith(want.lower()[:4])]
                if hit:
                    kh1_cast(S, pc, hit[0])
            else:
                cost = dict(SPELLS).get(want, 10)
                cast(S, pc, want, cost)
        elif op == "jump":
            S.pending.append(lambda: setattr(pc, "bPressedJump", True))
        elif op == "glide":
            st["dir_space"] = now + float(step.get("hold", 0.15))
        elif op == "dodge":
            st["dir_duck"] = now + 0.15
        elif op == "lock":
            lock_toggle(S, pc)
        elif op == "face":
            yaw = math.radians(float(step.get("yaw", 0)))
            S.pending.append(lambda: setattr(pc, "Rotation", unrealsdk.make_struct(
                "Rotator", Pitch=int(pc.Rotation.Pitch), Yaw=int(yaw / UU), Roll=0)))
        elif op == "teleport":
            x, y, z = (float(step.get(k, 0.0)) for k in ("x", "y", "z"))
            S.pending.append(lambda: setattr(pawn, "Location", unrealsdk.make_struct("Vector", X=x, Y=y, Z=z)))
        elif op == "cfg":
            S.CFG.update(step.get("set", {}))
        elif op == "resync":
            kh1_resync(S)
        elif op == "mark":
            pass
    except Exception as ex:  # noqa: BLE001
        director_log(S, f"   error {type(ex).__name__}: {ex}")


def _fld(o, name, default=None):
    try:
        return getattr(o, name)
    except Exception:  # noqa: BLE001
        return default


def level_lights(S) -> dict:
    """The level's sun (brightest DirectionalLightComponent) and sky light, read once per map (find_all is slow and
    walking GObjects mid-level has crashed BL before - only at the first tick of a new world)."""
    st = S.state
    key = st.get("world_key", "")
    if st.get("lights_key") == key and "lights" in st:
        return st["lights"]
    out = {"sun": None, "sky": None, "n_dir": 0, "n_sky": 0}
    try:
        best = -1.0
        for c in unrealsdk.find_all("DirectionalLightComponent", exact=False):
            if str(c.Name).startswith("Default__") or not _fld(c, "bEnabled", True):
                continue
            out["n_dir"] += 1
            col = _fld(c, "LightColor")
            b = float(_fld(c, "Brightness", 0.0) or 0.0)
            owner = _fld(c, "Owner")
            rot = _fld(owner, "Rotation") if owner is not None else None
            if b > best and rot is not None:
                best = b
                out["sun"] = {"b": b, "col": [col.R, col.G, col.B] if col else [255, 255, 255],
                              "rot": [rot.Pitch, rot.Yaw, rot.Roll], "name": str(c.Name), "owner": str(owner.Name)}
        best = -1.0
        for c in unrealsdk.find_all("SkyLightComponent", exact=False):
            if str(c.Name).startswith("Default__") or not _fld(c, "bEnabled", True):
                continue
            out["n_sky"] += 1
            b = float(_fld(c, "Brightness", 0.0) or 0.0)
            if b > best:
                best = b
                col, low = _fld(c, "LightColor"), _fld(c, "LowerColor")
                out["sky"] = {"b": b, "col": [col.R, col.G, col.B] if col else None,
                              "lb": float(_fld(c, "LowerBrightness", 0.0) or 0.0),
                              "lcol": [low.R, low.G, low.B] if low else None}
    except Exception as ex:  # noqa: BLE001
        out["error"] = f"{type(ex).__name__}: {ex}"
    st["lights"], st["lights_key"] = out, key
    S.status["level_lights"] = out
    return out


def light_tick(S, frame) -> None:
    """Renderer v25: light the KH models with the level's own sun (direction + colour) and sky light (sky from above,
    ground bounce from below). Config: light_level (0 = old fixed light), light_amb, light_sun, light_sky_tint,
    light_rim, light_soft, light_self_shadow, light_rim_pow."""
    CFG = S.CFG
    L = level_lights(S)
    sun = L.get("sun")
    if sun and CFG.get("light_level", True):
        p, y = (v * math.tau / 65536.0 for v in sun["rot"][:2])
        frame.light[:] = (math.cos(p) * math.cos(y), math.cos(p) * math.sin(y), math.sin(p))
    else:
        frame.light[:] = (-0.4, 0.3, -0.85)
    dll = S.dll
    if not hasattr(dll, "sr_light"):
        return

    def norm(c, tint):                                      # colour with luminance 1, pulled toward white by (1 - tint)
        if not c:
            return [1.0, 1.0, 1.0]
        lum = max(1e-3, 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2])
        return [1.0 + (v / lum - 1.0) * tint for v in c]

    if not (sun and CFG.get("light_level", True)):
        p = [0.0] * 16
    else:
        sky = L.get("sky") or {}
        tint = CFG.get("light_sky_tint", 0.6)
        sc = [v / max(1, *sun["col"]) * CFG.get("light_sun", 1.0) for v in sun["col"]]
        up = norm(sky.get("col"), tint)
        ratio = (sky.get("lb", 0.0) / sky["b"]) if sky.get("b") else 0.4
        dn = [v * min(1.0, max(0.5, 0.5 + ratio)) for v in norm(sky.get("lcol") or [200, 190, 170], tint)]
        p = [*sc, CFG.get("light_amb", 0.5), *up, CFG.get("light_rim", 0.25), *dn, CFG.get("light_soft", 0.06),
             CFG.get("light_self_shadow", 0.6), CFG.get("light_rim_pow", 3.0), 0.0, 0.0]
    if S.state.get("light_sent") != p:
        S.state["light_sent"] = p
        dll.sr_light((ctypes.c_float * 16)(*[float(x) for x in p]), 16)
        S.status["light"] = [round(x, 3) for x in p[:14]]


def shadow_tick(S) -> None:
    """Renderer v24: Sora, Donald, Goofy and the Heartless cast shadows on the Borderlands world (config shadow_*)."""
    CFG = S.CFG
    dll = S.dll
    if not hasattr(dll, "sr_shadow"):
        return
    p = (CFG.get("shadow", 0.5), CFG.get("shadow_fade", [220.0, 700.0])[0], CFG.get("shadow_fade", [220.0, 700.0])[1],
         CFG.get("shadow_soft", 1.6), *CFG.get("shadow_dir", [0.0, 0.0, 0.0]), CFG.get("shadow_extent", 1600.0))
    if S.state.get("shadow_sent") != p:
        S.state["shadow_sent"] = p
        dll.sr_shadow((ctypes.c_float * 8)(*[float(x) for x in p]), 8)
    if hasattr(dll, "sr_outline"):                       # v30 ink lines: outline (strength, 0 = off), outline_width (px at 1080p),
        ol = (0.0 if S.state.get("outlines_off") else CFG.get("outline", 0.9), CFG.get("outline_width", 1.6), CFG.get("outline_rel", 0.035), CFG.get("outline_abs", 4.0),
              *CFG.get("outline_color", [0.06, 0.045, 0.04]))   # outline_rel / _abs = depth step that counts as an edge
        if S.state.get("outline_sent") != ol:
            S.state["outline_sent"] = ol
            dll.sr_outline((ctypes.c_float * 7)(*[float(x) for x in ol]), 7)
    S.status["shadows"] = dll.sr_shadow_count()
    if hasattr(dll, "sr_ground_light"):                  # v26: light of the ground he stands on (calibrates relight_ground_ref)
        g = (ctypes.c_float * 8)()
        dll.sr_ground_light(g)
        S.status["ground_light"] = [round(x, 3) for x in g]


def polish_frame(S, frame, pawn) -> None:
    """Relight from Borderlands' own picture, haze, and the close-camera fade (GTA's 'camera inside Steve' fix)."""
    CFG = S.CFG
    if not hasattr(frame, "fade"):
        return
    # camera pulled into Sora (Borderlands pulls its camera in against walls): fade him out with a dither
    head = (frame.pos[0], frame.pos[1], frame.pos[2] + 100.0 * frame.scale)
    d = math.dist(tuple(frame.cam_pos), head)
    near0, near1 = CFG.get("fade_near", [45.0, 110.0])
    fade = max(0.0, min(1.0, (d - near0) / max(1.0, near1 - near0)))
    frame.fade = fade if fade > 0.0 else 0.0005          # (exactly 0 means "old struct" to the renderer)
    frame.relight[:] = (CFG.get("relight", 0.8), CFG.get("relight_tint", 0.5), CFG.get("relight_ref", 0.3),
                        CFG.get("relight_mip", 6.0))
    frame.relight2[:] = (CFG.get("relight_min", 0.3), CFG.get("relight_max", 1.3), CFG.get("relight_ground_ref", 0.45), 0.0)
    frame.haze[:] = (CFG.get("haze_start", 3000.0), CFG.get("haze_range", 9000.0), CFG.get("haze_max", 0.0), 0.0)
    S.status["fade"] = round(fade, 2)


# ---------------------------------------------------------------- Keyblade hits from the real blade (renderer v8)
# Like the GTA mod tracing Minecraft's arrows through GTA's world: the renderer exports the Keyblade's hilt and tip
# exactly as drawn (sr_get_blade). While KH1 Sora swings, the blade is swept from its last pose to this one and any
# Borderlands enemy whose collision cylinder it passes through takes the hit - once per enemy per swing step.
# No blade contact, no damage; the KH1 stand-ins only steer KH1 (targeting, dash, combos).
KH_FINISHERS = {203, 213}


def blade_on(S) -> bool:
    return bool(S.CFG.get("kh_blade_hits", True)) and hasattr(S.dll, "sr_get_blade") and kh1_combat_on(S)


def _seg_points(a, b, n):
    return [[a[i] + (b[i] - a[i]) * k / n for i in range(3)] for k in range(n + 1)]


def kh1_attacking_now(S, ks) -> bool:
    """A KH1 swing, or anything KH1 does while one of our abilities runs (Sonic Blade, Strike Raid ... use their own
    animations, not the 200-239 combo swings)."""
    # ground swings run in state 6; air swings (204...) stay in state 2 - the swing animation alone decides
    if ks["anim"] in KH_SWING_ANIMS:
        return True
    return bool(S.state.get("kh_abil")) and ks["anim"] not in (0, 1, 2, 5, 6, 7, 9, 10, 11, 12) and ks["anim"] not in KH_CAST_ANIM


def kh1_blade_tick(S, pc, ks, now: float) -> None:
    st, CFG = S.state, S.CFG
    out = (ctypes.c_float * 8)()
    if not S.dll.sr_get_blade(out) or out[6] > 0.1:
        st["blade_prev"] = None
        return
    cur = (tuple(out[0:3]), tuple(out[3:6]))
    prev = st.get("blade_prev") or cur
    st["blade_prev"] = cur
    swinging = kh1_attacking_now(S, ks)
    if not swinging:
        st["blade_swing"] = None
        return
    if ks["anim"] != st.get("blade_swing"):               # new combo step: everyone can be hit again
        st["blade_swing"], st["blade_hit"] = ks["anim"], set()
        if ks["anim"] in CFG.get("kh_finisher_anims", [203, 213]):
            voice(S, "vfin", chance=0.8, cd=0.9)
        else:
            voice(S, "vatk", chance=CFG.get("voice_atk_chance", 0.4), cd=0.45)
        if CFG.get("sfx_swing", False):
            sfx(S, "swing", 0.6)
    # sweep: blade samples along its length, at several moments between the previous and current pose
    pts = []
    for t in (0.0, 0.33, 0.67, 1.0):
        h = [prev[0][i] + (cur[0][i] - prev[0][i]) * t for i in range(3)]
        tip = [prev[1][i] + (cur[1][i] - prev[1][i]) * t for i in range(3)]
        # a little past the tip: the swing trail reaches further than the mesh
        ext = [tip[i] + (tip[i] - h[i]) * CFG.get("blade_extend", 0.15) for i in range(3)]
        pts += _seg_points(h, ext, 6)
    mid = [sum(p_[i] for p_ in pts) / len(pts) for i in range(3)]
    pad = CFG.get("blade_pad", 15.0)
    for dist, p, _ in enemies(S, pc, 500.0, origin=mid):
        key = bl_key(p)
        if key in st["blade_hit"]:
            continue
        try:
            loc = p.Location
            cc = p.CylinderComponent
            r, hh = float(cc.CollisionRadius) + pad, float(cc.CollisionHeight) + pad
            ex, ey, ez = float(loc.X), float(loc.Y), float(loc.Z)
        except Exception:  # noqa: BLE001
            continue
        hit_at = next((q for q in pts if math.hypot(q[0] - ex, q[1] - ey) < r and abs(q[2] - ez) < hh), None)
        if hit_at is None:
            continue
        st["blade_hit"].add(key)
        fin = ks["anim"] in KH_FINISHERS
        mult = CFG.get("kb_finisher_mult", 1.6) if fin else 1.0
        if CFG.get("kb_kh1_damage", True):
            dmg = kb_damage(S, pc, p, mult, 0, "blade-fin" if fin else "blade")
        else:
            dmg = max_hp_share(p, CFG.get("sora_hit_frac", 0.10)) * mult
        # KH1 hit feel: the pose bites for a moment, KH1's hit spark, and combo finishers launch the enemy
        if hasattr(S.dll, "sr_hitstop") and CFG.get("hitstop", False):   # vanilla audit: KH1 has no hit-freeze (off)
            S.dll.sr_hitstop(ctypes.c_float(CFG.get("hitstop_finisher", 0.08) if fin else CFG.get("hitstop_hit", 0.05)))
        if invented_ok(S): _part(S, pos=list(hit_at), size=110 if fin else 80, color=(1.0, 0.95, 0.8), life=0.13, spr="burst", rot=random.uniform(0, 6.3))
        push = CFG.get("kb_push", 30000.0) * (CFG.get("kb_finisher_push", 2.2) if fin else 1.0)
        if not CFG.get("kh_knockback", False):           # vanilla audit: the push strengths were made up (off)
            push = 0.0
        sfx(S, "heavy" if fin else "hit")
        if hurt(S, pc, p, dmg, push=push, lift=CFG.get("kb_finisher_lift", 1.0) if fin else 0.3,
                lvl_scale=not CFG.get("kb_kh1_damage", True)):
            st["blade_hits"] = st.get("blade_hits", 0) + 1
            st["mp"] = min(MP_MAX, st.get("mp", MP_MAX) + 4.0)
            kh1_add_mp(S, CFG.get("kh_mp_per_hit", 0.34))
            if invented_ok(S): fx_flash(S, hit_at, (1.0, 0.95, 0.7), 120, 0.12)
    S.status["blade"] = {"hits": st.get("blade_hits", 0), "swing": st.get("blade_swing"),
                         "len": round(math.dist(cur[0], cur[1]))}


# ---------------------------------------------------------------- pin to the room's anchor (not "where he was")
def kh1_pin_tick(S, ks, now: float, speed: float, radius: float) -> bool:
    """Keeps KH1 Sora around the fixed anchor of this room. The renderer's pin re-anchored at his current spot each
    time forwarding restarted (alt-tab, loading, pose dropouts), so the centre crept away over a session. Here the
    centre is always the anchor: past `radius` he is put back on it, and when you stand still for 1.5 s he is
    re-centred too (KH1 only, invisible in Borderlands). Returns False when this room has no anchor."""
    import struct
    st, CFG = S.state, S.CFG
    anchors = {**KH_ANCHORS, **CFG.get("kh_anchors", {})}
    room = kh1_room(S)
    if room not in anchors or not ks:
        return False
    a = anchors[room]
    if st.get("kh_pin_pause") or ks["state"] != 2 or now < st.get("kh_warping_until", 0):
        st["kh_idle_t"] = now
        return True
    if speed > 30:
        st["kh_idle_t"] = now
    d = math.hypot(ks["pos"][0] - a[0], ks["pos"][2] - a[1])
    idle = now - st.get("kh_idle_t", now) > CFG.get("kh_recenter_idle_s", 1.5)
    if d > radius or (idle and d > CFG.get("kh_recenter_cm", 60.0)):
        k32 = ctypes.windll.kernel32
        h = ctypes.c_void_p(S.kh["h"])
        k32.WriteProcessMemory(h, ctypes.c_void_p(ks["addr"] + 0x10), struct.pack("<f", a[0]), 4, None)
        k32.WriteProcessMemory(h, ctypes.c_void_p(ks["addr"] + 0x18), struct.pack("<f", a[1]), 4, None)
        mapfeed_shift(S, a[0] - ks["pos"][0], a[1] - ks["pos"][2])   # map feed: the mapping moves with them NOW
        kh1_shift_prizes(S, a[0] - ks["pos"][0], a[1] - ks["pos"][2])
        if party_on(S) or st.get("kh_summon_ent"):   # Donald and Goofy (and the active SUMMON) move with him: their
            ddx, ddz = a[0] - ks["pos"][0], a[1] - ks["pos"][2]   # formation around Sora must not jump in Borderlands
            # (summon left behind -> KH1 warped it back next to Sora every recentre = "Simba teleports", user 2026-10-06)
            for ent in party_addrs(S) + ([st["kh_summon_ent"]] if st.get("kh_summon_ent") else []):
                pb_ = kh1_abs(S, ent + 0x10, 12) if ent else None
                if pb_:
                    px, py, pz = struct.unpack("<3f", pb_)
                    k32.WriteProcessMemory(h, ctypes.c_void_p(ent + 0x10), struct.pack("<3f", px + ddx, py, pz + ddz), 12, None)
        st["kh_recenters"] = st.get("kh_recenters", 0) + 1
        st["kh_idle_t"] = now
    S.status["kh_pin"] = {"room": room, "off_cm": round(d), "recenters": st.get("kh_recenters", 0)}
    return True


# ---------------------------------------------------------------- glide (the GTA mod's Minecraft-driven flight)
# GTA x Minecraft: while flying, Minecraft's physics own the player, Minecraft is forced "not on ground" (otherwise
# it cancelled every glide), and the flight ends when the player reaches GTA's real ground. Same here:
#   - KH1 Sora gets NO floor while gliding (bridge floor off): KH1 never lands until Borderlands lands
#   - KH1's glide start point (entity +0x490) is kept 20 m above him: KH1 ends a glide when he rises above it or
#     comes near it (FUN_1402b2780), which the bridge's long-fall lifts used to trigger
#   - Circle gets a fresh press every 0.4 s until KH1 really glides (it needs a press edge while > 50 cm up,
#     entity +0x3D4 > 50, FUN_1402b2d30), then it is held
#   - Borderlands follows KH1: horizontal glide speed x kh_glide_speed, sink x kh_glide_sink; landing ends it.
KH_GLIDE_REF = 0x490


def kh1_glide_tick(S, ks, now: float) -> None:
    import struct
    st = S.state
    if not st.get("kh_glide_on") or not ks:
        st["kh_glide_retry"] = 0.0
        return
    gliding = ks["anim"] in KH_GLIDE_ANIMS
    if gliding:
        # keep holding (window stamped with this frame's `now`: kh1_press uses a later clock reading, which made the
        # press start "in the future" every frame, so KH1 never saw it held and ended the glide at its first check)
        st.setdefault("kh_press", {})[PAD_CIRCLE] = (now, now + 0.15)
        # held in the open air above the room's anchor while he glides: KH1's object collision (props, fixtures) still
        # applies to him, and a long glide sank him onto the ground - both glitched the glide pose. Borderlands only
        # uses KH1's per-frame movement and sink rate, so moving him here is invisible there.
        x, y, z = ks["pos"]
        anchors = {**KH_ANCHORS, **S.CFG.get("kh_anchors", {})}
        a_ = anchors.get(kh1_room(S))
        fy = st.get("kh_floor_ref")
        if a_ is not None:
            x, z = a_[0], a_[1]
            if mapfeed_active(S) and st.get("mf_xf_held") and st.get("mf_xf") is not None and S.CFG.get("kh_glide_feed_follow", True):
                x, z = mapfeed_glide_spot(S, a_)
        if fy is not None and y > fy - S.CFG.get("kh_glide_min_up", 100.0):
            y = fy - S.CFG.get("kh_glide_up", 400.0)
        if (x, y, z) != tuple(ks["pos"]):
            ctypes.windll.kernel32.WriteProcessMemory(ctypes.c_void_p(S.kh["h"]), ctypes.c_void_p(ks["addr"] + 0x10),
                                                      struct.pack("<3f", x, y, z), 12, None)
        ref = struct.pack("<3f", x, y - 2000.0, z)
        ctypes.windll.kernel32.WriteProcessMemory(ctypes.c_void_p(S.kh["h"]), ctypes.c_void_p(ks["addr"] + KH_GLIDE_REF), ref, 12, None)
        if not st.get("kh_glide_seen"):
            st["kh_glide_seen"] = True
            kh1_event(S, "glide: KH1 is gliding")
    elif now >= st.get("kh_glide_retry", 0.0):
        st["kh_glide_retry"] = now + 0.4
        st.setdefault("kh_press", {})[PAD_CIRCLE] = (now + 0.08, now + 0.38)   # release 80 ms, then press + hold
        st["kh_glide_tries"] = st.get("kh_glide_tries", 0) + 1
    S.status["kh_glide"] = {"on": True, "gliding": gliding, "tries": st.get("kh_glide_tries", 0), "anim": ks["anim"]}
    # trace (glide_trace.txt): one line per tick while gliding is wanted
    if dbg_on(S, "dbg_glide_trace"):
        try:
            sh = kh1_shared(S)
            mv = kh1_mem(S, 0, 0) if False else None
            raw = kh1_abs(S, ks["addr"] + 0x160, 0x10) or bytes(16)
            ref = kh1_abs(S, ks["addr"] + KH_GLIDE_REF, 12) or bytes(12)
            timer = None
            hnd = kh1_abs(S, ks["addr"] + 0x148, 4)
            if hnd:
                ta = kh1_resolve(S, int.from_bytes(hnd, "little"))
                tb = kh1_abs(S, ta + 0x100, 4) if ta else None
                timer = struct.unpack("<f", tb)[0] if tb else None
            h3d4 = kh1_abs(S, ks["addr"] + 0x3D4, 4)
            mstate = kh1_abs(S, ks["addr"] + 0x70, 4)    # +0x1c dword index = movement state (0x15 = glide)
            press = st.get("kh_press", {}).get(PAD_CIRCLE)
            held = bool(press and press[0] <= now < press[1])
            line = (f"{now % 1000:8.3f} anim {ks['anim']:3d} st {ks['state']:3d} mv {int.from_bytes(mstate or bytes(4), 'little'):3d} "
                    f"y {ks['pos'][1]:8.1f} vy {ks['vel'][1] * 60:7.1f} ref_dy {struct.unpack('<3f', ref)[1] - ks['pos'][1]:8.1f} "
                    f"timer {timer if timer is None else round(timer, 2)} h3d4 {struct.unpack('<f', h3d4)[0] if h3d4 else None:.1f} "
                    f"floor_on {sh_u32(sh, SH_FLOOR_ON) if sh else '-'} air {sh_u32(sh, SH_SORA_AIR) if sh else '-'} circle {int(held)}")
            tr = st.setdefault("glide_trace", [])
            tr.append(line)
            if len(tr) % 30 == 0 or len(tr) > 3000:
                (HERE / "glide_trace.txt").write_text("\n".join(tr[-3000:]) + "\n")
                if len(tr) > 3000:
                    del tr[:-3000]
        except Exception as ex:  # noqa: BLE001
            S.status["glide_trace_error"] = str(ex)


# ---------------------------------------------------------------- per frame
# ---------------------------------------------------------------- KH1's own menu inside Borderlands (renderer v32)
PAD_START = 0x08


def _bl_focused() -> bool:
    u32 = ctypes.windll.user32
    pid = ctypes.c_ulong()
    u32.GetWindowThreadProcessId(u32.GetForegroundWindow(), ctypes.byref(pid))
    return pid.value == os.getpid()


def _key(vk: int) -> bool:
    return bool(vk) and bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


# Unreal key names (what the Mods menu keybinds store) -> Windows virtual-key codes, for the keys polled with
# GetAsyncKeyState (the KH1 menu key must also work while Borderlands' input is blocked under the menu)
_UE_VK = {**{f"F{i}": 0x6F + i for i in range(1, 13)}, **{chr(c): c for c in range(0x41, 0x5B)},
          **{n: 0x30 + i for i, n in enumerate(("Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"))},
          **{f"NumPad{n}": 0x60 + i for i, n in enumerate(("Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"))},
          "Tab": 0x09, "Enter": 0x0D, "SpaceBar": 0x20, "Escape": 0x1B, "BackSpace": 0x08, "CapsLockKey": 0x14,
          "LeftShift": 0xA0, "RightShift": 0xA1, "LeftControl": 0xA2, "RightControl": 0xA3, "LeftAlt": 0xA4, "RightAlt": 0xA5,
          "Insert": 0x2D, "Delete": 0x2E, "Home": 0x24, "End": 0x23, "PageUp": 0x21, "PageDown": 0x22,
          "Up": 0x26, "Down": 0x28, "Left": 0x25, "Right": 0x27, "Tilde": 0xC0, "Backslash": 0xDC, "Slash": 0xBF,
          "Semicolon": 0xBA, "Quote": 0xDE, "Comma": 0xBC, "Period": 0xBE, "Underscore": 0xBD, "Equals": 0xBB,
          "LeftBracket": 0xDB, "RightBracket": 0xDD, "Multiply": 0x6A, "Add": 0x6B, "Subtract": 0x6D, "Decimal": 0x6E,
          "Divide": 0x6F, "Pause": 0x13, "ScrollLock": 0x91, "NumLock": 0x90,
          "LeftMouseButton": 0x01, "RightMouseButton": 0x02, "MiddleMouseButton": 0x04,
          "ThumbMouseButton": 0x05, "ThumbMouseButton2": 0x06}


def kh_menu_vk(S) -> int:
    """The KH1 menu key: the Mods menu keybind "Sora: KH1 menu" (default F9, rebindable), else config kh_menu_vk.
    0 = unbound (no menu key)."""
    kb = getattr(S, "menu_keybind", None)
    if kb is not None:
        return _UE_VK.get(kb.key, 0) if kb.key else 0
    return S.CFG.get("kh_menu_vk", 0x78)


def kh_menu_tick(S, pc, now: float) -> None:
    """F9 (config kh_menu_vk): KH1's real menu (equipment, abilities, stats, items, party...) shown full-screen in
    Borderlands, grabbed live from KH1's window. Arrows/WASD move, Enter confirms, Backspace goes back; F9 again closes.
    Borderlands' world is slowed to a near-freeze meanwhile (kh_menu_dilation)."""
    st, CFG, dll = S.state, S.CFG, S.dll
    if not hasattr(dll, "sr_kh1_view") or not (kh1_combat_on(S) or st.get("kh_menu_open")):
        return
    if CFG.get("kh_menu_reset") and st.get("kh_menu_reset_n") != CFG.get("nonce"):   # unstick: view off, world speed back
        st["kh_menu_reset_n"] = CFG.get("nonce")
        st["kh_menu_open"], st["kh_menu_close_t"] = False, None
        kh_menu_input_block(S, pc, False)
        dll.sr_kh1_view(0, ctypes.c_float(1.0))
        wi = pc.WorldInfo
        S.pending.append(lambda wi=wi: setattr(wi, "TimeDilation", 1.0))
    if CFG.get("unstick") and st.get("unstick_n") != CFG.get("nonce"):   # one-shot: clear a stuck glide / menu
        st["unstick_n"] = CFG.get("nonce")
        st["kh_glide_on"], st["kh_glide_seen"], st["kh_glide_tries"] = False, False, 0
        st.pop("kh_press", None)
    down = _bl_focused() and _key(kh_menu_vk(S))
    edge = down and not st.get("kh_menu_key")
    st["kh_menu_key"] = down
    kh_open_now = kh1_menu_is_open(S)
    if st.get("kh_menu_open") and st.get("kh_menu_phase") == "open":
        # KH1 closed its own menu (Backspace / Circle on the top level): close ours too, without pressing Start
        # (user 2026-10-05: Backspace out, then F9 pressed Start into a KH1 with no menu -> both games out of sync)
        if kh_open_now:
            st["kh_menu_seen"], st["kh_menu_gone_t"] = True, None
        elif st.get("kh_menu_seen"):
            st["kh_menu_gone_t"] = st.get("kh_menu_gone_t") or now
            if now - st["kh_menu_gone_t"] > 0.25:
                st["kh_menu_by_kh1"] = True
                edge = True
                S.status["kh_menu_closed_by"] = "kh1"
        elif kh1_u8(S, KH_PAUSED) == 1 and now - st.get("kh_menu_start_t", now) > 0.3:
            # Start hit KH1 while it still counted as battle -> PAUSE screen: unpause, wait longer, try again
            kh1_press(S, PAD_START, 0.12)
            st["kh_menu_tries"] = st.get("kh_menu_tries", 0) + 1
            S.status["kh_menu_paused_retry"] = st["kh_menu_tries"]
            if st["kh_menu_tries"] <= CFG.get("kh_menu_retries", 3):
                st["kh_menu_phase"], st["kh_menu_field_t"] = "clearing", None
                st["kh_menu_t0"] = now - 0.0
                st["kh_menu_extra"] = st.get("kh_menu_extra", 0.0) + 0.4
            else:
                st["kh_menu_by_kh1"] = True
                edge = True
                S.status["kh_menu_closed_by"] = "paused x%d" % st["kh_menu_tries"]
        elif now - st.get("kh_menu_start_t", now) > CFG.get("kh_menu_open_timeout", 3.0):
            st["kh_menu_by_kh1"] = True                     # Start never opened it: give up, close ours
            edge = True
            S.status["kh_menu_closed_by"] = "timeout"
    if edge:
        st["kh_menu_open"] = not st.get("kh_menu_open")
        kh_menu_input_block(S, pc, st["kh_menu_open"])
        if st["kh_menu_open"]:
            st["kh_menu_seen"], st["kh_menu_gone_t"], st["kh_menu_by_kh1"] = False, None, False
            st["kh_menu_tries"], st["kh_menu_extra"], st["kh_menu_field_t"] = 0, 0.0, None
            # KH1 refuses its menu in battle mode (Start = PAUSE there) and Traverse Town keeps it in battle: first move
            # every KH1 enemy far away for a moment, THEN press Start (kh_menu_clear_s later)
            st["kh_menu_phase"], st["kh_menu_t0"] = "clearing", now
            st["kh_glide_on"] = False
            st["kh_menu_park_until"] = now + 3600
        else:
            if st.get("kh_menu_phase") == "open" and not st.get("kh_menu_by_kh1") and (kh_open_now or kh_open_now is None):
                kh1_press(S, PAD_START, 0.12)               # Start closes KH1's menu - only if it is open
            st["kh_menu_phase"], st["kh_menu_close_t"] = None, now
            st["kh_menu_shut_t"] = now
            st["kh_menu_park_until"] = now + 0.5
            st["kh_menu_restore_t"] = now + 0.3                 # after KH1 has closed its menu (unpaused)
        wi = pc.WorldInfo
        dil = CFG.get("kh_menu_dilation", 0.02) if st["kh_menu_open"] else 1.0
        S.pending.append(lambda wi=wi, dil=dil: setattr(wi, "TimeDilation", dil))
    if st.get("kh_menu_phase") == "clearing":
        ks = kh1_state(S)
        if ks:
            foes, _ = kh1_scan_foes(S, ks, 30000.0)
            far = (ks["pos"][0] + CFG.get("kh_menu_park_dist", 6000.0), ks["pos"][1], ks["pos"][2] + CFG.get("kh_menu_park_dist", 6000.0))
            orig = st.setdefault("kh_menu_orig", {})
            for _, a, _, _ in foes:
                if a not in orig:                               # where it stood: put back when the menu closes
                    raw = kh1_abs(S, a + 0x10, 12)
                    if raw:
                        orig[a] = raw
                ctypes.windll.kernel32.WriteProcessMemory(ctypes.c_void_p(S.kh["h"]), ctypes.c_void_p(a + 0x10),
                                                          struct.pack("<3f", *far), 12, None)
            S.status["kh_menu_parked"] = len(foes)
        # KH1 out of battle mode (flag 0x2DB8400 == 1, ~1 s after parking) -> Start now; kh_menu_clear_s = fallback
        # What KH1's Start does is decided each frame in FUN_14017efc0 -> 0x232DFA4 (1/2 = PAUSE loop, 0/3 = main
        # menu); 2 comes from bit 0 of 0x2867364 = battle mode, which KH1 drops ~1.7 s after the enemies are gone.
        # kh_menu_force_field: drop that bit ourselves while the enemies are parked, so the menu opens at once.
        if CFG.get("kh_menu_force_field", True):
            fb = kh1_mem(S, KH_BATTLE_BITS, 4)
            if fb and fb[0] & 1:
                kh1_poke(S, KH_BATTLE_BITS, bytes([fb[0] & 0xFE]))
        sm = kh1_mem(S, KH_START_MODE, 4)
        smode = struct.unpack("<i", sm)[0] if sm else None
        S.status["kh_menu_start_mode"] = smode
        ready = smode in (0, 3) and now - st["kh_menu_t0"] > CFG.get("kh_menu_min_s", 0.1)
        if ready or now - st["kh_menu_t0"] > CFG.get("kh_menu_clear_s", 4.0) + st.get("kh_menu_extra", 0.0):
            S.status["kh_menu_start_after"] = round(now - st["kh_menu_t0"], 2)
            st["kh_menu_start_t"] = now
            st["kh_menu_phase"] = "open"
            if not kh_menu_native(S):                     # old: KH1's whole window grabbed and shown
                dll.sr_kh1_view(1, ctypes.c_float(1.0))
            kh1_press(S, PAD_START, 0.12)
    if st.get("kh_menu_restore_t") and now >= st["kh_menu_restore_t"]:   # the enemies come back where they were
        st["kh_menu_restore_t"] = None
        for a, raw in st.pop("kh_menu_orig", {}).items():
            ctypes.windll.kernel32.WriteProcessMemory(ctypes.c_void_p(S.kh["h"]), ctypes.c_void_p(a + 0x10), raw, 12, None)
        st["kh_refill_t"] = now                             # and no "room empty" refill right now
    if not st.get("kh_menu_open") and st.get("kh_menu_close_t") and now - st["kh_menu_close_t"] > 0.35:
        dll.sr_kh1_view(0, ctypes.c_float(1.0))            # keep showing KH1 until its menu has closed
        st["kh_menu_close_t"] = None
    if hasattr(dll, "sr_khui_menu"):
        dll.sr_khui_menu(1 if st.get("kh_menu_open") or st.get("kh_menu_close_t") else 0)
    try:
        kh1_weapons_tick(S, now)
    except Exception as ex:  # noqa: BLE001
        S.status["kh1_weapons"] = f"ERR {type(ex).__name__}: {ex}"
    try:
        kh_menu_models(S)
    except Exception as ex:  # noqa: BLE001
        S.status["menu_models"] = f"ERR {type(ex).__name__}: {ex}"
    try:
        test_dummy_tick(S, pc)
    except Exception as ex:  # noqa: BLE001
        S.status["test_dummy"] = f"ERR {type(ex).__name__}: {ex}"
    try:
        dbg_exec_tick(S, pc)
    except Exception as ex:  # noqa: BLE001
        S.status["dbg_exec"] = f"ERR {type(ex).__name__}: {ex}"
    try:
        kh_menu_preview(S, now)
    except Exception as ex:  # noqa: BLE001
        S.status["menu_preview"] = f"ERR {type(ex).__name__}: {ex}"
    try:
        kh1_loadout_tick(S, pc, now)
    except Exception as ex:  # noqa: BLE001
        S.status["kh1_loadout"] = f"ERR {type(ex).__name__}: {ex}"
    try:
        kh1_level_tick(S, pc, now)
    except Exception as ex:  # noqa: BLE001
        S.status["kh1_level"] = f"ERR {type(ex).__name__}: {ex}"
    S.status["kh_menu"] = [bool(st.get("kh_menu_open")), bool(st.get("kh_menu_close_t")), round(float(pc.WorldInfo.TimeDilation), 3), round(now % 1000, 1)]


KH_BATTLE_MODE = 0x2DB8400   # u8: 0 battle mode, 1 field (Start opens the main menu), 2 briefly after the menu closes
KH_MENU_OPEN = 0x2E1F470     # u8: 1 while KH1's main menu (F9) is open (incl. submenus), 0 otherwise
KH_PAUSED = 0x2E1AC59        # u8: 1 while KH1's PAUSE screen is up (Start in battle mode)
KH_START_MODE = 0x232DFA4    # i32, rebuilt every frame by FUN_14017efc0: 0/3 Start -> main menu, 1/2 -> PAUSE loop
KH_BATTLE_BITS = 0x2867364   # u32: bit 0 = battle mode (-> start mode 2)


def kh1_u8(S, rva: int):
    b = kh1_mem(S, rva, 1)
    return b[0] if b else None


def kh1_menu_is_open(S):
    v = kh1_u8(S, KH_MENU_OPEN)
    return None if v is None else v == 1


def kh_menu_input_block(S, pc, on: bool) -> None:
    """Borderlands' mouse look and movement are off while KH1's menu is open (user: the camera still moved)."""
    st = S.state
    if bool(st.get("kh_menu_blocked")) == on:
        return
    st["kh_menu_blocked"] = on

    def _blk():
        try:
            pc.IgnoreLookInput(on)
            pc.IgnoreMoveInput(on)
            S.status["kh_menu_block"] = [int(pc.bIgnoreLookInput), int(pc.bIgnoreMoveInput)]
        except Exception as ex:  # noqa: BLE001
            S.status["kh_menu_block_error"] = str(ex)
    S.pending.append(_blk)


KH_MENU_MDL_RET = 0x2DBB33   # kh_skel_eval's caller for KH1's main-menu party models (kh1_fx v16 log, Local\SoraSkel)


def kh_menu_models(S) -> None:
    """v57: KH1's main menu shows Sora, Donald and Goofy walking in place; KH1 poses them with three extra skeletons.
    kh1_fx v16 logs every kh_skel_eval while the menu is open; the menu ones (caller KH_MENU_MDL_RET) are matched to
    the party by their model data (skel +0x18 = the field character's skel +0x18) and their joint arrays go to the
    renderer, which draws our meshes of them between KH1's menu backdrop and its panels (config kh_menu_models)."""
    import ctypes.wintypes as W
    st, CFG, dll = S.state, S.CFG, S.dll
    if not hasattr(dll, "sr_menu_mdl"):
        S.status["menu_models"] = "renderer < v57"
        return
    on = bool(CFG.get("kh_menu_models", True)) and bool(st.get("kh_menu_open")) and kh1_menu_is_open(S) is True
    # vanilla: KH1 shows the party models only on the menu's top screen, never in Items / Equipment / Status (it keeps
    # posing them there, so the pose can't tell). Bytes 0x52ABA3 and 0x21AAEF8 = 1 on the top screen, 0 in sub-screens
    # (memory diff 2026-10-05, checked on Items, Equipment, Status; 0x22C0342 was 0 on the top screen at times).
    if on and CFG.get("kh_menu_mdl_top_only", True):
        top = all((kh1_mem(S, a, 1) or b"\0")[0] == 1 for a in CFG.get("kh_menu_top_flags", [0x52ABA3, 0x21AAEF8]))
        if not top:
            if st.get("mm_on"):
                dll.sr_menu_mdl(0, None, None, 0)
                st["mm_on"] = False
            S.status["menu_models_on"] = [False, True, "sub-screen"]
            return
    S.status["menu_models_on"] = [on, bool(st.get("kh_menu_open")), kh1_menu_is_open(S)]
    if not on:
        if st.get("mm_on"):
            dll.sr_menu_mdl(0, None, None, 0)
            st["mm_on"], st["mm_skels"] = False, None
        return
    m = getattr(S, "skel_map", None)
    if m is None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenFileMappingW.restype = W.HANDLE
        k32.MapViewOfFile.restype = ctypes.c_void_p
        k32.MapViewOfFile.argtypes = [W.HANDLE, W.DWORD, W.DWORD, W.DWORD, ctypes.c_size_t]
        h = k32.OpenFileMappingW(0x0004, False, r"Local\SoraSkel")
        pm = h and k32.MapViewOfFile(h, 0x0004, 0, 0, 16 + 1024 * 56)
        if not pm:
            S.status["menu_models"] = "no SoraSkel mapping (kh1_fx < v16)"
            return
        m = S.skel_map = (ctypes.c_char * (16 + 1024 * 56)).from_address(pm)
    skels = st.get("mm_skels")
    if not skels:
        found = {}
        w = struct.unpack_from("<I", m, 8)[0]
        for i in range(w - 1, max(-1, w - 200), -1):
            seq, ret, _p1, skel = struct.unpack_from("<IIQQ", m, 16 + (i % 1024) * 56)
            if seq == i + 1 and ret == KH_MENU_MDL_RET and skel not in found:
                found[skel] = True
        want = []
        for a in (0x2D37280, 0x2D37288, 0x2D37290):           # Sora, Donald, Goofy in the field
            ent = int.from_bytes(kh1_mem(S, a, 8) or bytes(8), "little")
            hb = ent and kh1_abs(S, ent + 0x15C, 4)
            fsk = kh1_resolve(S, int.from_bytes(hb, "little")) if hb else 0
            md = fsk and kh1_abs(S, fsk + 0x18, 8)
            want.append(md)
        skels = []
        for md in want:
            skels.append(next((sk for sk in found if md and kh1_abs(S, sk + 0x18, 8) == md), 0))
        if not all(skels):
            S.status["menu_models"] = "looking (%d menu skeletons)" % len(found)
            return
        st["mm_skels"] = skels
    J = []
    for sk in skels:
        hb = kh1_abs(S, sk + 0x1B4, 4)
        J.append(kh1_resolve(S, int.from_bytes(hb, "little")) if hb else 0)
    off = CFG.get("kh_menu_mdl_offsets", [[-70.0, 0.0, 0.0], [70.0, 0.0, 0.0], [225.0, 0.0, 0.0]])
    lt = CFG.get("kh_menu_mdl_light", [0.35, -0.55, 0.75])
    P = [CFG.get("kh_menu_mdl_f", 0.916), CFG.get("kh_menu_mdl_oy", 0.364), CFG.get("kh_menu_mdl_mirror", 1.0),
         CFG.get("kh_menu_mdl_bright", 1.0), lt[0], lt[1], lt[2]]
    for k in range(3):
        P += [float(off[k][0]), float(off[k][1]), float(off[k][2])]
    P += [10.0, 5000.0]
    dll.sr_menu_mdl(1, (ctypes.c_uint64 * 3)(*J), (ctypes.c_float * len(P))(*P), len(P))
    st["mm_on"] = True
    if hasattr(dll, "sr_menu_mdl_stats"):
        S.status["menu_models"] = {"drawn": dll.sr_menu_mdl_stats(), "joints": [hex(j) for j in J]}


WPN_RANGES = ((0, 5010, 5209), (1, 5210, 5399), (2, 5400, 5599))   # Sora's Keyblades (FM: 5190 Diamond Dust, 5200 One-Winged Angel), Donald's staves, Goofy's shields


def _wpn_scan(S, regions_hint):
    """Background thread: which weapon files KH1's HD asset cache holds ("xw_ex_5150_wpnc4c0.cvbl" records). KH1 loads
    only the equipped weapon of each party member, so that is what each of them holds. Cached region first."""
    import re
    import ctypes.wintypes as W
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = W.HANDLE
    h = k32.OpenProcess(0x0410, False, S.kh["pid"])
    if not h:
        return

    class MBI(ctypes.Structure):
        _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p), ("AllocationProtect", W.DWORD),
                    ("PartitionId", W.WORD), ("RegionSize", ctypes.c_size_t), ("State", W.DWORD), ("Protect", W.DWORD),
                    ("Type", W.DWORD)]

    def read(a, n):
        buf = ctypes.create_string_buffer(n)
        got = ctypes.c_size_t()
        ok = k32.ReadProcessMemory(h, ctypes.c_void_p(a), buf, n, ctypes.byref(got))
        return buf.raw[:got.value] if (ok or got.value) else b""

    pat = re.compile(rb"xw_ex_5(\d{3})_wpn[0-9a-f]+\.cvbl")

    live_vt = S.kh["base"] + 0x3B3198     # live cache record's class; a freed one is reset to 0x3B3188 (stale name)

    def scan(regs):
        found, where = {}, []
        for a, n in regs:
            b = read(a, n)
            for m in pat.finditer(b):
                o = m.start()
                if o < 0x10:
                    continue
                vt, data = struct.unpack_from("<QQ", b, o - 0x10)
                if vt != live_vt or not data:
                    continue
                wid = 5000 + int(m.group(1))
                found[wid] = found.get(wid, 0) + 1
                where.append((a, n))
        return found, where

    t0 = time.time()
    try:
        found, where = scan(regions_hint) if regions_hint else ({}, None)
        have = {who for who, lo, hi in WPN_RANGES for wid in found if lo <= wid <= hi}
        if len(have) < 3:          # someone's (new) weapon lives elsewhere in the heap: search everything
            found, where = {}, None
            regs, a, m = [], 0, MBI()
            while k32.VirtualQueryEx(h, ctypes.c_void_p(a), ctypes.byref(m), ctypes.sizeof(m)):
                base = m.BaseAddress or 0
                if m.State == 0x1000 and m.Protect & 0x04 and m.Type == 0x20000 and m.RegionSize <= (64 << 20):
                    regs.append((base, m.RegionSize))            # private read-write heap (the asset cache)
                a = base + m.RegionSize
                if a >= 1 << 47:
                    break
            found, where = scan(regs)
            S.state["wpn_full_scans"] = S.state.get("wpn_full_scans", 0) + 1
        S.state["wpn_found"] = found
        S.state["wpn_region"] = sorted(set(w for w in where)) if where else None
        S.state["wpn_scan_ms"] = round((time.time() - t0) * 1000)
        S.state["wpn_scan_t"] = time.time()
    finally:
        k32.CloseHandle(h)
        S.state["wpn_scanning"] = False


def kh1_weapons_tick(S, now: float) -> None:
    """v58: Sora / Donald / Goofy hold the weapon KH1 has equipped (KH1's F9 Equipment menu): rescanned at start, after
    every F9 menu, and every kh1_wpn_rescan_s; renderer sr_set_weapon swaps the drawn mesh (config kh1_weapons)."""
    import threading
    st, CFG, dll = S.state, S.CFG, S.dll
    if not hasattr(dll, "sr_set_weapon") or not getattr(S, "kh", None) or not S.kh.get("pid"):
        return
    if not CFG.get("kh1_weapons", True):
        if st.get("wpn_sent"):
            for who in range(3):
                dll.sr_set_weapon(who, 0)
            st["wpn_sent"] = None
        return
    menu_closed = st.get("wpn_menu_seen") and not st.get("kh_menu_open")
    st["wpn_menu_seen"] = bool(st.get("kh_menu_open"))
    due = (st.get("wpn_scan_t") is None or menu_closed or now - st.get("wpn_scan_t", 0) > CFG.get("kh1_wpn_rescan_s", 10.0))
    if menu_closed:
        st["wpn_after_menu"] = now + 1.5                     # KH1 loads the new weapon right after the menu closes
    if st.get("wpn_after_menu") and now > st["wpn_after_menu"]:
        st["wpn_after_menu"], due = None, True
    # fast mode (user: swap showed ~6 s late): every kh1_wpn_fast_s while the F9 menu is open and for 8 s after it closes
    if menu_closed:
        st["wpn_fast_until"] = now + CFG.get("kh1_wpn_fast_after_s", 8.0)
    fast = bool(st.get("kh_menu_open")) or now < st.get("wpn_fast_until", 0)
    gap = CFG.get("kh1_wpn_fast_s", 0.5) if fast else 1.0
    if fast and now - st.get("wpn_scan_t", 0) > gap:
        due = True
    if due and not st.get("wpn_scanning") and now - st.get("wpn_scan_try", 0) > gap:
        st["wpn_scanning"], st["wpn_scan_try"] = True, now
        threading.Thread(target=_wpn_scan, args=(S, st.get("wpn_region")), daemon=True).start()
    found = st.get("wpn_found") or {}
    S.status.pop("kh1_weapons_multi", None)
    want = []
    for who, lo, hi in WPN_RANGES:
        ids = sorted((n, wid) for wid, n in found.items() if lo <= wid <= hi)
        prev = (st.get("wpn_sent") or [0, 0, 0])[who]
        new = [wid for _, wid in ids if wid != prev]
        # two live at once = KH1 just loaded the newly equipped one and hasn't freed the old one yet -> show the new one
        want.append(ids[-1][1] if len(ids) == 1 else (new[-1] if new else (ids[-1][1] if ids else 0)))
        if len(ids) > 1:
            S.status["kh1_weapons_multi"] = {who: [w for _, w in ids]}
    drawn = [dll.sr_weapon_state(k) for k in range(3)]
    if want != st.get("wpn_sent") or any(w and d != w and d != -w for w, d in zip(want, drawn)):   # new renderer DLL
        for who, wid in enumerate(want):
            dll.sr_set_weapon(who, int(wid))
        st["wpn_sent"] = want
    S.status["kh1_weapons"] = {"equipped": want, "drawn": [dll.sr_weapon_state(k) for k in range(3)], "found": found,
                               "scan_ms": st.get("wpn_scan_ms")}


def kh1_music_mode(S) -> bool:
    """KH1 MUSIC (user's list: KH1's field theme instead of Borderlands' music, battle theme in combat): KH1 itself plays
    its music - its own field / battle switching follows KH1's battle mode, which the enemy stand-ins drive - while
    kh1_fx v17+ silences KH1's sound effects and voices at the source (Borderlands already plays them) and Borderlands'
    Music audio group is turned down. Config kh1_music (off until the user has listened), kh1_music_vol (KH1 0.75)."""
    return bool(S.CFG.get("kh1_music", False)) and kh1_combat_on(S)


def test_dummy_tick(S, pc) -> None:
    """TEST DUMMY (user 2026-10-05): press F3 -> the nearest Borderlands enemy becomes a passive punching bag: huge
    health (kept topped up) and its AI frozen (Controller.CustomTimeDilation 0: no attacks / movement of its own; its
    body still takes hits, knockback and juggle). F3 again releases it. Key read with GetAsyncKeyState (no restart)."""
    st = S.state
    down = bool(ctypes.windll.user32.GetAsyncKeyState(S.CFG.get("test_dummy_vk", 0x72)) & 0x8000)
    pressed = down and not st.get("dummy_key")
    st["dummy_key"] = down
    d = st.get("test_dummy")
    if d is not None and not bl_alive(d):
        d = st["test_dummy"] = None
    if pressed:
        if d is not None:
            def _rel(p=d):
                try:
                    p.Controller.CustomTimeDilation = 1.0
                except Exception:  # noqa: BLE001
                    pass
            S.pending.append(_rel)
            st["test_dummy"] = None
            say(S, "test dummy released")
        else:
            near = enemies(S, pc, 3000.0)
            if near:
                p = near[0][1]
                st["test_dummy"] = p

                def _mk(p=p):
                    p.Controller.CustomTimeDilation = 0.0
                S.pending.append(_mk)
    d = st.get("test_dummy")
    if d is not None:
        hp_ = S.CFG.get("test_dummy_hp", 1000000.0)

        def _top(p=d, hp_=hp_):
            try:
                pool = p.HealthPool.Data
                for name in ("BaseMaxValue", "MaxValue"):
                    try:
                        setattr(pool, name, hp_)
                    except Exception:  # noqa: BLE001
                        pass
                pool.CurrentValue = hp_
            except Exception:  # noqa: BLE001
                pass
        if time.time() - st.get("dummy_top_t", 0) > 0.5:
            st["dummy_top_t"] = time.time()
            S.pending.append(_top)
    S.status["test_dummy"] = bl_key(d) if d is not None else None


def dbg_exec_tick(S, pc) -> None:
    """Probe hook for Claude: config dbg_exec = {"nonce": <config nonce>, "code": "..."} runs that code once on the game
    tick (S, pc, unrealsdk, out = list of lines) and writes dbg_exec.txt. Fires only when its nonce == config nonce."""
    d = S.CFG.get("dbg_exec")
    if not isinstance(d, dict) or d.get("nonce") != S.CFG.get("nonce") or S.state.get("dbg_exec_done") == d.get("nonce"):
        return
    S.state["dbg_exec_done"] = d.get("nonce")
    code = d.get("code", "")

    def _run(code=code):
        out = []
        try:
            exec(code, {**globals(), "S": S, "pc": get_pc_safe(), "out": out})   # all of logic.py in scope
        except Exception as ex:  # noqa: BLE001
            import traceback
            out.append(traceback.format_exc())
        (HERE / "dbg_exec.txt").write_text(chr(10).join(str(x) for x in out))
    if d.get("inline"):                  # run now, in PostRender (BL's front end may never PlayerTick the jobs)
        _run()
    else:
        S.pending.append(_run)


def get_pc_safe():
    try:
        from mods_base import get_pc  # type: ignore
        return get_pc()
    except Exception:  # noqa: BLE001
        return None


def kh1_item_model(S, who: int, item: int) -> int:
    """KH1 weapon item -> xw_ex model id, read from KH1's own weapon record (item record +6 = weapon index; the weapon
    record (0x58 B, table per character) starts with the model name "xw_ex_5350"). The ids have gaps (Meteor Strike =
    5350, Mighty Shield = 5560), so the formula below is only the fallback. Cached per KH1 process."""
    key = (S.kh.get("pid") if getattr(S, "kh", None) else None, who, item)
    cache = S.state.setdefault("xw_model_cache", {})
    if key in cache:
        return cache[key]
    wid = 0
    try:
        tab = int.from_bytes(kh1_mem(S, 0x2D22D38, 8) or bytes(8), "little")
        rec = tab and kh1_abs(S, tab + (item - 1) * 0x14, 0x14)
        widx = struct.unpack_from("<h", rec, 6)[0] if rec else 0
        w = widx > 0 and kh1_mem(S, 0x2D22D40 + _lo_u32(S, 0x528060 + 4 * who) + (widx - 1) * 0x58, 16)
        if w and w[:6] == b"xw_ex_" and w[6:10].isdigit():
            wid = int(w[6:10])
    except Exception:  # noqa: BLE001
        wid = 0
    if wid:
        cache[key] = wid
    return wid or kh1_item_to_xw(who, item)


def kh1_item_to_xw(who: int, item: int) -> int:
    """KH1 weapon item id -> xw_ex model id (Kingdom Key 0x51 = 5010; Jungle King 0x56 .. 5020 + 10 per item, Wishing
    Star 0x63 = 5150 verified; Donald 0x6A = 5240, Goofy 0x7B = 5450 verified; the rest by the same steps)."""
    if who == 0:
        if item in (0x65, 0x66):           # Final Mix additions sit after the jump: Diamond Dust 5190, One-Winged Angel 5200
            return 5190 + (item - 0x65) * 10
        return 5010 if item == 0x51 else (5020 + (item - 0x56) * 10 if 0x56 <= item <= 0x64 else 0)
    if who == 1:
        return 5210 + (item - 0x67) * 10 if 0x67 <= item <= 0x76 else 0
    return 5410 + (item - 0x77) * 10 if 0x77 <= item <= 0x88 else 0


def kh_menu_preview(S, now: float) -> None:
    """v61/v62: KH1's Equipment weapon list shows the hovered weapon turning at the left (vanilla; user's screenshots
    2026-10-05). KH1 poses each party member's menu weapon skeleton at 0x29E5860 + 0xC800 * who (Sora, Donald; 0x40 per
    joint; the same object rides the menu model's hand on the top screen, where KH1 doesn't draw it); in the list it sits
    at (-200, 160, 430) and turns. Hovered item at 0x2E9A0A0 + 4 * who. Shown while a skeleton sits at the preview spot
    AND keeps turning (it is left there, still, after the list closes). Config kh_menu_preview, kh_menu_wpn_offset."""
    st, CFG, dll = S.state, S.CFG, S.dll
    if not hasattr(dll, "sr_menu_wpn"):
        return
    show, wid, who, mats = False, 0, 0, []
    top = (kh1_mem(S, 0x52ABA3, 1) or b"")[0] == 1
    if CFG.get("kh_menu_preview", True) and st.get("kh_menu_open") and not top:
        # where the menu weapon skeletons are (seen: Sora 0x29E5860, Donald 0x29F2060, Goofy 0x29FB0E0 - uneven, so
        # found by a scan of that block for arrays posed at the preview spot, twice a second)
        if now - st.get("mw_scan_t", 0) > 0.5:
            st["mw_scan_t"] = now
            lo, hi = CFG.get("kh_menu_wpn_scan", [0x29E0000, 0x2A20000])
            blob = kh1_mem(S, lo, hi - lo) or b""
            def hit(o):
                if o < 0:
                    return False
                tx, ty, tz, tw = struct.unpack_from("<4f", blob, o + 48)
                return tw == 1.0 and abs(tx + 200.0) < 40.0 and abs(tz - 430.0) < 60.0 and 100.0 < ty < 260.0
            cands = [lo + o for o in range(0, len(blob) - 64, 16) if hit(o) and not hit(o - 64)]
            st["mw_cands"] = cands[:24]
        bases = CFG.get("kh_menu_wpn_bases", [0x29E5860, 0x29F2060, 0x29FB0E0])
        turn = st.setdefault("mw_turn", {})
        for base in st.get("mw_cands", []):
            raw = kh1_mem(S, base, 64 * 32)
            if not raw:
                continue
            j0 = struct.unpack_from("<16f", raw, 0)
            if not (abs(j0[12] + 200.0) < 40.0 and abs(j0[14] - 430.0) < 60.0 and abs(j0[15] - 1.0) < 1e-3):
                continue
            rot = tuple(round(x, 4) for x in j0[:11])
            last = turn.get(base)
            if last is None or last[0] != rot:
                turn[base] = (rot, now if last is not None else 0.0)
            if now - turn[base][1] > CFG.get("kh_menu_preview_still_s", 0.3):
                continue                                   # left there from an earlier list, not turning
            ms = []
            for k in range(32):
                m = struct.unpack_from("<16f", raw, 64 * k)
                if abs(m[15] - 1.0) > 1e-3 or abs(m[12] - j0[12]) > 80 or abs(m[14] - j0[14]) > 80 or abs(m[13] - j0[13]) > 200:
                    break
                ms.extend(m)
            w_guess = min(range(3), key=lambda i: abs(base - bases[i]))
            sel = kh1_mem(S, 0x2E9A064, 4)                 # KH1's own Equipment-list character (eqcall.c FUN_140304fe0)
            w_sel = struct.unpack("<i", sel)[0] if sel else -1
            w = w_sel if 0 <= w_sel <= 2 else w_guess      # the address guess picked Goofy on Sora's list (user 2026-10-12)
            st["mw_who"] = {"sel": w_sel, "guess": w_guess}
            it = kh1_mem(S, 0x2E9A0A0 + 4 * w, 4)
            wid = kh1_item_model(S, w, struct.unpack("<I", it)[0]) if it else 0
            if wid > 0 and ms:
                show, who, mats = True, w, ms
                st["mw_base"] = hex(base)
                break
    if show:
        off = CFG.get("kh_menu_wpn_offsets", [[0.0, -5.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]])[who]
        lt = CFG.get("kh_menu_mdl_light", [0.35, -0.55, 0.75])
        P = [CFG.get("kh_menu_mdl_f", 0.916), CFG.get("kh_menu_mdl_oy", 0.364), CFG.get("kh_menu_mdl_mirror", 1.0),
             CFG.get("kh_menu_mdl_bright", 1.0), lt[0], lt[1], lt[2], float(off[0]), float(off[1]), float(off[2]), 10.0, 5000.0,
             float(CFG.get("kh_menu_wpn_joint", [0, 4, 4])[who])]   # 1 + forced joint: Donald / Goofy weapons are one bone; KH1 joint 3 = staff head / shield (matched by eye)
        nj = len(mats) // 16
        dll.sr_menu_wpn(1, who, int(wid), (ctypes.c_float * len(mats))(*mats), nj, (ctypes.c_float * len(P))(*P), len(P))
        st["mw_on"] = True
    elif st.get("mw_on"):
        dll.sr_menu_wpn(0, 0, 0, None, 0, None, 0)
        st["mw_on"] = False
    S.status["menu_preview"] = {"on": show, "who": who, "id": wid, "joints": len(mats) // 16, "base": st.get("mw_base"),
                                "who_src": st.get("mw_who"), "cands": [hex(c) for c in st.get("mw_cands", [])],
                                "drawn": dll.sr_menu_wpn_stats() if hasattr(dll, "sr_menu_wpn_stats") else None}


LVL_SIZE = 116                          # v37: + loadout equip / refresh request (@92..@112)


def _lvl_map(S):
    """kh1_fx v22 "Local\\SoraLvl" (level link request block, layout in kh1_fx.cpp LvlShared)."""
    import ctypes.wintypes as W
    m = getattr(S, "lvl_map", None)
    if m is not None and len(m) < LVL_SIZE:                # mapped by an older logic with a smaller layout
        m = S.lvl_map = None
    if m is None and time.time() > S.state.get("lvl_map_retry", 0):
        S.state["lvl_map_retry"] = time.time() + 3.0
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenFileMappingW.restype = W.HANDLE
        k32.MapViewOfFile.restype = ctypes.c_void_p
        k32.MapViewOfFile.argtypes = [W.HANDLE, W.DWORD, W.DWORD, W.DWORD, ctypes.c_size_t]
        h = k32.OpenFileMappingW(0x0006, False, r"Local\SoraLvl")
        pm = h and k32.MapViewOfFile(h, 0x0006, 0, 0, 0)   # whole section (an older DLL made it smaller)
        if pm:
            m = S.lvl_map = (ctypes.c_char * LVL_SIZE).from_address(pm)
    return m


def kh1_level_tick(S, pc, now: float) -> None:
    """LEVEL LINK (user 2026-10-05): every Borderlands level-up gives Sora one KH1 level-up, granted as EXP through KH1's
    own kill-reward path (kh1_fx v22), so KH1 itself raises the stats, teaches abilities and shows its LEVEL UP for
    Sora - and Donald / Goofy level up from the same EXP like after a vanilla KH1 kill. Relative on purpose: the
    absolute "Sora level = BL level" sync is planned for the end (user: doing it now would mess the saves up).
    Config kh1_level_link (on), kh1_levels_per_bl (1). Test: config dbg_kh1_levelup = nonce."""
    st, CFG = S.state, S.CFG
    m = _lvl_map(S)
    lv = level(pc)
    if CFG.get("kh1_loadout", True):                           # the BL level loadout sets KH1's levels itself
        st["lvl_bl"] = lv
        return
    if not CFG.get("kh1_level_link", True) or m is None or not kh1_combat_on(S):
        st["lvl_bl"] = lv                                      # off: no backlog when turned on again
        return
    magic, req, done = struct.unpack_from("<3I", m, 0)
    if magic != 0x4C564C53:
        return
    struct.pack_into("<I", m, 84, 1 if CFG.get("kh1_block_kill_exp", True) else 0)   # kh1_fx v26: BL levels drive KH1
    base = st.get("lvl_bl")
    if base is None or lv < base:                              # first look / another character or save loaded
        st["lvl_bl"] = base = lv
    if done != req:                                            # KH1 hasn't handled the last one yet
        return
    dbg = CFG.get("dbg_kh1_levelup")
    test = dbg is not None and dbg == CFG.get("nonce") and st.get("lvl_dbg_done") != dbg
    if lv > base or test:
        n = (lv - base) * int(CFG.get("kh1_levels_per_bl", 1)) if lv > base else 1
        struct.pack_into("<I", m, 12, max(1, min(10, n)))
        struct.pack_into("<I", m, 4, req + 1)
        st["lvl_bl"] = lv
        st["lvl_req_t"] = now
        if test:
            st["lvl_dbg_done"] = dbg
        S.status["kh1_level_req"] = {"bl": [base, lv], "kh1_levels": n, "test": test}
    granted, count = struct.unpack_from("<iI", m, 16)
    names = ("sora", "donald", "goofy")
    rec = {"bl_level": lv, "requests": count, "granted_exp": granted}
    for i, who in enumerate(names):
        b, a = list(m[24 + 8 * i:32 + 8 * i]), list(m[48 + 8 * i:56 + 8 * i])
        rec[who] = {"lv": [b[0], a[0]], "hp": [b[2], a[2]], "mp": [b[4], a[4]], "ap": [b[5], a[5]], "str": [b[6], a[6]],
                    "def": [b[7], a[7]]}
    S.status["kh1_level"] = rec


# ---------------------------------------------------------------- BL LEVEL LOADOUT (release plan step 1, 2026-10-12)
# User-approved design (MODLOG "2026-10-12: NEW PLAN" + "LEVEL-BAND DRAFT"): the Borderlands level drives a TEMPORARY
# KH1 loadout. KH1 level = BL level x kh1_loadout_mult (1.6, cap 100) for Sora / Donald / Goofy; stats + level
# abilities move along KH1's OWN growth tables (FUN_1402b51b0 / FUN_1402b5660: code 1 maxHP+3, 2 maxMP+1, 3 AP+2,
# 4 STR+2, 5 DEF+2, 6 item slot, 7 accessory slot, >= 0x80 ability), relative to the save's real values (stat-up items
# stay). Magic / summons / shared abilities / Sora's Keyblades = pure bands (extras LOCKED while in BL).
# SAVE SAFETY: live memory only (never the save file); the originals are snapshotted once per KH1 process (also to
# Documents\SoraInBorderlands\kh1_loadout_snapshot.json) and written back when KH1 gets the focus (no KH1 save point
# can be reached boosted), when the mod is disabled, and at the next BL start if a stale one is found.
# Not touched: slot counts (6 / 7), story abilities, Donald / Goofy weapons.
KH_SPELL_IDS = {"Fire": 0, "Blizzard": 1, "Thunder": 2, "Cure": 3, "Gravity": 4, "Stop": 5, "Aero": 6}
KH_SUMMON_IDS = {"Dumbo": 0, "Bambi": 1, "Genie": 2, "Tinker Bell": 3, "Mushu": 4, "Simba": 5}
KH_SHARED_IDS = {"High Jump": 1, "Mermaid Kick": 2, "Glide": 3, "Superglide": 4}
KH_BLADE_IDS = {"Kingdom Key": 0x51, "Jungle King": 0x56, "Three Wishes": 0x57, "Fairy Harp": 0x58, "Pumpkinhead": 0x59,
                "Crabclaw": 0x5A, "Divine Rose": 0x5B, "Spellbinder": 0x5C, "Olympia": 0x5D, "Lionheart": 0x5E,
                "Metal Chocobo": 0x5F, "Oathkeeper": 0x60, "Oblivion": 0x61, "Lady Luck": 0x62, "Wishing Star": 0x63,
                "Ultima Weapon": 0x64, "Diamond Dust": 0x65, "One-Winged Angel": 0x66}
# Donald's staves 0x67..0x75 / Goofy's shields 0x77..0x85 (KH1 item record +2 = character + 2, +6 = weapon index 1..15;
# 0x76 / 0x86 are unused duplicates, 0x87+ = guest weapons). Names in KH1's weapon-index order.
KH_STAFF_IDS = {n: 0x67 + i for i, n in enumerate((
    "Mage's Staff", "Morning Star", "Shooting Star", "Magus Staff", "Wisdom Staff", "Warhammer", "Silver Mallet",
    "Grand Mallet", "Lord Fortune", "Violetta", "Dream Rod", "Save the Queen", "Wizard's Relic", "Meteor Strike",
    "Fantasista"))}
KH_SHIELD_IDS = {n: 0x77 + i for i, n in enumerate((
    "Knight's Shield", "Mythril Shield", "Onyx Shield", "Stout Shield", "Golem Shield", "Adamant Shield", "Smasher",
    "Gigas Fist", "Genji Shield", "Herc's Shield", "Dream Shield", "Save the King", "Defender", "Mighty Shield",
    "Seven Elements"))}
LO_PARTY_WPN = (KH_BLADE_IDS, KH_STAFF_IDS, KH_SHIELD_IDS)     # per character: the weapons the bands gate
LO_WPN_LO, LO_WPN_HI = 0x51, 0x85                               # inventory range written / restored / guarded (all three)
# BL level -> what unlocks (user-approved 2026-10-12; spell name = tier 1, "Fira" etc. = tier 2/3). config kh1_loadout_bands overrides.
KH_LOADOUT_BANDS = {
    1: ["Fire", "Cure", "Kingdom Key"], 3: ["Blizzard", "Spellbinder"], 5: ["Thunder", "Simba"], 6: ["Jungle King"],
    7: ["High Jump", "Lady Luck"], 8: ["Three Wishes"],   # Cure from level 1 (user 2026-10-12) 10: ["Fira", "Blizzara", "Dumbo", "Olympia"],
    11: ["Crabclaw"], 12: ["Bambi"], 14: ["Thundara", "Cura", "Gravity", "Pumpkinhead", "Wishing Star"],
    16: ["Glide", "Genie", "Fairy Harp"], 17: ["Stop", "Lionheart"], 19: ["Aero", "Divine Rose"],
    21: ["Tinker Bell", "Metal Chocobo"], 22: ["Gravira", "Stopra", "Aerora"], 24: ["Mushu", "Oathkeeper"],
    26: ["Firaga", "Blizzaga", "Thundaga", "Curaga", "Oblivion"], 28: ["Diamond Dust"], 30: ["Graviga", "Stopga", "Aeroga"],
    32: ["One-Winged Angel"], 36: ["Superglide"], 45: ["Ultima Weapon"]}
# Donald / Goofy weapons (user TODO 2026-10-12): the same BL levels as the Keyblades, in KH1's own weapon order; 15 each
# vs 17 Keyblade levels -> none at 7 and 11; the strongest STR (Meteor Strike / Mighty Shield) with Ultima Weapon.
for _at, _d, _g in ((1, "Mage's Staff", "Knight's Shield"), (3, "Morning Star", "Mythril Shield"),
                    (6, "Shooting Star", "Onyx Shield"), (8, "Magus Staff", "Stout Shield"),
                    (10, "Wisdom Staff", "Golem Shield"), (14, "Warhammer", "Adamant Shield"),
                    (16, "Silver Mallet", "Smasher"), (17, "Grand Mallet", "Gigas Fist"),
                    (19, "Lord Fortune", "Genji Shield"), (21, "Violetta", "Herc's Shield"),
                    (24, "Dream Rod", "Dream Shield"), (26, "Save the Queen", "Save the King"),
                    (28, "Wizard's Relic", "Defender"), (32, "Fantasista", "Seven Elements"),
                    (45, "Meteor Strike", "Mighty Shield")):
    KH_LOADOUT_BANDS.setdefault(_at, []).extend((_d, _g))
KH_SPELL_TIER = {n: (i, t + 1) for i, names in enumerate(KH_SPELLS) for t, n in enumerate(names)}   # "Fira" -> (0, 2)
KH_SPELL_TIER.update({"Gravity": (4, 1), "Gravira": (4, 2), "Graviga": (4, 3), "Stop": (5, 1), "Stopra": (5, 2),
                      "Stopga": (5, 3), "Aero": (6, 1), "Aerora": (6, 2), "Aeroga": (6, 3)})
# party block (0x74 B each, base = *(KH_CHAR_TABLE)): +4 level +5 HP +6 maxHP +7 MP +8 maxMP +9 AP +0xA STR +0xB DEF
# +0x36 weapon +0x38 growth index +0x40 EXP +0x44 abilities[0x30]. Live stats page (entity +0x6C handle):
# +0x3C HP +0x40 maxHP +0x44 MP +0x48 maxMP +0x4C STR (+weapon) +0x50 DEF +0xE4 AP (FUN_1402a5130 builds it).
LO_BLK = {"lv": 4, "hp": 5, "mhp": 6, "mp": 7, "mmp": 8, "ap": 9, "str": 0xA, "def": 0xB}
LO_STP = {"hp": 0x3C, "mhp": 0x40, "mp": 0x44, "mmp": 0x48, "str": 0x4C, "def": 0x50, "ap": 0xE4}
LO_CODE = {1: (("mhp", 3), ("hp", 3)), 2: (("mmp", 1), ("mp", 1)), 3: (("ap", 2),), 4: (("str", 2),), 5: (("def", 2),)}
LO_INV, LO_MAGIC_A, LO_MAGIC_B, LO_SHARED, LO_SUMMONS, LO_UNLOCK = 0x49A, 0x492, 0x1C1E, 0x599, 0x7D0, 0x74
# ROOM SET PIN (user 2026-10-12, "A"): a room's Heartless come from its set number = one save byte per room at
# *(0x232DDB0) + 0x6B + rooms of the earlier worlds (u8 table 0x4DCBE0) + room byte index (FUN_14017cc70 writes these
# from allset.set). Home tw25 (Traverse Town 2nd District, KH1 room 3:24 = byte 3:25): set 17 = Neoshadow + Defender
# (late game; Defenders guard = the "blocked hits"), set 16 = first visit (Shadow, Soldier, Air Soldier, Red Nocturne,
# Blue Rhapsody). Pinned for every save while the loadout is on; the room is re-entered to load it; restored with the rest.
KH_ROOM_SET_PIN = {"3:25": [16, "3:24"]}        # "world:byte index" -> [set, "world:room" to reload]  (config kh1_room_set_pin)


def _lo_docs() -> Path:
    buf = ctypes.create_unicode_buffer(260)
    try:
        ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf)      # CSIDL_PERSONAL = the real Documents (OneDrive too)
    except Exception:  # noqa: BLE001
        pass
    return Path(buf.value or os.path.expanduser("~/Documents"))


def kh1_save_backup(S) -> None:
    """Once per BL start: copy KH1's save files to Documents\\SoraInBorderlands\\kh1_save_backups\\<time> (keep the
    last kh1_save_backups_keep) + RESTORE_KH1_SAVE.bat (copies the newest backup back; run with KH1 closed)."""
    import shutil
    if S.state.get("lo_backup_done"):
        return
    S.state["lo_backup_done"] = True
    docs = _lo_docs()
    src_root = docs / "My Games" / "KINGDOM HEARTS HD 1.5+2.5 ReMIX"
    dst_root = docs / "SoraInBorderlands" / "kh1_save_backups"
    files = [p for p in src_root.glob("*/*/KHFM*") if p.is_file()] if src_root.exists() else []
    if not files:
        S.status["kh1_save_backup"] = f"no KH1 save found under {src_root}"
        return
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    for p in files:
        d = dst_root / stamp / p.relative_to(src_root).parent
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, d / p.name)
    olds = sorted(x for x in dst_root.iterdir() if x.is_dir())
    for x in olds[:-int(S.CFG.get("kh1_save_backups_keep", 10))]:
        shutil.rmtree(x, ignore_errors=True)
    bat = dst_root.parent / "RESTORE_KH1_SAVE.bat"
    if not bat.exists():
        bat.write_text(
            "@echo off\r\nrem Sora in Borderlands: puts your newest KH1 save backup back. Close Kingdom Hearts first.\r\n"
            "tasklist | find /i \"KINGDOM HEARTS\" >nul && (echo Close Kingdom Hearts first. & pause & exit /b 1)\r\n"
            "for /f \"delims=\" %%d in ('dir /b /ad /o-n \"%~dp0kh1_save_backups\"') do (set NEWEST=%%d& goto :got)\r\n"
            "echo No backups found. & pause & exit /b 1\r\n:got\r\n"
            f"echo Restoring backup %NEWEST% ...\r\nxcopy /e /y /i \"%~dp0kh1_save_backups\\%NEWEST%\" \"{src_root}\"\r\n"
            "echo Done. & pause\r\n", encoding="utf-8")
    S.status["kh1_save_backup"] = {"files": len(files), "to": str(dst_root / stamp)}


# KH1 title screen (found 2026-10-15 by memory diff + traces, Steam 1.0.0.2):
KH_TITLE = 0x23404E8         # u32: 1 once the title menu has been reached
KH_TITLE_PIC = 0x2EE8BB8     # u32: screen id: 2..0x18 opening poem / logos (attract loop), 0x1a/0x1b title, 0x22 load menu
KH_TITLE_CURSOR = 0x2EE8BB0  # u32: title menu row: 0 NEW GAME, 1 LOAD, 2 THEATER, 3 BACK
KH_MENU_CHECK = 0x2E92838    # u32: 3 while the load menu's slot list takes input, 0 once a slot was picked


def kh1_autoload(S) -> None:
    """Once per KH1 process: when KH1 sits at its opening / title screen, load the bridge and press through it
    (skip the poem, LOAD, Enter on the slot the list opens on = the last one used) so the player lands in the field
    without touching KH1. Presses go through the bridge pad hook (KH1 ignores keys while unfocused). Never presses
    anything else (F in the load menu deletes the file). Melty builds only: build_pkg.py sets config kh1_autoload true;
    GitHub installs leave it off (the player loads their KH1 save by hand)."""
    st, now = S.state, time.time()
    if not S.CFG.get("kh1_autoload", False) or getattr(S, "dll", None) is None:
        return
    al = st.get("kh_autoload")
    if al and al.get("phase") in ("done", "gave up", "skipped"):
        kh = getattr(S, "kh", None) or {}
        if not kh.get("pid") or kh.get("pid") == al.get("pid"):
            return
        al = None                                                     # a new KH1 process: run again
    if kh1_state(S) is not None and not al:                           # already in the field (BL restarted mid-game)
        st["kh_autoload"] = {"pid": (S.kh or {}).get("pid"), "phase": "skipped"}
        return
    kh = getattr(S, "kh", None) or {}
    pid = kh.get("pid")
    if not pid:
        return
    if not al:
        al = st["kh_autoload"] = {"pid": pid, "phase": "bridge", "t0": now, "next": 0.0, "press": None, "picked": False}

    def u32(rva):
        b = kh1_mem(S, rva, 4)
        return int.from_bytes(b, "little") if b else None

    def finish(phase):
        al["phase"] = phase
        S.dll.sr_set_pad(0, 0, 0x80, 0x80, 0x80, 0x80)               # hand the pad back
        kh1_event(S, f"KH1 auto-load: {phase} after {now - al['t0']:.1f} s")

    sora = kh1_mem(S, KH_SORA_PTR, 8)
    if sora and int.from_bytes(sora, "little"):
        return finish("done")
    if now - al["t0"] > float(S.CFG.get("kh1_autoload_timeout", 90.0)):
        return finish("gave up")
    pic, title, cur, check = u32(KH_TITLE_PIC), u32(KH_TITLE), u32(KH_TITLE_CURSOR), u32(KH_MENU_CHECK)
    S.status["kh1_autoload"] = {"phase": al["phase"], "pic": pic, "title": title, "cursor": cur, "check": check}
    if al["phase"] == "bridge":                                       # the pad hook first (khfx_inject loads kh1_fx later)
        sh = kh1_shared(S)
        if sh and sh_u32(sh, SH_BRIDGE_VERSION) >= 3 and kh1_has_module(pid, "kh1_bridge.dll"):
            al["phase"] = "menu"
        elif now >= al["next"]:
            al["next"] = now + 5.0
            bpath = HERE / "data" / "kh1_bridge.dll"
            if kh1_has_module(pid, "kh1_bridge.dll") is False and bpath.exists():
                _kh1_load(S, pid, str(bpath.resolve()))
        return
    # one press at a time: hold 0.12 s, then at least 0.6 s released before the next decision
    btn = 0
    if al["press"]:
        bits, until = al["press"]
        if now < until:
            btn = bits
        else:
            al["press"] = None
    elif now >= al["next"] and pic is not None:
        if al["picked"]:
            pass                                                      # slot chosen: just wait for the field
        elif pic == 0x22:
            if check == 3 and now - al.get("list_t", now) > 1.0:      # slot list up (after "Checking..."): Enter
                btn = PAD_CROSS
                al["picked_try"] = al.get("picked_try", 0) + 1
            al.setdefault("list_t", now)
        elif title == 1 and pic in (0x1a, 0x1b) and cur is not None:
            btn = PAD_CROSS if cur == 1 else (PAD_DOWN if cur == 0 else PAD_UP)
        elif pic < 0x1a:
            btn = PAD_CROSS                                           # opening poem / attract loop: skip to the title
        if btn:
            al["press"] = (btn, now + 0.12)
            al["next"] = now + 0.72
    if pic == 0x22 and check == 0 and al.get("picked_try"):
        al["picked"] = True                                           # the list let go of input = the load started
    S.dll.sr_set_pad(1, btn, 0x80, 0x80, 0x80, 0x80)


MELTY_TEST_FILE = Path(os.environ.get("LOCALAPPDATA", "")) / "Melty" / "test" / "sora-in-borderlands.json"
MELTY_CHECKS = ("kh1-autoload", "bridge-input", "kh1-fx-loaded", "sora-in-level")


def bl_frontend_play() -> str:
    """One step from BL's front end toward a level: in the lobby press Play Game (extStart), else open the single-player
    lobby from the main menu (OpenSP). Returns what it did."""
    import unrealsdk  # type: ignore
    def live(cls):
        return [o for o in unrealsdk.find_all(cls) if o._path_name().startswith("Transient.")]
    lobby = live("WillowGFxMenuLobby2")
    if lobby:
        lobby[0].extStart(0, "")
        return "lobby extStart"
    fe = live("WillowGFxMenuFrontend")
    if fe:
        return f"frontend OpenSP -> {fe[0].OpenSP()}"
    ps = [o for o in unrealsdk.find_all("WillowGFxMoviePressStart") if not o.Name.startswith("Default__")]
    if ps:                                     # "press start": what any key does there
        try:
            ps[-1].extContinue()
            return f"press start extContinue ({ps[-1]._path_name()})"
        except Exception as ex:  # noqa: BLE001
            return f"press start ERR {type(ex).__name__}: {ex}"
    return "no front end yet"


def melty_test_tick(S, pc) -> None:
    """Melty test runs only (while %LOCALAPPDATA%\\Melty\\test\\sora-in-borderlands.json exists): leave BL's main menu
    for Fyrestone by itself and append "pass <check>" / "fail <check>: why" lines to the file the test names. Never
    quits the game (Melty closes it)."""
    st, now = S.state, time.time()
    mt = st.get("melty_test")
    if mt is None:
        if now < st.get("melty_test_look", 0):
            return
        st["melty_test_look"] = now + 10.0
        try:
            cfg = json.loads(MELTY_TEST_FILE.read_text(encoding="utf-8-sig"))
        except Exception:  # noqa: BLE001
            return
        mt = st["melty_test"] = {"t0": now, "out": cfg.get("checks"), "done": {}, "menu_t": None, "opened": 0}
        kh1_event(S, f"Melty test mode: run {cfg.get('run')} of {cfg.get('runs')}")

    def report(name, ok, why=""):
        if name in mt["done"] or not mt["out"]:
            return
        mt["done"][name] = ok
        try:
            with open(mt["out"], "a", encoding="utf-8") as f:
                f.write(f"pass {name}\n" if ok else f"fail {name}: {why}\n")
        except Exception as ex:  # noqa: BLE001
            S.status["melty_test"] = f"ERR writing checks: {ex}"

    pawn = pc.Pawn if pc is not None else None
    # main menu -> lobby -> Play Game with the last save, through BL's own front-end calls. Called right here (PostRender):
    # S.pending jobs never run in the front end (no PlayerTick there), so a queued console 'open' never fired.
    if pawn is None:
        mt["menu_t"] = mt["menu_t"] or now
        al = st.get("kh_autoload") or {}
        ready = al.get("phase") in ("done", "gave up", "skipped") or now - mt["menu_t"] > 90
        if ready and now - mt["menu_t"] > 3 and mt["opened"] < 16 and now >= mt.get("open_next", 0):
            mt["opened"] += 1                  # press start -> main menu -> lobby -> level: one step each try
            mt["open_next"] = now + 12.0
            mt["step"] = bl_frontend_play()
    else:
        mt["menu_t"] = None
    al = st.get("kh_autoload") or {}
    if al.get("phase") in ("done", "skipped"):
        report("kh1-autoload", True)
    elif al.get("phase") == "gave up":
        report("kh1-autoload", False, f"KH1 never reached the field (last {S.status.get('kh1_autoload')})")
    sh = kh1_shared(S)
    seq = sh_u32(sh, 16) if sh else 0                                  # SharedBlock.kh_seq: overridden pad reads
    if pawn is not None and kh1_state(S) is not None:
        mt.setdefault("seq0", seq)
        if seq - mt["seq0"] > 60:
            report("bridge-input", True)
        mt["live_since"] = mt.get("live_since") or now
        if now - mt["live_since"] > 10 and S.frame.visible:
            report("sora-in-level", True)
    else:
        mt["live_since"] = None
    pid = (getattr(S, "kh", None) or {}).get("pid")
    if pid and st.get("khfx_injected") == pid:
        report("kh1-fx-loaded", True)
    if now - mt["t0"] > float(S.CFG.get("melty_test_deadline", 240.0)):   # Melty's run is 5 min: answer before it
        why = {"kh1-autoload": "KH1 still not in the field", "bridge-input": "no forwarded pad reads in KH1",
               "kh1-fx-loaded": "kh1_fx.dll never loaded into KH1", "sora-in-level": "no live Sora in a BL level"}
        for name in MELTY_CHECKS:
            report(name, False, why[name])
    S.status["melty_test"] = {"done": mt["done"], "opened": mt["opened"], "step": mt.get("step")}


def _lo_bands(S) -> dict:
    b = S.CFG.get("kh1_loadout_bands")
    return {int(k): v for k, v in b.items()} if isinstance(b, dict) else KH_LOADOUT_BANDS


def kh1_loadout_target(S, bl_level: int) -> dict:
    """What a Sora of this Borderlands level gets: KH1 level, spell tiers, summons, shared abilities, Keyblades."""
    lv = max(1, min(100, int(round(bl_level * float(S.CFG.get("kh1_loadout_mult", 1.6))))))
    tiers, summons, shared, blades, staves, shields = [0] * 7, [], [], [], [], []
    for at in sorted(_lo_bands(S)):
        if at > bl_level:
            break
        for name in _lo_bands(S)[at]:
            if name in KH_SPELL_TIER:
                sid, t = KH_SPELL_TIER[name]
                tiers[sid] = max(tiers[sid], t)
            elif name in KH_SUMMON_IDS:
                summons.append(KH_SUMMON_IDS[name])
            elif name in KH_SHARED_IDS:
                shared.append(KH_SHARED_IDS[name])
            elif name in KH_BLADE_IDS:
                blades.append(KH_BLADE_IDS[name])
            elif name in KH_STAFF_IDS:
                staves.append(KH_STAFF_IDS[name])
            elif name in KH_SHIELD_IDS:
                shields.append(KH_SHIELD_IDS[name])
    return {"bl": bl_level, "lv": lv, "tiers": tiers, "summons": summons, "shared": shared, "blades": blades,
            "wpns": [blades, staves, shields]}


def kh1_mapped_level(S, bl_level: int) -> float:
    """Damage formulas: an enemy's BL level on the same KH1 scale as Sora's (kh1_loadout on), else unchanged."""
    if S.CFG.get("kh1_loadout", True):
        return max(1.0, min(100.0, bl_level * float(S.CFG.get("kh1_loadout_mult", 1.6))))
    return float(bl_level)


def _lo_u32(S, rva: int) -> int:
    b = kh1_mem(S, rva, 4)
    return struct.unpack("<I", b)[0] if b else 0


def _lo_growth(S, base: int, who: int, lv_from: int, lv_to: int, idx: int):
    """KH1's level-up steps from lv_from to lv_to (lv_to > lv_from), exactly like FUN_1402b51b0: one stat code per level
    (stat table) + the growth index walking the ability table to the next non-zero entry. -> ([codes], new index)."""
    if who == 0:
        dream = (kh1_abs(S, base + 0x48C, 1) or b"\x00")[0]
        abil_off = _lo_u32(S, 0x528D10 + 4 * dream)
    else:
        abil_off = _lo_u32(S, 0x528CC4 + 8 * who)
    stat = kh1_mem(S, 0x2D22D40 + _lo_u32(S, 0x528CC0 + 8 * who), 100) or bytes(100)
    abil = kh1_mem(S, 0x2D22D40 + abil_off, 100) or bytes(100)
    codes = []
    for lv in range(lv_from, lv_to):                     # level lv -> lv + 1
        codes.append(stat[lv - 1])
        n = (lv + 1) - idx
        k = 0
        while k < n and idx <= 100:
            idx += 1
            e = abil[idx - 2]
            if e:
                codes.append(e)
                break
            k += 1
    return [c for c in codes if c], idx


def _lo_growth_from1(S, base: int, who: int, lv: int):
    """Simulated growth from level 1 (index 1) -> per-level growth index, to reverse the steps of levels lv_t..lv_c."""
    idxs = {1: 1}
    idx = 1
    for L in range(1, lv):
        _, idx = _lo_growth(S, base, who, L, L + 1, idx)
        idxs[L + 1] = idx
    return idxs


def _lo_exp_for(S, base: int, who: int, lv: int) -> int:
    off = _lo_u32(S, 0x528028 + 4 * (kh1_abs(S, base + 0x48D, 1) or b"\x00")[0]) if who == 0 else _lo_u32(S, 0x528000 + 4 * who)
    inc = kh1_mem(S, 0x2D22D40 + off, 200) or bytes(200)
    return sum(struct.unpack("<99H", inc[:198])[:max(0, lv - 1)])


def _lo_stp(S, who: int) -> int:
    ent = int.from_bytes(kh1_mem(S, KH_PARTY[who], 8) or bytes(8), "little")
    raw = ent and kh1_abs(S, ent + 0x6C, 4)
    return (raw and kh1_resolve(S, struct.unpack("<I", raw)[0])) or 0


def _lo_wpn_bonus(S, who: int, item: int):
    """(STR, MP) a weapon item adds (FUN_140286a00: item record +6 = weapon index, weapon table per character)."""
    if not item:
        return 0, 0
    tab = int.from_bytes(kh1_mem(S, 0x2D22D38, 8) or bytes(8), "little")
    rec = tab and kh1_abs(S, tab + (item - 1) * 0x14, 0x14)
    widx = struct.unpack_from("<h", rec, 6)[0] if rec else 0
    if widx <= 0:
        return 0, 0
    w = kh1_mem(S, 0x2D22D40 + _lo_u32(S, 0x528060 + 4 * who) + (widx - 1) * 0x58, 0x40)
    return (struct.unpack_from("<b", w, 0x30)[0], struct.unpack_from("<b", w, 0x38)[0]) if w else (0, 0)


def _lo_room_set_addr(S, key: str) -> int:
    w, i = (int(x) for x in key.split(":"))
    ptr = int.from_bytes(kh1_mem(S, 0x232DDB0, 8) or bytes(8), "little")
    rooms = kh1_mem(S, 0x4DCBE0, 32) or bytes(32)
    return ptr + 0x6B + sum(struct.unpack("<32b", rooms)[:w]) + i if ptr else 0


def kh1_loadout_snapshot(S, base: int) -> dict:
    snap = {"pid": S.kh.get("pid"), "base": base, "blocks": [], "stp": [], "room_sets": {}}
    for key in S.CFG.get("kh1_room_set_pin", KH_ROOM_SET_PIN):
        a = _lo_room_set_addr(S, key)
        b = a and kh1_abs(S, a, 1)
        if b:
            snap["room_sets"][key] = b[0]
    for who in range(3):
        snap["blocks"].append((kh1_abs(S, base + 0x74 * who, 0x74) or b"").hex())
    for name, off, n in (("inv", LO_INV, 0x100), ("magic_a", LO_MAGIC_A, 7), ("magic_b", LO_MAGIC_B, 7),
                         ("shared", LO_SHARED, 9), ("summons", LO_SUMMONS, 6), ("unlock", LO_UNLOCK, 1)):
        snap[name] = (kh1_abs(S, base + off, n) or b"").hex()
    return snap


def _lo_write(S, addr: int, data: bytes) -> None:
    data = bytes(data)
    ctypes.windll.kernel32.WriteProcessMemory(ctypes.c_void_p(S.kh["h"]), ctypes.c_void_p(addr), data, len(data), None)


def _lo_set_char(S, base: int, who: int, orig: bytes, want: dict, now_blk: bytes) -> dict:
    """Writes one party member's level/stats/EXP/abilities/weapon (block + live stats page, by delta)."""
    blk = bytearray(now_blk)
    stp = _lo_stp(S, who)
    before = {k: blk[o] for k, o in LO_BLK.items()}
    o_lv, t_lv = orig[4], want["lv"]
    # start from the ORIGINAL values, then KH1's own growth from the original level to the target
    for k, o in LO_BLK.items():
        blk[o] = orig[o]
    blk[0x38] = orig[0x38]
    blk[0x44:0x74] = orig[0x44:0x74]
    sign = 1
    if t_lv > o_lv:
        codes, idx = _lo_growth(S, base, who, o_lv, t_lv, orig[0x38])
    elif t_lv < o_lv:
        sign = -1
        idxs = _lo_growth_from1(S, base, who, o_lv)
        codes, _ = _lo_growth(S, base, who, t_lv, o_lv, idxs.get(t_lv, 1))
        idx = idxs.get(t_lv, 1)
    else:
        codes, idx = [], orig[0x38]
    abil = [b for b in blk[0x44:0x74] if b]
    for c in codes:
        if c >= 0x80:
            if sign > 0:
                abil.append(c | 0x80)                       # KH1 learns them unequipped (FUN_140284e10)
            else:
                hit = next((j for j in range(len(abil) - 1, -1, -1) if abil[j] & 0x7F == c & 0x7F), None)
                if hit is not None:
                    abil.pop(hit)
        for k, d in LO_CODE.get(c, ()):
            o = LO_BLK[k]
            blk[o] = max(0, min(255 if k in ("hp", "mhp", "mp", "mmp") else 100, blk[o] + sign * d))
    blk[4], blk[0x38] = t_lv, max(1, min(100, idx))
    blk[5], blk[7] = min(now_blk[5], blk[6]), min(now_blk[7], blk[8])     # current HP / MP: kept, clamped
    # the player's own ability choices from the F9 menu while in BL (kh1_ability_prefs_tick): on / off as last seen
    if want.get("abil_on") is not None:
        pref = list(want["abil_on"])
        for j, a in enumerate(abil):
            if a & 0x7F in pref:
                pref.remove(a & 0x7F)
                abil[j] = a & 0x7F
            else:
                abil[j] = a | 0x80
    # AP: unequip from the end while the equipped abilities cost more than Sora has (KH1 shows 0 AP left otherwise)
    atab = int.from_bytes(kh1_mem(S, 0x2D36D48, 8) or bytes(8), "little")
    def cost(a):
        r = atab and kh1_abs(S, atab + ((a & 0x7F) - 1) * 0xC, 1)
        return r[0] if r else 0
    page_ap = kh1_abs(S, stp + 0xE4, 4) if stp else None    # AP total incl. accessories (stats page) - block AP
    bonus = struct.unpack("<i", page_ap)[0] - now_blk[9] if page_ap else 0
    if who == 0:                                          # C / right click ARE these: always on, outside the AP budget
        for j, a in enumerate(abil):
            if a & 0x7F in KH_ABIL_ALWAYS_ON:
                abil[j] = a & 0x7F
    used = sum(cost(a) for a in abil if not a & 0x80 and not (who == 0 and a & 0x7F in KH_ABIL_ALWAYS_ON))
    for j in range(len(abil) - 1, -1, -1):
        if used <= blk[9] + bonus:
            break
        if not abil[j] & 0x80 and not (who == 0 and abil[j] & 0x7F in KH_ABIL_ALWAYS_ON):
            used -= cost(abil[j])
            abil[j] |= 0x80
    blk[0x44:0x74] = bytes(abil[:0x30]).ljust(0x30, b"\x00")
    struct.pack_into("<I", blk, 0x40, _lo_exp_for(S, base, who, t_lv) if t_lv != o_lv else struct.unpack_from("<I", orig, 0x40)[0])
    # live stats page: same deltas (+ weapon STR / MP change), HP / MP clamped
    if stp:
        page = kh1_abs(S, stp, 0xE8)
        if page:
            page = bytearray(page)
            ws0 = wm0 = ws1 = wm1 = 0                              # (weapon: KH1's equip + refresh, kh1_fx v37)
            for k, o in LO_STP.items():
                if k in ("hp", "mp"):
                    continue
                v = struct.unpack_from("<i", page, o)[0] + blk[LO_BLK[k]] - before[k]
                v += (ws1 - ws0) if k == "str" else (wm1 - wm0) if k == "mmp" else 0
                struct.pack_into("<i", page, o, max(0, v))
            for k, mk in (("hp", "mhp"), ("mp", "mmp")):
                cur, mx = struct.unpack_from("<i", page, LO_STP[k])[0], struct.unpack_from("<i", page, LO_STP[mk])[0]
                struct.pack_into("<i", page, LO_STP[k], max(1 if k == "hp" else 0, min(cur, mx)))
            for k in LO_STP:
                _lo_write(S, stp + LO_STP[k], page[LO_STP[k]:LO_STP[k] + 4])
            # block HP / MP = the live values: KH1's refresh rebuilds the stats page from the block (FUN_1402a5130)
            blk[5] = max(1, min(255, struct.unpack_from("<i", page, LO_STP["hp"])[0]))
            blk[7] = max(0, min(255, struct.unpack_from("<i", page, LO_STP["mp"])[0]))
    _lo_write(S, base + 0x74 * who, bytes(blk))
    return {"lv": [o_lv, t_lv], "str": [orig[0xA], blk[0xA]], "def": [orig[0xB], blk[0xB]], "mhp": [orig[6], blk[6]],
            "ap": [orig[9], blk[9]], "codes": len(codes), "stp": bool(stp)}


def kh1_loadout_apply(S, base: int, tgt: dict) -> dict:
    snap = S.state["lo_snap"]
    rec = {"bl": tgt["bl"], "kh1_lv": tgt["lv"]}
    # weapons (Sora's Keyblades + Donald's staves / Goofy's shields): allowed ones owned (count >= 1), locked ones 0;
    # a locked equipped weapon -> the newest allowed one through KH1's own equip (queued: one at a time)
    inv = bytearray(bytes.fromhex(snap["inv"]))
    gate = [S.CFG.get("kh1_loadout_blades", True)] + [S.CFG.get("kh1_loadout_party_wpn", True)] * 2
    equips = []
    for who in range(3):
        if not gate[who]:
            continue
        allowed = tgt["wpns"][who]
        for wid in LO_PARTY_WPN[who].values():
            inv[wid - 1] = max(1, inv[wid - 1]) if wid in allowed else 0
        wpn = (kh1_abs(S, base + 0x74 * who + 0x36, 1) or b"\x00")[0]
        if wpn in LO_PARTY_WPN[who].values() and wpn not in allowed:
            new = allowed[-1] if allowed else KB_START_WEAPON[who]
            equips.append(new)
            rec.setdefault("weapon", {})[("sora", "donald", "goofy")[who]] = [hex(wpn), hex(new)]
    if any(gate):
        _lo_write(S, base + LO_INV + LO_WPN_LO - 1, bytes(inv[LO_WPN_LO - 1:LO_WPN_HI]))
    want0 = {"lv": tgt["lv"]}
    # level / stats / abilities
    for who in range(3):
        orig = bytes.fromhex(snap["blocks"][who])
        now_blk = kh1_abs(S, base + 0x74 * who, 0x74) or orig
        want = {"lv": tgt["lv"], "abil_on": _lo_abil_prefs(S).get(str(who))}
        rec[("sora", "donald", "goofy")[who]] = _lo_set_char(S, base, who, orig, want, now_blk)
    # magic: unlock bit + both level arrays
    if S.CFG.get("kh1_loadout_magic", True):
        bits = sum(1 << i for i, t in enumerate(tgt["tiers"]) if t)
        _lo_write(S, base + LO_UNLOCK, bytes([bits]))
        _lo_write(S, base + LO_MAGIC_A, bytes(tgt["tiers"]))
        _lo_write(S, base + LO_MAGIC_B, bytes(tgt["tiers"]))
        S.state["kh_spells_t"] = 0
    # summons (owned list, 0xFF = empty) and shared abilities (High Jump / Glide / Superglide; kept equipped like the save)
    if S.CFG.get("kh1_loadout_summons", True):
        own = [b for b in bytes.fromhex(snap["summons"]) if b != 0xFF]
        keep = [b for b in own if b in tgt["summons"]] + [s for s in tgt["summons"] if s not in own]
        _lo_write(S, base + LO_SUMMONS, bytes(keep[:6]).ljust(6, b"\xFF"))
    if S.CFG.get("kh1_loadout_shared", True):
        own = [b for b in bytes.fromhex(snap["shared"]) if b]
        banded = {KH_SHARED_IDS[n] for at in _lo_bands(S) for n in _lo_bands(S)[at] if n in KH_SHARED_IDS}
        keep = [b for b in own if b & 0x7F in tgt["shared"] or b & 0x7F not in banded]   # (Mermaid Kick: not banded)
        keep += [s for s in tgt["shared"] if s not in [b & 0x7F for b in own]]
        _lo_write(S, base + LO_SHARED, bytes(keep[:8]).ljust(9, b"\x00"))
    if S.CFG.get("kh1_room_set_pin_on", True) and kh1_proxies_on(S):
        for key, (sv, _room) in S.CFG.get("kh1_room_set_pin", KH_ROOM_SET_PIN).items():
            a = _lo_room_set_addr(S, key)
            if a and key in snap.get("room_sets", {}):
                _lo_write(S, a, bytes([sv]))
                rec.setdefault("room_set", {})[key] = [snap["room_sets"][key], sv]
    rec.update(tiers=tgt["tiers"], summons=tgt["summons"], shared=tgt["shared"], blades=[hex(b) for b in tgt["blades"]],
               staves=[hex(b) for b in tgt["wpns"][1]], shields=[hex(b) for b in tgt["wpns"][2]])
    rec["kh1_request"] = kh1_equip_queue(S, equips)
    return rec


def kh1_equip_queue(S, items) -> str:
    """KH1's equip (kh1_fx lo_do) takes one weapon at a time and skips while a weapon model is still loading, so the
    party's equips go through a queue (kh1_equip_queue_tick); each request also runs KH1's refresh. No items = refresh."""
    items = [int(i) for i in items if i]
    if not items:
        return kh1_loadout_request(S, 0, refresh=True)
    q = S.state.setdefault("lo_eq_q", [])
    for it in items:
        if it in q:
            q.remove(it)
        q.append(it)
    return f"queued {[hex(i) for i in q]}"


def kh1_equip_queue_tick(S, now: float) -> None:
    st = S.state
    q, fl = st.setdefault("lo_eq_q", []), st.get("lo_eq_inflight")
    if not q and not fl:
        return
    m = _lvl_map(S)
    if m is None or struct.unpack_from("<I", m, 0)[0] != 0x4C564C53:
        return
    req, done = struct.unpack_from("<2I", m, 92)
    if fl:
        if req != done:
            if now - fl[2] > 10.0:                            # KH1 never handled it (paused / loading): send again
                st["lo_eq_inflight"] = None
                q.insert(0, fl[0])
            return
        res = struct.unpack_from("<I", m, 108)[0]
        st["lo_eq_inflight"] = None
        if res & 0x10 and fl[1] < 40:                         # equip busy (model loading / KH1 menu open): retry
            q.insert(0, fl[0])
            st["lo_eq_retry"] = fl[1] + 1
            st["lo_eq_next"] = now + 0.5
        else:
            st["lo_eq_retry"] = 0
            S.status["kh1_equip_last"] = {"item": hex(fl[0]), "result": hex(res), "t": time.strftime("%H:%M:%S")}
        return
    # (session 9 KH1 crashes: an equip during the mod's home warp / room reload hit a party member without a model)
    if not q or now < st.get("lo_eq_next", 0) or req != done or _lo_u32(S, 0x2E9A0C4) or now < st.get("kh_warping_until", 0):
        return
    item = q.pop(0)
    kh1_loadout_request(S, item, refresh=True)
    st["lo_eq_inflight"] = (item, st.get("lo_eq_retry", 0), now)


def kh1_loadout_request(S, item: int = 0, refresh: bool = True) -> str:
    """kh1_fx v37 (LvlShared @92): on KH1's game thread, equip Sora's weapon through KH1's own Equipment-menu path
    (FUN_140286720: model unload / load included) and run KH1's menu-close refresh FUN_1402a4e50 (stats page + ability
    effects rebuilt from the party blocks we just wrote)."""
    m = _lvl_map(S)
    if m is None or struct.unpack_from("<I", m, 0)[0] != 0x4C564C53:
        return "no level mapping"
    req, done = struct.unpack_from("<2I", m, 92)
    if req != done:
        return f"busy (kh1_fx < v37?) req {req} done {done}"
    # v41 flag 2: KH1 MAGIC SYNC - reload every spell whose save level (+0x492) differs from what KH1 has loaded (runtime
    # table 0x2D22B80). Without it the magic menu showed stale level-1 / greyed rows and only 3 spells cast (2026-10-12).
    flags = (1 if refresh else 0) | (2 if S.CFG.get("kh1_loadout_magic_sync", True) else 0)
    struct.pack_into("<2I", m, 100, item, flags)
    struct.pack_into("<I", m, 92, req + 1)
    S.state["lo_req_t"] = time.time()
    return f"sent item {hex(item)} refresh {refresh}"


def kh1_loadout_restore(S, why: str = "") -> bool:
    """Writes every snapshotted original back (same KH1 process only). Safe to call any time."""
    snap = S.state.get("lo_snap")
    if not snap or getattr(S, "kh", None) is None or not S.kh.get("h") or S.kh.get("pid") != snap.get("pid"):
        return False
    kh1_guard_disarm(S)                                       # kh1_fx must not restore over our restore
    base = snap["base"]
    for who in range(3):
        orig = bytes.fromhex(snap["blocks"][who])
        now_blk = kh1_abs(S, base + 0x74 * who, 0x74) or orig
        _lo_set_char(S, base, who, orig, {"lv": orig[4]}, now_blk)
    inv = bytes.fromhex(snap["inv"])
    _lo_write(S, base + LO_INV + LO_WPN_LO - 1, inv[LO_WPN_LO - 1:LO_WPN_HI])   # weapons only: potions etc. found meanwhile stay
    for key, v in snap.get("room_sets", {}).items():           # the room's own set back (loads on the next entry)
        a = _lo_room_set_addr(S, key)
        if a:
            _lo_write(S, a, bytes([v]))
    equips = []
    for who in range(3):
        cur_wpn = (kh1_abs(S, base + 0x74 * who + 0x36, 1) or b"\x00")[0]
        orig_wpn = bytes.fromhex(snap["blocks"][who])[0x36]
        if orig_wpn and cur_wpn != orig_wpn:
            equips.append(orig_wpn)
    S.state["lo_eq_q"] = []                                   # drop banded equips still waiting: the originals win
    S.status["kh1_loadout_restore_req"] = kh1_equip_queue(S, equips)
    for name, off in (("magic_a", LO_MAGIC_A), ("magic_b", LO_MAGIC_B), ("shared", LO_SHARED),
                      ("summons", LO_SUMMONS), ("unlock", LO_UNLOCK)):
        _lo_write(S, base + off, bytes.fromhex(snap[name]))
    S.state["lo_applied"] = None
    S.state["lo_snap"] = None                                 # re-snapshot before the next apply (KH1 may change meanwhile)
    S.state["kh_spells_t"] = 0
    _lo_snap_save(dict(snap, applied=False))
    S.status["kh1_loadout_restored"] = {"why": why, "t": time.strftime("%H:%M:%S")}
    return True


# GUARD (release step 6 + J5, kh1_fx v38 "Local\SoraGuard", layout in kh1_fx.cpp GuardShared): Borderlands keeps the
# ORIGINAL bytes of everything the level loadout changed inside KH1 (kh1_fx holds them) and bumps a heartbeat every tick.
# BL process gone / heartbeat stale (config kh1_guard_stale_ms) -> kh1_fx writes the originals back on KH1's game thread
# (+ KH1's own equip / refresh) and presses Start so KH1 pauses instead of fighting on alone. When it has fired, this side
# re-snapshots and re-applies (KH1 holds the real values again) and unpauses KH1 (paused_by_guard).
GUARD_SIZE, GUARD_PATCHES, GUARD_PATCH_MAX, GUARD_MAGIC = 64 + 16 * 0x90, 16, 0x80, 0x44524753


def _guard_map(S):
    import ctypes.wintypes as W
    pid = (getattr(S, "kh", None) or {}).get("pid")
    if getattr(S, "guard_map_pid", None) != pid:               # KH1 restarted: its new kh1_fx made a new section
        S.guard_map = None
        S.guard_map_pid = pid
    m = getattr(S, "guard_map", None)
    if m is None and pid and time.time() > S.state.get("guard_map_retry", 0):
        S.state["guard_map_retry"] = time.time() + 3.0
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenFileMappingW.restype = W.HANDLE
        k32.MapViewOfFile.restype = ctypes.c_void_p
        k32.MapViewOfFile.argtypes = [W.HANDLE, W.DWORD, W.DWORD, W.DWORD, ctypes.c_size_t]
        h = k32.OpenFileMappingW(0x0006, False, r"Local\SoraGuard")
        pm = h and k32.MapViewOfFile(h, 0x0006, 0, 0, GUARD_SIZE)
        if pm:
            m = S.guard_map = (ctypes.c_char * GUARD_SIZE).from_address(pm)
            S.state["guard_fired_seen"] = struct.unpack_from("<I", m, 48)[0]
    return m


def kh1_guard_arm(S, snap: dict) -> str:
    """Hands kh1_fx the originals of every field kh1_loadout_apply writes (same list as kh1_loadout_restore)."""
    m = _guard_map(S)
    if m is None:
        return "no guard (kh1_fx < v38?)"
    base = snap["base"]
    patches = [(base + 0x74 * who, bytes.fromhex(snap["blocks"][who]), 1) for who in range(3)]
    patches.append((base + LO_INV + LO_WPN_LO - 1, bytes.fromhex(snap["inv"])[LO_WPN_LO - 1:LO_WPN_HI], 0))
    for name, off in (("magic_a", LO_MAGIC_A), ("magic_b", LO_MAGIC_B), ("shared", LO_SHARED),
                      ("summons", LO_SUMMONS), ("unlock", LO_UNLOCK)):
        patches.append((base + off, bytes.fromhex(snap[name]), 0))
    for key, v in snap.get("room_sets", {}).items():
        a = _lo_room_set_addr(S, key)
        if a:
            patches.append((a, bytes([v]), 0))
    patches = [p for p in patches if 0 < len(p[1]) <= GUARD_PATCH_MAX and (p[2] != 1 or len(p[1]) == 0x74)]
    struct.pack_into("<I", m, 20, 0)                          # disarmed while the table changes
    for i, (addr, data, kind) in enumerate(patches[:GUARD_PATCHES]):
        o = 64 + i * 0x90
        struct.pack_into("<QHHI", m, o, addr, len(data), kind, 0)
        m[o + 16:o + 16 + len(data)] = data
    # original weapons: byte i = party member i's (kh1_fx v43 re-equips all three; v38-v42 read Sora's byte only)
    items = 0
    for who in range(3):
        blk = bytes.fromhex(snap["blocks"][who])
        items |= (blk[0x36] if len(blk) > 0x36 else 0) << (8 * who)
    struct.pack_into("<II", m, 24, min(len(patches), GUARD_PATCHES), items)
    struct.pack_into("<I", m, 20, 1)
    return f"armed {min(len(patches), GUARD_PATCHES)} patches"


def kh1_guard_disarm(S) -> None:
    m = _guard_map(S)
    if m is not None:
        struct.pack_into("<I", m, 20, 0)


def kh1_guard_tick(S) -> None:
    st = S.state
    m = _guard_map(S)
    if m is None:
        S.status["kh1_guard"] = "no mapping"
        return
    on = bool(S.CFG.get("kh1_guard", True))
    hb = struct.unpack_from("<I", m, 12)[0]
    # test hook (dbg_exec only, never config): S.state["guard_hold_until"] = time + N -> no heartbeat, 3 s stale limit,
    # so the guard fires like after a Borderlands hang without killing anything
    hold = time.time() < st.get("guard_hold_until", 0)
    struct.pack_into("<IIIII", m, 0, GUARD_MAGIC, 1, os.getpid() if on else 0, hb if hold else (hb + 1) & 0xFFFFFFFF,
                     3000 if hold else int(S.CFG.get("kh1_guard_stale_ms", 20000)))
    struct.pack_into("<II", m, 32, 1 if S.CFG.get("kh1_guard_pause", True) else 0, PAD_START)
    state, reason, fired, result, paused = struct.unpack_from("<5I", m, 40)
    if fired != st.get("guard_fired_seen", fired):           # it fired while Borderlands was away / stuck
        st["guard_fired_seen"] = fired
        why = {1: "Borderlands gone", 2: "heartbeat stale"}.get(reason, reason)
        # the DLL writes the result a moment after the fired count: report it once KH1 is done (state 0) or after 6 s
        st["guard_report"] = {"why": why, "t": time.strftime("%H:%M:%S"), "until": time.time() + 6.0}
        kh1_event(S, f"guard fired ({why}): KH1 has its real values again -> re-apply")
        snap = st.get("lo_snap")
        if snap:
            _lo_snap_save(dict(snap, applied=False))
        st["lo_snap"] = st["lo_applied"] = None
        st["kh_spells_t"] = 0
    st.setdefault("guard_fired_seen", fired)
    rep = st.get("guard_report")
    if rep and ((state == 0 and time.time() > rep["until"] - 5.0) or time.time() > rep["until"]):
        st["guard_report"] = None
        kh1_event(S, f"guard result {result:#x} (patches 1, equip/refresh << 8, paused 0x10000, weapons queued << 24,"
                     f" re-equips done << 28)")
        S.status["kh1_guard_fired"] = {"why": rep["why"], "result": hex(result), "t": rep["t"]}
    if paused and kh1_combat_on(S) and time.time() > st.get("guard_unpause_t", 0):
        st["guard_unpause_t"] = time.time() + 2.0
        if (kh1_mem(S, KH_PAUSED, 1) or b"\0")[0] == 1 or (kh1_mem(S, KH_MENU_OPEN, 1) or b"\0")[0]:
            kh1_press(S, PAD_START, 0.12)                     # the guard's own pause / menu -> closed again
            kh1_event(S, "guard: unpaused KH1 (it paused it while Borderlands was away)")
        struct.pack_into("<I", m, 56, 0)
    if st.get("lo_applied") and st.get("lo_snap") and state == 0 and not struct.unpack_from("<I", m, 20)[0]:
        S.status["kh1_guard_arm"] = kh1_guard_arm(S, st["lo_snap"])   # (kh1_fx swapped in after the loadout was applied)
    S.status["kh1_guard"] = {"on": on, "state": state, "armed": struct.unpack_from("<I", m, 20)[0],
                             "fired": fired, "result": hex(result), "dll_alive": struct.unpack_from("<I", m, 60)[0]}

# HEARTLESS RANK (user 2026-10-12, "B"): KH1 enemies get STR / DEF at spawn from FUN_1402a58e0(ent, params):
# STR = rank_table[params[1]].byte0, DEF = rank_table[params[2]].byte1, rank table = 0x2D2F5E8 (2 B per rank 0..20,
# ranks above 20 are not stat rows). A 100% save spawns even first-visit Soldiers at rank 17 (35 / 27). Here every live
# Heartless gets the rank of its Borderlands enemy's level x 1.6 (free Heartless: the player's), KH1 level 1 -> rank 0
# .. level kh1_enemy_rank_top_lvl (60, about vanilla KH1's end) -> rank 20. Not saved by KH1 (stats re-init at spawn).
KH_ENEMY_RANKS = 0x2D2F5E8


def kh1_enemy_rank(S, kh1_level: float) -> int:
    top = float(S.CFG.get("kh1_enemy_rank_top_lvl", 60))
    return int(max(0, min(20, round((kh1_level - 1) * 20.0 / max(2.0, top - 1)))))


def kh1_heartless_rank_tick(S, pc) -> None:
    st = S.state
    table = kh1_mem(S, KH_ENEMY_RANKS, 42)
    ptr = int.from_bytes(kh1_mem(S, 0x296B630, 8) or bytes(8), "little")
    n = struct.unpack("<i", kh1_mem(S, 0x296B628, 4) or bytes(4))[0]
    pool = kh1_mem(S, KH_POOL, 96 * KH_STRIDE)
    if not (table and ptr and pool and 0 < n < 512):
        return
    recs = kh1_abs(S, ptr, n * 0x78) or b""
    kind = {struct.unpack_from("<I", recs, i * 0x78)[0]: recs[i * 0x78 + 0x54] for i in range(len(recs) // 0x78)}
    pmap, lv_cache, me = st.get("kh_pmap", {}), st.get("bl_lvl", {}), level(pc)
    done = st.setdefault("lo_rank_done", {})
    out = {}
    for i in range(96):
        o = i * KH_STRIDE
        if not struct.unpack_from("<I", pool, o + 0x374)[0] & 1 or kind.get(struct.unpack_from("<I", pool, o + 4)[0]) != 5:
            continue                                          # alive enemies only (record class 5)
        a = S.kh["base"] + KH_POOL + o
        stp = kh1_resolve(S, struct.unpack_from("<I", pool, o + 0x6C)[0])
        if not stp:
            continue
        bl_lv = lv_cache.get(pmap.get(a), me)
        r = kh1_enemy_rank(S, kh1_mapped_level(S, bl_lv))
        want = struct.pack("<2i", table[2 * r], table[2 * r + 1])
        if kh1_abs(S, stp + 0x4C, 8) != want:
            _lo_write(S, stp + 0x4C, want)
            done[a] = r
        out[hex(a)] = [bl_lv, r]
    S.status["kh1_heartless_rank"] = out


# Sora's abilities the mod's controls depend on (user 2026-10-12: C only stepped forward - the loadout's AP trim had
# unequipped Dodge Roll at low level and the prefs tick then kept it off): 0x16 Dodge Roll (C), 0x15 Guard (right
# click). Kept equipped whenever learned, not counted against AP.
KH_ABIL_ALWAYS_ON = (0x15, 0x16)


def _lo_abil_prefs(S) -> dict:
    """{"0": [ids equipped], "1": [...], "2": [...]} - which abilities the player switched on in the F9 menu while in
    BL (user 2026-10-12: the loadout taught them unequipped like KH1 does, and every re-apply switched them off again).
    Kept in kh1_ability_prefs.json next to logic.py; only applied to the boosted loadout, never to the restore."""
    if "lo_abil_prefs" not in S.state:
        try:
            S.state["lo_abil_prefs"] = json.loads((HERE / "kh1_ability_prefs.json").read_text())
        except Exception:  # noqa: BLE001
            S.state["lo_abil_prefs"] = {}
    return S.state["lo_abil_prefs"]


def kh1_ability_prefs_tick(S, base: int) -> None:
    """While the loadout is applied (and not mid-menu): remember each character's equipped abilities."""
    prefs, changed = _lo_abil_prefs(S), False
    for who in range(3):
        blk = kh1_abs(S, base + 0x74 * who, 0x74)
        if not blk:
            continue
        if who == 0:                                      # Dodge Roll / Guard switched off (menu, older build): back on
            fixed = False
            for j in range(0x44, 0x74):
                if blk[j] & 0x80 and blk[j] & 0x7F in KH_ABIL_ALWAYS_ON:
                    kh1_poke(S, base + j - S.kh["base"], bytes([blk[j] & 0x7F]))   # (base is absolute)
                    kh1_event(S, f"ability {blk[j] & 0x7F:#x} switched back on (the controls need it)")
                    fixed = True
            if fixed:                                     # KH1's abilities-in-effect word (0x2D5EC10, Dodge Roll 0x20)
                kh1_loadout_request(S, 0, True)           # only follows the save bytes on its party refresh
            blk = kh1_abs(S, base, 0x74) or blk
        on = sorted(a for a in blk[0x44:0x74] if a and not a & 0x80)
        if prefs.get(str(who)) != on:
            prefs[str(who)], changed = on, True
    if changed:
        try:
            (HERE / "kh1_ability_prefs.json").write_text(json.dumps(prefs))
        except Exception:  # noqa: BLE001
            pass
    S.status["kh1_ability_prefs"] = {k: [hex(a) for a in v] for k, v in prefs.items()}


def _lo_snap_file() -> Path:
    return _lo_docs() / "SoraInBorderlands" / "kh1_loadout_snapshot.json"


def _lo_snap_save(snap: dict) -> None:
    f = _lo_snap_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(snap))


def kh1_loadout_tick(S, pc, now: float) -> None:
    st, CFG = S.state, S.CFG
    try:
        kh1_save_backup(S)
    except Exception as ex:  # noqa: BLE001
        S.status["kh1_save_backup"] = f"ERR {type(ex).__name__}: {ex}"
    try:
        kh1_equip_queue_tick(S, now)
    except Exception as ex:  # noqa: BLE001
        S.status["kh1_equip_queue"] = f"ERR {type(ex).__name__}: {ex}"
    if not CFG.get("kh1_loadout", True):
        if st.get("lo_applied"):
            kh1_loadout_restore(S, "turned off")
        return
    if not kh1_combat_on(S) or now < st.get("lo_next", 0):
        return
    st["lo_next"] = now + 0.5
    base = int.from_bytes(kh1_mem(S, KH_CHAR_TABLE, 8) or bytes(8), "little")
    if not base:
        return
    snap = st.get("lo_snap")
    if snap and snap.get("pid") != S.kh.get("pid"):           # KH1 restarted: it reloaded the real save itself
        st["lo_snap"] = st["lo_applied"] = snap = None
    if not st.get("lo_file_checked"):                         # BL crashed / quit while boosted, same KH1 still running
        st["lo_file_checked"] = True
        try:
            old = json.loads(_lo_snap_file().read_text()) if _lo_snap_file().exists() else None
        except Exception:  # noqa: BLE001
            old = None
        if old and old.get("applied") and old.get("pid") == S.kh.get("pid") and old.get("base") == base:
            st["lo_snap"] = old
            kh1_loadout_restore(S, "left boosted by the last BL session")
    # KH1 window in front -> real save values back (a KH1 save point must never see the boosted loadout)
    fg = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(ctypes.windll.user32.GetForegroundWindow(), ctypes.byref(fg))
    if fg.value == S.kh.get("pid"):
        if st.get("lo_applied"):
            kh1_loadout_restore(S, "KH1 focused")
        return
    busy = st.get("kh_menu_open") or st.get("kh_intro_view") or (_lo_u32(S, 0x2867364) & 0x2000)         or now < st.get("kh_warping_until", 0)                 # (session 9: no loadout / equips during a room reload)
    if st.get("lo_applied") and not busy and not st.get("lo_eq_q") and not st.get("lo_eq_inflight"):
        try:
            kh1_ability_prefs_tick(S, base)
        except Exception as ex:  # noqa: BLE001
            S.status["kh1_ability_prefs"] = f"ERR {type(ex).__name__}: {ex}"
    if st.get("lo_applied") and not busy and S.CFG.get("kh1_room_set_pin_on", True) and kh1_proxies_on(S)             and now - st.get("lo_room_reload_t", 0) > 10.0 and now > st.get("kh_warping_until", 0):
        live_set = _lo_u32(S, 0x233FE90)
        for key, (sv, room) in S.CFG.get("kh1_room_set_pin", KH_ROOM_SET_PIN).items():
            if kh1_room(S) == room and live_set != sv:          # loaded before the pin: enter it again
                st["lo_room_reload_t"] = now
                if kh1_go_home(S, True):
                    st["kh_anchor_room"] = None
                    S.status["kh1_room_set_reload"] = {"room": room, "was": live_set, "pin": sv, "t": time.strftime("%H:%M:%S")}
    tgt = kh1_loadout_target(S, int(CFG.get("dbg_loadout_bl") or level(pc)))   # dbg_loadout_bl: test a BL level (never ship it set)
    if CFG.get("dbg_loadout_reapply") and CFG.get("dbg_loadout_reapply") == CFG.get("nonce") and st.get("lo_dbg") != CFG.get("nonce"):
        st["lo_dbg"], st["lo_applied"] = CFG.get("nonce"), None    # test hook: re-apply once (config edit)
    if st.get("lo_applied") and S.CFG.get("kh1_enemy_rank_on", True):
        try:
            kh1_heartless_rank_tick(S, pc)
        except Exception as ex:  # noqa: BLE001
            S.status["kh1_heartless_rank"] = f"ERR {type(ex).__name__}: {ex}"
    if busy or (st.get("lo_applied") or {}).get("bl") == tgt["bl"]:
        return
    if st.get("lo_snap") is None:                             # originals, taken right before the first change
        st["lo_snap"] = kh1_loadout_snapshot(S, base)
    st["lo_snap"]["applied"] = True
    _lo_snap_save(st["lo_snap"])
    S.status["kh1_loadout"] = kh1_loadout_apply(S, base, tgt)
    st["lo_applied"] = tgt
    try:
        S.status["kh1_guard_arm"] = kh1_guard_arm(S, st["lo_snap"])
    except Exception as ex:  # noqa: BLE001
        S.status["kh1_guard_arm"] = f"ERR {type(ex).__name__}: {ex}"
    st["lvl_bl"] = level(pc)                                  # the relative level link stays quiet (loadout owns levels)


def _snd_map(S):
    import ctypes.wintypes as W
    m = getattr(S, "snd_map", None)
    if m is None and time.time() > S.state.get("snd_map_retry", 0):
        S.state["snd_map_retry"] = time.time() + 3.0
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenFileMappingW.restype = W.HANDLE
        k32.MapViewOfFile.restype = ctypes.c_void_p
        k32.MapViewOfFile.argtypes = [W.HANDLE, W.DWORD, W.DWORD, W.DWORD, ctypes.c_size_t]
        h = k32.OpenFileMappingW(0x0006, False, r"Local\SoraSnd")
        pm = h and k32.MapViewOfFile(h, 0x0006, 0, 0, 16 + 64 * 16)
        if pm:
            m = S.snd_map = (ctypes.c_char * (16 + 64 * 16)).from_address(pm)
    return m


def kh1_music_tick(S, pc, now: float) -> None:
    st, CFG = S.state, S.CFG
    on = kh1_music_mode(S) or bool(st.get("kh_intro_view"))   # (kh1_combat_on drops while Sora is absent in the intro)
    m = _snd_map(S)
    if m is not None:
        # kh1_fx: KH1 SE / voices silent at the source - except during a summon intro shown on KH1's screen: then KH1
        # plays its own summon sounds / voices (user 2026-10-06: "totally silent when summoning")
        # summon intro: KH1's SE mute OFF. With it on, kh1_fx refuses to (re)start effect entries and KH1's scripted
        # intro waits forever for one of its sounds (Simba froze on the posed shot, event flag 0x23AB2D0 bit 4 stuck,
        # 2026-10-06 18:35 - every intro with the mute off went through). Borderlands still relays the summon SE.
        # F9 menu: KH1's mute OFF too -> KH1 plays its OWN menu sounds (cursor = se000 no 1, confirm / back / equip);
        # the mute silenced them at the source and the BL relay has no clips for se000 1..15 (user 2026-10-06: "no menu
        # sounds like in Kingdom Hearts"). KH1's field is paused under its menu, so nothing else starts meanwhile.
        # (dbg_se_unmute_until: a self-expiring test window, set through dbg_exec)
        menu_snd = CFG.get("kh1_menu_se", True) and (st.get("kh_menu_open") or now < st.get("dbg_se_unmute_until", 0))
        struct.pack_into("<I", m, 4, 1 if on and not st.get("kh_intro_view") and not menu_snd else 0)
    if on and pc is not None and now - st.get("bl_music_t", 0) > 3.0:
        st["bl_music_t"], st["bl_music_off"] = now, True      # (re-applied: level loads reset it)
        S.pending.append(lambda pc=pc: pc.SetAudioGroupVolume("Music", 0.0))
    elif not on and st.get("bl_music_off") and pc is not None:
        st["bl_music_off"] = False
        S.pending.append(lambda pc=pc: pc.SetMusicVolumeFromProfile())
    S.status["kh1_music"] = {"on": on, "se_mute_ok": m is not None, "bl_music_off": bool(st.get("bl_music_off"))}
    try:
        kh1_volume_tick(S, pc, now, on)
    except Exception as ex:  # noqa: BLE001
        S.status["kh1_volume"] = f"ERR {type(ex).__name__}: {ex}"



BL_VOL_IDS = {"master": 101, "music": 107, "sfx": 108}   # WillowPlayerController.WPS_* profile setting ids (ints 0..10+)


def kh1_volume_tick(S, pc, now: float, music_on: bool) -> None:
    """J9 (Jet: "KH1 window too loud"): KH1's audio follows Borderlands' volume sliders. KH1's Windows per-app volume =
    master x louder of (music, sfx) (renderer v79 sr_vol_kh1), and KH1's music inside that (kh1_fx bgm volume, KH1's
    default 0.75) scaled so music ends at master x music. Config kh1_follow_bl_volume (off = KH1 at 100 %, music 0.75),
    kh1_volume_scale (extra trim for KH1, 1.0)."""
    st, CFG = S.state, S.CFG
    if pc is not None and now - st.get("blvol_t", 0) > 2.0:          # profile read on the game thread
        st["blvol_t"] = now

        def _rd(pc=pc):
            try:
                prof = pc.OnlinePlayerData.ProfileProvider.Profile
                st["blvol"] = {k: float(prof.GetProfileSettingValueInt(i, 0)[1]) for k, i in BL_VOL_IDS.items()}
            except Exception as ex:  # noqa: BLE001
                st["blvol_err"] = str(ex)[:120]
        S.pending.append(_rd)
    v = st.get("blvol")
    follow = CFG.get("kh1_follow_bl_volume", True)
    if follow and v:
        master = min(1.0, v["master"] / 10.0)
        mus, sfx = min(1.0, v["music"] / 10.0), min(1.0, v["sfx"] / 10.0)
        # session 9 (user: KH1 music "gets quieter and louder randomly"): forcing every KH1 BGM stream to one level
        # every 10 s snapped KH1's faded-out field / battle track back up mid-crossfade. KH1's own SE are muted at the
        # source (Borderlands plays them), so KH1's process audio ~= its music: the WINDOWS volume carries master x
        # music and KH1 keeps doing its own fades (kh1_bgm_force True = the old per-stream write)
        if CFG.get("kh1_bgm_force", False):
            top = max(mus, sfx)
            sess = master * top * CFG.get("kh1_volume_scale", 1.0)
            bgm = 0.75 * (mus / top if top > 0 else 0.0)
        else:
            sess, bgm = master * mus * CFG.get("kh1_music_level", 0.75) * CFG.get("kh1_volume_scale", 1.0), None   # 0.75 = the old level
    else:
        sess, bgm = 1.0, 0.75
    want = (round(sess, 3), round(bgm, 3) if bgm is not None else None)
    if want != st.get("kh1_vol_set") or now - st.get("kh1_vol_t", 0) > 10.0:   # (re-applied: KH1 resets music on track change)
        st["kh1_vol_t"] = now
        ok = None
        if hasattr(S.dll, "sr_vol_kh1"):
            S.dll.sr_vol_kh1.argtypes = [ctypes.c_float]
            ok = S.dll.sr_vol_kh1(ctypes.c_float(sess))
        sh = khfx_shared(S)
        if sh is not None and music_on and bgm is not None:
            seq = struct.unpack_from("<I", sh, 244)[0]
            struct.pack_into("<f", sh, 240, max(0.0001, bgm))
            struct.pack_into("<I", sh, 244, seq + 1)
        if ok is not None and ok >= 0:
            st["kh1_vol_set"] = want
        S.status["kh1_volume"] = {"bl": v, "session": want[0], "bgm": want[1], "sessions": ok, "err": st.get("blvol_err")}


def kh_menu_native(S) -> bool:
    """v55: F9 shows KH1's menu redrawn natively (its 2D draws via kh1_fx, dimming the frozen Borderlands world) instead
    of the grabbed KH1 window (config kh_menu_native)."""
    return bool(S.CFG.get("kh_menu_native", True)) and hasattr(S.dll, "sr_khui_menu") and bool(S.CFG.get("kh1_ui_native", True))


def kh_menu_pad(S):
    """The pad KH1 gets while its menu is open: d-pad from arrows / WASD, Cross = Enter, Circle = Backspace,
    Triangle = Q, L1 / R1 = Z / C (page tabs)."""
    b = 0
    if _bl_focused():
        for vks, bit in (((0x26, 0x57), PAD_UP), ((0x28, 0x53), PAD_DOWN), ((0x25, 0x41), PAD_LEFT), ((0x27, 0x44), PAD_RIGHT),
                         ((0x0D,), PAD_CROSS), ((0x08,), PAD_CIRCLE), ((0x51,), PAD_TRIANGLE), ((0x5A,), PAD_L1), ((0x43,), 0x800)):
            if any(_key(v) for v in vks):
                b |= bit
    return b


HOT_RESET_KEYS = ("skin_models", "skin_argtypes", "kh_injected", "shadow_sent", "light_sent", "outline_sent")


def render_hot_swap(S) -> None:
    """Renderer hot swap (v29+, idea from justbustin/minecraft-crossover-bridge's swappable core): drop a new build at
    data/sora_render_next.dll and it replaces the running renderer within a tick, no Borderlands restart. Each build is
    loaded from its own copy in data/hot/ (a loaded DLL file is locked); sora_render.dll is refreshed for the next
    game start once the original is no longer loaded."""
    data = Path(S.HERE) / "data"
    nxt = data / "sora_render_next.dll"
    if S.dll is None or not nxt.exists():
        return
    if not hasattr(S.dll, "sr_shutdown"):
        S.status["hot_swap"] = "the running renderer predates hot swap: restart Borderlands once"
        return
    import shutil
    hot = data / "hot"
    hot.mkdir(exist_ok=True)
    dst = hot / f"sora_render_{int(time.time() * 1000)}.dll"
    try:
        shutil.copy2(nxt, dst)
        nxt.unlink()
    except OSError as ex:                                  # still being written: next tick
        S.status["hot_swap"] = f"waiting for the new build ({ex})"
        return
    t0 = time.time()
    old, S.dll = S.dll, None
    clean = old.sr_shutdown()
    k32 = ctypes.windll.kernel32
    k32.FreeLibrary.argtypes = [ctypes.c_void_p]
    k32.FreeLibrary(ctypes.c_void_p(old._handle))
    try:
        shutil.copy2(dst, data / "sora_render.dll")       # next game start (fails while the original is still loaded)
    except OSError:
        pass
    dll = ctypes.CDLL(str(dst))
    dll.sr_init.argtypes = [ctypes.c_char_p]
    dll.sr_set_frame.argtypes = [ctypes.POINTER(type(S.frame))]
    dll.sr_motion_duration.restype = ctypes.c_float
    ok = dll.sr_init(str(data).encode("mbcs"))
    S.dll = dll
    for k in HOT_RESET_KEYS:
        S.state.pop(k, None)
    S.sfx_ids = None
    for old_dll in sorted(hot.glob("sora_render_*.dll"))[:-3]:   # keep the last few builds
        try:
            old_dll.unlink()
        except OSError:
            pass
    S.status["hot_swap"] = {"build": dst.name, "init": ok, "clean_shutdown": clean, "ms": round((time.time() - t0) * 1000)}
    kh1_event(S, f"renderer hot-swapped -> {dst.name} (init {ok}, clean {clean})")


# J2 (Jet's video, 2026-10-12): BL's own melee (V) and action skill (F) still fired while playing Sora (F also crossed
# Mushu's controls). Found by probing (F -> SkillOnPressCallback + ServerStartSimpleSkill, V -> MeleeAttack +
# ServerMeleeAttack). Blocked while Sora's renderer is up; config bl_block_melee / bl_block_skill. Registered from here
# (re-registered on every logic reload, same identifier) so no BL restart is needed.
BL_MELEE_FUNCS = ("WillowGame.WillowPlayerController:MeleeAttack", "WillowGame.WillowPlayerController:ServerMeleeAttack")
BL_SKILL_PRESS = "WillowGame.WillowPlayerController:SkillOnPressCallback"
# BL1 routes jump, melee AND the action skill through SkillOnPressCallback / ServerStartSimpleSkill ("simple skills").
# Probed (user 2026-10-12): F = Button 0 (skill 36), V = Button 8 (skill 14 -> MeleeAttack), jump = Button 5 (skills 13
# + 10). Blocking the whole callback killed jump + Keyblade swings -> only Button 0 / 8 presses are blocked here
# (config bl_block_buttons), plus MeleeAttack itself.
BL_UNBLOCK_LEGACY = ("WillowGame.WillowPlayerController:ServerStartSimpleSkill",)
# Grenade (G) too (user 2026-10-12: "while the mod is active block the grenade key G"); config bl_block_grenade.
BL_GRENADE_FUNCS = ("WillowGame.WillowPlayerController:ThrowGrenade",
                    "WillowGame.WillowPlayerController:ServerThrowGrenade")


# J7 roll hooks (re-registered on every logic reload, like bl_input_block_hooks): while Sora mode is on, Borderlands'
# crouch is blocked (C = Dodge Roll only; the old fix - pawn.bCanCrouch False - stuck on the pawn after the mod was
# turned off), and the player takes no Borderlands damage during the roll's invincibility window.
BL_CROUCH_FUNC = "Engine.Pawn:ShouldCrouch"
BL_PLAYER_DAMAGE_FUNC = "WillowGame.WillowPlayerPawn:TakeDamage"


def bl_roll_hooks(S) -> None:
    from unrealsdk import hooks as H  # type: ignore
    if S.state.get("bl_roll_hooks") == id(bl_roll_hooks):
        return
    def count(kind):
        b = S.status.setdefault("bl_blocked", {})
        b[kind] = b.get(kind, 0) + 1
    def crouch(obj, args, ret, func):
        st = S.state
        if S.dll is None or st.get("in_vehicle") or not st.get("kh_on") or st.get("kh_own_crouch")                 or not S.CFG.get("kh_block_bl_crouch", True):
            return None
        try:
            if not bool(args.bCrouch) or obj.Controller is None or "Player" not in str(obj.Controller.Class.Name):
                return None
        except Exception:  # noqa: BLE001
            return None
        count("crouch")
        return H.Block
    def damage(obj, args, ret, func):
        if S.dll is None or time.time() >= S.state.get("kh_iframe_until", 0):
            return None
        count("roll_iframe_hit")
        return H.Block
    H.remove_hook(BL_CROUCH_FUNC, H.Type.PRE, "sora_roll")
    H.remove_hook(BL_PLAYER_DAMAGE_FUNC, H.Type.PRE, "sora_roll")
    H.add_hook(BL_CROUCH_FUNC, H.Type.PRE, "sora_roll", crouch)
    H.add_hook(BL_PLAYER_DAMAGE_FUNC, H.Type.PRE, "sora_roll", damage)
    S.state["bl_roll_hooks"] = id(bl_roll_hooks)


def bl_input_block_hooks(S) -> None:
    from unrealsdk import hooks as H  # type: ignore
    if S.state.get("bl_block_hooks") == id(bl_input_block_hooks):
        return
    def count(kind):
        b = S.status.setdefault("bl_blocked", {})
        b[kind] = b.get(kind, 0) + 1
    def melee(obj, args, ret, func):
        if S.dll is None or S.state.get("in_vehicle") or not S.CFG.get("bl_block_melee", True):
            return None
        count("melee")
        return H.Block
    def grenade(obj, args, ret, func):
        if S.dll is None or S.state.get("in_vehicle") or not S.CFG.get("bl_block_grenade", True):
            return None
        count("grenade")
        return H.Block
    def press(obj, args, ret, func):
        if S.dll is None or S.state.get("in_vehicle"):
            return None
        try:
            btn = int(args.Button)
        except Exception:  # noqa: BLE001
            return None
        want = S.CFG.get("bl_block_buttons", {"0": "skill", "8": "melee"})
        kind = want.get(str(btn))
        if kind and S.CFG.get(f"bl_block_{kind}", True):
            count(f"{kind}_btn{btn}")
            return H.Block
        return None
    for f in BL_UNBLOCK_LEGACY + BL_MELEE_FUNCS + BL_GRENADE_FUNCS + (BL_SKILL_PRESS,):
        H.remove_hook(f, H.Type.PRE, "sora_block_bl_input")
    for f in BL_GRENADE_FUNCS:
        H.add_hook(f, H.Type.PRE, "sora_block_bl_input", grenade)
    for f in BL_MELEE_FUNCS:
        H.add_hook(f, H.Type.PRE, "sora_block_bl_input", melee)
    H.add_hook(BL_SKILL_PRESS, H.Type.PRE, "sora_block_bl_input", press)
    S.state["bl_block_hooks"] = id(bl_input_block_hooks)


# J1 VEHICLES (Jet's video 18:15): in a Catch-A-Ride car pc.Pawn is the VEHICLE (or a WillowVehicleWeaponPawn for the
# gunner seat), so the normal tick hid the car's mesh as if it were the player, drew Sora at the car's origin and kept
# sending the move keys to KH1. Now, while in a vehicle: the car is shown again, Borderlands' own vehicle camera / input
# drive (no forced "camera 3rd", F / V not blocked, no KH1 pad -> the bridge hands KH1 its own idle pad after 0.5 s),
# Sora is drawn on the driver's seat facing the car's way (config vehicle_sora "seat" | "hidden", vehicle_sora_dz UU),
# Donald / Goofy are hidden (KH1 doesn't follow a car). Leaving the car = the normal tick again (new pawn -> camera 3rd).
def bl_in_vehicle(pawn) -> bool:
    try:
        return "Vehicle" in str(pawn.Class.Name)
    except Exception:  # noqa: BLE001
        return False


def bl_vehicle_tick(S, pc, pawn, now: float) -> None:
    st, frame, CFG = S.state, S.frame, S.CFG
    car = getattr(pawn, "MyVehicle", None) or pawn            # gunner seat: the weapon pawn's vehicle
    if not st.get("in_vehicle"):
        st["in_vehicle"] = True
        st["lock_on"] = False
        st.pop("lock", None)
        kh1_event(S, f"entered a vehicle ({pawn.Class.Name}) -> Borderlands drives, Sora rides")
    for a in {id(pawn): pawn, id(car): car}.values():        # the normal tick hid it on the first frame
        try:
            if a.Mesh is not None and a.Mesh.HiddenGame:
                a.Mesh.SetHidden(False)
        except Exception:  # noqa: BLE001
            pass
    res = pc.GetPlayerViewPoint(unrealsdk.make_struct("Vector"), unrealsdk.make_struct("Rotator"))
    loc, rot = [x for x in res if x is not Ellipsis][:2]
    frame.cam_pos[:] = (float(loc.X), float(loc.Y), float(loc.Z))
    frame.cam_rot[:] = (int(rot.Pitch) * UU, int(rot.Yaw) * UU, int(rot.Roll) * UU)
    frame.fov = float(pc.GetFOVAngle())
    drv = getattr(pawn, "Driver", None)
    seat = drv if drv is not None else pawn
    try:
        half = float(seat.CylinderComponent.CollisionHeight) if drv is not None else 0.0
    except Exception:  # noqa: BLE001
        half = 0.0
    p = seat.Location
    frame.pos[:] = (float(p.X), float(p.Y), float(p.Z) - half + float(CFG.get("vehicle_sora_dz", 0.0)))
    st["yaw"] = int(car.Rotation.Yaw) * UU
    frame.yaw = st["yaw"]
    if hasattr(frame, "bank"):
        frame.bank = 0.0
    frame.visible = 1                                         # (scale 0 = hidden: visible 0 stalls KH1 detection)
    if CFG.get("vehicle_sora", "seat") == "hidden":
        frame.scale = 0.0
    if hasattr(S.dll, "sr_set_actor"):
        for aid in (0, 1):
            S.dll.sr_set_actor(aid, ctypes.byref(ActorFrame()))
    st["kh1_gone_hid"] = False                                # the normal tick re-sends the party after the car
    S.status["vehicle"] = {"class": str(pawn.Class.Name), "driver": drv is not None, "t": round(now, 1)}


def tick(S, pc) -> None:
    """Wrapper: keeps the traceback of a tick error in status.json (error_trace)."""
    _guard_jobs(S)
    try:
        kh1_save_backup(S)                    # once per BL start, before anything connects to KH1 (no-op after)
    except Exception as ex:  # noqa: BLE001
        S.status["kh1_save_backup"] = f"ERR {type(ex).__name__}: {ex}"
    try:
        kh1_guard_tick(S)                     # heartbeat first: kh1_fx restores + pauses KH1 when ticks stop
    except Exception as ex:  # noqa: BLE001
        S.status["kh1_guard"] = f"ERR {type(ex).__name__}: {ex}"
    try:
        bl_input_block_hooks(S)
        bl_roll_hooks(S)
        _tick(S, pc)
        S.status.pop("error", None)
    except Exception:
        import traceback
        S.status["error_trace"] = traceback.format_exc()[-1200:]
        raise
    try:
        kh1_autoload(S)                       # after _tick: its title-screen presses are the frame's last pad write
    except Exception as ex:  # noqa: BLE001
        S.status["kh1_autoload"] = f"ERR {type(ex).__name__}: {ex}"
    try:
        melty_test_tick(S, pc)
    except Exception as ex:  # noqa: BLE001
        S.status["melty_test"] = f"ERR {type(ex).__name__}: {ex}"
    try:
        dbg_exec_tick(S, pc)                  # also here: _tick never gets that far in BL's front end
    except Exception as ex:  # noqa: BLE001
        S.status["dbg_exec"] = f"ERR {type(ex).__name__}: {ex}"


def _has_uobject(v, depth: int = 0) -> bool:
    from unrealsdk.unreal import UObject  # type: ignore
    if isinstance(v, UObject):
        return True
    if depth > 3:
        return False
    if isinstance(v, dict):
        return any(_has_uobject(k, depth + 1) or _has_uobject(x, depth + 1) for k, x in v.items())
    if isinstance(v, (list, tuple, set)):
        return any(_has_uobject(x, depth + 1) for x in v)
    return False


def _drop_uobjects(S) -> None:
    """Level change: every S.state entry that still holds a Borderlands object (pawns, projectiles, targets...) goes -
    the old map's actors are destroyed and touching them crashes inside pyunrealsdk (crash logger 2026-10-12)."""
    gone = [k for k, v in list(S.state.items()) if _has_uobject(v)]
    for k in gone:
        S.state.pop(k, None)
    _hud.clear()                                           # cached font / texture objects get looked up again
    if gone:
        S.status["world_dropped"] = gone[:20]


def _tick(S, pc) -> None:
    """S: the mod's shared namespace (dll, frame, state, CFG, status)."""
    try:
        render_hot_swap(S)
    except Exception as ex:  # noqa: BLE001
        S.status["hot_swap_error"] = f"{type(ex).__name__}: {ex}"
    frame, state, CFG = S.frame, S.state, S.CFG
    pawn = pc.Pawn if pc is not None else None
    dbg_clean_old(S)
    # Level change / no character (quitting to the menu, loading, dying): drop every queued game-tick job and every
    # remembered actor. Jobs holding the old pawn ran against a destroyed object in the menu -> General protection fault.
    try:
        world = str(pc.WorldInfo) if pc is not None else ""
    except Exception:  # noqa: BLE001
        world = ""
    if pawn is None or world != state.get("world_key"):
        S.pending.clear()
        _enemy_cache["list"], _enemy_cache["t"] = [], 0.0
        for k in ("lock", "aero_pawn", "kh_crouch_pawn"):
            state.pop(k, None)
        state["lock_on"] = False
        state["kh_pmap"], state["stopped"], state["proj"] = {}, [], []
        state["skin_hidden"], state["skin_last"], state["skin_of"], state["skin_slots"] = {}, {}, {}, 0
        _drop_uobjects(S)                                   # anything else still holding the old map's actors
        state.pop("kh_hp_seen", None)                       # HP sync starts fresh (no "heal" from the new save's max HP)
        state["world_key"] = world if pawn is not None else ""
    if pawn is None:
        frame.visible = 0
        return
    now = time.time()
    if CFG.get("vehicle_mode", True) and bl_in_vehicle(pawn):
        bl_vehicle_tick(S, pc, pawn, now)
        return
    if state.get("in_vehicle"):
        state["in_vehicle"] = False
        state["cam"] = False                                  # back on foot: the mod's 3rd-person camera again
        kh1_event(S, "left the vehicle")
    dt = min(0.1, now - state["last"])
    state["last"] = now
    if state.get("cam_pawn") is not pawn:          # respawned: new pawn starts in first person
        state["cam_pawn"], state["cam"] = pawn, False
    downed = pool_values(pawn)[0] <= 0              # Fight For Your Life: BL forces first person
    if state.get("was_downed") and not downed:
        state["cam"] = False
    state["was_downed"] = downed
    # REVIVE CAMERA (user 2026-10-12): after Fight For Your Life (Donald / Goofy second wind, Cure) Borderlands sometimes
    # went back to first person a moment AFTER our one "camera 3rd" -> stuck over Sora's shoulder with an invisible gun.
    # Watchdog: first person (bBehindView False) for kh_cam_fix_s while standing -> third person again.
    try:
        behind = bool(pc.bBehindView)
    except Exception:  # noqa: BLE001
        behind = True
    if state["cam"] and not downed and not behind:
        if not state.get("cam_fp_t"):
            state["cam_fp_t"] = now
        elif now - state["cam_fp_t"] > CFG.get("kh_cam_fix_s", 0.3):
            state["cam"], state["cam_fp_t"] = False, 0.0
            state["cam_fixes"] = state.get("cam_fixes", 0) + 1
            S.status["cam_fixes"] = state["cam_fixes"]
    else:
        state["cam_fp_t"] = 0.0
    if not state["cam"] and not downed:
        pc.ConsoleCommand("camera 3rd", False)
        state["cam"] = True
    mesh = pawn.Mesh
    if mesh is not None and not mesh.HiddenGame:
        mesh.SetHidden(True)
    try:
        if not pawn.bHideAttachedWeapon:
            pawn.bHideAttachedWeapon = True
        w = pawn.Weapon
        if w is not None:
            for comp in (w.ThirdPersonMesh, w.FirstPersonMesh, w.ThirdPersonMuzzleFlash):
                if comp is not None and not comp.HiddenGame:
                    comp.SetHidden(True)
    except Exception as ex:  # noqa: BLE001
        S.status["weapon_error"] = str(ex)
    res = pc.GetPlayerViewPoint(unrealsdk.make_struct("Vector"), unrealsdk.make_struct("Rotator"))
    loc, rot = [x for x in res if x is not Ellipsis][:2]  # void fn -> (..., out, out)
    frame.cam_pos[:] = (float(loc.X), float(loc.Y), float(loc.Z))
    frame.cam_rot[:] = (int(rot.Pitch) * UU, int(rot.Yaw) * UU, int(rot.Roll) * UU)
    frame.fov = float(pc.GetFOVAngle())
    lock_reproject(S, frame)
    half = float(pawn.CylinderComponent.CollisionHeight)
    try:
        crouched = bool(pawn.bIsCrouched)
    except Exception:  # noqa: BLE001
        crouched = False
    if not crouched:
        state["stand_half"] = half
    size_half = state.get("stand_half", half)          # Sora's scale uses the standing height
    p = pawn.Location
    frame.pos[:] = (float(p.X), float(p.Y), float(p.Z) - half)
    frame.scale = 2.0 * size_half * CFG["scale_fudge"] / 155.0
    v = pawn.Velocity
    speed = math.hypot(float(v.X), float(v.Y))
    # KH style: face the way you move; standing still keeps the last facing
    if state.get("kh_move_on"):
        want = state["yaw"]                           # KH1 drives facing (kh1_move_drive)
    elif state.get("action"):
        want = state.get("face_want", state["yaw"])   # turn toward the target smoothly (no snap per swing)
    elif speed > 30:
        want = math.atan2(float(v.Y), float(v.X))
    else:
        want = state["yaw"] if state["yaw"] is not None else int(pawn.Rotation.Yaw) * UU
    if state["yaw"] is None:
        state["yaw"] = want
    state["yaw"] = ang(state["yaw"] + ang(want - state["yaw"]) * min(1.0, CFG["turn_speed"] * dt))
    frame.yaw = state["yaw"]
    if hasattr(frame, "bank"):
        # gliding: lean into turns (KH1's banking turn animations were swapped for a straight glide - no pose freezes)
        gliding = bool(state.get("kh_glide_on")) and ks_anim(S) in KH_GLIDE_ANIMS if state.get("kh_move_on") else False
        rate = ang(state["yaw"] - state.get("bank_yaw", state["yaw"])) / max(dt, 1e-3)
        state["bank_yaw"] = state["yaw"]
        want = max(-0.6, min(0.6, rate * CFG.get("glide_bank", 0.3))) if gliding else 0.0
        state["bank"] = state.get("bank", 0.0) + (want - state.get("bank", 0.0)) * min(1.0, CFG.get("glide_bank_ease", 3.5) * dt)
        frame.bank = -state["bank"] * CFG.get("glide_bank_sign", 1.0)   # renderer: + rolls his head to the left
        tnow = time.time()
        gw = state.get("glide_w", 0.0)                       # 0..1: how much the glide "riding the air" motion applies
        gw += ((1.0 if gliding else 0.0) - gw) * min(1.0, 3.0 * dt)
        state["glide_w"] = gw
        if gw > 0.01:
            # drifting on the air: a slow sway around his forward axis and a gentle float up and down
            frame.bank += gw * CFG.get("glide_sway", 0.0) * math.sin(tnow * 1.4)
            frame.pos[2] += gw * CFG.get("glide_float", 0.0) * math.sin(tnow * 2.1)
        if gliding:
            if not state.get("glide_snd_on"):
                voice(S, "vatk", chance=CFG.get("glide_grunt", 0.0), cd=0.5, vol=0.6)  # (invented, off)
            if tnow - state.get("glide_wind_t", 0) > CFG.get("glide_wind_every", 0.6):
                state["glide_wind_t"] = tnow                 # air rushing past: overlapping copies of a soft wind loop
                sfx(S, "wind", CFG.get("glide_wind_vol", 0.18), pitch_jitter=0.05, pitch=CFG.get("glide_wind_pitch", 1.0))
        state["glide_snd_on"] = gliding
    frame.yaw_offset = math.radians(CFG["yaw_offset_deg"])

    menu_state(S)
    if CFG.get("dbg_trace") and state.get("dbg_tn") != CFG.get("nonce"):
        state["dbg_tn"] = CFG.get("nonce")
        base = vec3(pawn.Location)
        def V(a):
            return unrealsdk.make_struct("Vector", X=a[0], Y=a[1], Z=a[2])
        up = [base[0], base[1], base[2] + 150]
        r = {}
        for name, a, b in (("up150", up, [up[0] + 50, up[1], up[2]]), ("down_into_ground", base, [base[0], base[1], base[2] - 500]),
                           ("from_center_out", base, [base[0] + 200, base[1], base[2]])):
            try:
                res = pawn.Trace(V([0, 0, 0]), V([0, 0, 0]), V(b), V(a), False)
                r[name] = str([x for x in (res if isinstance(res, tuple) else (res,))])[:200]
            except Exception as ex:  # noqa: BLE001
                r[name] = "ERR " + str(ex)
        S.status["trace_test"] = r
    if CFG.get("dbg_trace2") and state.get("dbg_tn2") != CFG.get("nonce"):
        state["dbg_tn2"] = CFG.get("nonce")
        base = vec3(pawn.Location)

        def V(a):
            return unrealsdk.make_struct("Vector", X=a[0], Y=a[1], Z=a[2])
        top, bot = [base[0], base[1], base[2] + 20], [base[0], base[1], base[2] - 600]
        r = {}
        tests = {
            "pawn_kw": lambda: pawn.Trace(V([0, 0, 0]), V([0, 0, 0]), TraceEnd=V(bot), TraceStart=V(top), bTraceActors=False),
            "pawn_sky": lambda: pawn.Trace(V([0, 0, 0]), V([0, 0, 0]), TraceEnd=V([base[0], base[1], base[2] + 5000]), TraceStart=V(top), bTraceActors=False),
            "pawn_far_down": lambda: pawn.Trace(V([0, 0, 0]), V([0, 0, 0]), TraceEnd=V([base[0] + 300, base[1], base[2] - 2000]), TraceStart=V([base[0] + 300, base[1], base[2] + 300]), bTraceActors=False),
            "world_pos": lambda: pc.WorldInfo.Trace(V([0, 0, 0]), V([0, 0, 0]), V(bot), V(top), False),
            "pawn_kw_actors": lambda: pawn.Trace(TraceEnd=V(bot), TraceStart=V(top), bTraceActors=True),
            "pc_kw": lambda: pc.Trace(TraceEnd=V(bot), TraceStart=V(top), bTraceActors=False),
            "world_kw": lambda: pc.WorldInfo.Trace(TraceEnd=V(bot), TraceStart=V(top), bTraceActors=False),
            "fast_down": lambda: pc.WorldInfo.FastTrace(V(bot), V(top)),
            "fast_up_sky": lambda: pc.WorldInfo.FastTrace(V([base[0], base[1], base[2] + 5000]), V(top)),
            "pawn_extent": lambda: pawn.Trace(TraceEnd=V(bot), TraceStart=V(top), bTraceActors=False, Extent=V([1, 1, 1])),
        }
        for name, fn in tests.items():
            try:
                res = fn()
                parts = res if isinstance(res, tuple) else (res,)
                r[name] = [str(x)[:120] if not hasattr(x, "X") else [round(x.X), round(x.Y), round(x.Z)] for x in parts]
            except Exception as ex:  # noqa: BLE001
                r[name] = "ERR " + str(ex)[:160]
        r["pawn_z"] = round(base[2])
        S.status["trace_test2"] = r
    if CFG.get("dbg_hit") and state.get("dbg_hn") != CFG.get("nonce"):
        # one test hit on the nearest enemy (enemies() is the same safe lookup used every frame)
        state["dbg_hn"] = CFG.get("nonce")
        near = enemies(S, pc, 3000)
        try:
            e0 = near[0][1] if near else None
            S.status["pool"] = [str(e0.HealthPool)[:200], str(type(e0.HealthPool.Data))] if e0 else None
        except Exception as ex:  # noqa: BLE001
            S.status["pool"] = "ERR " + str(ex)
        S.status["dbg_hit"] = {"found": len(near), "mode": CFG.get("dmg_mode"),
                               "dist": round(near[0][0]) if near else None, "before": hp(near[0][1]) if near else None}
        if near:
            hurt(S, pc, near[0][1], 10, push=0)
    if CFG.get("dbg_hud_probe") and state.get("dbg_hp_nonce") != CFG.get("nonce"):   # test hook: Borderlands HUD movie
        state["dbg_hp_nonce"] = CFG.get("nonce")
        lines = []
        try:
            hud = pc.myHUD
            lines.append(f"myHUD {hud.Class.Name}")
            for name in ("HUDMovie", "HudMovie", "WillowHUDMovie"):
                try:
                    mv = getattr(hud, name)
                    lines.append(f"{name} = {mv} class {mv.Class.Name if mv else None}")
                except Exception as ex:  # noqa: BLE001
                    lines.append(f"{name}: {ex}")
            cls = hud.Class
            while cls:
                for f in cls._fields():
                    lines.append(f"HUD {cls.Name}.{f.Name} : {f.Class.Name}")
                cls = cls.SuperField
                if cls is not None and cls.Name in ("Actor", "Object"):
                    break
        except Exception as ex:  # noqa: BLE001
            lines.append(f"error {ex}")
        open(HERE / "hud_probe.txt", "w").write(chr(10).join(lines))
    if CFG.get("dbg_hud_probe2") and state.get("dbg_hp2_nonce") != CFG.get("nonce"):  # test hook: HUD movie fields
        state["dbg_hp2_nonce"] = CFG.get("nonce")
        lines = []
        try:
            mv = pc.myHUD.HUDMovie
            cls = mv.Class
            while cls is not None:
                for f in cls._fields():
                    val = ""
                    if f.Class.Name.endswith("Property"):
                        try:
                            val = " = " + str(getattr(mv, f.Name))[:120]
                        except Exception as ex:  # noqa: BLE001
                            val = " ! " + str(ex)[:60]
                    lines.append(f"MV {cls.Name}.{f.Name} : {f.Class.Name}{val}")
                cls = cls.SuperField
                if cls is not None and cls.Name == "Object":
                    break
        except Exception as ex:  # noqa: BLE001
            lines.append(f"error {ex}")
        open(HERE / "hud_probe2.txt", "w").write(chr(10).join(lines))
    if CFG.get("dbg_gfx_log") and state.get("dbg_gfx_nonce") != CFG.get("nonce"):    # test hook: log HUD GFx calls 3 s
        state["dbg_gfx_nonce"] = CFG.get("nonce")
        from unrealsdk import hooks as _hk
        glog = state["gfx_log"] = []

        def _gcb(obj, args, _ret, func):
            if len(glog) < 4000:
                try:
                    vals = [str(getattr(args, p_.Name))[:80] for p_ in func._fields() if p_.Class.Name.endswith("Property")][:4]
                except Exception as ex:  # noqa: BLE001
                    vals = [str(ex)[:60]]
                glog.append(f"{obj.Class.Name} {func.Name} {vals}")
        names = ["GFxUI.GFxMoviePlayer:" + n for n in ("SetVariableNumber", "SetVariableString", "SetVariableBool",
                 "SetVariable", "Invoke", "ActionScript", "GotoAndStop", "GotoAndPlay")]
        names += ["WillowGame.WillowGFxMovie:" + n for n in ("SingleArgInvokeSF", "SingleArgInvokeB", "SingleArgInvokeS",
                  "SingleArgInvokeF")]
        names += ["WillowGame.WillowHUDGFxMovie:" + n for n in ("UpdateHealth", "UpdateShield", "UpdateAmmo", "UpdateXP",
                  "UpdateLevel", "UpdateGrenades", "UpdateBearing", "UpdateCredits", "UpdateMinimap", "UpdateMainFrame", "OnTick")]
        ok = []
        for n in names:
            try:
                _hk.add_hook(n, _hk.Type.PRE, "sora_gfxprobe", _gcb)
                ok.append(n.split(":")[1])
            except Exception as ex:  # noqa: BLE001
                glog.append(f"hook fail {n} {ex}")
        state["gfx_log_until"] = time.time() + 4.0
        state["gfx_log_names"] = names
        S.status["gfx_hooks"] = ok
    if state.get("gfx_log_until") and time.time() > state["gfx_log_until"]:
        from unrealsdk import hooks as _hk
        for n in state.get("gfx_log_names", []):
            try:
                _hk.remove_hook(n, _hk.Type.PRE, "sora_gfxprobe")
            except Exception:  # noqa: BLE001
                pass
        state["gfx_log_until"] = None
        open(HERE / "gfx_log.txt", "w").write(chr(10).join(state.get("gfx_log", [])))
    if CFG.get("dbg_hud_swf") and state.get("dbg_swf_nonce") != CFG.get("nonce"):    # test hook: dump the HUD .gfx
        state["dbg_swf_nonce"] = CFG.get("nonce")
        try:
            raw = pc.myHUD.HUDMovie.MovieInfo.RawData
            open(HERE / "hud_movie.gfx", "wb").write(bytes(int(x) & 0xFF for x in raw))
            S.status["hud_swf"] = len(raw)
        except Exception as ex:  # noqa: BLE001
            S.status["hud_swf"] = "ERR " + str(ex)
    if CFG.get("dbg_kh1_start") == CFG.get("nonce") and state.get("dbg_ks_nonce") != CFG.get("nonce"):   # test hook: KH1 Start once (value must = nonce)
        state["dbg_ks_nonce"] = CFG.get("nonce")
        kh1_press(S, PAD_START, 0.12)
    if CFG.get("dbg_sc_probe") == CFG.get("nonce") and state.get("dbg_sc_nonce") != CFG.get("nonce"):   # test hook: BL sound classes
        state["dbg_sc_nonce"] = CFG.get("nonce")
        lines = []
        try:
            for sc in unrealsdk.find_all("SoundClass"):
                try:
                    pr = sc.Properties
                    lines.append(f"{sc._path_name()} vol {pr.Volume} pitch {pr.Pitch} children {[c for c in sc.ChildClassNames]}")
                except Exception as ex:  # noqa: BLE001
                    lines.append(f"{sc} ! {ex}")
            for ad in unrealsdk.find_all("AudioDevice"):
                lines.append(f"AudioDevice {ad} fields " + ", ".join(f.Name for f in ad.Class._fields())[:1500])
        except Exception as ex:  # noqa: BLE001
            lines.append(f"error {ex}")
        open(HERE / "sc_probe.txt", "w").write(chr(10).join(lines))
    if CFG.get("dbg_seldbg") and state.get("dbg_sd_nonce") != CFG.get("nonce") and hasattr(S.dll, "sr_khfx_seldbg"):
        state["dbg_sd_nonce"] = CFG.get("nonce")                  # test hook: renderer v47 KH1 frame selections
        buf = (ctypes.c_uint32 * 4096)()
        n = S.dll.sr_khfx_seldbg(buf, 1024)
        json.dump([list(buf[i * 4:i * 4 + 4]) for i in range(n)], open(HERE / "seldbg.json", "w"))
    if CFG.get("dbg_kh1_cast") is not None and state.get("dbg_kc_nonce") != CFG.get("nonce"):   # KH1 itself casts (sound capture)
        state["dbg_kc_nonce"] = CFG.get("nonce")
        kh1_cast(S, pc, int(CFG["dbg_kh1_cast"]))
    if CFG.get("dbg_kh1_press") and state.get("dbg_kp_nonce") != CFG.get("nonce"):   # test hook: press KH1 pad bits
        state["dbg_kp_nonce"] = CFG.get("nonce")
        if CFG.get("dbg_park"):
            state["kh_menu_park_until"] = now + float(CFG["dbg_park"])
        kh1_press(S, int(CFG["dbg_kh1_press"]), float(CFG.get("dbg_kh1_hold", 0.3)))
    if CFG.get("dbg_hl") is not None and state.get("dbg_hl_nonce") != CFG.get("nonce"):   # test hook: free Heartless on/off
        state["dbg_hl_nonce"] = CFG.get("nonce")
        state["hl_off"] = not bool(CFG["dbg_hl"])
        if state["hl_off"]:
            state["free_bl"], state["free_smooth"] = {}, {}
    if CFG.get("dbg_lock") and state.get("dbg_lock_nonce") != CFG.get("nonce"):    # test hook: same as middle mouse
        state["dbg_lock_nonce"] = CFG.get("nonce")
        lock_toggle(S, pc)
    dc = CFG.get("debug_cast")
    if dc and state.get("dbg_nonce") != CFG.get("nonce"):
        state["dbg_nonce"] = CFG.get("nonce")
        launch_spell(S, pc, dc)
        S.status["dbg"] = {"proj": [(pr["kind"], [round(x) for x in pr["pos"]]) for pr in state["proj"]]}
    try:
        probe_tick(S, pc, now)
    except Exception as ex:  # noqa: BLE001
        S.status["probe_error"] = f"{type(ex).__name__}: {ex}"
    update_spells(S, pc, dt, now)
    if state["proj"]:
        pr = state["proj"][0]
        S.status["proj0"] = [pr["kind"], [round(x) for x in pr["pos"]], round(now - pr["t0"], 2)]
    fill_fx(S, now)
    acting = update_action(S, pc, now)
    if state.get("was_air") and int(pawn.Physics) != 2 and not acting:
        state["was_air"] = False
        if speed < 30:                       # KH1 landing pose when you touch down standing still
            start_action(S, "land", CFG["land"], 1.3)
            acting = True
    if acting and state["action"] and state["action"]["kind"] == "land" and speed > 30:
        state["action"] = None               # moving cancels the landing
        acting = False
    if acting:
        state["loco"] = False
    elif CFG["debug_anim"] >= 0:
        state["loco"] = False
        play(S, CFG["debug_anim"])
    elif int(pawn.Physics) == 2:
        state["loco"] = False
        state["was_air"] = True
        play(S, CFG["jump_up"] if float(v.Z) > 80 else CFG["jump_fall"])
    elif speed > 30:
        # walk or run, played at the rate that matches ground speed (no foot sliding)
        kh_speed = speed / max(frame.scale, 0.01)          # KH cm/s
        motion, natural = (CFG["run"], CFG["run_speed"]) if kh_speed > CFG["walk_max"] else (CFG["walk"], CFG["walk_speed"])
        if state["anim"] != motion or not state.get("loco"):
            play(S, motion)
        state["loco"] = True
        rate = max(0.5, min(1.6, kh_speed / natural))
        state["phase"] = state.get("phase", 0.0) + dt * rate
    elif now < state.get("recover_until", 0):
        state["loco"] = False
        play(S, CFG["battle_idle"])
    else:
        state["loco"] = False
        play(S, CFG["idle"])

    frame.anim = state["anim"]
    frame.time = state["phase"] if state.get("loco") else (now - state["t0"]) * (state["action"]["fps"] / 60.0 if state.get("action") else 1.0)
    prev = state.get("prev")
    if hasattr(frame, "blend") and prev and now - prev[2] < BLEND_TIME:
        frame.anim2 = prev[0]
        frame.time2 = prev[1]
        frame.blend = 1.0 - (now - prev[2]) / BLEND_TIME
    elif hasattr(frame, "blend"):
        frame.blend = 0.0
    frame.flags = CFG["flags"]
    if hasattr(S.dll, "sr_live_state"):   # flag 64: pose comes from a running KH1 (0 = not found, 1 = live, 2 = KH1 up, no pose)
        S.status["kh1_live"] = S.dll.sr_live_state()
    try:
        kh1_lock_sync(S, pc, time.time())
    except Exception as ex:  # noqa: BLE001
        S.status["kh1_lock_error"] = f"{type(ex).__name__}: {ex}"
    lock_update(S, pc, dt)
    barriers_tick(S, now)
    if state.get("probe_at") and now > state["probe_at"]:
        state["probe_at"] = None
        S.pending.append(lambda: barriers_probe(S, pc))
    if CFG.get("dbg_block") and state.get("dbg_block_n") != CFG.get("dbg_block"):
        state["dbg_block_n"] = CFG.get("dbg_block")
        S.pending.append(lambda: barriers_probe(S, pc))
    kh1_pad(S, pc, pawn, speed, now)
    try:
        hl_toggle_tick(S, now)
        heartless_tick(S, pc, now)
    except Exception as ex:  # noqa: BLE001
        S.status["free_error"] = f"{type(ex).__name__}: {ex}"
    try:
        skin_tick(S, pc, now)
    except Exception as ex:  # noqa: BLE001
        S.status["skin_error"] = f"{type(ex).__name__}: {ex}"
    try:
        party_tick(S, pc, now)
        try:
            summon_draw_tick(S, pc, now)
            summon_unstick(S, now)
        except Exception as ex:  # noqa: BLE001
            S.status["summon_draw"] = f"ERR {type(ex).__name__}: {ex}"
    except Exception as ex:  # noqa: BLE001
        S.status["party_error"] = f"{type(ex).__name__}: {ex}"
    try:
        party_shots_tick(S, pc, now)
    except Exception as ex:  # noqa: BLE001
        S.status["party_shots"] = f"ERR {type(ex).__name__}: {ex}"
    try:
        cam_log_tick(S, now)
    except Exception as ex:  # noqa: BLE001
        S.status["cam_log_error"] = str(ex)
    bl_hud_tick(S, pc, now)
    try:
        khfx_tick(S, pc, now)
        S.status.pop("khfx_error", None)
    except Exception as ex:  # noqa: BLE001
        S.status["khfx_error"] = f"{type(ex).__name__}: {ex}"
    try:
        trail_tick(S, pc, now)
        S.status.pop("trail_error", None)
        kh_menu_tick(S, pc, now)
        sound_tick(S, pc, pawn, kh1_state(S) if kh1_combat_on(S) else None, now)
    except Exception as ex:  # noqa: BLE001
        S.status["trail_error"] = f"{type(ex).__name__}: {ex}"
    if blade_on(S):
        ks_b = kh1_state(S)
        if ks_b:
            kh1_blade_tick(S, pc, ks_b, now)
    director_tick(S, pc, now)
    polish_frame(S, frame, pawn)
    if hasattr(frame, "bright"):
        frame.bright = CFG.get("bright", 1.0)
    frame.light[:] = (-0.4, 0.3, -0.85)
    try:
        light_tick(S, frame)
    except Exception as ex:  # noqa: BLE001
        S.status["lights_error"] = f"{type(ex).__name__}: {ex}"
    try:
        shadow_tick(S)
    except Exception as ex:  # noqa: BLE001
        S.status["shadow_error"] = f"{type(ex).__name__}: {ex}"
    frame.visible = 0 if state.get("was_downed") else 1    # first person while downed: don't draw Sora over the view
    # TRUE PASSTHROUGH ONLY (user 2026-10-05): no KH1 pose for > kh1_gone_s -> no Sora / Donald / Goofy at all (the
    # baked fallback poses looked glitched after a KH1 crash), and the input handlers do nothing without KH1
    if S.status.get("kh1_live") == 1:
        state["kh1_seen_t"] = now
    kh1_gone = CFG.get("kh1_only", True) and now - state.get("kh1_seen_t", 0) > CFG.get("kh1_gone_s", 1.0)
    if kh1_gone:
        # NOT frame.visible = 0: the renderer only looks for KH1 while Sora is drawn (that deadlocked: no Sora, first
        # person, Borderlands HUD - user). Zero size instead: nothing fake shows, KH1 detection keeps running.
        frame.scale = 0.0
        if not state.get("kh1_gone_hid") and hasattr(S.dll, "sr_set_actor"):
            for aid in (0, 1):
                S.dll.sr_set_actor(aid, ctypes.byref(ActorFrame()))
            state["kh1_gone_hid"] = True
    else:
        state["kh1_gone_hid"] = False
    try:                                        # every tick (v68: Sora stays "live" through a summon intro now)
        summon_watchdog(S, now)
        summon_spawn_watch(S, now)
        summon_protect(S, pc)
        summon_intro_view(S, pc, now)
    except Exception as ex:  # noqa: BLE001
        S.status["summon_watchdog"] = f"ERR {type(ex).__name__}: {ex}"
    S.status["kh1_gone"] = bool(kh1_gone)
    if now < state.get("kh_baked_until", 0) and not CFG.get("kh1_only", True):   # (old: baked cast when KH1 refused)
        frame.flags = CFG["flags"] & ~64
    fov = CFG.get("cam_fov")                    # KH1's view is much tighter than Borderlands' default
    if fov and abs(float(pc.GetFOVAngle()) - float(fov)) > 0.5 and now - state.get("fov_t", 0) > 0.5:
        state["fov_t"] = now
        S.pending.append(lambda: pc.ConsoleCommand(f"fov {float(fov):g}", False))
    try:
        if "cam_scale" in CFG:
            pawn.CameraScale = CFG["cam_scale"]
            pawn.CurrentCameraScale = CFG["cam_scale"]
        if "cam_up" in CFG:
            pawn.CameraScaleUp = CFG["cam_up"]
    except Exception as ex:  # noqa: BLE001
        S.status["cam_error"] = str(ex)
    if dbg_on(S, "dbg_damage_probe") and not state.get("dmg_probe") and all_enemies():
        state["dmg_probe"] = True
        lines = []
        try:
            e = all_enemies()[0]
            cls = e.Class
            while cls is not None:
                for f in cls._fields():
                    if "Damage" in str(f.Name) and str(f.Class.Name) == "Function":
                        try:
                            params = [f"{a.Name}:{a.Class.Name}" for a in f._fields()]
                        except Exception:  # noqa: BLE001
                            params = ["?"]
                        lines.append(f"{cls.Name}.{f.Name}({', '.join(params)})")
                cls = cls.SuperField
            for c in unrealsdk.find_all("Class"):
                n = str(c.Name)
                if "Damage" in n or "Dmg" in n:
                    lines.append("CLASS " + n)
        except Exception as ex:  # noqa: BLE001
            lines.append("ERR " + str(ex))
        (HERE / "damage_probe.txt").write_text(chr(10).join(lines), encoding="utf-8")
    if dbg_on(S, "dbg_fields_dump") and not state.get("dumped"):
        state["dumped"] = True
        field_dump([("pc", pc), ("pawn", pawn), ("weapon", pawn.Weapon)], str(HERE / "fields.txt"))
    S.status.update(action=(state.get("action") or {}).get("motion"), anim=state["anim"], speed=round(speed),
                    fov=frame.fov, scale=round(frame.scale, 3), pos=[round(x) for x in frame.pos],
                    nfx=getattr(frame, "nfx", 0), mp=round(state.get("mp", 0)), drawn=S.dll.sr_frames_drawn(), error=None)
