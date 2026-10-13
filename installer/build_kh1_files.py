# Builds every Kingdom Hearts file the mod needs (models, motions, HUD art, effects, weapons, sounds) from the
# player's OWN KH1 extract. Nothing from KH1 ships in the zip; this script makes it on the player's PC.
#   python build_kh1_files.py --extract <folder with kh1_first, kh1_third, ...> --work <scratch folder>
#                             --vgmstream <vgmstream-cli.exe> --out <Borderlands>\sdk_mods\sora\data
# The build scripts in buildtools\ were written against D:\SoraBL; they are copied into --work with those paths
# rewritten, then run in order. Run with numpy + pillow available (setup.ps1 uses uv for that).
import argparse, json, shutil, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ap = argparse.ArgumentParser()
ap.add_argument("--extract", required=True)
ap.add_argument("--work", required=True)
ap.add_argument("--vgmstream", required=True)
ap.add_argument("--out", required=True)
a = ap.parse_args()
X, W, VGM, OUT = (Path(p).resolve() for p in (a.extract, a.work, a.vgmstream, a.out))

for need in ("kh1_first", "kh1_third", "kh1_fourth", "kh1_fifth"):
    if not (X / need / "original").is_dir() or not (X / need / "remastered").is_dir():
        sys.exit(f"missing KH1 extract folder: {X / need} (needs 'original' and 'remastered' inside)")

# ---- copy the build scripts with their D:\SoraBL paths pointed at this PC's folders
fwd = lambda p: str(p).replace("\\", "/")
SUBS = [("D:/SoraBL/kh1_extract", "\0X/", fwd(X)), (r"D:\SoraBL\kh1_extract", "\0X\\", str(X)),
        (r"D:\SoraBL\tools\vgmstream\vgmstream-cli.exe", "\0V", str(VGM)),
        ("D:/SoraBL", "\0W/", fwd(W)), (r"D:\SoraBL", "\0W\\", str(W))]
for sub in ("tools", "passthrough"):
    (W / sub).mkdir(parents=True, exist_ok=True)
    for f in (HERE / "buildtools" / sub).glob("*.py"):
        t = f.read_text(encoding="utf-8")
        for old, tok, _ in SUBS:     # placeholders first, so a new path can't be rewritten again
            t = t.replace(old, tok)
        for _, tok, new in SUBS:
            t = t.replace(tok, new)
        (W / sub / f.name).write_text(t, encoding="utf-8")
BAKE, WORK = W / "bake", W / "work"
BAKE.mkdir(exist_ok=True); WORK.mkdir(exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)

orig, rem = X / "kh1_first/original/xa_ex_0010.mdls", X / "kh1_first/remastered/xa_ex_0010.mdls"
STEPS = [   # (what the player sees, script, args)
    ("Sora's model", "tools/kh1_model.py", [orig, rem, WORK / "sora"]),
    ("Sora's motion events", "tools/export_events.py", [BAKE / "sora_events.json"]),
    ("Sora's mesh and motions", "tools/bake_sora.py", [BAKE]),
    ("Sora's skeleton", "passthrough/make_live_bin.py", []),
    ("KH1 HUD and menu art", "passthrough/make_hud_bin.py", []),
    ("KH1 spell effects", "passthrough/make_fx_bin.py", [BAKE / "sora_fx.bin"]),
    ("Donald and Goofy", "tools/make_party.py", [BAKE]),
    ("Donald and Goofy flying", "tools/make_party_fly.py", [BAKE]),
    ("Heartless", "tools/make_heartless.py", [BAKE, "2010", "2160"]),
    ("Summons", "tools/make_summon.py", [BAKE]),
    ("Item drops", "tools/make_items.py", [BAKE]),
    ("Keyblades, staves and shields", "tools/bake_weapons.py", [OUT / "weapons"]),
    ("Keyblade sounds", "passthrough/make_sfx.py", [OUT / "sfx"]),
    ("Voices and party sounds", "passthrough/make_sfx2.py", [OUT]),
]
for i, (label, script, args) in enumerate(STEPS, 1):
    print(f"  [{i}/{len(STEPS) + 1}] {label}...", flush=True)
    t0 = time.time()
    r = subprocess.run([sys.executable, str(W / script)] + [str(x) for x in args], cwd=W,
                       capture_output=True, text=True)
    (W / "build_log.txt").open("a", encoding="utf-8").write(f"== {script}\n{r.stdout}\n{r.stderr}\n")
    if r.returncode != 0:
        print(r.stdout[-2000:], r.stderr[-3000:])
        sys.exit(f"build step failed: {label} ({script}). Full log: {W / 'build_log.txt'}")
    print(f"        done ({time.time() - t0:.0f} s)", flush=True)

# ---- every KH1 sound effect the mod relays (kh1_sounds.json: clip -> source .scd in the extract + subsong)
print(f"  [{len(STEPS) + 1}/{len(STEPS) + 1}] KH1 sound effects (about 1,200 clips)...", flush=True)
snd = OUT / "sfx" / "kh1"; snd.mkdir(parents=True, exist_ok=True)
man = json.loads((HERE / "kh1_sounds.json").read_text())
bad = []
for k, (name, (src, sub)) in enumerate(sorted(man.items()), 1):
    cmd = [str(VGM)] + (["-s", str(sub)] if sub else []) + ["-o", str(snd / name), str(X / src)]
    if subprocess.run(cmd, capture_output=True).returncode != 0:
        bad.append(name)
    if k % 200 == 0:
        print(f"        {k} / {len(man)}", flush=True)
if bad:
    print(f"        {len(bad)} sounds could not be made (the mod skips them): {bad[:10]}")

# ---- the baked models into the mod's data folder
n = 0
for f in list(BAKE.glob("*.bin")) + [BAKE / "sora_events.json"]:
    shutil.copy2(f, OUT / f.name); n += 1
need = ["sora_mesh.bin", "sora_anim.bin", "sora_events.json", "sora_live.bin", "sora_hud.bin", "sora_fx.bin"]
miss = [m for m in need if not (OUT / m).exists()]
if miss:
    sys.exit(f"these files were not built: {miss}. Log: {W / 'build_log.txt'}")
print(f"  KH1 files built: {n} models, {len(list((OUT / 'weapons').glob('*.bin')))} weapons, "
      f"{len(list(snd.glob('*.wav')))} sound effects.")
