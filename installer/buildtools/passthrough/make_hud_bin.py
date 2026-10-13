# sora_hud.bin: 'SORH', u32 version, u32 count, then per texture u32 w, u32 h, RGBA8. Made from the user's own KH1 files.
#   tex 0: Sora HUD portraits (normal / hurt / KO), 384x128     tex 1: KH1 gauge parts (UK uitex 10780), 256x256
import struct
from PIL import Image
R = 'D:/SoraBL/kh1_extract/kh1_first/remastered/'
texs = [R + 'xa_ex_0010.mdls/-a3290.dds', R + 'command2/uitex.bin/UK_uitex_bin10780.dds',
        R + 'command2/uitex.bin/-18080.dds',   # tex 2: lock-on reticles (arrows ring at 66,2-126,62)
        R + 'command2/uitex.bin/-4140.dds',    # tex 3: command menu frame pieces (title tab, row slots, tint layers)
        R + 'command2/uitex.bin/UK_uitex_bin20c0.png',   # tex 4: KH1 title words (COMMAND, MAGIC, ITEMS...)
        'D:/SoraBL/kh1_extract/kh1_third/remastered/menu/sysfont.bin/US_sysfont_bin0.dds']   # tex 5: KH1 system font
with open('D:/SoraBL/bake/sora_hud.bin', 'wb') as f:
    f.write(struct.pack('<4sII', b'SORH', 1, len(texs)))
    for t in texs:
        im = Image.open(t).convert('RGBA'); f.write(struct.pack('<II', *im.size)); f.write(im.tobytes()); print(t[-30:], im.size)
