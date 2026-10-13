// ------------------------------------------------------------------ v35: KH1's own effects (kh1_fx.dll capture)
// kh1_fx.dll (in KH1) copies every effect sprite KH1 draws into the ring "Local\SoraFx" (passthrough/fx/fx_shared.h).
// Here: pick KH1's latest complete frame, load each texture from KH1's memory (the DDS file bytes an Axa::EntryTex
// keeps), turn each quad PS2 screen space -> KH1 world (inverse of view*proj, like KH1's own PC renderer) -> Borderlands
// (affine map from logic.py, the same one that places Donald and Goofy), re-face billboards to Borderlands' camera and
// draw with KH1's blend (GS ALPHA 0x48 additive, 0x44 alpha).
#include "fx_shared.h"
static FxShared* g_fxs = nullptr; static HANDLE g_fxMap = nullptr; static ULONGLONG g_fxRetry = 0;
static float g_khfxCfg[96] = {}; static volatile ULONGLONG g_khfxCfgT = 0;
// cfg: [0..15] KH1 world -> Borderlands affine (row vectors), [16] on, [17] radius around KH1 Sora (KH1 units),
// [18] ambient: a sprite shape KH1 draws without a break for this long (s) is room decoration and is skipped,
// [19] brightness, [20] alpha scale, [21..23] KH1 Sora position, [24] scale (KH1 -> Borderlands units),
// [25] max frame age (ms), [26] frame gap (us), [27] debug: 1 = ignore radius / ambient filters,
// v42 spell stretch (KH1 spells reach ~4-8 m; the KH1 target stands within reach, Borderlands shows the flight landing
// on the real enemy): [28] on, [29] a0 (UU along the line left as is), [30..32] Sora (BL), [33..35] w = KH1 target
// mapped to BL - Sora, [36..38] delta = real BL target - mapped KH1 target. Points past a0 along w move by t * delta.
// v66/v67: [43] count (<= 12, v66 4) of mute spheres, [44 + 4i .. 47 + 4i] KH1 x y z r: KH1 effects whose centre falls inside are
// skipped - Heartless spawn points next to pinned KH1 Sora (a parked Heartless leaves by itself and KH1 respawns it
// there every ~17 s; its appear cloud was drawn in Borderlands, user 2026-10-06)
static bool khfx_muted(const float* C, float x, float y, float z) {
    int n = (int)C[43]; if (n > 12) n = 12;
    for (int i = 0; i < n; i++) {
        const float* m = C + 44 + 4 * i; float dx = x - m[0], dy = y - m[1], dz = z - m[2];
        if (dx * dx + dy * dy + dz * dz < m[3] * m[3]) return true;
    }
    return false;
}
struct KhTex { uint64_t data = 0; uint32_t size = 0; ID3D11ShaderResourceView* srv = nullptr; bool bad = false; bool room = false;
               bool summon = false; };   // v72: "xs_<name>" = a summon's own effects (exempt from the ambient filter)
static std::map<uint64_t, KhTex> g_khTex;
struct KhShape { double first = 0, last = 0; float p[3] = {}; };
static std::map<uint64_t, KhShape> g_khShape;
static ID3D11Buffer* g_khfxVb = nullptr; static const int KHFX_MAXQ = 16384;   // v75: x4 headroom (summon intros)
static volatile LONG g_khfxDrawn = 0, g_khfxSkipped = 0, g_khfxTex = 0, g_khfxFrameQ = 0;
static double qpc_us() { static LARGE_INTEGER f{}; if (!f.QuadPart) QueryPerformanceFrequency(&f); LARGE_INTEGER c; QueryPerformanceCounter(&c); return (double)c.QuadPart * 1e6 / (double)f.QuadPart; }
// v47 debug: which KH1 records each Borderlands frame drew (sprites kind 0, models kind 1)
static uint32_t g_selDbg[1024][4]; static volatile uint32_t g_selDbgN = 0;
static void sel_dbg(uint32_t kind, uint32_t start, uint32_t end) {
    uint32_t i = g_selDbgN++ % 1024; LARGE_INTEGER t; QueryPerformanceCounter(&t); LARGE_INTEGER f; QueryPerformanceFrequency(&f);
    g_selDbg[i][0] = (uint32_t)(t.QuadPart * 1000000 / f.QuadPart); g_selDbg[i][1] = kind; g_selDbg[i][2] = start; g_selDbg[i][3] = end;
}
extern "C" __declspec(dllexport) int sr_khfx_seldbg(uint32_t* out, int maxn) {
    uint32_t n = g_selDbgN; int c = 0;
    for (uint32_t i = n > 1024 ? n - 1024 : 0; i < n && c < maxn; i++, c++) memcpy(out + c * 4, g_selDbg[i % 1024], 16);
    return c;
}
extern "C" __declspec(dllexport) void sr_khfx(const float* p, int n) { for (int k = 0; k < n && k < 96; k++) g_khfxCfg[k] = p[k]; g_khfxCfgT = GetTickCount64(); }
static volatile LONG g_khfxAmb = 0; static volatile double g_khfxDbg[4] = {};
extern "C" __declspec(dllexport) void sr_khfx_stats2(double* out) { int n = 0; for (auto& kv : g_khShape) if (kv.second.p[0] > 0.5f) n++; out[0] = n; out[1] = (double)g_khShape.size(); for (int i = 0; i < 4; i++) out[2 + i] = g_khfxDbg[i]; }
extern "C" __declspec(dllexport) void sr_khfx_stats(int* out) { out[0] = g_fxs ? 1 : 0; out[1] = g_khfxDrawn; out[2] = g_khfxSkipped; out[3] = g_khfxTex; out[4] = g_khfxFrameQ; out[5] = g_fxs ? (int)g_fxs->write : 0; }

static bool khfx_map() {
    if (g_fxs) return true;
    if (GetTickCount64() < g_fxRetry) return false;
    g_fxRetry = GetTickCount64() + 2000;
    g_fxMap = OpenFileMappingW(FILE_MAP_ALL_ACCESS, FALSE, FX_SHARED_NAME);
    if (!g_fxMap) return false;
    g_fxs = (FxShared*)MapViewOfFile(g_fxMap, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(FxShared));
    if (!g_fxs || g_fxs->magic != FX_MAGIC || g_fxs->rec_size != sizeof(FxQuad)) {
        if (g_fxs) UnmapViewOfFile(g_fxs); g_fxs = nullptr; CloseHandle(g_fxMap); g_fxMap = nullptr; return false;
    }
    logf("khfx: SoraFx mapped (kh1_fx v%u) - renderer v42", g_fxs->version);
    return true;
}
// v77: KH1 effect FRAMES (kh1_fx v40, fx_frame.h): the record ranges + camera of KH1's last complete effect pass.
// The summon intro latches ONE frame per Borderlands frame (g_frmUse) and both rings draw exactly that frame with its
// camera - per-ring time-gap guesses picked different KH1 frames for sprites and meshes (intro background pop-in).
#include "fx_frame.h"
static FxFrame* g_frmMap = nullptr; static ULONGLONG g_frmRetry = 0;
static FxFrame g_frmCur{}; static bool g_frmUse = false; static uint32_t g_frmLatched = 0, g_frmHeld = 0;
static uint32_t g_frmLatchedG = 0, g_frmHeldG = 0;   // v78: gameplay latch counts
static bool khfx_frame_read(FxFrame& out) {
    if (!g_frmMap) {
        if (GetTickCount64() < g_frmRetry) return false;
        g_frmRetry = GetTickCount64() + 2000;
        HANDLE h = OpenFileMappingW(FILE_MAP_READ, FALSE, FXF_SHARED_NAME); if (!h) return false;
        g_frmMap = (FxFrame*)MapViewOfFile(h, FILE_MAP_READ, 0, 0, sizeof(FxFrame));
        if (!g_frmMap) { CloseHandle(h); return false; }
        logf("khfx: SoraFxFrame mapped (KH1 effect frames)");
    }
    for (int tries = 0; tries < 8; tries++) {
        uint32_t s1 = g_frmMap->seq; if (s1 & 1) { YieldProcessor(); continue; }
        MemoryBarrier(); memcpy(&out, (const void*)g_frmMap, sizeof out); MemoryBarrier();
        if (g_frmMap->seq == s1) return out.magic == FXF_MAGIC && out.frame > 0;
    }
    return false;
}
extern "C" __declspec(dllexport) void sr_khfx_frame_stats(uint32_t* out) { out[0] = g_frmUse; out[1] = g_frmCur.frame; out[2] = g_frmLatched; out[3] = g_frmHeld; }
static void khfx_frame_latch_gameplay() {
    g_frmUse = false;
    if (g_khfxCfg[95] < 0.5f && g_khfxCfg[16] > 0.5f) {
        FxFrame f;
        double hold = (g_khfxCfg[25] > 0 ? g_khfxCfg[25] : 70) * 1000.0;
        if (khfx_frame_read(f) && qpc_us() - (double)f.time_us < hold && f.view[15] != 0.f) {
            if (f.frame != g_frmCur.frame) g_frmLatchedG++; else g_frmHeldG++;
            g_frmCur = f; g_frmUse = true;
        }
    }
}
static void khfx_frame_release() { g_frmUse = false; }
extern "C" __declspec(dllexport) void sr_khfx_frame_stats2(uint32_t* out) { out[0] = g_frmLatchedG; out[1] = g_frmHeldG; out[2] = g_frmCur.frame; }
#include "fx_mdl.h"
static FxMdlShared* g_fxm = nullptr; static HANDLE g_fxmMap = nullptr; static ULONGLONG g_fxmRetry = 0;
static ID3D11Buffer* g_khmVb = nullptr; static const int KHM_MAXV = 98304;   // triangle-list vertices per frame (v75: x4 - Simba's intro roar sky hit 8192 tris and glitched)
static std::map<uint64_t, KhShape> g_khMdlSeen;                               // ambient filter for room models
static ID3D11RasterizerState* g_rsNoCull = nullptr; static ID3D11SamplerState* g_sampWrap = nullptr;   // PS2 REPEAT
static ID3D11SamplerState* g_samp4[4] = {};   // v50: [1] clamp U, [2] clamp V, [3] clamp both
static volatile LONG g_khmDrawn = 0, g_khmBatches = 0;
static void khfx_release() {
    for (auto& kv : g_khTex) rel(kv.second.srv);
    rel(g_khmVb); rel(g_rsNoCull); rel(g_sampWrap); for (auto& smp : g_samp4) rel(smp); g_khMdlSeen.clear();
    if (g_fxm) UnmapViewOfFile(g_fxm); g_fxm = nullptr; if (g_fxmMap) CloseHandle(g_fxmMap); g_fxmMap = nullptr;
    g_khTex.clear(); g_khShape.clear(); rel(g_khfxVb);
    if (g_fxs) UnmapViewOfFile(g_fxs); g_fxs = nullptr; if (g_fxMap) CloseHandle(g_fxMap); g_fxMap = nullptr;
}
#include <wincodec.h>
// v52: some KH1 interface textures are PNG files in memory (uitex_bin20c0.png): decode with WIC -> BGRA
static bool khfx_png(const uint8_t* p, size_t n, std::vector<uint8_t>& out, uint32_t& w, uint32_t& h) {
    static const GUID CLSID_Factory = {0xcacaf262, 0x9370, 0x4615, {0xa1, 0x3b, 0x9f, 0x55, 0x39, 0xda, 0x4c, 0x0a}};
    static const GUID FMT_BGRA = {0x6fddc324, 0x4e03, 0x4bfe, {0xb1, 0x85, 0x3d, 0x77, 0x76, 0x8d, 0xc9, 0x0f}};
    CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    IWICImagingFactory* f = nullptr; IWICStream* st = nullptr; IWICBitmapDecoder* dec = nullptr; IWICBitmapFrameDecode* fr = nullptr; IWICFormatConverter* cv = nullptr;
    bool ok = false;
    if (SUCCEEDED(CoCreateInstance(CLSID_Factory, nullptr, CLSCTX_INPROC_SERVER, __uuidof(IWICImagingFactory), (void**)&f)) &&
        SUCCEEDED(f->CreateStream(&st)) && SUCCEEDED(st->InitializeFromMemory((BYTE*)p, (DWORD)n)) &&
        SUCCEEDED(f->CreateDecoderFromStream(st, nullptr, WICDecodeMetadataCacheOnDemand, &dec)) && SUCCEEDED(dec->GetFrame(0, &fr)) &&
        SUCCEEDED(f->CreateFormatConverter(&cv)) && SUCCEEDED(cv->Initialize(fr, FMT_BGRA, WICBitmapDitherTypeNone, nullptr, 0, WICBitmapPaletteTypeCustom)) &&
        SUCCEEDED(cv->GetSize(&w, &h)) && w && h) {
        out.resize((size_t)w * h * 4);
        ok = SUCCEEDED(cv->CopyPixels(nullptr, w * 4, (UINT)out.size(), out.data()));
    }
    if (cv) cv->Release(); if (fr) fr->Release(); if (dec) dec->Release(); if (st) st->Release(); if (f) f->Release();
    return ok;
}
// texture behind a KH1 Axa::EntryTex: (re)load when its file bytes moved (room change frees / reuses entries)
static KhTex* khfx_tex(uint64_t obj) {
    uint8_t e[0x80];
    if (!obj || !kh_read((uintptr_t)obj, e, sizeof e) || *(uint32_t*)(e + 0x10) != 0x2f02eb5) return nullptr;
    uint64_t data = *(uint64_t*)(e + 0x68); uint32_t size = *(uint32_t*)(e + 0x78);
    KhTex& t = g_khTex[obj];
    if (t.data == data && t.size == size) return t.bad ? nullptr : &t;
    rel(t.srv); t.data = data; t.size = size; t.bad = true;
    char name[33]; memcpy(name, e + 0x14, 32); name[32] = 0;
    // the room's own textures ("tw00_14_img...", "tw_wdt...") belong to KH1's map, not to an effect we want
    // (two letters + two digits = a map, "tw00_14_img..."; "<world>_wdt" = world data). NOT "xm_" magic / "xw_" weapons.
    t.room = (name[0] >= 'a' && name[0] <= 'z' && name[1] >= 'a' && name[1] <= 'z' && name[2] >= '0' && name[2] <= '9' && name[3] >= '0' && name[3] <= '9')
             || strstr(name, "_wdt") != nullptr;
    t.summon = !strncmp(name, "xs_", 3);   // v72: Tinker Bell's glow / sparkles last the whole summon (> ambient s)
    if (size < 128 || size > (16u << 20)) return nullptr;
    std::vector<uint8_t> buf(size);
    if (!kh_read((uintptr_t)data, buf.data(), size)) return nullptr;
    if (!memcmp(buf.data(), "\x89PNG", 4)) {                       // v52: PNG (WIC)
        std::vector<uint8_t> px; uint32_t pw = 0, ph = 0;
        if (!khfx_png(buf.data(), size, px, pw, ph)) { logf("khfx: %s PNG decode failed", name); return nullptr; }
        D3D11_TEXTURE2D_DESC td{}; td.Width = pw; td.Height = ph; td.MipLevels = 1; td.ArraySize = 1; td.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
        td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_IMMUTABLE; td.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        D3D11_SUBRESOURCE_DATA sd0{px.data(), pw * 4, 0}; ID3D11Texture2D* tex = nullptr;
        if (FAILED(dev->CreateTexture2D(&td, &sd0, &tex))) return nullptr;
        dev->CreateShaderResourceView(tex, nullptr, &t.srv); tex->Release();
        t.bad = !t.srv; g_khfxTex++;
        logf("khfx: texture %s %ux%u PNG", name, pw, ph);
        return t.bad ? nullptr : &t;
    }
    if (memcmp(buf.data(), "DDS ", 4) != 0) { logf("khfx: %s not a DDS", name); return nullptr; }
    uint32_t h = *(uint32_t*)(buf.data() + 12), w = *(uint32_t*)(buf.data() + 16), mips = *(uint32_t*)(buf.data() + 28);
    uint32_t pff = *(uint32_t*)(buf.data() + 80); char four[5] = {0}; memcpy(four, buf.data() + 84, 4);
    DXGI_FORMAT fmt = DXGI_FORMAT_UNKNOWN; uint32_t bpb = 0, off = 128;
    if (pff & 4) {
        if (!memcmp(four, "DXT5", 4)) { fmt = DXGI_FORMAT_BC3_UNORM; bpb = 16; }
        else if (!memcmp(four, "DXT1", 4)) { fmt = DXGI_FORMAT_BC1_UNORM; bpb = 8; }
        else if (!memcmp(four, "DXT3", 4)) { fmt = DXGI_FORMAT_BC2_UNORM; bpb = 16; }
        else if (!memcmp(four, "DX10", 4)) {
            fmt = (DXGI_FORMAT) * (uint32_t*)(buf.data() + 128); off = 148;
            bpb = (fmt == DXGI_FORMAT_BC1_UNORM) ? 8 : (fmt == DXGI_FORMAT_BC3_UNORM || fmt == DXGI_FORMAT_BC7_UNORM || fmt == DXGI_FORMAT_BC2_UNORM) ? 16 : 0;
        }
    } else if (*(uint32_t*)(buf.data() + 88) == 32) { fmt = DXGI_FORMAT_B8G8R8A8_UNORM; bpb = 0; }
    if (fmt == DXGI_FORMAT_UNKNOWN || !w || !h) { logf("khfx: %s unsupported format %s", name, four); return nullptr; }
    uint32_t levels = mips ? mips : 1;
    std::vector<D3D11_SUBRESOURCE_DATA> sd(levels); size_t p = off; uint32_t lw = w, lh = h, ok = 0;
    for (uint32_t l = 0; l < levels; l++) {
        uint32_t pitch, bytes;
        if (bpb) { pitch = ((lw + 3) / 4) * bpb; bytes = pitch * ((lh + 3) / 4); } else { pitch = lw * 4; bytes = pitch * lh; }
        if (p + bytes > size) break;
        sd[l].pSysMem = buf.data() + p; sd[l].SysMemPitch = pitch; p += bytes; ok++;
        lw = lw > 1 ? lw / 2 : 1; lh = lh > 1 ? lh / 2 : 1;
    }
    if (!ok) return nullptr;
    D3D11_TEXTURE2D_DESC td{}; td.Width = w; td.Height = h; td.MipLevels = ok; td.ArraySize = 1; td.Format = fmt;
    td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_IMMUTABLE; td.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    ID3D11Texture2D* tex = nullptr;
    if (FAILED(dev->CreateTexture2D(&td, sd.data(), &tex))) { logf("khfx: %s CreateTexture2D failed", name); return nullptr; }
    dev->CreateShaderResourceView(tex, nullptr, &t.srv); tex->Release();
    t.bad = !t.srv; g_khfxTex++;
    logf("khfx: texture %s %ux%u %s (%u mips)%s", name, w, h, four, ok, t.room ? " [room]" : "");
    return t.bad ? nullptr : &t;
}
static void m4rv(const float* a, const float* b, float* o) {   // o = a * b (row-major 4x4)
    for (int i = 0; i < 4; i++) for (int j = 0; j < 4; j++) { float s = 0; for (int k = 0; k < 4; k++) s += a[i * 4 + k] * b[k * 4 + j]; o[i * 4 + j] = s; }
}
static const float* g_khfxK = nullptr;   // v74: K override (summon intro redraw: identity = stay in KH1 world)
static void khfx_draw(const float Y[3], const float Z[3], ID3D11DepthStencilView* dsv) {
    const float* C = g_khfxCfg;
    if (C[16] < 0.5f || !g_khfxCfgT || GetTickCount64() - g_khfxCfgT > 500) { if (g_fxs) g_fxs->enabled = 0; return; }
    if (!fxvs || !khfx_map() || (!g_kh && !kh_open())) return;
    g_fxs->enabled = 1;
    double now = qpc_us();
    uint32_t w = g_fxs->write; if (!w) return;
    const FxQuad* ring = g_fxs->ring;
    // newest record; KH1's frame = a run of records without a gap > frame-gap; skip the run still being written
    uint32_t i = w - 1; const FxQuad* r = &ring[i % FX_RING];
    if (g_frmUse) {                                                   // v77: the latched KH1 frame, exactly
        if (g_frmCur.spr_end == g_frmCur.spr_begin || w - g_frmCur.spr_begin > FX_RING - 64 || g_frmCur.spr_end > w) { g_khfxFrameQ = 0; return; }
    } else
    if (r->seq != i + 1 || now - (double)r->time_us > (C[25] > 0 ? C[25] : 70) * 1000.0) { g_khfxFrameQ = 0; return; }
    double gap = C[26] > 0 ? C[26] : 5000.0;
    auto run_start = [&](uint32_t end) -> uint32_t {   // first index of the run ending at end (inclusive)
        uint32_t s = end; int64_t t = ring[end % FX_RING].time_us;
        while (s > 0 && w - s < FX_RING - 64) {
            const FxQuad& q = ring[(s - 1) % FX_RING];
            if (q.seq != s || t - q.time_us > gap) break;
            t = q.time_us; s--;
        }
        return s;
    };
    uint32_t end = w - 1, start = run_start(end);
    if (g_frmUse) { start = g_frmCur.spr_begin; end = g_frmCur.spr_end - 1; }
    else if (now - (double)ring[end % FX_RING].time_us < gap && start > 0) {   // still being drawn: use the previous frame
        uint32_t pe = start - 1;
        if (ring[pe % FX_RING].seq == pe + 1 && now - (double)ring[pe % FX_RING].time_us < 70000.0) { end = pe; start = run_start(pe); }
    }
    uint32_t nq = end - start + 1; if (nq > KHFX_MAXQ) { start = end + 1 - KHFX_MAXQ; nq = KHFX_MAXQ; }
    sel_dbg(0, start, end);
    g_khfxFrameQ = nq;
    // PS2 screen -> KH1 world, then KH1 world -> Borderlands
    float vp[16], ivp[16];
    const float* camV = g_frmUse ? g_frmCur.view : (const float*)g_fxs->view;   // v77: the frame's own camera
    const float* camP = g_frmUse ? g_frmCur.proj : (const float*)g_fxs->proj;
    m4rv(camV, camP, vp);
    if (!inv4(vp, ivp)) return;
    float V3[9]; for (int a = 0; a < 3; a++) for (int b = 0; b < 3; b++) V3[a * 3 + b] = camV[a * 4 + b];
    const float* K = g_khfxK ? g_khfxK : C; float sc = C[24] > 0 ? C[24] : 1.f, R2 = C[17] * C[17];
    float ambient = C[18] > 0 ? C[18] : 10.f; bool dbg = C[27] > 0.5f;
    if (!g_khfxVb) {
        D3D11_BUFFER_DESC b{}; b.ByteWidth = KHFX_MAXQ * 6 * sizeof(FxV); b.Usage = D3D11_USAGE_DYNAMIC; b.BindFlags = D3D11_BIND_VERTEX_BUFFER;
        b.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE; dev->CreateBuffer(&b, nullptr, &g_khfxVb);
        if (!g_khfxVb) return;
    }
    struct Run { ID3D11ShaderResourceView* srv; int blend; int first, count; };
    static std::vector<Run> runs; runs.clear();
    D3D11_MAPPED_SUBRESOURCE ms;
    if (FAILED(ctx->Map(g_khfxVb, 0, D3D11_MAP_WRITE_DISCARD, 0, &ms))) return;
    FxV* o = (FxV*)ms.pData; int nv = 0, skipped = 0;
    double tnow = now * 1e-6;
    for (uint32_t k = start; k <= end; k++) {
        FxQuad q; memcpy(&q, &ring[k % FX_RING], sizeof q);
        if (q.seq != k + 1) { skipped++; continue; }
        KhTex* tx = khfx_tex(q.texobj);
        if (!tx || (tx->room && !dbg)) { skipped++; continue; }
        float W[16]; m4rv(q.m, ivp, W);                       // sprite plane -> KH1 world
        float cx = W[12], cy = W[13], cz = W[14];
        if (!dbg) {
            float dx = cx - C[21], dy = cy - C[22], dz = cz - C[23];
            if (C[17] > 0 && dx * dx + dy * dy + dz * dz > R2) { skipped++; continue; }
            if (khfx_muted(C, cx, cy, cz)) { skipped++; continue; }
            // room decoration: a shape KH1 keeps drawing for seconds without a break (same shape may be drawn at several
            // spots per frame, so no position test); once flagged it stays flagged until unseen for 30 s
            KhShape& sh = g_khShape[q.shape];
            if (tx->summon) sh.p[0] = 0;                       // v72: summon effects are never room decoration
            if (tnow - sh.last > (sh.p[0] > 0.5f ? 30.0 : 0.5)) { g_khfxDbg[3] = tnow - sh.last; sh.first = tnow; sh.p[0] = 0; }
            sh.last = tnow;
            if (tnow - sh.first > ambient) sh.p[0] = 1;
            g_khfxDbg[0] = tnow; g_khfxDbg[1] = sh.first; g_khfxDbg[2] = sh.last;
            if (sh.p[0] > 0.5f && !tx->summon) { skipped++; continue; }
        }
        // billboard? its plane axes are the KH1 camera's (W3 * V3 = s * I): rebuild it facing Borderlands' camera
        float Cm[9]; for (int a = 0; a < 3; a++) for (int b = 0; b < 3; b++) Cm[a * 3 + b] = W[a * 4 + 0] * V3[0 * 3 + b] + W[a * 4 + 1] * V3[1 * 3 + b] + W[a * 4 + 2] * V3[2 * 3 + b];
        float s0 = sqrtf(Cm[0] * Cm[0] + Cm[1] * Cm[1] + Cm[2] * Cm[2]) + 1e-6f;
        bool bill = fabsf(Cm[2]) + fabsf(Cm[5]) < 0.05f * s0;   // plane faces the KH1 camera (any in-plane spin kept)
        float bc[3] = {cx * K[0] + cy * K[4] + cz * K[8] + K[12], cx * K[1] + cy * K[5] + cz * K[9] + K[13], cx * K[2] + cy * K[6] + cz * K[10] + K[14]};
        auto warp = [&](float* P_) {
            if (C[28] < 0.5f) return;
            float wl = sqrtf(C[33] * C[33] + C[34] * C[34]); if (wl < C[29] + 1.f) return;
            float a = ((P_[0] - C[30]) * C[33] + (P_[1] - C[31]) * C[34]) / wl;
            if (a <= C[29]) return;
            float t = (a - C[29]) / (wl - C[29]);
            P_[0] += t * C[36]; P_[1] += t * C[37]; P_[2] += t * C[38];
        };
        if (bill) warp(bc);
        float P[4][3];
        for (int c = 0; c < 4; c++) {
            float x = q.xy[c * 2], y = q.xy[c * 2 + 1];
            if (bill) {   // camera space offset (x right, y down) -> Borderlands camera right / up
                float ox = x * Cm[0] + y * Cm[3], oy = x * Cm[1] + y * Cm[4];
                for (int a = 0; a < 3; a++) P[c][a] = bc[a] + sc * (ox * Y[a] - oy * Z[a]);
            } else {
                float wx = x * W[0] + y * W[4] + W[12], wy = x * W[1] + y * W[5] + W[13], wz = x * W[2] + y * W[6] + W[14];
                P[c][0] = wx * K[0] + wy * K[4] + wz * K[8] + K[12]; P[c][1] = wx * K[1] + wy * K[5] + wz * K[9] + K[13];
                P[c][2] = wx * K[2] + wy * K[6] + wz * K[10] + K[14];
                warp(P[c]);
            }
        }
        // v73: sprite UVs are 1/4096 of the texture like effect meshes (v51). The TEX0 size is a placeholder for HD
        // textures (Tinker Bell's sheet says 128x64 while her UVs span 4096 x 4096 -> glow / pixie dust sampled past the
        // edge, user 2026-10-06); 256x256 sheets (TW = TH = 8) give the same result either way. [42] 1 = old TEX0 scale.
        float tw = 4096.f, th = 4096.f;
        if (C[42] > 0.5f) { tw = (float)(1u << ((q.tex0 >> 26) & 15)) * 16.f; th = (float)(1u << ((q.tex0 >> 30) & 15)) * 16.f; }
        float U[4] = {q.uv[0] / tw, q.uv[2] / tw, q.uv[2] / tw, q.uv[0] / tw}, Vv[4] = {q.uv[1] / th, q.uv[1] / th, q.uv[3] / th, q.uv[3] / th};
        // GS ALPHA 0x44 = (Cs-Cd)*As+Cd (alpha), 0x48 = Cs*As+Cd (add), 0x42 = Cd-Cs*As (subtract: dark effects, Gravity)
        // v70: GS ALPHA (A-B)*C+D with D field (bits 6-7) 2 = no destination term (0x88 = Cs*As: Tinker Bell's body)
        // = drawn over the scene, not added to it (was additive -> washed-out white Tinker Bell, user 2026-10-06)
        int blend = (q.b4 == 0x44 || ((q.b4 >> 6) & 3) == 2) ? 2 : (q.b4 == 0x42 ? 3 : 1);
        float br = C[19] > 0 ? C[19] : 1.f, as = C[20] > 0 ? C[20] : 1.f;
        float col[4] = {q.col[0] * br, q.col[1] * br, q.col[2] * br, q.col[3] * as};
        if (blend == 2 && col[3] > 1.f) col[3] = 1.f;
        if (nv + 6 > KHFX_MAXQ * 6) break;
        static const int idx[6] = {0, 1, 2, 0, 2, 3};
        for (int e = 0; e < 6; e++) {
            FxV& v = o[nv + e]; int c = idx[e];
            v.p[0] = P[c][0]; v.p[1] = P[c][1]; v.p[2] = P[c][2]; v.uv[0] = U[c]; v.uv[1] = Vv[c];
            for (int a = 0; a < 4; a++) v.c[a] = col[a];
            v.k = (float)(blend == 2 ? 2 : 1);
        }
        if (!runs.empty() && runs.back().srv == tx->srv && runs.back().blend == blend) runs.back().count += 6;
        else runs.push_back({tx->srv, blend, nv, 6});
        nv += 6;
    }
    ctx->Unmap(g_khfxVb, 0);
    g_khfxDrawn = nv / 6; g_khfxSkipped = skipped;
    if (!nv) return;
    UINT fs = sizeof(FxV), fo = 0;
    ctx->IASetInputLayout(fxil); ctx->IASetVertexBuffers(0, 1, &g_khfxVb, &fs, &fo); ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ctx->VSSetShader(fxvs, nullptr, 0); ctx->PSSetShader(fxps, nullptr, 0); ctx->PSSetSamplers(0, 1, &sampClamp);
    ctx->OMSetDepthStencilState(dsv ? dsFx : dsOff, 0);
    float bz[4] = {0, 0, 0, 0};
    for (auto& rn : runs) {
        ctx->PSSetShaderResources(0, 1, &rn.srv);
        ctx->OMSetBlendState(rn.blend == 2 ? bsAlpha : (rn.blend == 3 && bsSub ? bsSub : bsAdd), bz, 0xffffffff);
        ctx->Draw(rn.count, rn.first);
    }
}
#include "khfx_mdl.inc"
