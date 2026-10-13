# Bakes KH1's 3 ITEM BALL models (prize.ard, from YOUR KH1 install's extract) for the renderer's STATIC model path:
# ex_6000 / 6001 / 6002 _mesh.bin + _live.bin (same formats as make_summon.py). Drawn with sr_set_enemy(slot, 0, id)
# = no KH1 entity: identity pose, placed by logic.py (item_drops_tick). Model = item record byte 3 (class 0..8) // 3.
# Sources: prize.ard remastered -240 / -95c0 / -12940 .cvbl (one joint, 180 verts, origin = the ball's bottom),
# textures -340 (gold / purple), -96c0 (red), -12a40 (blue); the second dds of each pair is a duplicate.
# uv run --no-project --with numpy --with pillow python make_items.py [out dir]
import glob, os, struct, sys
import numpy as np
from PIL import Image
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kh1_model as M

X = 'D:/SoraBL/kh1_extract'
ITEMS = [(6000, '-240', '-340'), (6001, '-95c0', '-96c0'), (6002, '-12940', '-12a40')]
out = sys.argv[1] if len(sys.argv) > 1 else 'D:/SoraBL/bake'
os.makedirs(out, exist_ok=True)
hits = glob.glob(os.path.join(X, 'kh1_*', 'remastered', 'prize.ard'))
if not hits:
    sys.exit('prize.ard missing in the extract')
rdir = hits[0]
for mid, cv, tex in ITEMS:
    V, I = [], []
    for s in M.read_cvbl(os.path.join(rdir, cv + '.cvbl')):
        base = len(V)
        for v in s['verts']:
            V.append((*v['p'], *v['n'], v['uv'][0], v['uv'][1], 0))
        for f in s['faces']:
            I += [base + f[0], base + f[1], base + f[2]]
    with open(os.path.join(out, f'ex_{mid:04d}_mesh.bin'), 'wb') as f:
        f.write(struct.pack('<4sIIIII', b'SORM', 1, len(V), len(I), 1, 1))
        for v in V:
            f.write(struct.pack('<8fI', *v))
        f.write(struct.pack('<%dI' % len(I), *I))
        f.write(struct.pack('<III', 0, 0, len(I)))
        im = Image.open(os.path.join(rdir, tex + '.dds')).convert('RGBA')
        f.write(struct.pack('<II', *im.size)); f.write(im.tobytes())
    with open(os.path.join(out, f'ex_{mid:04d}_live.bin'), 'wb') as f:
        f.write(struct.pack('<4sIII', b'SORL', 1, 1, 1))
        f.write(struct.pack('<I16f', 0, *np.eye(4, dtype=np.float32).ravel()))
    print('item model', mid, 'from', cv, tex, 'verts', len(V), 'tris', len(I) // 3)
