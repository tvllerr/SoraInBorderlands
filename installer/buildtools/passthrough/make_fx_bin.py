# Packs KH1's own effect sprites (texcommon.dpd, from YOUR KH1 install's extract) into sora_fx.bin for the renderer.
# Texture order = tex ids used by logic.py (FX_TEX):
#   0 particle atlas (-a0, 512x512, 64 px cells: glows, stars, shards, sparkles, lightning, smoke flipbook rows 3-5)
#   1 flame flipbook (-14960, 4x4 x 64 px, no alpha: additive)
#   2 smoke puffs (-18d80, 4x4 x 64 px)
#   3 hit spark / jagged burst (-209e0)
#   4 light rays + ring (-c4c0)
#   5 shock ring (-ed00)
#   6 speed lines (-12540)
#   7 electric caustics (-1e5c0)
#   8 Keyblade swing trail (xw_ex_5010.wpn -4310: bright new end at u=0, feathered tail toward u=1)
#   9-11 KH1 PRIZE orbs (prize.dpx, 256x128 = sphere left half, glow / shell right half):
#        9 big orb (-320, green->blue sphere + white glow), 10 small orb (-1740), 11 munny orb (-3b60, + bubble shell)
#   12-14 KH1 ITEM BALLS (prize.ard, the dropped-item models' panel atlases: 12 gold / purple -340, 13 red -96c0,
#        14 blue -12a40): one ornate panel cut to a disc with sphere shading = billboard stand-in for the model
# Format (like sora_hud.bin): 'SFX1', version u32, count u32, then per texture w u32, h u32, RGBA8 pixels.
# Run: uv run --no-project --with pillow python make_fx_bin.py [out]
import struct
import sys
from pathlib import Path

from PIL import Image

SRC = Path(r"D:\SoraBL\kh1_extract\kh1_first\remastered\texcommon.dpd")
NAMES = ["-a0.dds", "-14960.dds", "-18d80.dds", "-209e0.dds", "-c4c0.dds", "-ed00.dds", "-12540.dds", "-1e5c0.dds",
         "../xw_ex_5010.wpn/-4310.dds",   # 8: the Kingdom Key's swing trail (from the Keyblade package)
         "../prize.dpx/-320.dds", "../prize.dpx/-1740.dds", "../prize.dpx/-3b60.dds",   # 9-11: prize orbs
         "../prize.ard/-340.dds", "../prize.ard/-96c0.dds", "../prize.ard/-12a40.dds"]   # 12-14: item balls


def item_ball(im):
    """Top-left ornate panel (1/3 of the 512 atlas) -> 256 px disc, darker toward the rim, soft alpha edge."""
    w, h = im.size
    cell = im.crop((0, 0, w // 3, h // 3)).resize((256, 256), Image.LANCZOS)
    px = cell.load()
    for y in range(256):
        for x in range(256):
            dx, dy = (x - 127.5) / 127.5, (y - 127.5) / 127.5
            r2 = dx * dx + dy * dy
            if r2 >= 1.0:
                px[x, y] = (0, 0, 0, 0)
                continue
            shade = 0.45 + 0.55 * (1.0 - r2) ** 0.5 + 0.18 * max(0.0, -dx - dy) * (1.0 - r2)   # light from top-left
            r, g, b, _ = px[x, y]
            a = 255 if r2 < 0.9 else int(255 * (1.0 - r2) / 0.1)
            px[x, y] = (min(255, int(r * shade)), min(255, int(g * shade)), min(255, int(b * shade)), a)
    return cell
out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"D:\SoraBL\bake\sora_fx.bin")
blob = bytearray(b"SFX1" + struct.pack("<II", 1, len(NAMES)))
for n in NAMES:
    im = Image.open(SRC / n).convert("RGBA")
    if n == "-14960.dds":
        # flames are drawn on black with no alpha: alpha = brightness so they can blend either way
        r, g, b, _ = im.split()
        im.putalpha(Image.eval(Image.merge("RGB", (r, g, b)).convert("L"), lambda v: min(255, v * 2)))
    if "prize.ard" in n:
        im = item_ball(im)
    blob += struct.pack("<II", *im.size) + im.tobytes()
    print(n, im.size)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_bytes(bytes(blob))
print("wrote", out, len(blob) // 1024, "KB")
