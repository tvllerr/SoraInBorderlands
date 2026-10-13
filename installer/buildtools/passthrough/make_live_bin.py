# sora_live.bin: what the renderer needs to turn KH1's live joint matrices into skinning matrices.
#   'SORL', u32 version, u32 bones, u32 kh_joints; then per palette bone: u32 joint, 16 f32 inverse bind (row-major, column-vector convention)
import json, struct, numpy as np
D = json.load(open('D:/SoraBL/work/sora.json'))
used = sorted(set(int(j) for m in D['meshes'] for jj in m['J'] for j in jj[:1]))   # same order as bake_sora.py palette
with open('D:/SoraBL/bake/sora_live.bin', 'wb') as f:
    f.write(struct.pack('<4sIII', b'SORL', 1, len(used), len(D['joints'])))
    for j in used:
        f.write(struct.pack('<I16f', j, *np.linalg.inv(np.array(D['world'][j])).astype(np.float32).ravel()))
print(len(used), 'bones')
