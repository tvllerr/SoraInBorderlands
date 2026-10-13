# Bakes Donald's and Goofy's Neverland flight motions (xa_ex_00X0_pp.mset, from YOUR KH1 install's extract) into
# <name>_fly.bin for the renderer: skinning rows (3x4) per frame for the same palette as <name>_live.bin, so the
# renderer can draw them flying behind gliding Sora (KH1 has no glide motion for them).
#   'SORF', version, motion count, palette size, fps; per motion: frame count, duration, frames x palette x 12 floats
# uv run --no-project --with numpy --with pillow python make_party_fly.py <out dir>
import glob, os, struct, sys
import numpy as np
sys.path.insert(0, 'D:/SoraBL/tools')
import kh1_anim as A, kh1_model as M

X = 'D:/SoraBL/kh1_extract'
out = sys.argv[1] if len(sys.argv) > 1 else 'D:/SoraBL/bake'
FPS = 30.0


def find(rel):
    hits = glob.glob(os.path.join(X, 'kh1_*', rel))
    return hits[0] if hits else None


def lm(i, v):
    if isinstance(v, tuple):
        return v[1]
    return M.local_matrix(dict(s=v[0:3], r=v[3:6], t=v[6:9]))


for name, mid in (("donald", "0040"), ("goofy", "0050")):
    joints, _ = M.read_joints(find(f'original/xa_ex_{mid}.mdls'))
    Wb = M.world_matrices(joints)
    A.BIND = Wb
    A.BIND_LOCAL = [j['r'] for j in joints]
    A.ROOT_RESET = (2,)
    A.FOOT_COPY = True
    A.FOOT_MODE = 'helper'
    A.WEAPON_DEFAULT = None
    A.KNEE_FWD = None
    A.HAND_MODE = None
    # palette = the order in <name>_live.bin
    lb = open(os.path.join(out, f'{name}_live.bin'), 'rb').read()
    npal = struct.unpack_from('<I', lb, 8)[0]
    pal = [struct.unpack_from('<I', lb, 16 + k * 68)[0] for k in range(npal)]
    ib = [np.array(struct.unpack_from('<16f', lb, 16 + k * 68 + 4)).reshape(4, 4) for k in range(npal)]
    MS = [m for m in A.motions(find(f'original/xa_ex_{mid}_pp.mset')) if m]
    # v2 (user 2026-10-05): + their battle "Superman" air dash from the normal field set (KH1 party catch-up: anim 9
    # jump -> 10 -> 113 dash -> 6 land) = motion 2, and the jump = motion 3
    main = A.motions(find(f'original/xa_ex_{mid}.mset'))
    # KH1 anim byte (+0x164) -> motion (+0x168), read live 2026-10-05: 113 -> Donald 64 / Goofy 62, jump 9 -> 8
    for k in ({"donald": 64, "goofy": 62}[name], 8):
        if k < len(main) and main[k]:
            MS.append(main[k])
    with open(os.path.join(out, f'{name}_fly.bin'), 'wb') as f:
        f.write(struct.pack('<4sIIIf', b'SORF', 1, len(MS), npal, FPS))
        for mo in MS:
            dur = mo['frames'] / mo['fps']
            nf = max(1, int(round(dur * FPS)))
            f.write(struct.pack('<If', nf, dur))
            for k in range(nf):
                t = k / FPS * mo['fps']
                W = A.solve_ik(mo, joints, A.sample(mo, joints, t), t, lm)
                for j, ibj in zip(pal, ib):
                    f.write((W[j] @ ibj)[:3, :].astype('<f4').tobytes())
            print(name, 'motion', mo['index'], nf, 'frames', round(dur, 2), 's')
