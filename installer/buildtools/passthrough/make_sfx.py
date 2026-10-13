# Decodes the KH1 sounds the mod plays (from YOUR KH1 install's extract) into sdk_mods/sora/data/sfx/*.wav.
# Needs vgmstream-cli (github.com/vgmstream/vgmstream). Picks by analysis of the Kingdom Key's sound set (xw_ex_5010.se):
#   clang = the strongly ringing metal hit; hit_* = instant broadband impacts; heavy_* = impacts with long tails;
#   swing_* = the ramping whooshes.
import subprocess, sys
from pathlib import Path
VGM = r"D:\SoraBL\tools\vgmstream\vgmstream-cli.exe"
SRC = Path(r"D:\SoraBL\kh1_extract\kh1_first\remastered\xw_ex_5010.se")
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else r"D:\SteamLibrary\steamapps\common\BorderlandsGOTYEnhanced\sdk_mods\sora\data\sfx")
PICK = {"clang": "se011028",
        "hit_1": "se011024", "hit_2": "se011025", "hit_3": "se011026", "hit_4": "se011029", "hit_5": "se011049",
        "heavy_1": "se011042", "heavy_2": "se011043", "heavy_3": "se011044", "heavy_4": "se011047",
        "swing_1": "se011031", "swing_2": "se011032", "swing_3": "se011033", "swing_4": "se011035"}
OUT.mkdir(parents=True, exist_ok=True)
for name, se in PICK.items():
    subprocess.run([VGM, "-o", str(OUT / f"{name}.wav"), str(SRC / f"{se}.win32.scd")], check=True, capture_output=True)
    print(name, "<-", se)
