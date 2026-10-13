// ------------------------------------------------------------------ v57: KH1's main-menu party models in Borderlands
// While KH1's main menu (F9) is open, KH1 poses three extra skeletons from its menu model updater (kh_skel_eval called
// from 0x2DBB33; kh1_fx v16 logs them in Local\SoraSkel): Sora, Donald and Goofy walking / standing in place. Logic
// finds their joint arrays (skel +0x18 = the same model data as the field party's skeleton) and hands them here.
// Their joints sit in a small menu space of their own (x ~ 0, feet at y ~ 188 with y down, ~400 in front of the menu
// camera); each is drawn with Borderlands' copies of their meshes through a fixed menu camera (focal f, principal y,
// per-model offset - config kh_menu_mdl_*), between KH1's menu backdrop and its 2D panels (khui layers 0 / 1).
// Weapons are not shown in KH1's menu: palette bones on joints >= the weapon joint (Sora 280, Donald 187, Goofy 189)
// collapse to nothing.
static volatile LONG g_mmOn = 0; static uint64_t g_mmJ[3] = {0, 0, 0}; static float g_mmP[24] = {};
static std::vector<float> g_mmLast[3]; static ULONGLONG g_mmLastT[3] = {0, 0, 0}; static volatile LONG g_mmDrawn = 0;
// p: 0 f (focal, screen heights), 1 principal y (NDC), 2 mirror x (+1 / -1), 3 brightness, 4..6 light dir (view space),
//    7 + 3k: model k offset x, y, z (KH1 menu cm, k = 0 Sora, 1 Donald, 2 Goofy), 16 z near, 17 z far
extern "C" __declspec(dllexport) void sr_menu_mdl(int on, const uint64_t* joints, const float* p, int np) {
    EnterCriticalSection(&g_cs);
    if (joints) for (int k = 0; k < 3; k++) g_mmJ[k] = joints[k];
    if (p) for (int k = 0; k < np && k < 24; k++) g_mmP[k] = p[k];
    g_mmOn = on ? 1 : 0;
    LeaveCriticalSection(&g_cs);
}
extern "C" __declspec(dllexport) int sr_menu_mdl_stats() { return g_mmDrawn; }
// palette rows from a menu joint array (menu space = camera-relative, no entity matrix); bones of the weapon collapse
static bool mm_bones(int k, const std::vector<LiveBone>& live, uint64_t ja, uint32_t weaponJ, std::vector<float>& out) {
    auto hold = [&]() { if (g_mmLast[k].empty() || GetTickCount64() - g_mmLastT[k] > 400) return false; out = g_mmLast[k]; return true; };
    if (!ja || live.empty() || (!g_kh && !kh_open())) return false;
    uint32_t nj = 0; for (auto& lb : live) if (lb.j + 1 > nj) nj = lb.j + 1;
    static float L[300 * 16];
    if (nj > 300 || !kh_read((uintptr_t)ja, L, (size_t)nj * 64)) return hold();
    const float* L0 = &L[0];
    out.resize(live.size() * 12);
    for (size_t b = 0; b < live.size(); b++) {
        const LiveBone& lb = live[b]; float* o = &out[b * 12];
        if (lb.j >= weaponJ) { memset(o, 0, 12 * sizeof(float)); continue; }
        const float* Lj = &L[lb.j * 16];
        float dx = Lj[12] - L0[12], dy = Lj[13] - L0[13], dz = Lj[14] - L0[14];
        if (!std::isfinite(dx + dy + dz + Lj[0] + Lj[5] + Lj[10]) || dx * dx + dy * dy + dz * dz > 300.f * 300.f) return hold();
        float Wm[16];   // column convention, KH1 y-down / z-forward flipped like the field actors (F * Lj^T)
        for (int r = 0; r < 4; r++) for (int c = 0; c < 4; c++) Wm[r * 4 + c] = Lj[c * 4 + r] * ((r == 1 || r == 2) ? -1.f : 1.f);
        for (int r = 0; r < 3; r++) for (int c = 0; c < 4; c++) { float s = 0; for (int q = 0; q < 4; q++) s += Wm[r * 4 + q] * lb.ib[q * 4 + c]; o[r * 4 + c] = s; }
    }
    g_mmLast[k] = out; g_mmLastT[k] = GetTickCount64();
    return true;
}
static void menu_mdl_draw(ID3D11RenderTargetView* target, UINT W, UINT H) {
    g_mmDrawn = 0;
    if (!g_mmOn || !vs || !ps) return;
    uint64_t J[3]; float P[24];
    EnterCriticalSection(&g_cs); memcpy(J, g_mmJ, sizeof J); memcpy(P, g_mmP, sizeof P); LeaveCriticalSection(&g_cs);
    float f = P[0] > 0.05f ? P[0] : 0.916f, oy = P[1], mir = P[2] < 0 ? -1.f : 1.f, bright = P[3] > 0 ? P[3] : 1.f;
    float zn = P[16] > 0 ? P[16] : 10.f, zf = P[17] > zn ? P[17] : 5000.f;
    float ys = 2.f * f, xs = ys * (float)H / (float)W;
    M4 proj{}; proj.m[0][0] = xs; proj.m[1][1] = ys; proj.m[1][2] = oy; proj.m[3][2] = 1;
    proj.m[2][2] = zf / (zf - zn); proj.m[2][3] = -zn * zf / (zf - zn);
    ensure_own_depth(W, H); if (!g_ownDsv) return;
    ctx->ClearDepthStencilView(g_ownDsv, D3D11_CLEAR_DEPTH, 1.0f, 0);
    D3D11_VIEWPORT vpt{0, 0, (float)W, (float)H, 0, 1};
    ctx->OMSetRenderTargets(1, &target, g_ownDsv); ctx->RSSetViewports(1, &vpt);
    ctx->RSSetState(g_rsNoCull ? g_rsNoCull : rs); ctx->OMSetDepthStencilState(dsOwn, 0);
    float bf[4] = {0, 0, 0, 0}; ctx->OMSetBlendState(bsOff, bf, 0xffffffff);
    ctx->IASetInputLayout(il); ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ID3D11Buffer* cbs[2] = {cbFrame, cbBones};
    ctx->VSSetShader(vs, nullptr, 0); ctx->VSSetConstantBuffers(0, 2, cbs);
    ctx->PSSetShader(ps, nullptr, 0); ctx->PSSetConstantBuffers(0, 2, cbs);
    ctx->PSSetSamplers(0, 1, &samp); ctx->PSSetSamplers(1, 1, &sampClamp);
    ID3D11ShaderResourceView* none[3] = {nullptr, nullptr, nullptr}; ctx->PSSetShaderResources(0, 3, none);
    ctx->GSSetShader(nullptr, nullptr, 0); ctx->HSSetShader(nullptr, nullptr, 0); ctx->DSSetShader(nullptr, nullptr, 0);
    static std::vector<float> bones;
    for (int k = 0; k < 3; k++) {
        const std::vector<LiveBone>& live = k == 0 ? g_live : g_actors[k - 1].live;
        ID3D11Buffer* mvb = k == 0 ? vb : g_actors[k - 1].vb; ID3D11Buffer* mib = k == 0 ? ib : g_actors[k - 1].ib;
        const std::vector<Sub>& subs = k == 0 ? g_subs : g_actors[k - 1].subs;
        const std::vector<ID3D11ShaderResourceView*>& sv = k == 0 ? srvs : g_actors[k - 1].srvs;
        uint32_t wj = k == 0 ? 280u : g_actors[k - 1].weaponJ;
        if (!mvb || !mib || !wj) continue;
        if (!mm_bones(k, live, J[k], wj, bones)) continue;
        upload_cb(cbBones, bones.data(), sizeof(float) * (bones.size() < 900 * 4 ? bones.size() : 900 * 4));
        // bone space = (x, -y, -z) of KH1's menu space -> view (x right, y up, z depth): x * mir + dx, y + dy, -z + dz
        M4 world{}; world.m[0][0] = mir; world.m[1][1] = 1; world.m[2][2] = -1; world.m[3][3] = 1;
        world.m[0][3] = P[7 + 3 * k]; world.m[1][3] = -P[8 + 3 * k]; world.m[2][3] = P[9 + 3 * k];
        float cb[92]; memset(cb, 0, sizeof cb);
        for (int r = 0; r < 4; r++) for (int c = 0; c < 4; c++) { cb[c * 4 + r] = world.m[r][c]; cb[16 + c * 4 + r] = proj.m[r][c]; }
        float lx = P[4], ly = P[5], lz = P[6], ln = sqrtf(lx * lx + ly * ly + lz * lz);
        if (ln < 1e-4f) { lx = 0.35f; ly = -0.55f; lz = 0.75f; ln = sqrtf(lx * lx + ly * ly + lz * lz); }
        cb[32] = lx / ln; cb[33] = ly / ln; cb[34] = lz / ln; cb[36] = bright;
        cb[44] = 1.f;                                    // env2.x: no screen-door fade; haze, relight, sun, shadow all off
        UINT st = sizeof(Vtx), off = 0;
        upload_cb(cbFrame, cb, sizeof cb);
        ctx->IASetVertexBuffers(0, 1, &mvb, &st, &off); ctx->IASetIndexBuffer(mib, DXGI_FORMAT_R32_UINT, 0);
        for (auto& sb : subs) { ID3D11ShaderResourceView* t = sb.tex < sv.size() ? sv[sb.tex] : nullptr; ctx->PSSetShaderResources(0, 1, &t); ctx->DrawIndexed(sb.count, sb.start, 0); }
        g_mmDrawn |= 1 << k;
    }
    ctx->PSSetShaderResources(0, 1, none);
}
// ------------------------------------------------------------------ v61: KH1's Equipment weapon preview
// In KH1's Equipment weapon list the hovered weapon turns slowly at the left (vanilla). KH1 keeps that weapon object's
// matrix in menu space (Sora's: 0x29E5B20, the same object that rides menu Sora's hand on the top screen, where KH1
// doesn't draw it); logic hands it over with the hovered weapon (item 0x2E9A0A0 -> xw_ex id) while KH1 shows it.
// v62: drawn with KH1's whole menu weapon skeleton (keychain links swing like KH1's).
static volatile LONG g_mwOn = 0; static int g_mwWho = 0, g_mwWant = 0, g_mwHave = 0, g_mwN = 1; static float g_mwM[16 * 32] = {}, g_mwP[16] = {};
static Actor g_mwpn; static volatile LONG g_mwDrawn = 0;
// p: 0 f, 1 principal y, 2 mirror, 3 brightness, 4..6 light, 7..9 offset x y z, 10 z near, 11 z far, 12 1 + forced joint (0 = by bone)
// m: nj KH1 joint matrices of the menu weapon skeleton (v62: 0x29E5860 + 0xC800 * who, 0x40 each; palette bone j -> joint j - weaponJ)
extern "C" __declspec(dllexport) void sr_menu_wpn(int on, int who, int id, const float* m, int nj, const float* p, int np) {
    EnterCriticalSection(&g_cs);
    if (m && nj > 0) { g_mwN = nj < 32 ? nj : 32; memcpy(g_mwM, m, sizeof(float) * 16 * g_mwN); }
    if (p) for (int k = 0; k < np && k < 16; k++) g_mwP[k] = p[k];
    g_mwWho = (who >= 0 && who < 3) ? who : 0; g_mwWant = id; g_mwOn = on ? 1 : 0;
    LeaveCriticalSection(&g_cs);
}
extern "C" __declspec(dllexport) int sr_menu_wpn_stats() { return g_mwDrawn; }
static void menu_wpn_draw(ID3D11RenderTargetView* target, UINT W, UINT H) {
    g_mwDrawn = 0;
    if (!g_mwOn || !vs || !ps) return;
    static float M[16 * 32]; float P[16]; int who, want, nj;
    EnterCriticalSection(&g_cs); memcpy(M, g_mwM, sizeof M); memcpy(P, g_mwP, sizeof P); who = g_mwWho; want = g_mwWant; nj = g_mwN; LeaveCriticalSection(&g_cs);
    if (want != g_mwHave) {
        wpn_free(g_mwpn); g_mwHave = want;
        char fn[96]; snprintf(fn, sizeof fn, "weapons\\wpn_%s_%d.bin", kWpnWho[who], want);
        if (want > 0 && wpn_load(g_mwpn, fn)) { init_actor_gpu(g_mwpn); logf("menu preview %s -> %d", kWpnWho[who], want); }
        else { logf("menu preview %s -> %d: %s missing", kWpnWho[who], want, fn); wpn_free(g_mwpn); }
    }
    if (!g_mwpn.vb || !g_mwpn.ib) return;
    const std::vector<LiveBone>& live = who == 0 ? g_live : g_actors[who - 1].live;
    uint32_t wj = who == 0 ? 280u : g_actors[who - 1].weaponJ;
    if (live.empty() || !wj) return;
    static std::vector<float> bones; bones.assign(live.size() * 12, 0.f);
    for (size_t b = 0; b < live.size(); b++) {
        if (live[b].j < wj) continue;
        uint32_t k = live[b].j - wj; if (P[12] >= 0.5f) k = (uint32_t)(P[12] - 0.5f); if ((int)k >= nj) k = 0;   // v63: P[12] = 1 + forced joint
        const float* Lj = &M[k * 16]; float Wm[16];
        for (int r = 0; r < 4; r++) for (int c = 0; c < 4; c++) Wm[r * 4 + c] = Lj[c * 4 + r] * ((r == 1 || r == 2) ? -1.f : 1.f);
        float* o = &bones[b * 12];
        for (int r = 0; r < 3; r++) for (int c = 0; c < 4; c++) { float sum = 0; for (int q = 0; q < 4; q++) sum += Wm[r * 4 + q] * live[b].ib[q * 4 + c]; o[r * 4 + c] = sum; }
    }
    float f = P[0] > 0.05f ? P[0] : 0.916f, oy = P[1], mir = P[2] < 0 ? -1.f : 1.f, bright = P[3] > 0 ? P[3] : 1.f;
    float zn = P[10] > 0 ? P[10] : 10.f, zf = P[11] > zn ? P[11] : 5000.f;
    float ys = 2.f * f, xs = ys * (float)H / (float)W;
    M4 proj{}; proj.m[0][0] = xs; proj.m[1][1] = ys; proj.m[1][2] = oy; proj.m[3][2] = 1;
    proj.m[2][2] = zf / (zf - zn); proj.m[2][3] = -zn * zf / (zf - zn);
    ensure_own_depth(W, H); if (!g_ownDsv) return;
    ctx->ClearDepthStencilView(g_ownDsv, D3D11_CLEAR_DEPTH, 1.0f, 0);
    D3D11_VIEWPORT vpt{0, 0, (float)W, (float)H, 0, 1};
    ctx->OMSetRenderTargets(1, &target, g_ownDsv); ctx->RSSetViewports(1, &vpt);
    ctx->RSSetState(g_rsNoCull ? g_rsNoCull : rs); ctx->OMSetDepthStencilState(dsOwn, 0);
    float bf[4] = {0, 0, 0, 0}; ctx->OMSetBlendState(bsOff, bf, 0xffffffff);
    ctx->IASetInputLayout(il); ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ID3D11Buffer* cbs[2] = {cbFrame, cbBones};
    ctx->VSSetShader(vs, nullptr, 0); ctx->VSSetConstantBuffers(0, 2, cbs);
    ctx->PSSetShader(ps, nullptr, 0); ctx->PSSetConstantBuffers(0, 2, cbs);
    ctx->PSSetSamplers(0, 1, &samp); ctx->PSSetSamplers(1, 1, &sampClamp);
    ID3D11ShaderResourceView* none[3] = {nullptr, nullptr, nullptr}; ctx->PSSetShaderResources(0, 3, none);
    ctx->GSSetShader(nullptr, nullptr, 0); ctx->HSSetShader(nullptr, nullptr, 0); ctx->DSSetShader(nullptr, nullptr, 0);
    upload_cb(cbBones, bones.data(), sizeof(float) * (bones.size() < 900 * 4 ? bones.size() : 900 * 4));
    M4 world{}; world.m[0][0] = mir; world.m[1][1] = 1; world.m[2][2] = -1; world.m[3][3] = 1;
    world.m[0][3] = P[7]; world.m[1][3] = -P[8]; world.m[2][3] = P[9];
    float cb[92]; memset(cb, 0, sizeof cb);
    for (int r = 0; r < 4; r++) for (int c = 0; c < 4; c++) { cb[c * 4 + r] = world.m[r][c]; cb[16 + c * 4 + r] = proj.m[r][c]; }
    float lx = P[4], ly = P[5], lz = P[6], ln = sqrtf(lx * lx + ly * ly + lz * lz);
    if (ln < 1e-4f) { lx = 0.35f; ly = -0.55f; lz = 0.75f; ln = sqrtf(lx * lx + ly * ly + lz * lz); }
    cb[32] = lx / ln; cb[33] = ly / ln; cb[34] = lz / ln; cb[36] = bright;
    cb[44] = 1.f;
    upload_cb(cbFrame, cb, sizeof cb);
    UINT st = sizeof(Vtx), off = 0;
    ctx->IASetVertexBuffers(0, 1, &g_mwpn.vb, &st, &off); ctx->IASetIndexBuffer(g_mwpn.ib, DXGI_FORMAT_R32_UINT, 0);
    for (auto& sb : g_mwpn.subs) { ID3D11ShaderResourceView* t = sb.tex < g_mwpn.srvs.size() ? g_mwpn.srvs[sb.tex] : nullptr; ctx->PSSetShaderResources(0, 1, &t); ctx->DrawIndexed(sb.count, sb.start, 0); }
    ctx->PSSetShaderResources(0, 1, none);
    g_mwDrawn++;
}
