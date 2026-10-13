# Bakes Donald and Goofy (from YOUR KH1 install's extract) for the renderer: <name>_mesh.bin + <name>_live.bin, the same
# formats as sora_mesh.bin / sora_live.bin. Party members are drawn from their LIVE KH1 joints only (no baked motions).
# Weapons: the staff/shield bones are the last chain of the wielder's skeleton (Sora 280-292 off the right hand,
# Donald 184-188 off joint 151, Goofy 186-190 off joint 117); weapon mesh joint k -> wielder joint base + k.
# uv run --no-project --with numpy --with pillow python make_party.py [out dir]
import glob, json, os, struct, sys
import numpy as np
from PIL import Image
sys.path.insert(0, 'D:/SoraBL/tools')
import kh1_model as M

X = 'D:/SoraBL/kh1_extract'
PARTY = {   # name: (model id, weapon package, weapon cvbl, weapon texture prefix, weapon joint base, rigid hand joint)
    "donald": ("0040", "xw_ex_5210.wpn", "-37c0.cvbl", "-38c", 184, None),   # Mage's Staff
    # Knight's Shield: its chain 186-190 carries garbage (190 always, 186-189 at times) -> rigidly on the hand, 117
    "goofy": ("0050", "xw_ex_5410.wpn", "-5740.cvbl", "-584", 186, None),   # (renderer repairs its bad frames)
}
out = sys.argv[1] if len(sys.argv) > 1 else 'D:/SoraBL/bake'
os.makedirs(out, exist_ok=True)


def find(rel):
    hits = glob.glob(os.path.join(X, 'kh1_*', rel))
    return hits[0] if hits else None


for name, (mid, wpkg, wcv, wtex, wbase, rigid) in PARTY.items():
    joints, mesh_tex = M.read_joints(find(f'original/xa_ex_{mid}.mdls'))
    rdir = find(f'remastered/xa_ex_{mid}.mdls')
    cv = sorted(glob.glob(os.path.join(rdir, '*.cvbl')), key=os.path.getsize)[-1]
    meshes, W = M.bake(joints, M.read_cvbl(cv), mesh_tex, texpath=lambda t: os.path.join(rdir, '-1c%d.dds' % t))
    wdir = find(f'remastered/{wpkg}')
    if wdir:
        wm, _ = M.bake(joints, M.read_cvbl(os.path.join(wdir, wcv)), [0], joint_base=wbase,
                       texpath=lambda t, wdir=wdir: os.path.join(wdir, f'{wtex}{t}.dds'))
        for m in wm:
            m['tex'] = 100 + m['tex']
            if rigid is not None:   # bind-pose positions stay; only the driving joint changes (follows the hand)
                m['J'] = [[rigid] + list(jj[1:]) for jj in m['J']]
        meshes += wm
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
    with open(os.path.join(out, f'{name}_mesh.bin'), 'wb') as f:
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
    with open(os.path.join(out, f'{name}_live.bin'), 'wb') as f:
        f.write(struct.pack('<4sIII', b'SORL', 1, len(used), len(joints)))
        for j in used:
            f.write(struct.pack('<I16f', j, *np.linalg.inv(np.array(W[j])).astype(np.float32).ravel()))
    print(name, 'joints', len(joints), 'verts', len(V), 'tris', len(I) // 3, 'palette', len(used), 'textures', len(texids))
