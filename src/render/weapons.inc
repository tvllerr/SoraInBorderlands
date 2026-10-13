// ------------------------------------------------------------------ v58: the weapon KH1 has equipped
// tools/bake_weapons.py bakes every KH1 weapon as its own mesh (weapons\wpn_<who>_<id>.bin, SORM like the body meshes,
// bones = indices into that character's palette). Logic tells which one KH1 has loaded (sr_set_weapon(who, xw_ex id),
// who 0 Sora, 1 Donald, 2 Goofy, 0 = keep the baked one); the body is then drawn without its baked weapon parts (subs
// whose vertices ride the weapon joints: Sora >= 280, Donald >= 187, Goofy >= 189) plus that mesh, same bones.
static Actor g_wpn[3]; static volatile LONG g_wpnWant[3] = {0, 0, 0}; static int g_wpnHave[3] = {0, 0, 0};
static std::vector<char> g_wpnSkip[3]; static bool g_wpnSkipOk[3] = {false, false, false};
static const char* kWpnWho[3] = {"sora", "donald", "goofy"};
extern "C" __declspec(dllexport) void sr_set_weapon(int who, int id) { if (who >= 0 && who < 3) g_wpnWant[who] = id; }
extern "C" __declspec(dllexport) int sr_weapon_state(int who) { return (who >= 0 && who < 3) ? g_wpnHave[who] : 0; }
static bool wpn_load(Actor& a, const char* fn) {
    std::vector<uint8_t> b;
    if (!read_file(fn, b) || b.size() < 24 || memcmp(b.data(), "SORM", 4) != 0) return false;
    const uint8_t* p = b.data() + 4; rd<uint32_t>(p);
    uint32_t nv = rd<uint32_t>(p), ni = rd<uint32_t>(p), ns = rd<uint32_t>(p), nt = rd<uint32_t>(p);
    if (b.size() < 24 + (size_t)nv * sizeof(Vtx) + (size_t)ni * 4 + (size_t)ns * sizeof(Sub)) return false;
    a.v.resize(nv); memcpy(a.v.data(), p, nv * sizeof(Vtx)); p += nv * sizeof(Vtx);
    a.i.resize(ni); memcpy(a.i.data(), p, ni * 4); p += ni * 4;
    a.subs.resize(ns); memcpy(a.subs.data(), p, ns * sizeof(Sub)); p += ns * sizeof(Sub);
    a.tex.resize(nt);
    const uint8_t* end = b.data() + b.size();
    for (auto& t : a.tex) {
        if (p + 8 > end) return false;
        t.w = rd<uint32_t>(p); t.h = rd<uint32_t>(p);
        if (p + (size_t)t.w * t.h * 4 > end) return false;
        t.rgba.assign(p, p + (size_t)t.w * t.h * 4); p += (size_t)t.w * t.h * 4;
    }
    return true;
}
static void wpn_free(Actor& a) {
    if (a.vb) { a.vb->Release(); a.vb = nullptr; } if (a.ib) { a.ib->Release(); a.ib = nullptr; } for (auto* s : a.srvs) if (s) s->Release();
    a.srvs.clear(); a.v.clear(); a.i.clear(); a.subs.clear(); a.tex.clear();
}
// render thread: the weapon mesh to draw for `who` (nullptr = keep the body's baked weapon)
static Actor* wpn_get(int who) {
    int want = g_wpnWant[who];
    if (want != g_wpnHave[who] && want != -g_wpnHave[who]) {
        wpn_free(g_wpn[who]);
        g_wpnHave[who] = want;
        if (want > 0) {
            char fn[96]; snprintf(fn, sizeof fn, "weapons\\wpn_%s_%d.bin", kWpnWho[who], want);
            if (wpn_load(g_wpn[who], fn)) { init_actor_gpu(g_wpn[who]); logf("weapon %s -> %d (%zu verts)", kWpnWho[who], want, g_wpn[who].v.size()); }
            else { logf("weapon %s -> %d: %s missing", kWpnWho[who], want, fn); g_wpnHave[who] = -want; }
        }
        if (who == 0) {   // v59: hit detection + swing trail follow the Keyblade actually drawn
            if (g_wpnHave[0] > 0 && !g_wpn[0].v.empty()) find_blade_in(g_wpn[0].v, "equipped");
            else find_blade_in(g_v, "baked");
        }
    }
    return (g_wpnHave[who] > 0 && g_wpn[who].vb) ? &g_wpn[who] : nullptr;
}
// which subs of the body mesh are its baked weapon
static const std::vector<char>* wpn_skip(int who) {
    if (g_wpnSkipOk[who]) return &g_wpnSkip[who];
    const std::vector<Vtx>& V = who == 0 ? g_v : g_actors[who - 1].v;
    const std::vector<uint32_t>& I = who == 0 ? g_i : g_actors[who - 1].i;
    const std::vector<Sub>& S = who == 0 ? g_subs : g_actors[who - 1].subs;
    const std::vector<LiveBone>& L = who == 0 ? g_live : g_actors[who - 1].live;
    uint32_t wj = who == 0 ? 280u : g_actors[who - 1].weaponJ;
    if (V.empty() || I.empty() || S.empty() || L.empty() || !wj) return nullptr;
    std::vector<char>& sk = g_wpnSkip[who]; sk.assign(S.size(), 0);
    int n = 0;
    for (size_t s = 0; s < S.size(); s++) {
        if (S[s].start >= I.size()) continue;
        uint32_t vi = I[S[s].start]; if (vi >= V.size()) continue;
        uint32_t bone = V[vi].bone; if (bone >= L.size()) continue;
        if (L[bone].j >= wj) { sk[s] = 1; n++; }
    }
    g_wpnSkipOk[who] = true;
    logf("weapon %s: %d of %zu body subs are the baked weapon", kWpnWho[who], n, S.size());
    return &sk;
}
// draw a record's subs (minus its baked weapon when another weapon replaces it) and then that weapon, same bones
static void draw_rec_subs(ID3D11Buffer* vbuf, ID3D11Buffer* ibuf, const std::vector<Sub>& subs,
                          const std::vector<ID3D11ShaderResourceView*>& sv, const std::vector<char>* skip, Actor* w) {
    UINT st = sizeof(Vtx), of = 0;
    ctx->IASetVertexBuffers(0, 1, &vbuf, &st, &of); ctx->IASetIndexBuffer(ibuf, DXGI_FORMAT_R32_UINT, 0);
    for (size_t s = 0; s < subs.size(); s++) {
        if (w && skip && s < skip->size() && (*skip)[s]) continue;
        ID3D11ShaderResourceView* t = subs[s].tex < sv.size() ? sv[subs[s].tex] : nullptr;
        ctx->PSSetShaderResources(0, 1, &t); ctx->DrawIndexed(subs[s].count, subs[s].start, 0);
    }
    if (w && w->vb && w->ib) {
        ctx->IASetVertexBuffers(0, 1, &w->vb, &st, &of); ctx->IASetIndexBuffer(w->ib, DXGI_FORMAT_R32_UINT, 0);
        for (auto& sb : w->subs) {
            ID3D11ShaderResourceView* t = sb.tex < w->srvs.size() ? w->srvs[sb.tex] : nullptr;
            ctx->PSSetShaderResources(0, 1, &t); ctx->DrawIndexed(sb.count, sb.start, 0);
        }
    }
}
