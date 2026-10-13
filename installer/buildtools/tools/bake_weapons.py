# Bakes every KH1 weapon (Keyblades xw_ex_50x0-51x0, Donald's staves 52x0-53x0, Goofy's shields 54x0-55x0) from YOUR
# KH1 install's extract into separate weapon meshes for the renderer: wpn_<who>_<id>.bin (same SORM format as
# sora_mesh.bin). Each vertex's bone is an index into that character's EXISTING palette (<who>_live.bin), so the
# body mesh, its baked motions and the live pose path stay exactly as they are; the renderer just draws the body
# without its baked weapon plus the weapon KH1 has equipped. Weapon mesh joint k -> wielder joint base + k (Sora 280,
# Donald 184, Goofy 186); a joint missing from the palette falls back to its nearest ancestor that is in it.
# uv run --no-project --with numpy --with pillow python bake_weapons.py [out dir]
import glob, os, struct, sys
import numpy as np
from PIL import Image
sys.path.insert(0, 'D:/SoraBL/tools')
import kh1_model as M

X = 'D:/SoraBL/kh1_extract'
BAKE = 'D:/SoraBL/bake'
out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BAKE, 'weapons')
os.makedirs(out, exist_ok=True)
WHO = {   # who: (model id, live palette file, weapon joint base, weapon id range)
    'sora': ('0010', 'sora_live.bin', 280, range(5010, 5210, 10)),   # 5190 Diamond Dust, 5200 One-Winged Angel (FM)
    'donald': ('0040', 'donald_live.bin', 184, range(5210, 5400, 10)),
    'goofy': ('0050', 'goofy_live.bin', 186, range(5400, 5600, 10)),
}


def find(rel):
    hits = sorted(glob.glob(os.path.join(X, 'kh1_*', rel)))
    return hits[0] if hits else None


def palette(fn):
    b = open(fn, 'rb').read()
    nb = struct.unpack_from('<I', b, 8)[0]
    return [struct.unpack_from('<I', b, 16 + k * 68)[0] for k in range(nb)]


for who, (mid, livefn, base, ids) in WHO.items():
    joints, _ = M.read_joints(find(f'original/xa_ex_{mid}.mdls'))
    parent = {j['i']: j['parent'] for j in joints}
    pal = palette(os.path.join(BAKE, livefn))
    pidx = {j: i for i, j in enumerate(pal)}

    def to_pal(j):
        seen = 0
        while j not in pidx and seen < 64:
            p = parent.get(j)
            if p is None or p == 0x3FF or p == j:
                break
            j, seen = p, seen + 1
        return pidx.get(j, pidx.get(pal[0]))

    for wid in ids:
        wdir = find(f'remastered/xw_ex_{wid}.wpn')
        if not wdir:
            continue
        cvs = sorted(glob.glob(os.path.join(wdir, '*.cvbl')), key=os.path.getsize)
        if not cvs:
            continue
        cv = cvs[-1]
        off = int(os.path.basename(cv)[1:-5], 16)
        prefix = '-%x' % (off + 0x100)
        prefix = prefix[:-1]

        def texpath(t, wdir=wdir, prefix=prefix):
            p = os.path.join(wdir, f'{prefix}{t}.dds')
            if os.path.exists(p):
                return p
            ds = sorted(glob.glob(os.path.join(wdir, f'{prefix}*.dds')))
            return ds[0] if ds else None

        try:
            meshes, _ = M.bake(joints, M.read_cvbl(cv), [0], joint_base=base, texpath=texpath)
        except Exception as ex:  # noqa: BLE001
            print(who, wid, 'bake failed:', ex)
            continue
        meshes = [m for m in meshes if m.get('texpath')]
        if not meshes:
            print(who, wid, 'no textures'); continue
        V, I, subs = [], [], []
        for m in meshes:
            b0, start = len(V), len(I)
            for p, nn, uv, jj in zip(m['P'], m['N'], m['UV'], m['J']):
                V.append((*p, *nn, uv[0], uv[1], to_pal(int(jj[0]))))
            for f in m['F']:
                I += [b0 + f[0], b0 + f[1], b0 + f[2]]
            subs.append((m['tex'], start, len(I) - start))
        texids = sorted(set(s[0] for s in subs))
        fn = os.path.join(out, f'wpn_{who}_{wid}.bin')
        with open(fn, 'wb') as f:
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
        used = sorted(set(pal[v[8]] for v in V))
        print(who, wid, os.path.basename(cv), 'verts', len(V), 'tris', len(I) // 3, 'tex', len(texids), 'joints', used)
