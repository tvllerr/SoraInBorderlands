# Play as Sora (Kingdom Hearts Final Mix) in Borderlands GOTY Enhanced.
# Sora is drawn by sora_render.dll (D3D11) from files baked out of the player's own KH1 install.
# Per-frame logic lives in logic.py (hot-reloaded on change); config in config.json (re-read on change).
import ctypes
import importlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

from unrealsdk import logging  # type: ignore
from unrealsdk.hooks import Block, Type  # type: ignore
from unrealsdk.unreal import BoundFunction, UObject, WrappedStruct  # type: ignore

from mods_base import BoolOption, ButtonOption, NestedOption, SliderOption, build_mod, get_pc, hook, keybind

from . import logic

HERE = Path(__file__).parent
DATA = HERE / "data"  # sora_mesh.bin, sora_anim.bin, sora_render.dll
CFG_FILE = HERE / "config.json"
DEFAULTS = {"idle": 0, "walk": 2, "run": 5, "walk_speed": 164.0, "run_speed": 678.0, "walk_max": 330.0, "jump_up": 9, "jump_fall": 10, "land": 11, "dodge": 12,
            "combo": [98, 99, 100, 101], "air_combo": [102, 103, 104], "battle_idle": 0,
            "spell_motions": {"Fire": 61, "Blizzard": 62, "Thunder": 63, "Cure": 64}, "cast_speed": 1.25,
            "kb_damage": 45, "kb_reach": 260, "atk_speed": 1.0, "scale_fudge": 0.9, "yaw_offset_deg": 0.0, "flags": 3,
            "cam_scale": 12.0, "cam_up": 1.5, "debug_anim": -1, "turn_speed": 12.0, "hud": True, "dmg_mode": "takedamage"}


class FxB(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("pos", ctypes.c_float * 3), ("size", ctypes.c_float), ("color", ctypes.c_float * 4),
                ("uv", ctypes.c_float * 4), ("tex", ctypes.c_int32), ("rot", ctypes.c_float), ("stretch", ctypes.c_float),
                ("mode", ctypes.c_int32)]   # v10: KH1 effect sprites (tex -1 = procedural dot)


class Frame(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("cam_pos", ctypes.c_float * 3), ("cam_rot", ctypes.c_float * 3), ("fov", ctypes.c_float),
        ("pos", ctypes.c_float * 3), ("yaw", ctypes.c_float), ("anim", ctypes.c_int32), ("time", ctypes.c_float),
        ("scale", ctypes.c_float), ("visible", ctypes.c_int32), ("flags", ctypes.c_int32),
        ("yaw_offset", ctypes.c_float), ("light", ctypes.c_float * 3),
        ("nfx", ctypes.c_int32), ("fx", FxB * 256),
        ("anim2", ctypes.c_int32), ("time2", ctypes.c_float), ("blend", ctypes.c_float),
        ("bright", ctypes.c_float),
        # v7 renderer: close-camera fade, relight from the game's picture, haze (see logic.polish_frame)
        ("fade", ctypes.c_float), ("relight", ctypes.c_float * 4), ("relight2", ctypes.c_float * 4),
        ("haze", ctypes.c_float * 4),
        ("bank", ctypes.c_float),          # v11: lean (roll about his forward axis), glide turns
    ]


S = SimpleNamespace(HERE=HERE, pending=[], dll=None, frame=Frame(), CFG=dict(DEFAULTS), status={}, opts={},
                    state={"anim": 0, "t0": time.time(), "yaw": None, "last": time.time(), "cam": False, "phase": 0.0})
_watch = {"t": 0.0, "logic": 0.0, "cfg": 0.0}


def load_dll() -> bool:
    if S.dll is not None:
        return True
    try:
        dll = ctypes.CDLL(str(DATA / "sora_render.dll"))
        dll.sr_init.argtypes = [ctypes.c_char_p]
        dll.sr_set_frame.argtypes = [ctypes.POINTER(Frame)]
        dll.sr_motion_duration.restype = ctypes.c_float
        ok = dll.sr_init(str(DATA).encode("mbcs"))
        logging.info(f"[Sora] renderer init -> {ok}, motions {dll.sr_motion_count()}")
        S.dll = dll
        return bool(ok)
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Sora] could not load renderer: {ex}")
        return False


def watch_files() -> None:
    global logic
    now = time.time()
    if now - _watch["t"] < 1.0:
        return
    _watch["t"] = now
    try:
        m = (HERE / "logic.py").stat().st_mtime
        if _watch["logic"] and m != _watch["logic"]:
            logic = importlib.reload(logic)
            logging.info("[Sora] logic.py reloaded")
        _watch["logic"] = m
        if CFG_FILE.exists():
            m = CFG_FILE.stat().st_mtime
            if m != _watch["cfg"]:
                S.CFG = {**DEFAULTS, **json.loads(CFG_FILE.read_text()), **S.opts}   # mod menu options win
                _watch["cfg"] = m
        (HERE / "status.json").write_text(json.dumps(S.status))
    except Exception as ex:  # noqa: BLE001
        S.status["reload_error"] = str(ex)


@hook(hook_func="Engine.PlayerController:PlayerTick", hook_type=Type.POST)
def on_player_tick(obj: UObject, __args: WrappedStruct, __ret, __func: BoundFunction) -> None:
    """Gameplay calls (melee, damage, healing) must run in the game tick, never inside PostRender:
    damaging a pawn from PostRender triggers physics during async render work -> General protection fault."""
    if not S.pending or obj != get_pc():
        return
    jobs, S.pending = S.pending, []
    for job in jobs:
        try:
            job()
        except Exception as ex:  # noqa: BLE001
            S.status["job_error"] = f"{type(ex).__name__}: {ex}"








@hook(hook_func="Engine.GameViewportClient:PostRender", hook_type=Type.POST)
def on_render(obj: UObject, __args: WrappedStruct, __ret, __func: BoundFunction) -> None:
    if S.dll is None:
        return
    watch_files()
    try:
        logic.draw_hud(S, get_pc(), __args.Canvas)
    except Exception as ex:  # noqa: BLE001
        S.status["hud_error"] = f"{type(ex).__name__}: {ex}"
    try:
        logic.tick(S, get_pc())
    except Exception as ex:  # noqa: BLE001
        S.status["error"] = f"{type(ex).__name__}: {ex}"
        S.frame.visible = 0
    S.dll.sr_set_frame(ctypes.byref(S.frame))


@hook(hook_func="Engine.PlayerController:StartFire", hook_type=Type.PRE)
def on_start_fire(obj: UObject, __args: WrappedStruct, __ret, __func: BoundFunction):
    """Left click swings the Keyblade instead of shooting (right click = magic)."""
    if S.dll is None or obj != get_pc():
        return None
    try:
        fire_mode = int(__args.FireModeNum) if hasattr(__args, "FireModeNum") else 0
        if fire_mode == 0:
            logic.attack(S, obj)
        else:
            logic.menu_confirm(S, obj)
    except Exception as ex:  # noqa: BLE001
        S.status["attack_error"] = f"{type(ex).__name__}: {ex}"
    return Block


@hook(hook_func="Engine.PlayerController:StartAltFire", hook_type=Type.PRE)
def on_start_alt_fire(obj: UObject, __args: WrappedStruct, __ret, __func: BoundFunction):
    if S.dll is None or obj != get_pc():
        return None
    try:
        logic.menu_confirm(S, obj)
    except Exception as ex:  # noqa: BLE001
        S.status["cast_error"] = f"{type(ex).__name__}: {ex}"
    return Block


@hook(hook_func="Engine.PlayerController:NextWeapon", hook_type=Type.PRE)
def on_next_weapon(obj: UObject, __args: WrappedStruct, __ret, __func: BoundFunction):
    """Mouse wheel moves the command menu cursor (instead of switching guns)."""
    if S.dll is None or obj != get_pc():
        return None
    logic.menu_move(S, +1)
    return Block


@hook(hook_func="Engine.PlayerController:PrevWeapon", hook_type=Type.PRE)
def on_prev_weapon(obj: UObject, __args: WrappedStruct, __ret, __func: BoundFunction):
    if S.dll is None or obj != get_pc():
        return None
    logic.menu_move(S, -1)
    return Block


@keybind("Command menu: back", "Q")
def on_menu_back() -> None:
    logic.menu_back(S)


@keybind("Command menu: up", "Up")
def on_menu_up() -> None:
    logic.menu_move(S, -1)


@keybind("Command menu: down", "Down")
def on_menu_down() -> None:
    logic.menu_move(S, +1)








@keybind("Sora: KH1 menu", "F9")
def on_kh_menu():
    # logic polls this key itself (kh_menu_vk reads .key: it must still close the menu while Borderlands input is
    # blocked); Block only stops Borderlands acting on the same key
    return Block


S.menu_keybind = on_kh_menu


@keybind("Sora: lock on", "MiddleMouseButton")
def on_lock() -> None:
    if hasattr(logic, "lock_toggle"):
        logic.lock_toggle(S, get_pc())


def _outlines_changed(_opt, value: bool) -> None:
    S.state["outlines_off"] = not value


opt_outlines = BoolOption("Outlines", True, description="Black ink outlines on Sora, Donald, Goofy and the Heartless.",
                          on_change=_outlines_changed)


# Mod menu options -> config keys (S.opts overrides config.json; logic reads them live through S.CFG).
# (config key, scale: menu value x scale = config value)
_OPT_KEYS = {}


def _opt_set(opt, value) -> None:
    key, scale = _OPT_KEYS[opt.identifier]
    v = value * scale if scale is not None else value
    S.opts[key] = v
    S.CFG[key] = v


def _slider(name, key, default, lo, hi, step, desc, scale=None, is_int=False):
    o = SliderOption(name, default, lo, hi, step=step, is_integer=is_int, description=desc, on_change_anytime=_opt_set)
    _OPT_KEYS[name] = (key, scale)
    return o


def _toggle(name, key, default, desc):
    o = BoolOption(name, default, description=desc, on_change_anytime=_opt_set)
    _OPT_KEYS[name] = (key, None)
    return o


opt_cam = [
    _slider("Camera Distance", "cam_scale", 9.0, 4.0, 16.0, 0.5, "How far the camera sits behind Sora. Default 9."),
    _slider("Camera Height", "cam_up", 0.7, 0.0, 2.5, 0.1, "How high the camera sits above Sora. Default 0.7."),
    _slider("Field of View", "cam_fov", 80, 60, 110, 1, "Camera field of view in degrees. Default 80 (KH1's view is "
            "tighter than Borderlands' 90).", is_int=True),
    _slider("Lock-on Camera Tilt", "lock_cam_pitch", -12, -30, 10, 1, "How far the camera looks down while locked on "
            "(negative = down). Default -12.", is_int=True),
]
opt_play = [
    _toggle("Donald & Goofy", "kh_party", True, "Donald and Goofy fight alongside Sora."),
    _slider("Run Speed", "kh_pace", 120, 100, 150, 5, "Sora's run speed in percent of KH1's. Default 120 (KH1's own "
            "run is Borderlands' walk speed).", scale=0.01, is_int=True),
    _slider("Dash Speed", "kh_dash_pace", 133, 100, 160, 1, "Speed while holding Sprint, in percent of KH1's run. "
            "Default 133.", scale=0.01, is_int=True),
]
opt_look = [
    opt_outlines,
    _toggle("Hide Borderlands HUD", "bl_hud_hide", True, "Hide Borderlands' health / ammo / crosshair while KH1's "
            "command menu and gauges are shown."),
]
opt_audio = [
    _toggle("Kingdom Hearts Music", "kh1_music", True, "Play KH1's field and battle music instead of Borderlands' music."),
    _toggle("Match Borderlands Volume", "kh1_follow_bl_volume", True, "KH1's sound follows Borderlands' Master, "
            "Music and SFX volume sliders."),
    _slider("KH1 Volume", "kh1_volume_scale", 100, 0, 150, 5, "Extra volume trim for KH1's sounds and music, in "
            "percent. Default 100.", scale=0.01, is_int=True),
]
_ALL_OPTS = opt_cam + opt_play + opt_look[1:] + opt_audio


def _reset_defaults(_btn) -> None:
    for o in _ALL_OPTS + [opt_outlines]:
        o.value = o.default_value
        if o in _ALL_OPTS:
            _opt_set(o, o.default_value)
    S.state["outlines_off"] = not opt_outlines.value
    mod.save_settings()


opt_reset = ButtonOption("Reset Sora Options", description="Put every Sora option back to its default.",
                         on_press=_reset_defaults)


def on_enable() -> None:
    S.state["outlines_off"] = not opt_outlines.value
    for o in _ALL_OPTS:
        _opt_set(o, o.value)
    load_dll()
    S.state["cam"] = False


def on_disable() -> None:
    try:
        logic.kh1_loadout_restore(S, "mod disabled")    # the temporary BL-level loadout never stays in KH1
    except Exception:  # noqa: BLE001
        pass
    pc = get_pc()
    if S.dll is not None:
        S.frame.visible = 0
        S.dll.sr_set_frame(ctypes.byref(S.frame))
    if pc is not None:
        pc.ConsoleCommand("camera firstperson", False)
        if pc.Pawn is not None and pc.Pawn.Mesh is not None:
            pc.Pawn.Mesh.SetHidden(False)


mod = build_mod(name="Sora", author="Claude", description="Play as Sora from Kingdom Hearts Final Mix.", version="0.1.0",
                keybinds=[on_kh_menu, on_menu_back, on_menu_up, on_menu_down, on_lock], options=[NestedOption("Camera", opt_cam, description="Camera distance, height, field of view."),
                         NestedOption("Gameplay", opt_play, description="Party and movement speed."),
                         NestedOption("Visuals", opt_look, description="Outlines and HUD."),
                         NestedOption("Audio", opt_audio, description="KH1 music and volume."), opt_reset],
                hooks=[on_render, on_player_tick, on_start_fire, on_start_alt_fire, on_next_weapon, on_prev_weapon], on_enable=on_enable, on_disable=on_disable)
mod.enable()
