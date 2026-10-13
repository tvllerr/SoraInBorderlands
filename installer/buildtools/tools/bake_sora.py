# Bakes Sora into two binary files the in-game renderer reads (never shipped: made from the user's own KH1 files).
#   sora_mesh.bin : mesh (KH model space, cm, Y-up), textures (RGBA8), bone palette
#   sora_anim.bin : every motion baked to skinning matrices (3x4 rows) per frame for the palette bones
# uv run --with numpy --with pillow python bake_sora.py <out dir> [fps]
import sys, json, struct, os
import numpy as np
from PIL import Image
sys.path.insert(0, 'D:/SoraBL/tools')
import kh1_anim as A, kh1_model as Mdl

out = sys.argv[1]; FPS = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
os.makedirs(out, exist_ok=True)
RDIR = 'D:/SoraBL/kh1_extract/kh1_first/remastered/xa_ex_0010.mdls'
D = json.load(open('D:/SoraBL/work/sora.json'))
joints = D['joints']; n = len(joints)
Wb = [np.array(w) for w in D['world']]; Wbi = [np.linalg.inv(w) for w in Wb]
A.BIND = Wb
A.BIND_LOCAL = [j['r'] for j in joints]
A.ROOT_RESET = (2,)   # IK aim roots: solver owns rotation
A.FOOT_COPY = True; A.FOOT_MODE = 'helper'   # feet take the foot helper's rotation
_toe = Wb[25][:3, 3] - Wb[22][:3, 3]; _toe[1] = 0; _toe /= np.linalg.norm(_toe)
A.KNEE_FWD = np.linalg.inv(Wb[0][:3, :3]) @ _toe   # knees bend toward pelvis-forward (heel->toe)
A.HAND_MODE = 'bind'   # walk/run/jump: hands take their IK helper rotation -> Keyblade trails behind like KH1

# ---- bone palette: only joints the mesh uses ----
used = sorted(set(int(j) for m in D['meshes'] for jj in m['J'] for j in jj[:1]))
pal = {j: i for i, j in enumerate(used)}
print('palette bones', len(used))

# ---- mesh ----
V, I, subs = [], [], []
for m in D['meshes']:
    base = len(V); start = len(I)
    for p, nn, uv, jj in zip(m['P'], m['N'], m['UV'], m['J']):
        V.append((*p, *nn, uv[0], uv[1], pal[int(jj[0])]))
    for f in m['F']:
        I += [base + f[0], base + f[1], base + f[2]]
    subs.append((m['tex'], start, len(I) - start))
texids = sorted(set(s[0] for s in subs))
with open(os.path.join(out, 'sora_mesh.bin'), 'wb') as f:
    f.write(struct.pack('<4sIIIII', b'SORM', 1, len(V), len(I), len(subs), len(texids)))
    for v in V: f.write(struct.pack('<8fI', *v))
    f.write(struct.pack('<%dI' % len(I), *I))
    for t, s, c in subs: f.write(struct.pack('<III', texids.index(t), s, c))
    for t in texids:
        tp = next((m.get('texpath') for m in D['meshes'] if m['tex'] == t and m.get('texpath')), None)
        im = Image.open(tp or os.path.join(RDIR, '-1c%d.dds' % t)).convert('RGBA')
        f.write(struct.pack('<II', *im.size)); f.write(im.tobytes())
print('mesh', len(V), 'verts', len(I) // 3, 'tris', len(subs), 'subs', len(texids), 'textures')

# ---- animations ----
MS = A.motions('D:/SoraBL/kh1_extract/kh1_first/original/xa_ex_0010.mset')
_L0 = A.sample(MS[0], joints, 0.0)
A.WEAPON_DEFAULT = {j: _L0[j] for j in A.WEAPON_JOINTS}   # motions without grip data inherit the idle grip
def lm(i, v):
    if isinstance(v, tuple): return v[1]
    return Mdl.local_matrix(dict(s=v[0:3], r=v[3:6], t=v[6:9]))
ROOT = {}
with open(os.path.join(out, 'sora_anim.bin'), 'wb') as f:
    f.write(struct.pack('<4sIIIf', b'SORA', 1, len(MS), len(used), FPS))
    for mi, mo in enumerate(MS):
        if mo is None:
            f.write(struct.pack('<If', 0, 0.0)); continue
        dur = mo['frames'] / mo['fps']
        nf = max(1, int(round(dur * FPS)))
        f.write(struct.pack('<If', nf, dur))
        frames = []
        for k in range(nf):
            t = k / FPS * mo['fps']
            L = A.sample(mo, joints, t)
            frames.append(A.solve_ik(mo, joints, L, t, lm))
        # root motion: horizontal travel of joint 0 relative to frame 0 (KH cm). Large travel (attacks) is removed from
        # the mesh and handed to the mod, which moves the real character instead (no drift-and-snap).
        r0 = frames[0][0][:3, 3]
        root = [[float(W[0][0, 3] - r0[0]), float(W[0][2, 3] - r0[2])] for W in frames]
        strip = max(abs(r[1]) for r in root) > 40
        ROOT[mi] = root if strip else None
        for W, r in zip(frames, root):
            for j in used:
                S = (W[j] @ Wbi[j])[:3, :].copy()
                if strip:
                    S[0, 3] -= r[0]; S[2, 3] -= r[1]
                f.write(S.astype('<f4').tobytes())
        if mi % 20 == 0: print('motion', mi, nf, 'frames')
print('done', os.path.getsize(os.path.join(out, 'sora_anim.bin')) // 1024, 'KB')

ev_path = os.path.join(out, 'sora_events.json')
E = json.load(open(ev_path))
for mi, r in ROOT.items():
    if E[mi] is not None:
        E[mi]['root'] = r     # per baked frame (FPS) [x, z] in KH cm; None = no root motion
json.dump(E, open(ev_path, 'w'))
print('root motion motions', [mi for mi, r in ROOT.items() if r])
