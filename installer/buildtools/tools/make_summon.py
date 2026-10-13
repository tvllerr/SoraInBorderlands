# Bakes KH1 SUMMONS (from YOUR KH1 install's extract) for the renderer, like make_party.py does Donald and Goofy:
# ex_<id>_mesh.bin + ex_<id>_live.bin (same formats as sora_mesh.bin / sora_live.bin), loaded by the renderer's
# enemy-model path (sr_enemy_model(id)). Drawn from the summon's LIVE KH1 joints only (no baked motions).
# Sources:
#   xa_ex_4070.mdls        Simba (his sinba / b_sinba sound banks are in that package; verified in game)
#   xs_<name>.dat          the summon packages: original = container (u32 count, then u32 offsets); sub-file 0 at
#                          0x80 is the model (.mdls layout); the remastered folder names its parts by offset in the
#                          container: -140.cvbl = 0x80 + 0xc0 mesh, -240.. = 0x80 + 0x1c0.. textures (hex numbered).
# Model ids written: 4100 + KH1 summon id (0 Dumbo, 1 Bambi?, 2 Genie, 3 Tinker Bell?, 4 Mushu?, 5 Simba -> 4070).
# uv run --no-project --with numpy --with pillow python make_summon.py [out dir] [name...]
import glob, os, struct, sys, tempfile
import numpy as np
from PIL import Image
sys.path.insert(0, 'D:/SoraBL/tools')
import kh1_model as M

X = 'D:/SoraBL/kh1_extract'
SUMMONS = {   # name: (source, model id written)
    "simba": ("xa_ex_4070.mdls", 4070),
    "dumbo": ("xs_dumbo.dat", 4100),
    "bambi": ("xs_bambi.dat", 4101),
    "genie": ("xs_genie.dat", 4102),
    "tink": ("xs_tink.dat", 4103),
    "mushu": ("xs_mushu.dat", 4104),
}
out = sys.argv[1] if len(sys.argv) > 1 else 'D:/SoraBL/bake'
want = sys.argv[2:] or list(SUMMONS)
os.makedirs(out, exist_ok=True)


def find(rel):
    hits = glob.glob(os.path.join(X, 'kh1_*', rel))
    return hits[0] if hits else None


def model_source(src):
    """(original .mdls path, remastered dir, texture name prefix offset) for an xa_ex mdls or an xs_ container."""
    orig, rdir = find(f'original/{src}'), find(f'remastered/{src}')
    if src.endswith('.mdls'):
        return orig, rdir, 0x1c0
    d = open(orig, 'rb').read()
    n = struct.unpack_from('<I', d, 0)[0]
    offs = list(struct.unpack_from('<%dI' % n, d, 4)) + [len(d)]
    sub = d[offs[0]:offs[1]]
    tmp = os.path.join(tempfile.gettempdir(), f'kh1_{src}.mdls')
    open(tmp, 'wb').write(sub)
    return tmp, rdir, offs[0] + 0x1c0


for name in want:
    src, mid = SUMMONS[name]
    orig, rdir, tex0 = model_source(src)
    if not orig or not rdir:
        print(name, 'missing in the extract'); continue
    joints, mesh_tex = M.read_joints(orig)
    cv = sorted(glob.glob(os.path.join(rdir, '*.cvbl')), key=os.path.getsize)[-1]
    meshes, W = M.bake(joints, M.read_cvbl(cv), mesh_tex, texpath=lambda t: os.path.join(rdir, '-%x.dds' % (tex0 + t)))
    used = sorted(set(int(j) for m in meshes for jj in m['J'] for j in jj[:1]))
    pal = {j: i for i, j in enumerate(used)}
    V, I, subs = [], [], []
    for m in meshes:
        base, start = len(V), len(I)
        for p, nn, uv, jj in zip(m['P'], m['N'], m['UV'], m['J']):
            V.append((*p, *nn, uv[0], uv[1], pal[int(jj[0])]))
        for f in m['F']:
            I += [base + f[0], base + f[1], base + f[2]]
        subs.append((m['tex'], start, len(I) - start))
    texids = sorted(set(s[0] for s in subs))
    with open(os.path.join(out, f'ex_{mid:04d}_mesh.bin'), 'wb') as f:
        f.write(struct.pack('<4sIIIII', b'SORM', 1, len(V), len(I), len(subs), len(texids)))
        for v in V:
            f.write(struct.pack('<8fI', *v))
        f.write(struct.pack('<%dI' % len(I), *I))
        for t, s0, c in subs:
            f.write(struct.pack('<III', texids.index(t), s0, c))
        for t in texids:
            tp = next(m['texpath'] for m in meshes if m['tex'] == t)
            im = Image.open(tp).convert('RGBA')
            f.write(struct.pack('<II', *im.size)); f.write(im.tobytes())
    with open(os.path.join(out, f'ex_{mid:04d}_live.bin'), 'wb') as f:
        f.write(struct.pack('<4sIII', b'SORL', 1, len(used), len(joints)))
        for j in used:
            f.write(struct.pack('<I16f', j, *np.linalg.inv(np.array(W[j])).astype(np.float32).ravel()))
    print(name, 'model', mid, 'from', src, 'cvbl', os.path.basename(cv), 'joints', len(joints), 'verts', len(V),
          'tris', len(I) // 3, 'palette', len(used), 'textures', len(texids))
