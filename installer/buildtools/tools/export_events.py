# Writes sora_events.json: per motion frames/fps + event tracks (hitbox on/off, combo window, release frame, spell id)
import struct, json, sys
b = open('D:/SoraBL/kh1_extract/kh1_first/original/xa_ex_0010.mset', 'rb').read()
tbl = 0xC0; offs = []; o = tbl
while True:
    v = struct.unpack_from('<I', b, o)[0]
    if v == 0xFFFFFFFF: break
    offs.append(v); o += 4
out = []
for i, off in enumerate(offs):
    m = tbl + off; H = struct.unpack_from('<16I', b, m)
    if H[4] == 0 or H[4] > 1000:
        out.append(None); continue
    p = m + H[15]; recs = []
    while struct.unpack_from('<I', b, p)[0] != 0xFFFFFFFF:
        t, c, _ = struct.unpack_from('<3I', b, p); recs.append((t, c)); p += 12
    p += 4
    ev = {}
    for t, c in recs:
        ents = []
        for k in range(c):
            fr, par = struct.unpack_from('<fI', b, p); ents.append([fr, par]); p += 16
        ev.setdefault('%x' % t, []).extend(ents)
    def window(key):
        e = ev.get(key)
        if not e: return None
        on = [f for f, v in e if v == 1]; off_ = [f for f, v in e if v == 0]
        return [on[0] if on else 0.0, off_[0] if off_ else float(H[1])]
    rel = ev.get('0')
    sid = [v >> 16 for f, v in ev.get('60000', []) if (v >> 16) >= 0x2AF0 and (v >> 16) < 0x2C00]
    out.append({'frames': H[1], 'fps': struct.unpack_from('<f', b, m + 8)[0], 'hit': window('10000'), 'combo': window('50000'),
                'w7': window('70000'), 'release': rel[0][0] if rel else None, 'spell': (sid[0] - 0x2B00) if sid else None})
json.dump(out, open(sys.argv[1], 'w'), indent=0)
print('motions', len(out), [(i, e['spell'], e['release']) for i, e in enumerate(out) if e and e['spell'] is not None])
