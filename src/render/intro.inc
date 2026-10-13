// ------------------------------------------------------------------ v74: KH1 SUMMON INTRO redrawn natively (like F9)
// User 2026-10-07: "implement the casting animations the same way you did the F9 menu". A summon's intro is a staged
// KH1 cutscene (black stage, Sora + the summon, KH1's own camera cuts). Instead of grabbing KH1's window, it is drawn
// here from KH1's live data: the stage backdrop, Sora's and the summon's meshes posed from their WORLD joint matrices
// (no entity inverse: the stage positions stay), and KH1's effect sprites / models - all through KH1's own camera
// (view 0x2E9D480 + PS2 projection 0x2E9D600 from kh1_fx's FxShared, cam_seq each frame).
// KH1 camera: row-vector view V (v = p * V; x right, y DOWN, z forward); PS2 screen = 2048 + f * (x, y) / z with
// f = proj[0] / proj[5]; KH1 HD shows PS2 rows 32..416 full height -> NDC y scale = proj[5] / 192 (square pixels).
// Bone rows as the field actors: W = F * L^T (F = diag(1, -1, -1)); view = G * V^T * F with G = diag(mir, -1, 1).
static volatile LONG g_inOn = 0; static uint64_t g_inEnt[4] = {}; static int g_inMdl[4] = {}; static int g_inN = 0;
static float g_inP[16] = {}; static volatile LONG g_inDrawn = 0;
// ents: KH1 entities; models: 0 Sora, 1 Donald, 2 Goofy, >= 1000 enemy / summon model id (sr_enemy_model loaded)
// p: 0 backdrop alpha (1 = KH1's black stage), 1 brightness, 2 mirror x (+1 / -1), 3 focal scale (x KH1's), 4 NDC y
//    offset, 5..7 light dir (view space), 8 effects on (1) / off (0)
extern "C" __declspec(dllexport) void sr_intro(int on, const uint64_t* ents, const int* models, int n, const float* p, int np) {
    EnterCriticalSection(&g_cs);
    g_inN = n < 0 ? 0 : (n > 4 ? 4 : n);
    for (int k = 0; k < g_inN; k++) { g_inEnt[k] = ents ? ents[k] : 0; g_inMdl[k] = models ? models[k] : 0; }
    if (p) for (int k = 0; k < np && k < 16; k++) g_inP[k] = p[k];
    g_inOn = on ? 1 : 0;
    LeaveCriticalSection(&g_cs);
}
extern "C" __declspec(dllexport) int sr_intro_stats() { return g_inDrawn; }

// palette rows straight from an entity's WORLD joints (KH1 stage space), F-flipped like every other actor
static bool in_bones(uint64_t ent, const std::vector<LiveBone>& live, uint32_t bodyJ, std::vector<float>& out) {
    if (!ent || live.empty() || (!g_kh && !kh_open())) return false;
    uint32_t h = 0;
    if (!kh_read((uintptr_t)ent + 0x15c, &h, 4)) return false;
    uintptr_t ad = kh_resolve(h); if (!ad || !kh_read(ad + 0x1b4, &h, 4)) return false;
    ad = kh_resolve(h);
    uint32_t nj = 0; for (auto& lb : live) if (lb.j + 1 > nj) nj = lb.j + 1;
    static float L[300 * 16];
    if (!ad || nj > 300 || !kh_read(ad, L, (size_t)nj * 64)) return false;
    const float* L0 = &L[0];
    out.resize(live.size() * 12);
    for (size_t b = 0; b < live.size(); b++) {
        const LiveBone& lb = live[b]; const float* Lj = &L[lb.j * 16];
        float dx = Lj[12] - L0[12], dy = Lj[13] - L0[13], dz = Lj[14] - L0[14];
        float lim = lb.j >= bodyJ ? 3000.f : 400.f;            // weapon joints may swing far (Keyblade raised)
        if (!std::isfinite(dx + dy + dz + Lj[0] + Lj[5] + Lj[10]) || dx * dx + dy * dy + dz * dz > lim * lim) return false;
        float W[16];
        for (int r = 0; r < 4; r++) for (int c = 0; c < 4; c++) W[r * 4 + c] = Lj[c * 4 + r] * ((r == 1 || r == 2) ? -1.f : 1.f);
        float* o = &out[b * 12];
        for (int r = 0; r < 3; r++) for (int c = 0; c < 4; c++) { float s = 0; for (int q = 0; q < 4; q++) s += W[r * 4 + q] * lb.ib[q * 4 + c]; o[r * 4 + c] = s; }
    }
    return true;
}

static void intro_draw(ID3D11RenderTargetView* target, UINT W, UINT H) {
    g_inDrawn = 0;
    if (!g_inOn || !vs || !ps || !khfx_map()) return;
    uint64_t E[4]; int Mdl[4], n; float P[16];
    EnterCriticalSection(&g_cs); memcpy(E, g_inEnt, sizeof E); memcpy(Mdl, g_inMdl, sizeof Mdl); n = g_inN; memcpy(P, g_inP, sizeof P); LeaveCriticalSection(&g_cs);
    float V[16], PR[16];
    memcpy(V, (const void*)g_fxs->view, 64); memcpy(PR, (const void*)g_fxs->proj, 64);
    // v77: latch KH1's newest COMPLETE effect frame for this whole Borderlands frame: stage camera, sprites and meshes all
    // from it. Held while KH1 publishes nothing newer (up to cfg[25] ms); [94] 1 = old per-ring guessing.
    bool fuse = false;
    if (g_khfxCfg[94] < 0.5f) {
        FxFrame f;
        double hold = (g_khfxCfg[25] > 0 ? g_khfxCfg[25] : 70) * 1000.0;
        if (khfx_frame_read(f) && qpc_us() - (double)f.time_us < hold && f.view[15] != 0.f) {
            if (f.frame != g_frmCur.frame) g_frmLatched++; else g_frmHeld++;
            g_frmCur = f; fuse = true;
            memcpy(V, f.view, 64); memcpy(PR, f.proj, 64);
        }
    }
    if (!(PR[5] > 1.f) || !std::isfinite(V[0] + V[5] + V[10] + V[12])) return;
    float mir = P[2] < 0 ? -1.f : 1.f, bright = P[1] > 0 ? P[1] : 1.f;
    float ys = PR[5] / 192.f * (P[3] > 0 ? P[3] : 1.f), xs = ys * (float)H / (float)W, zn = 10.f, zf = 50000.f;
    M4 proj{}; proj.m[0][0] = xs; proj.m[1][1] = ys; proj.m[1][2] = P[4]; proj.m[3][2] = 1;
    proj.m[2][2] = zf / (zf - zn); proj.m[2][3] = -zn * zf / (zf - zn);
    // KH1 world -> renderer view (column): G * V^T
    M4 vw{}; for (int r = 0; r < 4; r++) for (int c = 0; c < 4; c++) vw.m[r][c] = V[c * 4 + r];
    for (int c = 0; c < 4; c++) { vw.m[0][c] *= mir; vw.m[1][c] *= -1.f; }
    M4 F{}; F.m[0][0] = 1; F.m[1][1] = -1; F.m[2][2] = -1; F.m[3][3] = 1;
    M4 world = mul(vw, F);                                   // bone (F-world) space -> view
    // the stage: KH1 shows its summon intros on black
    float a = P[0];
    if (a > 0.99f) { float black[4] = {0, 0, 0, 1}; ctx->ClearRenderTargetView(target, black); }
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
    float lx = P[5], ly = P[6], lz = P[7], ln = sqrtf(lx * lx + ly * ly + lz * lz);
    if (ln < 1e-4f) { lx = 0.35f; ly = -0.55f; lz = 0.75f; ln = sqrtf(lx * lx + ly * ly + lz * lz); }
    static std::vector<float> bones;
    for (int k = 0; k < n; k++) {
        const std::vector<LiveBone>* live = nullptr; ID3D11Buffer *mvb = nullptr, *mib = nullptr;
        const std::vector<Sub>* subs = nullptr; const std::vector<ID3D11ShaderResourceView*>* sv = nullptr; uint32_t bodyJ = 100000;
        if (Mdl[k] == 0) { live = &g_live; mvb = vb; mib = ib; subs = &g_subs; sv = &srvs; bodyJ = 280; }
        else if (Mdl[k] == 1 || Mdl[k] == 2) {
            Actor& ac = g_actors[Mdl[k] - 1]; live = &ac.live; mvb = ac.vb; mib = ac.ib; subs = &ac.subs; sv = &ac.srvs; bodyJ = ac.weaponJ;
        } else {
            EnterCriticalSection(&g_cs); auto it = g_models.find(Mdl[k]); Actor* gm = it == g_models.end() ? nullptr : it->second; LeaveCriticalSection(&g_cs);
            if (!gm || gm->live.empty()) continue;
            if (!gm->vb) init_actor_gpu(*gm);
            live = &gm->live; mvb = gm->vb; mib = gm->ib; subs = &gm->subs; sv = &gm->srvs;
        }
        if (!mvb || !mib || !in_bones(E[k], *live, bodyJ, bones)) continue;
        upload_cb(cbBones, bones.data(), sizeof(float) * (bones.size() < 900 * 4 ? bones.size() : 900 * 4));
        float cb[92]; memset(cb, 0, sizeof cb);
        for (int r = 0; r < 4; r++) for (int c = 0; c < 4; c++) { cb[c * 4 + r] = world.m[r][c]; cb[16 + c * 4 + r] = proj.m[r][c]; }
        cb[32] = lx / ln; cb[33] = ly / ln; cb[34] = lz / ln; cb[36] = bright;
        cb[44] = 1.f;                                    // env2.x: no screen-door fade; haze, relight, sun, shadow off
        upload_cb(cbFrame, cb, sizeof cb);
        UINT st = sizeof(Vtx), off = 0;
        ctx->IASetVertexBuffers(0, 1, &mvb, &st, &off); ctx->IASetIndexBuffer(mib, DXGI_FORMAT_R32_UINT, 0);
        for (auto& sb : *subs) { ID3D11ShaderResourceView* t = sb.tex < sv->size() ? (*sv)[sb.tex] : nullptr; ctx->PSSetShaderResources(0, 1, &t); ctx->DrawIndexed(sb.count, sb.start, 0); }
        g_inDrawn |= 1 << k;
    }
    ctx->PSSetShaderResources(0, 1, none);
    // KH1's own effects of the intro, through the same camera: khfx with K = identity (KH1 world stays KH1 world) and
    // the frame constants' viewproj = proj * G * V^T
    if (P[8] > 0.5f) {
        static const float I16[16] = {1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1};
        M4 vp = mul(proj, vw);
        float cb[92]; memset(cb, 0, sizeof cb);
        for (int r = 0; r < 4; r++) for (int c = 0; c < 4; c++) { cb[c * 4 + r] = (r == c) ? 1.f : 0.f; cb[16 + c * 4 + r] = vp.m[r][c]; }
        cb[36] = 1.f; cb[44] = 1.f;
        upload_cb(cbFrame, cb, sizeof cb);
        ctx->VSSetConstantBuffers(0, 2, cbs); ctx->PSSetConstantBuffers(0, 2, cbs);
        float Yr[3] = {mir * V[0], mir * V[4], mir * V[8]};      // camera right in KH1 world (view column 0)
        float Zu[3] = {-V[1], -V[5], -V[9]};                     // camera up = -(view column 1, y down)
        g_khfxK = I16; g_frmUse = fuse;
        khfx_mdl_draw(Yr, Zu, g_ownDsv);
        khfx_draw(Yr, Zu, g_ownDsv);
        g_khfxK = nullptr; g_frmUse = false;
        g_inDrawn |= 0x100;
    }
}
