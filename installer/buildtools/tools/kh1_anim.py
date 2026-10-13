# KH1 Final Mix .mset (MMTN) motion reader + evaluator (reverse-engineered, see MODLOG).
# Motion header (u32 x16 at motion start):
#  [1] frame count  [2] fps (float)  [4] joint count  [5] per-joint flags offset
#  [6] fcurve count [7] fcurve offset  [8] IK-helper fcurve count [9] its offset
#  [10] key offset  [11] IK-helper count [12] IK-helper table offset (40 B each)
#  [13] static pose count [14] static pose offset  [15] trigger/footer offset
# fcurve: i16 joint, u8 channel (low 4 bits; high bits = loop flags), u8 key count, i16 first key.  channel 1-3 scale, 4-6 rot xyz (rad), 7-9 trans xyz
# key (16 B): u16 type (2 = hermite), u16 frame, f32 value, f32 tangent in, f32 tangent out
# static pose (8 B): i16 joint, i16 channel, f32 value — overrides the model's bind values
import struct, json, sys
import numpy as np

def motions(path):
    b = open(path, 'rb').read()
    base = 0x80
    assert b[base:base + 4] == b'MMTN'
    tbl = base + 0x40
    offs = []
    o = tbl
    while True:
        v = struct.unpack_from('<I', b, o)[0]
        if v == 0xFFFFFFFF: break
        offs.append(v); o += 4
    out = []
    for i, off in enumerate(offs):
        m = tbl + off
        H = struct.unpack_from('<16I', b, m)
        if H[4] == 0 or H[4] > 1000:
            out.append(None); continue
        frames, fps = H[1], struct.unpack_from('<f', b, m + 8)[0]
        def curves(n, at):
            # channel byte: low 4 bits = channel, upper bits = pre/post infinity (loop) flags, as in KH2
            out = []
            for k in range(n):
                j, ch, cnt, st = struct.unpack_from('<hBBh', b, m + at + 6 * k)
                out.append((j, ch, cnt, st))   # full byte kept: low 4 bits channel, high 4 bits loop flags
            return out
        def keys(c):
            j, ch, n, s = c
            # key frame is SIGNED (KH1 stores pre-roll keys at negative frames)
            return [struct.unpack_from('<Hhfff', b, m + H[10] + 16 * k) for k in range(s, s + n)]
        fc = [(c, keys(c)) for c in curves(H[6], H[7])]
        ik = [(c, keys(c)) for c in curves(H[8], H[9])]
        static = [struct.unpack_from('<hhf', b, m + H[14] + 8 * k) for k in range(H[13])]
        flags = struct.unpack_from('<%dI' % H[4], b, m + H[5])
        ikh = [b[m + H[12] + 40 * k: m + H[12] + 40 * k + 40] for k in range(H[11])]
        out.append(dict(index=i, frames=frames, fps=fps, joints=H[4], fcurves=fc, ik=ik, static=static, flags=flags, ikh=ikh))
    return out

def hermite(keys, t):
    if t <= keys[0][1]: return keys[0][2]
    if t >= keys[-1][1]: return keys[-1][2]
    for a, c in zip(keys, keys[1:]):
        if a[1] <= t <= c[1]:
            d = c[1] - a[1]
            if d == 0: return c[2]
            s = (t - a[1]) / d
            if a[0] == 0: return a[2]
            if a[0] == 1: return a[2] + (c[2] - a[2]) * s
            h00 = 2 * s ** 3 - 3 * s ** 2 + 1; h10 = s ** 3 - 2 * s ** 2 + s
            h01 = -2 * s ** 3 + 3 * s ** 2; h11 = s ** 3 - s ** 2
            return h00 * a[2] + h10 * d * a[4] + h01 * c[2] + h11 * d * c[3]
    return keys[-1][2]

WEAPON_JOINTS = (280, 281, 282, 283)
WEAPON_DEFAULT = None   # {joint: [9 locals]} grip used when a motion leaves the weapon joints untouched (walk/run)


def sample(mo, bind_joints, t):
    """Local (s, r, t) per joint at frame t."""
    L = [[*j['s'], *j['r'], *j['t']] for j in bind_joints]
    if WEAPON_DEFAULT is not None and not mo.get('_has_weapon'):
        touched = any(j in WEAPON_JOINTS for j, _, _ in mo['static']) or any(c[0] in WEAPON_JOINTS for c, _ in mo['fcurves'])
        mo['_has_weapon'] = 'yes' if touched else 'no'
    if WEAPON_DEFAULT is not None and mo.get('_has_weapon') == 'no':
        for j, v in WEAPON_DEFAULT.items():
            L[j] = list(v)
    for j, ch, v in mo['static']:
        if 0 <= j < len(L) and 1 <= ch <= 9: L[j][ch - 1] = v
    for (j, chb, n, s), ks in mo['fcurves']:
        ch = chb & 0x0F
        if 0 <= j < len(L) and 1 <= ch <= 9 and ks: L[j][ch - 1] = hermite(ks, curve_time(chb, ks, t))
    return L


def curve_time(chb, ks, t):
    """Looping curves (high bits of the channel byte) repeat over their key span."""
    if (chb & 0xF0) and len(ks) > 1:
        a, b = ks[0][1], ks[-1][1]
        if b > a:
            return a + (t - a) % (b - a)
    return t

# ---------------- IK (reverse-engineered from joint flags) ----------------
# flags: bits 11-15 = IK target helper (1-based) on a chain END; bits 20-23: 2 = one-bone chain root,
# 4/6 = two-bone chain root, 8 = chain end.  Helpers (40 B): scale xyz, parent (-1 root), rot xyz, trans xyz.
import numpy as np

def _rot(axis, a):
    c, s = np.cos(a), np.sin(a); m = np.eye(4)
    if axis == 0: m[1:3, 1:3] = [[c, -s], [s, c]]
    if axis == 1: m[0, 0], m[0, 2], m[2, 0], m[2, 2] = c, s, -s, c
    if axis == 2: m[0:2, 0:2] = [[c, -s], [s, c]]
    return m

def srt(v):
    S = np.diag([v[0], v[1], v[2], 1.0]); T = np.eye(4); T[:3, 3] = v[6:9]
    return T @ _rot(2, v[5]) @ _rot(1, v[4]) @ _rot(0, v[3]) @ S

def helpers(mo, t):
    H = []
    for raw in mo['ikh']:
        v = struct.unpack('<3fi3f3f', raw[:40]) if len(raw) >= 40 else None
        H.append([v[0], v[1], v[2], v[4], v[5], v[6], v[7], v[8], v[9], v[3]])
    for (j, chb, n, s), ks in mo['ik']:
        ch = chb & 0x0F
        if 0 <= j < len(H) and 1 <= ch <= 9 and ks: H[j][ch - 1] = hermite(ks, curve_time(chb, ks, t))
    W = []
    for h in H:
        M = srt(h[:9]); p = h[9]
        W.append(M if p < 0 or p >= len(W) else W[p] @ M)
    return W

def _aim(Wp, Wj_local_fn):
    pass

def solve_ik(mo, joints, L, t, local_matrix):
    n = len(joints); par = [j['parent'] for j in joints]
    fl = mo['flags']; HW = helpers(mo, t)
    def fk(L):
        W = [None] * n
        for i in range(n):
            M = local_matrix(i, L[i]); p = par[i]
            W[i] = M if (p == 0x3FF or p >= n) else W[p] @ M
        return W
    # IK chain roots: the solver owns their rotation, so animated/static rotation on them is discarded
    if ROOT_RESET:
        for i in range(n):
            if i < len(fl) and ((fl[i] >> 20) & 15) in ROOT_RESET and not isinstance(L[i], tuple) and BIND_LOCAL is not None:
                L[i] = list(L[i]); L[i][3:6] = BIND_LOCAL[i]
    # joints with a target but not a chain end (the hips): take the helper's position
    for i in range(n):
        if i < len(fl) and ((fl[i] >> 11) & 31) and ((fl[i] >> 20) & 15) != 8:
            h = ((fl[i] >> 11) & 31) - 1
            if h < len(HW) and not isinstance(L[i], tuple) and not NO_HIPS:
                p = par[i]
                if p == 0x3FF or p >= n:
                    if HIPS_ROT and BIND is not None:
                        M = HW[h].copy(); M[:3, :3] = HW[h][:3, :3] @ BIND[i][:3, :3]   # helper rotation on top of bind
                        L[i] = ('M', M)
                    else:
                        L[i] = list(L[i]); L[i][6:9] = list(HW[h][:3, 3])
    W = fk(L)
    def chain_root(e):
        i = par[e]
        while i < n:
            if ((fl[i] >> 20) & 15) in (2, 4, 6): return i
            i = par[i]
        return None
    ends = [i for i in range(n) if i < len(fl) and ((fl[i] >> 11) & 31) and ((fl[i] >> 20) & 15) == 8]
    for e in ends:
        tgt = ((fl[e] >> 11) & 31) - 1
        if tgt >= len(HW): continue
        T = HW[tgt][:3, 3]
        r = chain_root(e)
        if r is None: continue
        kind = (fl[r] >> 20) & 15
        # rotate helper: apply world-space rotation R to joint k (keeping its position), then refresh subtree
        def rotate_joint(k, R):
            Wk = W[k].copy(); Wk[:3, :3] = R @ Wk[:3, :3]
            p = par[k]; PW = W[p] if p < n else np.eye(4)
            loc = np.linalg.inv(PW) @ Wk
            L[k] = ('M', loc)
        def refresh():
            nonlocal W; W = fk(L)
        def rot_between(a, b):
            a = a / (np.linalg.norm(a) + 1e-12); b = b / (np.linalg.norm(b) + 1e-12)
            v = np.cross(a, b); c = np.dot(a, b)
            if c < -0.9999: return -np.eye(3)
            k = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
            return np.eye(3) + k + k @ k / (1 + c)
        if kind == 2 and NO_AIM:
            continue
        if kind == 2:
            R = rot_between(W[e][:3, 3] - W[r][:3, 3], T - W[r][:3, 3]); rotate_joint(r, R); refresh()
        else:
            # two-bone: r -> m -> e
            m = par[e]
            A, B, Cc = W[r][:3, 3], W[m][:3, 3], W[e][:3, 3]
            la, lb = np.linalg.norm(B - A), np.linalg.norm(Cc - B)
            d = np.clip(np.linalg.norm(T - A), abs(la - lb) + 1e-4, la + lb - 1e-4)
            ax = (T - A) / (np.linalg.norm(T - A) + 1e-12)
            if KNEE_FWD is not None and e in LEG_ENDS:
                # knees bend toward the pelvis' forward direction (KH1 legs never splay sideways)
                fwd = W[0][:3, :3] @ KNEE_FWD
                up = fwd - ax * np.dot(fwd, ax)
                if np.linalg.norm(up) < 1e-6: up = W[0][:3, :3] @ np.array([0.0, 0.0, 1.0])
                up /= (np.linalg.norm(up) + 1e-12)
            else:
                pole = np.cross(B - A, Cc - B)
                if np.linalg.norm(pole) < 1e-6: pole = W[r][:3, 2]
                pole /= np.linalg.norm(pole)
                up = np.cross(pole, ax); up /= (np.linalg.norm(up) + 1e-12)
                if np.dot(up, B - A) < 0: up = -up
            x = (la * la - lb * lb + d * d) / (2 * d); y = np.sqrt(max(la * la - x * x, 0))
            Bn = A + ax * x + up * y
            R1 = rot_between(B - A, Bn - A); rotate_joint(r, R1); refresh()
            B2, C2 = W[m][:3, 3], W[e][:3, 3]
            R2 = rot_between(C2 - B2, A + ax * d - B2); rotate_joint(m, R2); refresh()
        # end effector orientation: children of the chain end with an orient flag copy the target helper's
        # rotation on top of their bind (model-space) orientation
        kids = [k for k in range(n) if par[k] == e and (fl[k] & 0xF0) in (0x10, 0x20)]
        hand = HAND_MODE and e in HAND_ENDS and mo.get('_has_weapon') == 'no'   # only motions without their own grip
        if hand:
            kids = [k for k in range(n) if par[k] == e]
        for c in ([] if not FOOT_COPY else kids):
            Rb = BIND[c][:3, :3] if BIND is not None else np.eye(3)
            H = HW[tgt][:3, :3]
            mode = HAND_MODE if hand else FOOT_MODE
            Rw = {'bind': H @ Rb, 'helper': H, 'fkyaw': H @ W[c][:3, :3]}[mode]
            rotate_joint(c, Rw @ np.linalg.inv(W[c][:3, :3])); refresh()
    return W

BIND = None
FOOT_COPY = False
FOOT_MODE = 'helper'

NO_AIM = False
NO_HIPS = False

ROOT_RESET = ()
BIND_LOCAL = None

KNEE_FWD = None   # forward direction in joint 0's local frame (set by the caller)
LEG_ENDS = (19, 45)
HIPS_ROT = False
HAND_MODE = None   # None / 'helper' / 'bind'
HAND_ENDS = (95, 200)
