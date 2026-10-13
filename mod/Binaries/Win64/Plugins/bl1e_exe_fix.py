# This install runs BL1 Enhanced as Launcher.exe; the mod manager detects the game by exe name
# (expects BorderlandsGOTY.exe). Report the expected name, then run the normal SDK startup script.
import sys
from pathlib import Path

sys.executable = str(Path(sys.executable).with_name("BorderlandsGOTY.exe"))
_main = Path(__file__).resolve().parent.parent.parent.parent / "sdk_mods" / "__main__.py"
exec(compile(_main.read_text(encoding="utf-8"), str(_main), "exec"), {"__file__": str(_main), "__name__": __name__})
