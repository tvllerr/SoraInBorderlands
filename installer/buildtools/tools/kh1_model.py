# KH1 Final Mix (PC) character -> glTF: skeleton from the original .mdls, HD mesh from the remastered .cvbl.
# uv run --with numpy --with pillow python kh1_model.py <orig.mdls> <remastered dir> <out.glb>
import sys, struct, json, os, glob
import numpy as np

def u32(b, o): return struct.unpack_from('<I', b, o)[0]
def i32(b, o): return struct.unpack_from('<i', b, o)[0]

# ---------------- MDLS skeleton ----------------
def read_joints(path):
    b = open(path, 'rb').read()
    base = 0x80
    assert b[base:base + 4] == b'MOBJ'
    hdr = struct.unpack_from('<14i', b, base + 8)
    model_off = hdr[6]
    mh = base + model_off
    jcount, joff, _, mcount = struct.unpack_from('<4i', b, mh)
    mesh_tex = [struct.unpack_from('<4i', b, mh + 16 + 16 * i)[1] for i in range(mcount)]
    joints = []
    for i in range(jcount):
        o = mh + joff + 48 * i
        sx, sy, sz = struct.unpack_from('<3f', b, o)
        idx = u32(b, o + 12)
        rx, ry, rz = struct.unpack_from('<3f', b, o + 16)
        tx, ty, tz = struct.unpack_from('<3f', b, o + 32)
        h = u32(b, o + 44)
        parent = h & 0x3FF
        joints.append(dict(i=i, idx=idx, s=(sx, sy, sz), r=(rx, ry, rz), t=(tx, ty, tz), parent=parent, h=h))
    return joints, mesh_tex

def rot(axis, a):
    c, s = np.cos(a), np.sin(a)
    m = np.eye(4)
    if axis == 0: m[1:3, 1:3] = [[c, -s], [s, c]]
    if axis == 1: m[0, 0], m[0, 2], m[2, 0], m[2, 2] = c, s, -s, c
    if axis == 2: m[0:2, 0:2] = [[c, -s], [s, c]]
    return m

def local_matrix(j):
    # column-vector convention: M = T * Rz * Ry * Rx * S  (System.Numerics row-vector S*Rx*Ry*Rz*T)
    S = np.diag([*j['s'], 1.0]); T = np.eye(4); T[:3, 3] = j['t']
    return T @ rot(2, j['r'][2]) @ rot(1, j['r'][1]) @ rot(0, j['r'][0]) @ S

def world_matrices(joints):
    W = [None] * len(joints)
    def w(i):
        if W[i] is None:
            p = joints[i]['parent']
            L = local_matrix(joints[i])
            W[i] = L if (p == 0x3FF or p == i or p >= len(joints)) else w(p) @ L
        return W[i]
    for i in range(len(joints)): w(i)
    return W

# ---------------- CVBL mesh ----------------
def read_cvbl(path):
    b = open(path, 'rb').read()
    nmesh = u32(b, 4); nunk, hasunk = struct.unpack_from('<HH', b, 8)
    off = 16 + (nunk * 32 if hasunk == 1 else 0)
    entries = [struct.unpack_from('<HHiiI', b, off + 16 * i) for i in range(nmesh)]
    subs = []
    for (_, style, material, _, moff) in entries:
        slots = {}
        slotmat = {}
        verts = []
        faces = []
        so = moff + 16
        while so < len(b):
            typ, ln = u32(b, so), u32(b, so + 4)
            d = so + 8
            if typ == 1:
                n = u32(b, d)
                vo = d + 24
                for k in range(n):
                    if style == 8:
                        nx, ny, nz = struct.unpack_from('<3f', b, vo)
                        slot, ft, uf = struct.unpack_from('<HBB', b, vo + 12)
                        u, v = struct.unpack_from('<2f', b, vo + 16)
                        px, py, pz, wgt = struct.unpack_from('<4f', b, vo + 32)
                        js, ws, sz = [slot], [1.0], 48
                    elif style == 9:
                        nx, ny, nz = struct.unpack_from('<3f', b, vo)
                        cnt, ft, uf = struct.unpack_from('<HBB', b, vo + 12)
                        u, v = struct.unpack_from('<2f', b, vo + 16)
                        ids = struct.unpack_from('<8B', b, vo + 24)
                        px, py, pz, pw = struct.unpack_from('<4f', b, vo + 32)
                        wts = struct.unpack_from('<8f', b, vo + 48)
                        js, ws, sz = list(ids[:cnt]), list(wts[:cnt]), 80
                    elif style == 10:
                        px, py, pz = struct.unpack_from('<3f', b, vo)
                        slot, ft, uf = struct.unpack_from('<HBB', b, vo + 12)
                        nx = ny = nz = 0.0; u = v = 0.0; js, ws, sz = [slot], [1.0], 16
                    else:
                        raise ValueError('joint style %d' % style)
                    vi = len(verts)
                    verts.append(dict(p=(px, py, pz), n=(nx, ny, nz), uv=(u, v),
                                      j=[slots.get(s, 0) for s in js], w=ws,
                                      m=[slotmat.get(s) for s in js]))
                    if uf == 0:
                        if ft == 1: faces.append((vi - 2, vi - 1, vi))
                        elif ft == 2: faces.append((vi, vi - 1, vi - 2))
                    vo += sz
            elif typ == 17:
                jid, slot = u32(b, d), u32(b, d + 4)
                slots[slot] = jid
                if style == 9:
                    m = np.array(struct.unpack_from('<16f', b, d + 8 + 28 * 4), dtype=float).reshape(4, 4)
                    slotmat[slot] = m
            elif typ == 0x8000:
                break
            else:
                print('unknown subsection', typ, 'at', so); break
            so += ln
        subs.append(dict(style=style, material=material, verts=verts, faces=faces))
    return subs

def main():
    mdls, rdir, out = sys.argv[1:4]
    joints, mesh_tex = read_joints(mdls)
    W = world_matrices(joints)
    print('joints', len(joints), 'mesh tex', mesh_tex)
    for j in joints[:12]: print(j['i'], j['idx'], j['parent'], hex(j['h']), np.round(j['t'], 2), np.round(j['r'], 2), j['s'])
    cv = sorted(glob.glob(os.path.join(rdir, '*.cvbl')), key=os.path.getsize)[-1]
    subs = read_cvbl(cv)
    for s in subs:
        print('sub style', s['style'], 'mat', s['material'], 'verts', len(s['verts']), 'faces', len(s['faces']),
              'joints', sorted(set(x for v in s['verts'] for x in v['j']))[:12])
    meshes, W = bake(joints, subs, mesh_tex, texpath=lambda t: os.path.join(rdir, '-1c%d.dds' % t))
    # Kingdom Key: weapon joints k are the wielder's joints 280+k (the last 13); HD atlas -9b40
    wdir = os.path.join(os.path.dirname(rdir), 'xw_ex_5010.wpn')
    wsubs = read_cvbl(os.path.join(wdir, '-9a40.cvbl'))
    wm, _ = bake(joints, wsubs, [0], joint_base=280, texpath=lambda t: os.path.join(wdir, '-9b40.dds'))
    for m in wm: m['tex'] = 100 + m['tex']
    meshes += wm
    json.dump({"joints": joints, "world": W, "meshes": meshes}, open(out + ".json", "w"))
    print("weights", sorted(set(round(x, 2) for m in meshes for w in m["W"] for x in w))[:20])

def bake(joints, subs, mesh_tex, joint_base=0, texpath=None):
    W = world_matrices(joints)
    out = []
    for si, s in enumerate(subs):
        P, N, UV, J, WT = [], [], [], [], []
        for v in s['verts']:
            p = np.array([*v['p'], 1.0]); n = np.array([*v['n'], 0.0])
            acc, nacc = np.zeros(4), np.zeros(4)
            v = dict(v, j=[jj + joint_base for jj in v['j']])
            for jid, w, m in zip(v['j'], v['w'], v['m']):
                M = W[jid] if m is None else W[jid] @ m
                acc += w * (M @ p) if len(v['j']) > 1 else M @ p
                nacc += (M @ n)
            P.append(acc[:3]); nn = nacc[:3]; N.append(nn / (np.linalg.norm(nn) or 1)); UV.append(v['uv'])
            J.append(v['j'][:4] + [0] * (4 - len(v['j'][:4]))); WT.append(v['w'][:4] + [0] * (4 - len(v['w'][:4])))
        out.append(dict(tex=s['material'], texpath=(texpath(s['material']) if texpath else None), P=np.array(P).tolist(), N=np.array(N).tolist(),
                        UV=UV, J=J, W=WT, F=s['faces']))
    return out, [w.tolist() for w in W]

if __name__ == '__main__':
    main()
