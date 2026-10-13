// ------------------------------------------------------------------ v52: KH1's own 2D interface in Borderlands
// kh1_fx v15 copies every 2D primitive KH1's interface submits (kui_shared.h): HUD gauges, portraits, command menu,
// text. Here the newest complete KH1 frame of them is drawn over Borderlands at Present: PS2 screen space (640x448,
// origin 1728/1824) stretched over the Borderlands screen like KH1's HD port stretches it over 16:9, KH1's textures
// (UV = texels of the texture's PS2 size), KH1's blend (GS ALPHA 0x44 alpha / 0x48 add / 0x42 subtract) and wrap.
#include "kui_shared.h"
static HANDLE g_kuiMap = nullptr; static KuiShared* g_kui = nullptr; static ULONGLONG g_kuiRetry = 0;
static volatile LONG g_kuiOn = 0; static volatile LONG g_kuiDrawn = 0, g_kuiBatches = 0;
static ID3D11Buffer* g_kuiVb = nullptr; static ID3D11Buffer* g_kuiCb = nullptr;
static const int KUI_MAXV = 65536;
static volatile float g_kuiPartyDx = 0.f, g_kuiPartyDy = 0.f;
extern "C" __declspec(dllexport) void sr_khui(int on) { g_kuiOn = on ? 1 : 0; if (g_kui) g_kui->enabled = on ? 1 : 0; }
// v53: party gauges (top-right) moved by (dx, dy) PS2 pixels so they clear Borderlands' minimap
extern "C" __declspec(dllexport) void sr_khui_party(float dx, float dy) { g_kuiPartyDx = dx; g_kuiPartyDy = dy; }
// v55: KH1's main menu (F9) drawn natively: no party-gauge shift while it is open (its own top-right art)
static volatile LONG g_kuiMenu = 0;
static volatile LONG g_kuiHdY = 1;
// v84 HOLD: while hold_active() (sr_hold) the last complete HUD frame (CPU copy) is drawn when KH1's is missing / half-drawn   // v60: field HUD uses KH1 HD's vertical mapping too (measured against KH1's window)
extern "C" __declspec(dllexport) void sr_khui_hdy(int on) { g_kuiHdY = on ? 1 : 0; }
extern "C" __declspec(dllexport) void sr_khui_menu(int on) { g_kuiMenu = on ? 1 : 0; }
extern "C" __declspec(dllexport) void sr_khui_stats(int* out) {
    out[0] = g_kui ? 1 : 0; out[1] = g_kuiDrawn; out[2] = g_kuiBatches; out[3] = g_kui ? (int)g_kui->write : 0;
}
static bool khui_map() {
    if (g_kui) return true;
    if (GetTickCount64() < g_kuiRetry) return false;
    g_kuiRetry = GetTickCount64() + 2000;
    g_kuiMap = OpenFileMappingW(FILE_MAP_ALL_ACCESS, FALSE, KUI_SHARED_NAME);
    if (!g_kuiMap) return false;
    g_kui = (KuiShared*)MapViewOfFile(g_kuiMap, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(KuiShared));
    if (!g_kui || g_kui->magic != KUI_MAGIC) {
        if (g_kui) UnmapViewOfFile(g_kui);
        g_kui = nullptr; CloseHandle(g_kuiMap); g_kuiMap = nullptr; return false;
    }
    logf("khui: SoraKhUi mapped (KH1 interface)");
    return true;
}
static void khui_release() {
    rel(g_kuiVb); rel(g_kuiCb);
    if (g_kui) { g_kui->enabled = 0; UnmapViewOfFile(g_kui); } g_kui = nullptr;
    if (g_kuiMap) CloseHandle(g_kuiMap); g_kuiMap = nullptr;
}
static void khui_draw(ID3D11RenderTargetView* target, UINT W, UINT H, int layer = -1) {   // v57 layer: 0 full-screen backdrop, 1 the rest
    if (!g_kuiOn || !fxvs || !khui_map() || (!g_kh && !kh_open())) return;
    g_kui->enabled = 1;
    uint32_t w = g_kui->write; if (!w) return;
    const KuiBatch* ring = g_kui->b;
    double now = qpc_us();
    uint32_t last = w - 1;
    // v54: KH1 may be writing the newest batch right now (seq not set yet) - step back to the newest finished one
    // instead of drawing nothing this frame (user: the command menu blinked every 6-11 s)
    for (int back = 0; back < 64 && last > 0 && ring[last % KUI_BATCHES].seq != last + 1; back++) last--;
    bool kuiStale = ring[last % KUI_BATCHES].seq != last + 1 || now - (double)ring[last % KUI_BATCHES].time_us > 250000.0;
    if (kuiStale && !(layer < 0 && hold_active())) { g_kuiDrawn = 0; return; }
    const double gap = 9000.0;                         // v53: KH1 draws its interface well inside a 16.7 ms frame
    auto run_start = [&](uint32_t e) -> uint32_t {
        uint32_t s = e; int64_t t = ring[e % KUI_BATCHES].time_us;
        while (s > 0 && w - s < KUI_BATCHES - 16) {
            const KuiBatch& b = ring[(s - 1) % KUI_BATCHES];
            if (b.seq != s || t - b.time_us > gap) break;
            t = b.time_us; s--;
        }
        return s;
    };
    uint32_t end = last, start = run_start(end);
    if (now - (double)ring[end % KUI_BATCHES].time_us < gap && start > 0) {      // still being drawn: previous frame
        uint32_t pe = start - 1;
        if (ring[pe % KUI_BATCHES].seq == pe + 1) { end = pe; start = run_start(pe); }
    }
    // v53: a frame caught half-drawn (much fewer batches than the last good one) -> draw the last good one again
    static uint32_t s_goodStart = 0, s_goodEnd = 0, s_goodN = 0; static double s_goodT = 0;
    {
        uint32_t n = end - start + 1;
        bool ok = (ring[s_goodEnd % KUI_BATCHES].seq == s_goodEnd + 1) && (ring[s_goodStart % KUI_BATCHES].seq == s_goodStart + 1);
        if (s_goodN && n * 4 < s_goodN * 3 && now - s_goodT < 300000.0 && ok) { start = s_goodStart; end = s_goodEnd; }
        else { s_goodStart = start; s_goodEnd = end; s_goodN = n; s_goodT = now; }
    }
    if (!g_kuiVb) {
        D3D11_BUFFER_DESC b{}; b.ByteWidth = KUI_MAXV * sizeof(FxV); b.Usage = D3D11_USAGE_DYNAMIC; b.BindFlags = D3D11_BIND_VERTEX_BUFFER;
        b.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE; dev->CreateBuffer(&b, nullptr, &g_kuiVb);
        D3D11_BUFFER_DESC c{}; c.ByteWidth = 160; c.Usage = D3D11_USAGE_DYNAMIC; c.BindFlags = D3D11_BIND_CONSTANT_BUFFER; c.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
        dev->CreateBuffer(&c, nullptr, &g_kuiCb);
        if (!g_kuiVb || !g_kuiCb) return;
        float cb[40] = {}; for (int i = 0; i < 4; i++) { cb[i * 5] = 1.f; cb[16 + i * 5] = 1.f; }   // world = viewproj = I
        upload_cb(g_kuiCb, cb, sizeof cb);
    }
    if (!g_sampWrap) {
        D3D11_SAMPLER_DESC sd{}; sd.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR; sd.MaxLOD = D3D11_FLOAT32_MAX;
        sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_WRAP; dev->CreateSamplerState(&sd, &g_sampWrap);
    }
    if (!g_samp4[1]) {
        for (int i = 1; i < 4; i++) {
            D3D11_SAMPLER_DESC sd{}; sd.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR; sd.MaxLOD = D3D11_FLOAT32_MAX;
            sd.AddressU = (i & 1) ? D3D11_TEXTURE_ADDRESS_CLAMP : D3D11_TEXTURE_ADDRESS_WRAP;
            sd.AddressV = (i & 2) ? D3D11_TEXTURE_ADDRESS_CLAMP : D3D11_TEXTURE_ADDRESS_WRAP; sd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
            dev->CreateSamplerState(&sd, &g_samp4[i]);
        }
    }
    struct Run { ID3D11ShaderResourceView* srv; int blend, samp, first, count; };
    static std::vector<Run> runs; runs.clear();
    D3D11_MAPPED_SUBRESOURCE ms;
    static std::vector<FxV> cpu; if (cpu.size() < (size_t)KUI_MAXV) cpu.resize(KUI_MAXV);
    static std::vector<FxV> holdV; static std::vector<Run> holdRuns; static int holdNb = 0;
    FxV* o = cpu.data(); int nv = 0, nb = 0;
    for (uint32_t k = start; k <= end && nv < KUI_MAXV - 64 && !kuiStale; k++) {
        KuiBatch b; memcpy(&b, (const void*)&ring[k % KUI_BATCHES], sizeof b);
        if (b.seq != k + 1 || b.count == 0 || b.count > 4096) continue;
        bool textured = b.stride >= 28 && b.texobj;
        KhTex* tx = textured ? khfx_tex(b.texobj) : nullptr;
        if (textured && !tx) continue;
        float tw = (float)(b.pw ? b.pw : (1u << ((b.tex0 >> 26) & 15))), th = (float)(b.ph ? b.ph : (1u << ((b.tex0 >> 30) & 15)));
        int a = (int)(b.alpha & 0xff);
        int blend = a == 0x48 ? 1 : (a == 0x42 ? 3 : 2);
        int kind = textured ? (blend == 1 || blend == 3 ? 1 : 2) : (blend == 1 || blend == 3 ? 3 : 4);
        int wms = (int)(b.clamp & 3), wmt = (int)((b.clamp >> 2) & 3);
        int samp = ((wms == 1 || wms == 2) ? 1 : 0) | ((wmt == 1 || wmt == 2) ? 2 : 0);
        float sx = 0.f, sy = 0.f;                             // v53: party gauges = whole batch in the top-right corner
        {
            float mnx = 1e9f, mxy = -1e9f, mxx = -1e9f, mny = 1e9f;
            for (uint32_t i = 0; i < b.count; i++) { const float* v = g_kui->v[(b.first + i) % KUI_VERTS]; mnx = fminf(mnx, v[0]); mxy = fmaxf(mxy, v[1]); mxx = fmaxf(mxx, v[0]); mny = fminf(mny, v[1]); }
            bool full = mxx - mnx >= 600.f && mxy - mny >= 300.f;   // v57: KH1's menu dim / scanline backdrop
            if ((layer == 0 && !full) || (layer == 1 && full)) continue;
            // v55: KH1's menu blur = full-frame quads of its own last frame in render-target pixels (x up to 2624,
            // no usable texture) -> not drawn; everything on the 640x448 screen is
            if (mxx - 1728.f > 1000.f || mxy - 1824.f > 1000.f) continue;
            if (mnx - 1728.f > 400.f && mxy - 1824.f < 150.f && !g_kuiMenu) { sx = g_kuiPartyDx; sy = g_kuiPartyDy; }
        }
        auto emit = [&](uint32_t vi) {
            const float* v = g_kui->v[vi % KUI_VERTS];
            FxV& q = o[nv++];
            q.p[0] = (v[0] + sx - 1728.f) / 320.f - 1.f; q.p[1] = 1.f - (v[1] + sy - 1824.f) / 224.f; q.p[2] = 0.5f;
            if (g_kuiMenu || g_kuiHdY) q.p[1] = 1.f - (v[1] + sy - 1824.f - 32.f) / 192.f;   // v56/v60: KH1 HD shows PS2 rows 32..416 full height (menu AND field HUD)
            q.uv[0] = v[4] / tw; q.uv[1] = v[5] / th;
            uint32_t c; memcpy(&c, &v[3], 4);
            q.c[0] = (c & 0xff) / 128.f; q.c[1] = ((c >> 8) & 0xff) / 128.f; q.c[2] = ((c >> 16) & 0xff) / 128.f; q.c[3] = ((c >> 24) & 0xff) / 128.f;
            if (blend == 2 && q.c[3] > 1.f) q.c[3] = 1.f;
            q.k = (float)kind;
        };
        int first = nv; uint32_t n = b.count, f0 = b.first;
        if (b.mode == 6) { for (uint32_t i = 0; i + 3 < n && nv + 6 <= KUI_MAXV; i += 4) { emit(f0 + i); emit(f0 + i + 1); emit(f0 + i + 2); emit(f0 + i + 1); emit(f0 + i + 3); emit(f0 + i + 2); } }
        else if (b.mode == 8) { for (uint32_t i = 0; i + 3 < n && nv + 6 <= KUI_MAXV; i += 4) { emit(f0 + i); emit(f0 + i + 1); emit(f0 + i + 2); emit(f0 + i); emit(f0 + i + 2); emit(f0 + i + 3); } }
        else if (b.mode == 7) { for (uint32_t i = 1; i + 1 < n && nv + 3 <= KUI_MAXV; i++) { emit(f0); emit(f0 + i); emit(f0 + i + 1); } }   // fan (gauge arcs)
        else if (b.mode == 5) { for (uint32_t i = 0; i + 2 < n && nv + 3 <= KUI_MAXV; i += 3) { emit(f0 + i); emit(f0 + i + 1); emit(f0 + i + 2); } }
        else continue;
        if (nv == first) continue;
        ID3D11ShaderResourceView* srv = tx ? tx->srv : nullptr;
        if (!runs.empty() && runs.back().srv == srv && runs.back().blend == blend && runs.back().samp == samp && runs.back().first + runs.back().count == first) runs.back().count += nv - first;
        else runs.push_back({srv, blend, samp, first, nv - first});
        nb++;
    }
    const std::vector<Run>* drawRuns = &runs; const FxV* src = cpu.data(); int drawN = nv;
    bool holding = layer < 0 && hold_active() && !holdV.empty();
    if (holding && (kuiStale || nb * 4 < holdNb * 3)) { drawRuns = &holdRuns; src = holdV.data(); drawN = (int)holdV.size(); }
    else if (layer < 0 && !kuiStale && nb > 0 && (!hold_active() || nb * 4 >= holdNb * 3)) {
        holdV.assign(cpu.begin(), cpu.begin() + nv); holdRuns = runs; holdNb = nb;   // newest complete frame
    }
    if (FAILED(ctx->Map(g_kuiVb, 0, D3D11_MAP_WRITE_DISCARD, 0, &ms))) return;
    if (drawN > 0) memcpy(ms.pData, src, (size_t)drawN * sizeof(FxV));
    ctx->Unmap(g_kuiVb, 0);
    g_kuiDrawn = drawN / 3; g_kuiBatches = nb;
    if (!drawN) return;
    D3D11_VIEWPORT vp{0, 0, (float)W, (float)H, 0, 1};
    ctx->OMSetRenderTargets(1, &target, nullptr); ctx->RSSetViewports(1, &vp);
    ctx->RSSetState(g_rsNoCull ? g_rsNoCull : rs);
    ctx->OMSetDepthStencilState(dsOff, 0);
    UINT fs = sizeof(FxV), fo = 0;
    ctx->IASetInputLayout(fxil); ctx->IASetVertexBuffers(0, 1, &g_kuiVb, &fs, &fo); ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ctx->VSSetShader(fxvs, nullptr, 0); ctx->PSSetShader(fxps, nullptr, 0);
    ctx->VSSetConstantBuffers(0, 1, &g_kuiCb); ctx->PSSetConstantBuffers(0, 1, &g_kuiCb);
    ctx->GSSetShader(nullptr, nullptr, 0); ctx->HSSetShader(nullptr, nullptr, 0); ctx->DSSetShader(nullptr, nullptr, 0);
    float bz[4] = {0, 0, 0, 0};
    for (auto& rn : *drawRuns) {
        ctx->PSSetShaderResources(0, 1, &rn.srv);
        ID3D11SamplerState* smp = rn.samp && g_samp4[rn.samp] ? g_samp4[rn.samp] : g_sampWrap;
        ctx->PSSetSamplers(0, 1, &smp);
        ctx->OMSetBlendState(rn.blend == 1 ? bsAdd : (rn.blend == 3 && bsSub ? bsSub : bsAlpha), bz, 0xffffffff);
        ctx->Draw(rn.count, rn.first);
    }
    ID3D11ShaderResourceView* none = nullptr; ctx->PSSetShaderResources(0, 1, &none);
}
