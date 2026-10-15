"""Builds the Melty package for Sora in Borderlands: dist/SoraInBorderlands-melty-<ver>.zip (forward-slash paths)."""
import hashlib, io, json, sys, zipfile
from pathlib import Path
VER = sys.argv[1] if len(sys.argv) > 1 else "1.0.0"
REPO = Path("D:/SoraBL/release/repo"); SDK = Path("D:/SoraBL/melty/dl/bl1-enhanced-sdk.zip")
OUT = Path(f"D:/SoraBL/melty/dist/SoraInBorderlands-melty-{VER}.zip"); OUT.parent.mkdir(exist_ok=True)
TOML = """[unrealsdk]
locking_function_calls = true
exe_override = "BorderlandsGOTY.exe"

[pyunrealsdk]
init_script = "bl1e_exe_fix.py"
pyexec_root = '..\\..\\..\\sdk_mods'

[mod_manager]
display_version = "2.3 (7a4e3573)"

[willow1_mod_menu]
display_version = "Firehawk"
"""
items = []
with zipfile.ZipFile(SDK) as z:                               # willow1 PythonSDK v2.3 (LGPL-3.0), debug symbols left out
    for n in z.namelist():
        if n.endswith("/") or n.endswith(".pdb") or n.endswith("unrealsdk.toml") or "/.stubs/" in n:
            continue
        items.append(("game/" + n, z.read(n)))
items.append(("game/Binaries/Win64/Plugins/unrealsdk.toml", TOML.encode()))
for f in (REPO / "mod").rglob("*"):
    if f.is_file():
        items.append(("game/" + f.relative_to(REPO / "mod").as_posix(), f.read_bytes()))
for f in (REPO / "installer").rglob("*"):
    if f.is_file() and f.suffix not in (".c", ".pdb") and "__pycache__" not in f.parts:
        items.append(("setup/installer/" + f.relative_to(REPO / "installer").as_posix(), f.read_bytes()))
for f in (REPO / "LICENSES").rglob("*"):
    if f.is_file():
        items.append(("setup/LICENSES/" + f.relative_to(REPO / "LICENSES").as_posix(), f.read_bytes()))
items.append(("setup/LICENSES/willow1-mod-manager-LGPL-3.0.txt", Path("D:/SoraBL/melty/dl/willow1-LICENSE.txt").read_bytes()))
items.append(("game/Binaries/Win64/steam_appid.txt", b"729040"))   # Launcher.exe started directly stays the tracked process (no Steam relaunch)
items.append(("kh/steam_appid.txt", b"2552430"))   # KH1FM exe then starts directly (skips the HD launcher) when cwd = KH folder
for n in ("LICENSE", "README.md", "HOW TO INSTALL.txt"):
    items.append(("setup/" + n, (REPO / n).read_bytes()))
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
    for n, b in items:
        z.writestr(n, b)
data = OUT.read_bytes()
json.dump([{"path": n, "size": len(b)} for n, b in items], open(OUT.with_suffix(".entries.json"), "w"))
print(OUT, len(data), hashlib.sha256(data).hexdigest(), len(items), "files")
