# Sound pass (2026-10-01): decodes, from YOUR KH1 install's extract, into sdk_mods/sora/data/sfx/:
#  - Sora's own sound effects (xa_ex_0010.mdls, 21 clips) as sse<NN>_0.wav, NN = the index KH1's motion cues use
#    (cue param low 16 bits = index n -> se011(n+1); high 16 bits = KH1 volume 0..127)
#  - Traverse Town battle voices (voice/tw_*.vsb), grouped by use (labels from Whisper: see work/voices)
#  - data/sora_sfx_cues.json: per motion index, [frame, sound index, joint] from the mset event tracks 0x40000/0x40001
import json, struct, subprocess, sys
from pathlib import Path
VGM = r"D:\SoraBL\tools\vgmstream\vgmstream-cli.exe"
X = Path(r"D:\SoraBL\kh1_extract")
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else r"D:\SteamLibrary\steamapps\common\BorderlandsGOTYEnhanced\sdk_mods\sora\data")
SFX = OUT / "sfx"; SFX.mkdir(parents=True, exist_ok=True)
# cue index n = sound se011(n+1), spread over Sora's model folder AND the Kingdom Key's bank (se011018-022 live there)
pool = {f.name[:8]: f for d in ("kh1_first/remastered/xa_ex_0010.mdls", "kh1_first/remastered/xw_ex_5010.se")
        for f in (X / d).glob("*.scd")}
for old in SFX.glob("sse*_0.wav"):
    old.unlink()
n_se = 0
for idx in range(64):
    f = pool.get(f"se011{idx + 1:03d}")
    if f:
        subprocess.run([VGM, "-o", str(SFX / f"sse{idx:02d}_0.wav"), str(f)], check=True, capture_output=True); n_se += 1
print("sora se", n_se)
VOICE = {   # group: (bank, [subsong numbers])
    "vatk": ("kh1_first/remastered/voice/tw_sora.vsb/tw_sora.win32.scd", [25, 26, 27, 28, 31]),
    "vbig": ("kh1_first/remastered/voice/tw_sora.vsb/tw_sora.win32.scd", [29, 32]),
    "vfin": ("kh1_first/remastered/voice/tw_sora.vsb/tw_sora.win32.scd", [30, 35]),
    "vhurt": ("kh1_first/remastered/voice/tw_sora.vsb/tw_sora.win32.scd", [3, 6, 7, 8, 9, 10]),
    "vko": ("kh1_first/remastered/voice/tw_sora.vsb/tw_sora.win32.scd", [11]),
    "vitem": ("kh1_first/remastered/voice/tw_sora.vsb/tw_sora.win32.scd", [33]),
    "vgo": ("kh1_first/remastered/voice/tw_sora.vsb/tw_sora.win32.scd", [23, 2]),
    "vthrow": ("kh1_first/remastered/voice/tw_sora.vsb/tw_sora.win32.scd", [34]),
    "vthisisit": ("kh1_first/remastered/voice/tw_sora.vsb/tw_sora.win32.scd", [36]),
    "vgotcha": ("kh1_first/remastered/voice/tw_sora.vsb/tw_sora.win32.scd", [20]),
    "dhurt": ("kh1_fourth/remastered/voice/tw_donald.vsb/tw_donald.win32.scd", [15, 5]),
    "dheal": ("kh1_fourth/remastered/voice/tw_donald.vsb/tw_donald.win32.scd", [9]),
    "datk": ("kh1_fourth/remastered/voice/tw_donald.vsb/tw_donald.win32.scd", [11, 12, 17]),
    "dcheer": ("kh1_fourth/remastered/voice/tw_donald.vsb/tw_donald.win32.scd", [8, 1]),
    "ghurt": ("kh1_fourth/remastered/voice/tw_goofy.vsb/tw_goofy.win32.scd", [10, 11, 14]),
    "gheal": ("kh1_fourth/remastered/voice/tw_goofy.vsb/tw_goofy.win32.scd", [15]),
    "gatk": ("kh1_fourth/remastered/voice/tw_goofy.vsb/tw_goofy.win32.scd", [2, 5, 6]),
    "gcheer": ("kh1_fourth/remastered/voice/tw_goofy.vsb/tw_goofy.win32.scd", [13, 1]),
}
for i, n in enumerate(range(12, 19)):   # spell lines in KH1's spell order: Fire, Blizzard, Thunder, Cure, Gravity, Stop, Aero
    VOICE[f"vspell{i}"] = ("kh1_first/remastered/voice/tw_sora.vsb/tw_sora.win32.scd", [n])
for g, (bank, subs) in VOICE.items():
    for s in subs:
        subprocess.run([VGM, "-s", str(s), "-o", str(SFX / f"{g}_{s:02d}.wav"), str(X / bank)], check=True, capture_output=True)
print("voice groups", len(VOICE))
b = open(X / "kh1_first/original/xa_ex_0010.mset", "rb").read()
tbl, offs, o = 0xC0, [], 0xC0
while (v := struct.unpack_from("<I", b, o)[0]) != 0xFFFFFFFF:
    offs.append(v); o += 4
cues = {}
for i, off in enumerate(offs):
    m = tbl + off; H = struct.unpack_from("<16I", b, m)
    if H[4] == 0 or H[4] > 1000:
        continue
    p = m + H[15]; recs = []
    while struct.unpack_from("<I", b, p)[0] != 0xFFFFFFFF:
        t, c, _ = struct.unpack_from("<3I", b, p); recs.append((t, c)); p += 12
    p += 4
    ev = []
    for t, c in recs:
        for k in range(c):
            fr, par = struct.unpack_from("<fI", b, p); p += 16
            if t in (0x40000, 0x40001):
                ev.append([fr, par & 0xFFFF, par >> 16])
    if ev:
        cues[i] = {"len": H[1], "ev": sorted(ev)}
json.dump(cues, open(OUT / "sora_sfx_cues.json", "w"))
print("motions with sound cues", len(cues))

# ---- Donald / Goofy: motion cues like Sora's (index n -> se012(n+1) / se013(n+1), over model + weapon banks)
def party(mid, base, weapon, prefix):
    pool = {f.name[:8]: f for d in (f"xa_ex_{mid}.mdls", weapon) for g in X.glob(f"kh1_*/remastered/{d}") for f in g.glob("*.scd")}
    for old in SFX.glob(f"{prefix}*_0.wav"):
        old.unlink()
    n = 0
    for idx in range(64):
        f = pool.get(f"{base}{idx + 1:03d}")
        if f:
            subprocess.run([VGM, "-o", str(SFX / f"{prefix}{idx:02d}_0.wav"), str(f)], check=True, capture_output=True); n += 1
    b = open(next(X.glob(f"kh1_*/original/xa_ex_{mid}.mset")), "rb").read()
    tbl, offs, o = 0xC0, [], 0xC0
    while (v := struct.unpack_from("<I", b, o)[0]) != 0xFFFFFFFF:
        offs.append(v); o += 4
    cues = {}
    for i, off in enumerate(offs):
        m = tbl + off; H = struct.unpack_from("<16I", b, m)
        if H[4] == 0 or H[4] > 1000:
            continue
        p = m + H[15]; recs = []
        while struct.unpack_from("<I", b, p)[0] != 0xFFFFFFFF:
            t, c, _ = struct.unpack_from("<3I", b, p); recs.append((t, c)); p += 12
        p += 4
        ev = []
        for t, c in recs:
            for k in range(c):
                fr, par = struct.unpack_from("<fI", b, p); p += 16
                if t in (0x40000, 0x40001):
                    ev.append([fr, par & 0xFFFF, par >> 16])
        if ev:
            cues[i] = {"len": H[1], "ev": sorted(ev)}
    json.dump(cues, open(OUT / f"{prefix}_sfx_cues.json", "w"))
    print(prefix, "sounds", n, "motions with cues", len(cues))
party("0040", "se012", "xw_ex_5210.se", "dse")
party("0050", "se013", "xw_ex_5410.se", "gse")

# ---- Heartless (no motion cues: KH1 plays them from AI code) -> per model groups by sound family:
#   h<model>a = se001xxx (movement / attack), h<model>b = se003xxx (hurt), h<model>c = se005xxx (defeat)
for mid in ("2010", "2160"):
    d = next(X.glob(f"kh1_*/remastered/xa_ex_{mid}.mdls"))
    for old in SFX.glob(f"h{mid}*.wav"):
        old.unlink()
    for f in sorted(d.glob("*.scd")):
        fam = {"se001": "a", "se003": "b", "se005": "c"}.get(f.name[:5])
        if fam:
            subprocess.run([VGM, "-o", str(SFX / f"h{mid}{fam}_{f.name[5:8]}.wav"), str(f)], check=True, capture_output=True)
    print("heartless", mid, len(list(SFX.glob(f"h{mid}*.wav"))))
