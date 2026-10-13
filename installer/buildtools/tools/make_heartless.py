# Bakes KH1 enemy models (Heartless, bosses) from YOUR KH1 install's extract for the renderer: ex_<id>_mesh.bin +
# ex_<id>_live.bin (same formats as the party's). Drawn from the live KH1 joints of the enemy stand-in.
# uv run --no-project --with numpy --with pillow python make_heartless.py <out dir> <id> [id ...]
import glob, os, struct, sys
import numpy as np
from PIL import Image
sys.path.insert(0, 'D:/SoraBL/tools')
import kh1_model as M

X = 'D:/SoraBL/kh1_extract'
out = sys.argv[1]
os.makedirs(out, exist_ok=True)


def find(rel):
    hits = glob.glob(os.path.join(X, 'kh1_*', rel))
    return hits[0] if hits else None


for mid in sys.argv[2:]:
    o, rdir = find(f'original/xa_ex_{mid}.mdls'), find(f'remastered/xa_ex_{mid}.mdls')
    if not o or not rdir:
        print(mid, 'missing'); continue
    joints, mesh_tex = M.read_joints(o)
    cv = sorted(glob.glob(os.path.join(rdir, '*.cvbl')), key=os.path.getsize)[-1]
    meshes, W = M.bake(joints, M.read_cvbl(cv), mesh_tex, texpath=lambda t: os.path.join(rdir, '-1c%d.dds' % t))
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
    with open(os.path.join(out, f'ex_{mid}_mesh.bin'), 'wb') as f:
        f.write(struct.pack('<4sIIIII', b'SORM', 1, len(V), len(I), len(subs), len(texids)))
        for v in V:
            f.write(struct.pack('<8fI', *v))
        f.write(struct.pack('<%dI' % len(I), *I))
        for t, s0, c in subs:
            f.write(struct.pack('<III', texids.index(t), s0, c))
        for t in texids:
            im = Image.open(next(m['texpath'] for m in meshes if m['tex'] == t)).convert('RGBA')
            f.write(struct.pack('<II', *im.size)); f.write(im.tobytes())
    with open(os.path.join(out, f'ex_{mid}_live.bin'), 'wb') as f:
        f.write(struct.pack('<4sIII', b'SORL', 1, len(used), len(joints)))
        for j in used:
            f.write(struct.pack('<I16f', j, *np.linalg.inv(np.array(W[j])).astype(np.float32).ravel()))
    print(mid, 'joints', len(joints), 'verts', len(V), 'tris', len(I) // 3, 'palette', len(used), 'textures', len(texids),
          'MB', round(os.path.getsize(os.path.join(out, f'ex_{mid}_mesh.bin')) / 1e6, 1))
