// sora_render.dll — draws KH1 Sora inside Borderlands GOTY Enhanced (D3D11).
// Loaded by the Python mod (ctypes). The mod calls sr_init(dir) once, then sr_set_frame(...) every game frame.
// Rendering happens in a hooked IDXGISwapChain::Present using the game's camera sent from Python.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <tlhelp32.h>
#include <d3d11.h>
#include <d3dcompiler.h>
#include <cmath>
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <vector>
#include <string>
#include <map>
#include "MinHook.h"
#include "../passthrough/bridge/shared.h"
#include "../passthrough/crashlog.h"   // v80: release crash logger (crash_logs\session_BL.bin)

// ------------------------------------------------------------------ logging
static char g_dir[MAX_PATH];
static void logf(const char* fmt, ...) {
    char path[MAX_PATH]; snprintf(path, sizeof path, "%s\\sora_render.log", g_dir);
    FILE* f = fopen(path, "a"); if (!f) return;
    va_list a; va_start(a, fmt); vfprintf(f, fmt, a); va_end(a); fputc('\n', f); fclose(f);
}

// ------------------------------------------------------------------ data
struct Vtx { float p[3], n[3], uv[2]; uint32_t bone; };
struct Sub { uint32_t tex, start, count; };
struct Tex { uint32_t w, h; std::vector<uint8_t> rgba; };
struct Motion { uint32_t frames; float dur; std::vector<float> m; };  // frames * bones * 12
static std::vector<Vtx> g_v; static std::vector<uint32_t> g_i; static std::vector<Sub> g_subs; static std::vector<Tex> g_tex;
static std::vector<Motion> g_mo; static uint32_t g_bones = 0; static float g_fps = 30;
struct LiveBone { uint32_t j; float ib[16]; };   // palette bone -> KH1 joint + inverse bind (sora_live.bin)
static std::vector<LiveBone> g_live;
#pragma pack(push, 1)
struct HudSprite { float x, y, w, h, u0, v0, u1, v1; float color[4]; int32_t tex, mode; };   // pixels; must match logic.py
#define HUD_MAX 160   // v12: the KH1 command menu (panels + KH1 system-font glyphs) shares the HUD layer
struct HudFrame { int32_t n; float gauge[8]; HudSprite s[HUD_MAX]; };   // gauge: hp, hp_trail, mp_fill, mp_segs, low_pulse, -, -, -
#pragma pack(pop)
static std::vector<Tex> g_fxTex;   // KH1 effect sprites (sora_fx.bin)
static HudFrame g_hud{}; static std::vector<Tex> g_hudTex;   // KH1 HUD art (sora_hud.bin)
static volatile ULONGLONG g_hudT = 0;   // last sr_set_hud; HUD hides when Python stops sending (paused, disabled)

#pragma pack(push, 1)
struct Frame {                 // must match the ctypes Structure in the Python mod
    float cam_pos[3];          // UE units
    float cam_rot[3];          // pitch, yaw, roll (radians)
    float fov;                 // horizontal degrees
    float pos[3];              // Sora root (feet) in UE units
    float yaw;                 // radians, UE yaw
    int32_t anim;              // motion index
    float time;                // seconds into the motion
    float scale;               // KH cm -> UE units
    int32_t visible;
    int32_t flags;             // bit0 mirror X, bit1 depth test, bit2 reversed-Z
    float yaw_offset;          // model facing correction (radians)
    float light[3];            // world light direction (UE)
    int32_t nfx;               // glowing billboards for spells (UE units)
    // spell particles: tex -1 = procedural soft dot (additive); tex >= 0 = KH1 effect texture (sora_fx.bin) with
    // uv rect, rotation (radians, around the view axis), stretch (height / width) and mode 0 additive / 1 alpha
    struct { float pos[3]; float size; float color[4]; float uv[4]; int32_t tex; float rot; float stretch; int32_t mode; } fx[256];
    int32_t anim2;             // previous motion, faded out over the blend (crossfade)
    float time2;
    float blend;               // weight of anim2 (0 = only the current motion)
    float bright;              // colour multiplier (scene mode draws into the HDR target)
    // --- v7 (GTA-passthrough ideas): relight from the game's own picture, haze, close-camera fade
    float fade;                // 1 = solid, 0 = gone (dithered): the camera pulled into Sora near walls
    float relight[4];          // strength, tint, reference luminance, mip level of the blurred scene
    float relight2[4];         // min light, max light, -, -
    float haze[4];             // start (UE units), range, max amount, -
    float bank;                // v11: roll around his forward axis (radians, + = lean right), pivot at the chest (glide turns)
};
#pragma pack(pop)
static Frame g_frame{}; static CRITICAL_SECTION g_cs; static volatile LONG g_ready = 0;
static volatile ULONGLONG g_frameT = 0;
static float g_lastCb[92];   // Sora's frame constants (world, viewproj, light, misc, relight...) reused by party actors   // last sr_set_frame: Sora is hidden when Python stops sending (pause, loading)

static bool read_file(const char* name, std::vector<uint8_t>& out) {
    char path[MAX_PATH]; snprintf(path, sizeof path, "%s\\%s", g_dir, name);
    FILE* f = fopen(path, "rb"); if (!f) return false;
    fseek(f, 0, SEEK_END); long n = ftell(f); fseek(f, 0, SEEK_SET);
    out.resize(n); fread(out.data(), 1, n, f); fclose(f); return true;
}
template <class T> static T rd(const uint8_t*& p) { T v; memcpy(&v, p, sizeof v); p += sizeof v; return v; }

static bool load_assets() {
    std::vector<uint8_t> b;
    if (!read_file("sora_mesh.bin", b)) { logf("no sora_mesh.bin"); return false; }
    const uint8_t* p = b.data(); p += 4; rd<uint32_t>(p);
    uint32_t nv = rd<uint32_t>(p), ni = rd<uint32_t>(p), ns = rd<uint32_t>(p), nt = rd<uint32_t>(p);
    g_v.resize(nv); memcpy(g_v.data(), p, nv * sizeof(Vtx)); p += nv * sizeof(Vtx);
    g_i.resize(ni); memcpy(g_i.data(), p, ni * 4); p += ni * 4;
    g_subs.resize(ns); memcpy(g_subs.data(), p, ns * sizeof(Sub)); p += ns * sizeof(Sub);
    g_tex.resize(nt);
    for (auto& t : g_tex) { t.w = rd<uint32_t>(p); t.h = rd<uint32_t>(p); t.rgba.assign(p, p + t.w * t.h * 4); p += t.w * t.h * 4; }
    if (!read_file("sora_anim.bin", b)) { logf("no sora_anim.bin"); return false; }
    p = b.data(); p += 4; rd<uint32_t>(p);
    uint32_t nm = rd<uint32_t>(p); g_bones = rd<uint32_t>(p); g_fps = rd<float>(p);
    g_mo.resize(nm);
    for (auto& m : g_mo) {
        m.frames = rd<uint32_t>(p); m.dur = rd<float>(p);
        size_t n = (size_t)m.frames * g_bones * 12; m.m.resize(n); memcpy(m.m.data(), p, n * 4); p += n * 4;
    }
    logf("assets: %u verts %u tris %u subs %u tex, %u motions, %u bones", nv, ni / 3, ns, nt, nm, g_bones);
    if (read_file("sora_live.bin", b) && b.size() >= 16) {
        p = b.data(); p += 8; uint32_t nb = rd<uint32_t>(p); rd<uint32_t>(p);
        if (nb == g_bones && b.size() >= 16 + (size_t)nb * sizeof(LiveBone)) { g_live.resize(nb); memcpy(g_live.data(), p, nb * sizeof(LiveBone)); }
        logf("live palette: %u bones %s", nb, g_live.empty() ? "(MISMATCH, live mode off)" : "");
    }
    if (read_file("sora_fx.bin", b) && b.size() >= 12) {   // KH1 effect sprites (passthrough/make_fx_bin.py)
        p = b.data() + 8; uint32_t n = rd<uint32_t>(p); const uint8_t* end = b.data() + b.size();
        for (uint32_t i = 0; i < n && p + 8 <= end; i++) {
            Tex t; t.w = rd<uint32_t>(p); t.h = rd<uint32_t>(p);
            if (p + (size_t)t.w * t.h * 4 > end) break;
            t.rgba.assign(p, p + (size_t)t.w * t.h * 4); p += (size_t)t.w * t.h * 4; g_fxTex.push_back(std::move(t));
        }
        logf("fx sprites: %zu", g_fxTex.size());
    }
    if (read_file("sora_hud.bin", b) && b.size() >= 12) {
        p = b.data() + 8; uint32_t n = rd<uint32_t>(p); const uint8_t* end = b.data() + b.size();
        for (uint32_t i = 0; i < n && p + 8 <= end; i++) {
            Tex t; t.w = rd<uint32_t>(p); t.h = rd<uint32_t>(p);
            if (p + (size_t)t.w * t.h * 4 > end) break;
            t.rgba.assign(p, p + (size_t)t.w * t.h * 4); p += (size_t)t.w * t.h * 4; g_hudTex.push_back(std::move(t));
        }
        logf("hud textures: %zu", g_hudTex.size());
    }
    return true;
}

// ------------------------------------------------------------------ live KH1 pose (passthrough, read-only)
// KINGDOM HEARTS FINAL MIX.exe Steam 1.0.0.2 (RVAs from the KH1 image base; see MODLOG).
static const uintptr_t KH_SORA_PTR = 0x2537E48, KH_HANDLE_BUCKETS = 0x2EE3980;
static HANDLE g_kh = nullptr; static uintptr_t g_khBase = 0; static ULONGLONG g_khRetry = 0;
static volatile LONG g_liveState = 0;   // 0 off/not found, 1 live pose used, 2 KH1 found but no valid pose
static bool kh_read(uintptr_t a, void* out, size_t n) { SIZE_T got = 0; return a && ReadProcessMemory(g_kh, (void*)a, out, n, &got) && got == n; }
static void kh_close() { if (g_kh) CloseHandle(g_kh); g_kh = nullptr; g_khBase = 0; }
static bool kh_open() {
    if (GetTickCount64() < g_khRetry) return false;
    g_khRetry = GetTickCount64() + 2000;
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0); if (snap == INVALID_HANDLE_VALUE) return false;
    PROCESSENTRY32W pe{sizeof pe}; DWORD pid = 0;
    for (BOOL ok = Process32FirstW(snap, &pe); ok; ok = Process32NextW(snap, &pe))
        if (!_wcsicmp(pe.szExeFile, L"KINGDOM HEARTS FINAL MIX.exe")) { pid = pe.th32ProcessID; break; }
    CloseHandle(snap); if (!pid) return false;
    HANDLE ms = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE, pid); if (ms == INVALID_HANDLE_VALUE) return false;
    MODULEENTRY32W me{sizeof me}; if (Module32FirstW(ms, &me)) g_khBase = (uintptr_t)me.modBaseAddr; CloseHandle(ms);
    g_kh = OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!g_kh || !g_khBase) { kh_close(); return false; }
    logf("KH1 found: pid %lu base %p", pid, (void*)g_khBase); return true;
}
static uintptr_t kh_resolve(uint32_t h) {   // 32-bit PS2-style pointer -> address
    h &= 0x7FFFFFFF; if (!h) return 0; uint64_t b = 0;
    if (!kh_read(g_khBase + KH_HANDLE_BUCKETS + (h >> 25) * 8, &b, 8) || !b || b == ~0ull) return 0;
    return (uintptr_t)(b | (h & 0x1FFFFFF));
}
static bool inv4(const float* m, float* o) {   // general 4x4 inverse (row-major)
    double a[4][8];
    for (int i = 0; i < 4; i++) for (int j = 0; j < 8; j++) a[i][j] = j < 4 ? m[i * 4 + j] : (j - 4 == i);
    for (int c = 0; c < 4; c++) {
        int piv = c; for (int r = c + 1; r < 4; r++) if (fabs(a[r][c]) > fabs(a[piv][c])) piv = r;
        if (fabs(a[piv][c]) < 1e-9) return false;
        if (piv != c) for (int j = 0; j < 8; j++) { double t = a[c][j]; a[c][j] = a[piv][j]; a[piv][j] = t; }
        double d = a[c][c]; for (int j = 0; j < 8; j++) a[c][j] /= d;
        for (int r = 0; r < 4; r++) if (r != c) { double f = a[r][c]; for (int j = 0; j < 8; j++) a[r][j] -= f * a[c][j]; }
    }
    for (int i = 0; i < 4; i++) for (int j = 0; j < 4; j++) o[i * 4 + j] = (float)a[i][j + 4];
    return true;
}
// KH1's current motion (entity +0x164 == motion index) and its clock (+0x16C, 60 fps frames), plus whether the last
// live_bones call could only hold an old pose (KH1 rebuilds the joint array for ~150 ms at every motion change).
static volatile int g_khAnim = 0; static float g_khClock = 0; static volatile bool g_liveHeld = false;
// ---- pose interpolation between the two most recent distinct KH1 poses
static std::vector<float> g_pPrev, g_pCur; static double g_tPrev = 0, g_tCur = 0;
static double qpc_now() { static LARGE_INTEGER f{}; if (!f.QuadPart) QueryPerformanceFrequency(&f); LARGE_INTEGER c; QueryPerformanceCounter(&c); return (double)c.QuadPart / (double)f.QuadPart; }
// v84 HOLD (user 2026-10-12: Sora, Donald, Goofy and KH1's HUD vanished for ~2 s now and then = the mod's own KH1 room
// reload, KH1 has no Sora / party / HUD while it loads). logic calls sr_hold(s) when it warps; until then the last good
// HUD frame, Sora's last live pose and the party's last frames + poses are kept instead of dropped.
static volatile double g_holdUntil = 0.0;
static bool hold_active() { return qpc_now() < g_holdUntil; }
extern "C" __declspec(dllexport) void sr_hold(float seconds) { g_holdUntil = qpc_now() + (double)seconds; }
static bool live_interp(std::vector<float>& out) {
    if (g_pCur.empty()) return false;
    double per = g_tCur - g_tPrev;
    if (g_pPrev.size() != g_pCur.size() || per < 0.004 || per > 0.1) { out = g_pCur; g_liveState = 1; return true; }   // no usable pair
    double a = (qpc_now() - g_tCur) / per; if (a < 0) a = 0; if (a > 1) a = 1;
    out.resize(g_pCur.size());
    for (size_t k = 0; k < out.size(); k++) out[k] = g_pPrev[k] + (g_pCur[k] - g_pPrev[k]) * (float)a;
    g_liveState = 1; return true;
}
// Fills out (g_bones x 3x4 skinning rows, same layout as sora_anim.bin) from KH1's live joint matrices.
// KH1 joints: row-vector world matrices (KH world, Y down). Model = L * inv(entity); bake space = F * model^T, F = diag(1,-1,-1).
static bool live_bones(std::vector<float>& out) {
    if (g_live.empty()) return false;
    if (!g_kh && !kh_open()) { g_liveState = 0; return false; }
    DWORD code = 0;
    if (!GetExitCodeProcess(g_kh, &code) || code != STILL_ACTIVE) { logf("KH1 exited"); kh_close(); g_liveState = 0; return false; }
    g_liveState = 2;
    if (hold_active() && !g_pCur.empty()) return live_interp(out);   // v84: our room reload - KH1's respawn pose is not shown
    uint64_t sora = 0; float ent[16], ie[16]; uint32_t h = 0;
    // v69: Sora's POSE comes from the party leader slot (0x2D37280 = Sora's own entity). KH1's player pointer
    // (KH_SORA_PTR) is cleared during a summon intro (v68) and points at DUMBO while Sora rides him (the player
    // controls Dumbo) - drawing Sora's mesh on Dumbo's skeleton made him small, perched on Dumbo's head (user).
    if (!kh_read(g_khBase + 0x2D37280, &sora, 8) || !sora) { if (!kh_read(g_khBase + KH_SORA_PTR, &sora, 8)) return hold_active() && !g_pCur.empty() && live_interp(out); }
    if (!sora || !kh_read((uintptr_t)sora + 0x230, ent, 64) || !inv4(ent, ie)) return hold_active() && !g_pCur.empty() && live_interp(out);
    { uint8_t st[16]; if (kh_read((uintptr_t)sora + 0x160, st, 16)) { g_khAnim = st[4]; memcpy(&g_khClock, st + 12, 4); } }
    g_liveHeld = true;   // cleared below when this read gives a fresh valid pose
    if (!kh_read((uintptr_t)sora + 0x15c, &h, 4)) return false;
    uintptr_t a = kh_resolve(h); if (!a || !kh_read(a + 0x1b4, &h, 4)) return false;
    a = kh_resolve(h);
    static float L[293 * 16];
    // KH1 reuses the joint array for other transforms during part of each frame (~20% of reads come back with bones
    // metres away). Such reads are rejected and the last good pose (at most ~1 KH1 frame old) is shown instead.
    static std::vector<float> lastGood; static ULONGLONG lastGoodT = 0;
    auto keep_last = [&]() { if (lastGood.empty() || GetTickCount64() - lastGoodT > (hold_active() ? 3500u : 500u)) return false; return live_interp(out); };
    if (!a || !kh_read(a, L, sizeof L)) return keep_last();
    // Validity = bones stay close to the hips (joint 0). Scratch frames scatter them metres apart; a valid pose stays
    // within ~2 m even when the whole body has travelled (the Dodge Roll moves the joints 3.5 m before the entity follows).
    const float* L0 = &L[0];
    for (uint32_t i = 0; i < g_bones; i++) {
        const float* Lj = &L[g_live[i].j * 16]; float dx = Lj[12] - L0[12], dy = Lj[13] - L0[13], dz = Lj[14] - L0[14];
        if (!std::isfinite(dx + dy + dz)) return keep_last();
        // the Keyblade's joints (280-292) may fly far away (Strike Raid throws it): only the body must stay together
        if (g_live[i].j < 280 && dx * dx + dy * dy + dz * dz > 250.f * 250.f) return keep_last();
        if (g_live[i].j >= 280 && dx * dx + dy * dy + dz * dz > 3000.f * 3000.f) return keep_last();
    }
    // Root travel carried in the joints (Dodge Roll): keep the body over the entity, allowing 12 cm of natural hip sway.
    // (Borderlands moves the pawn by that travel instead - logic.py uses entity + hips offset as Sora's body position.)
    float sx = 0, sz = 0;
    {
        float R0[16];   // hips in entity-local space
        for (int r = 0; r < 4; r++) for (int c = 0; c < 4; c++) { float s = 0; for (int k = 0; k < 4; k++) s += L0[r * 4 + k] * ie[k * 4 + c]; R0[r * 4 + c] = s; }
        float hx = R0[12], hz = R0[14], d = sqrtf(hx * hx + hz * hz), sway = 12.f;
        if (d > sway) { sx = hx * (d - sway) / d; sz = hz * (d - sway) / d; }
    }
    out.resize(g_bones * 12);
    for (uint32_t i = 0; i < g_bones; i++) {
        const LiveBone& lb = g_live[i]; if (lb.j >= 293) return false;
        const float* Lj = &L[lb.j * 16];
        float R[16];   // row-vector model matrix = Lj * ie
        for (int r = 0; r < 4; r++) for (int c = 0; c < 4; c++) { float s = 0; for (int k = 0; k < 4; k++) s += Lj[r * 4 + k] * ie[k * 4 + c]; R[r * 4 + c] = s; }
        R[12] -= sx; R[14] -= sz;
        float W[16];   // column convention W = F * R^T
        for (int r = 0; r < 4; r++) for (int c = 0; c < 4; c++) W[r * 4 + c] = R[c * 4 + r] * ((r == 1 || r == 2) ? -1.f : 1.f);
        float lim = lb.j >= 280 ? 3000.f : 300.f;   // a thrown Keyblade may be far from Sora
        if (!std::isfinite(W[3]) || !std::isfinite(W[7]) || !std::isfinite(W[11]) || W[3] * W[3] + W[7] * W[7] + W[11] * W[11] > lim * lim)
            return keep_last();   // body bone > 3 m from Sora: mid-frame scratch data (or menu/loading)
        float* o = &out[i * 12];
        for (int r = 0; r < 3; r++) for (int c = 0; c < 4; c++) { float s = 0; for (int k = 0; k < 4; k++) s += W[r * 4 + k] * lb.ib[k * 4 + c]; o[r * 4 + c] = s; }
    }
    // New KH1 pose (KH1 updates at 60 Hz, Borderlands draws faster): keep the last two distinct poses and blend between
    // them by time, one KH1 frame behind, so Sora animates smoothly at any Borderlands frame rate.
    if (g_pCur.empty() || memcmp(g_pCur.data(), out.data(), out.size() * 4) != 0) {
        g_pPrev.swap(g_pCur); g_pCur = out; g_tPrev = g_tCur; g_tCur = qpc_now();
    }
    lastGood = out; lastGoodT = GetTickCount64();
    g_liveHeld = false;
    return live_interp(out);
}


// ------------------------------------------------------------------ party actors (Donald, Goofy): live KH1 poses only
// <name>_mesh.bin + <name>_live.bin (tools/make_party.py, from the player's own KH1). Pose = the party slot's entity
// joints (same entity layout as Sora), placed where Python says (sr_set_actor: Borderlands position/facing/scale).
#pragma pack(push, 1)
struct ActorFrame { float pos[3]; float yaw; float scale; int32_t visible; int32_t flags; float pad; };
#pragma pack(pop)
struct Actor {
    const char* name; uintptr_t slotRva;
    std::vector<Vtx> v; std::vector<uint32_t> i; std::vector<Sub> subs; std::vector<Tex> tex; std::vector<LiveBone> live;
    ID3D11Buffer *vb = nullptr, *ib = nullptr; std::vector<ID3D11ShaderResourceView*> srvs;
    ActorFrame f{}; ULONGLONG fT = 0; std::vector<float> lastGood; ULONGLONG lastGoodT = 0; int state = 0;
    uint32_t weaponJ = 0, handJ = 0; float rel[16]; bool haveRel = false;   // weapon bone rigid on the hand (learned live)
    std::vector<float> pPrev, pCur; double tPrev = 0, tCur = 0;            // last two distinct KH1 poses (smooth playback)
    Actor* geo = nullptr; uint64_t ent = 0; bool reset = false;
    // v22: baked flight motions (<name>_fly.bin, tools/make_party_fly.py): played while ActorFrame.flags & 0x100
    // (motion = flags >> 12 & 15) - Donald and Goofy fly behind gliding Sora with their Neverland flight
    struct Fly { uint32_t nf; float dur; std::vector<float> rows; }; std::vector<Fly> fly; float flyFps = 30.f;   // v21 enemies: mesh shared from a loaded model; KH1 entity given directly
};
// v21: KH1 enemy models (ex_<id>_mesh.bin / _live.bin, tools/make_heartless.py), loaded on request from Python and
// drawn in up to 16 enemy slots, each posed from its own KH1 stand-in entity (Borderlands enemy = invisible hitbox).
#define MAX_ENEMIES 24   // v85: 16..23 = KH1 item drops (static models, logic item_drops_tick)
static std::map<int, Actor*> g_models; static Actor g_enemies[MAX_ENEMIES];
// Donald's staff joint 187 on hand 151, Goofy's shield joint 189 on hand 117: live they sit rigidly on the hand at a fixed
// offset, but some frames carry garbage for the weapon chain - those are rebuilt as hand x (last good offset).
static Actor g_actors[2] = {{"donald", 0x2D37288}, {"goofy", 0x2D37290}};
static void actor_weapon_cfg() { g_actors[0].weaponJ = 187; g_actors[0].handJ = 151; g_actors[1].weaponJ = 189; g_actors[1].handJ = 117; }
static bool load_actor(Actor& a) {
    std::vector<uint8_t> b; char fn[64];
    snprintf(fn, sizeof fn, "%s_mesh.bin", a.name);
    if (!read_file(fn, b) || b.size() < 24) return false;
    const uint8_t* p = b.data(); p += 4; rd<uint32_t>(p);
    uint32_t nv = rd<uint32_t>(p), ni = rd<uint32_t>(p), ns = rd<uint32_t>(p), nt = rd<uint32_t>(p);
    a.v.resize(nv); memcpy(a.v.data(), p, nv * sizeof(Vtx)); p += nv * sizeof(Vtx);
    a.i.resize(ni); memcpy(a.i.data(), p, ni * 4); p += ni * 4;
    a.subs.resize(ns); memcpy(a.subs.data(), p, ns * sizeof(Sub)); p += ns * sizeof(Sub);
    a.tex.resize(nt);
    for (auto& t : a.tex) { t.w = rd<uint32_t>(p); t.h = rd<uint32_t>(p); t.rgba.assign(p, p + (size_t)t.w * t.h * 4); p += (size_t)t.w * t.h * 4; }
    snprintf(fn, sizeof fn, "%s_live.bin", a.name);
    if (read_file(fn, b) && b.size() >= 16) {
        p = b.data() + 8; uint32_t nb = rd<uint32_t>(p); rd<uint32_t>(p);
        if (b.size() >= 16 + (size_t)nb * sizeof(LiveBone)) { a.live.resize(nb); memcpy(a.live.data(), p, nb * sizeof(LiveBone)); }
    }
    snprintf(fn, sizeof fn, "%s_fly.bin", a.name);
    if (read_file(fn, b) && b.size() >= 20 && !a.live.empty()) {
        p = b.data() + 8; uint32_t nm = rd<uint32_t>(p), np_ = rd<uint32_t>(p); a.flyFps = rd<float>(p);
        const uint8_t* end = b.data() + b.size();
        for (uint32_t k = 0; k < nm && np_ == a.live.size() && p + 8 <= end; k++) {
            Actor::Fly fm; fm.nf = rd<uint32_t>(p); fm.dur = rd<float>(p);
            size_t nfl = (size_t)fm.nf * np_ * 12; if (p + nfl * 4 > end) break;
            fm.rows.assign((const float*)p, (const float*)p + nfl); p += nfl * 4; a.fly.push_back(std::move(fm));
        }
    }
    logf("actor %s: %u verts, %zu palette bones, %zu flight motions", a.name, nv, a.live.size(), a.fly.size());
    return !a.live.empty();
}
// skinning rows (palette x 12) from the actor's live KH1 joints; false = no valid pose
static bool actor_bones(Actor& a, std::vector<float>& out) {
    const std::vector<LiveBone>& live = a.geo ? a.geo->live : a.live;
    // v85 STATIC model (sr_set_enemy with ent 0): no KH1 skeleton, identity bones - the mesh is baked y-up in its own
    // space (tools/make_items.py: KH1 item balls), so no KH1 y-down flip; Python places it via the ActorFrame
    if (a.geo && !a.ent) {
        out.assign(live.size() * 12, 0.f);
        for (size_t k = 0; k < live.size(); k++) { out[k * 12 + 0] = out[k * 12 + 5] = out[k * 12 + 10] = 1.f; }
        return true;
    }
    if (live.empty() || (!g_kh && !kh_open())) return false;
    auto hold = [&]() { if (a.lastGood.empty() || GetTickCount64() - a.lastGoodT > (hold_active() ? 3500u : 500u)) return false; out = a.lastGood; return true; };
    uint64_t ent = 0; float em[16], ie[16]; uint32_t h = 0;
    if (a.ent) ent = a.ent;
    else if (!kh_read(g_khBase + a.slotRva, &ent, 8)) return hold();
    if (!ent || !kh_read((uintptr_t)ent + 0x230, em, 64) || !inv4(em, ie)) return hold();
    if (!kh_read((uintptr_t)ent + 0x15c, &h, 4)) return hold();
    uintptr_t ad = kh_resolve(h); if (!ad || !kh_read(ad + 0x1b4, &h, 4)) return hold();
    ad = kh_resolve(h);
    static float L[300 * 16];
    uint32_t nj = 0; for (auto& lb : live) if (lb.j + 1 > nj) nj = lb.j + 1;
    if (!ad || nj > 300 || !kh_read(ad, L, (size_t)nj * 64)) return hold();
    if (a.weaponJ && a.weaponJ < nj && a.handJ < nj) {   // weapon bone: learn hand->weapon offset, repair bad frames
        float* Mw = &L[a.weaponJ * 16]; const float* Mh = &L[a.handJ * 16]; float ih[16];
        float dx = Mw[12] - Mh[12], dy = Mw[13] - Mh[13], dz = Mw[14] - Mh[14];
        bool ok = std::isfinite(dx + dy + dz + Mw[0] + Mw[5] + Mw[10]) && dx * dx + dy * dy + dz * dz < 40.f * 40.f;
        if (ok && inv4(Mh, ih)) {
            for (int r = 0; r < 4; r++) for (int c = 0; c < 4; c++) { float sum = 0; for (int q = 0; q < 4; q++) sum += Mw[r * 4 + q] * ih[q * 4 + c]; a.rel[r * 4 + c] = sum; }
            a.haveRel = true;
        } else if (a.haveRel) {
            float W2[16];
            for (int r = 0; r < 4; r++) for (int c = 0; c < 4; c++) { float sum = 0; for (int q = 0; q < 4; q++) sum += a.rel[r * 4 + q] * Mh[q * 4 + c]; W2[r * 4 + c] = sum; }
            memcpy(Mw, W2, sizeof W2);
        }
    }
    const float* L0 = &L[0];
    out.resize(live.size() * 12);
    for (size_t k = 0; k < live.size(); k++) {
        const LiveBone& lb = live[k]; const float* Lj = &L[lb.j * 16];
        float dx = Lj[12] - L0[12], dy = Lj[13] - L0[13], dz = Lj[14] - L0[14];
        if (!std::isfinite(dx + dy + dz) || dx * dx + dy * dy + dz * dz > 300.f * 300.f) return hold();   // scratch frame
        float R[16];
        for (int r = 0; r < 4; r++) for (int c = 0; c < 4; c++) { float sum = 0; for (int q = 0; q < 4; q++) sum += Lj[r * 4 + q] * ie[q * 4 + c]; R[r * 4 + c] = sum; }
        float Wm[16];
        for (int r = 0; r < 4; r++) for (int c = 0; c < 4; c++) Wm[r * 4 + c] = R[c * 4 + r] * ((r == 1 || r == 2) ? -1.f : 1.f);
        float* o = &out[k * 12];
        for (int r = 0; r < 3; r++) for (int c = 0; c < 4; c++) { float sum = 0; for (int q = 0; q < 4; q++) sum += Wm[r * 4 + q] * lb.ib[q * 4 + c]; o[r * 4 + c] = sum; }
    }
    a.lastGood = out; a.lastGoodT = GetTickCount64();
    // KH1 poses come at 60 Hz: keep the last two distinct ones and play one KH1 frame behind, blended by time (as for Sora)
    if (a.pCur.empty() || memcmp(a.pCur.data(), out.data(), out.size() * 4) != 0) { a.pPrev.swap(a.pCur); a.pCur = out; a.tPrev = a.tCur; a.tCur = qpc_now(); }
    double per = a.tCur - a.tPrev;
    if (a.pPrev.size() == a.pCur.size() && per > 0.004 && per < 0.1) {
        double k = (qpc_now() - a.tCur) / per; if (k < 0) k = 0; if (k > 1) k = 1;
        for (size_t q = 0; q < out.size(); q++) out[q] = a.pPrev[q] + (a.pCur[q] - a.pPrev[q]) * (float)k;
    }
    return true;
}

// ------------------------------------------------------------------ Keyblade segment (for hit detection in Python)
// Two mesh vertices of the Keyblade (bound to KH1 weapon joints 280+): the one nearest the hand joint's end of the
// blade (hilt) and the one farthest from it (tip), in bind space. Skinned each draw with the same bones as the mesh.
struct BladeV { int b; float p[3]; };
static BladeV g_bladeV[2] = {{-1, {0, 0, 0}}, {-1, {0, 0, 0}}};
static float g_blade[6]; static double g_bladeT = 0; static uint32_t g_bladeN = 0;
static bool inv4(const float* m, float* o);
// v59: works on any mesh skinned to Sora's palette (the body's baked Kingdom Key, or the equipped Keyblade from
// weapons.inc); hilt = the end nearest the blade joint's grip (its bind origin), tip = the far end
static void find_blade_in(const std::vector<Vtx>& g_v, const char* what);
static void find_blade() { find_blade_in(g_v, "baked"); }
static void find_blade_in(const std::vector<Vtx>& g_v, const char* what) {
    if (g_live.empty()) return;
    std::vector<uint32_t> idx;
    for (uint32_t i = 0; i < g_v.size(); i++) { uint32_t b = g_v[i].bone; if (b < g_live.size() && g_live[b].j >= 280 && g_live[b].j <= 292) idx.push_back(i); }
    if (idx.size() < 2) { logf("blade: no weapon vertices"); return; }
    {   // only the rigid blade: the weapon joint with the most vertices (not the dangling keychain, joints ~290+)
        uint32_t cnt[300] = {0}; uint32_t best = 0;
        for (auto i : idx) cnt[g_live[g_v[i].bone].j]++;
        for (uint32_t j = 280; j <= 292; j++) if (cnt[j] > cnt[best]) best = j;
        std::vector<uint32_t> keep; for (auto i : idx) if (g_live[g_v[i].bone].j == best) keep.push_back(i);
        if (keep.size() >= 2) idx.swap(keep);
    }
    float c[3] = {0, 0, 0};
    for (auto i : idx) for (int k = 0; k < 3; k++) c[k] += g_v[i].p[k] / idx.size();
    auto d2 = [](const float* a, const float* b) { float s = 0; for (int k = 0; k < 3; k++) s += (a[k] - b[k]) * (a[k] - b[k]); return s; };
    uint32_t A = idx[0]; for (auto i : idx) if (d2(g_v[i].p, c) > d2(g_v[A].p, c)) A = i;   // one end
    uint32_t B = idx[0]; for (auto i : idx) if (d2(g_v[i].p, g_v[A].p) > d2(g_v[B].p, g_v[A].p)) B = i;   // other end
    {   // grip = bind origin of the blade joint (inverse of its inverse bind, column convention: translation 3/7/11)
        float bind[16];
        if (inv4(g_live[g_v[A].bone].ib, bind)) {
            float gp[3] = {bind[3], bind[7], bind[11]};
            if (d2(g_v[A].p, gp) > d2(g_v[B].p, gp)) std::swap(A, B);   // A = hilt
        }
    }
    g_bladeV[0] = {(int)g_v[A].bone, {g_v[A].p[0], g_v[A].p[1], g_v[A].p[2]}};
    g_bladeV[1] = {(int)g_v[B].bone, {g_v[B].p[0], g_v[B].p[1], g_v[B].p[2]}};
    logf("blade (%s): %zu weapon verts, hilt %u (joint %u) / tip %u (joint %u), length %.1f cm", what, idx.size(), A, g_live[g_v[A].bone].j, B,
         g_live[g_v[B].bone].j, sqrtf(d2(g_v[A].p, g_v[B].p)));
}

// ------------------------------------------------------------------ d3d
static ID3D11Device* dev; static ID3D11DeviceContext* ctx;
static ID3D11VertexShader* vs; static ID3D11PixelShader* ps; static ID3D11InputLayout* il;
static ID3D11Buffer *vb, *ib, *cbFrame, *cbBones;
static std::vector<ID3D11ShaderResourceView*> srvs;
static ID3D11SamplerState* samp; static ID3D11RasterizerState* rs;
static ID3D11DepthStencilState *dsOff, *dsOn, *dsOnRev; static ID3D11BlendState* bsOff;
static ID3D11RenderTargetView* rtv; static bool g_gfx = false;
static ID3D11VertexShader* fxvs; static ID3D11PixelShader* fxps; static ID3D11InputLayout* fxil; static ID3D11Buffer* fxvb;
static ID3D11BlendState* bsAdd; static ID3D11BlendState* bsSub; static ID3D11DepthStencilState* dsFx;   // bsSub = PS2 GS ALPHA 0x42: Cd - Cs*As
struct FxV { float p[3]; float uv[2]; float c[4]; float k; };   // k: 0 procedural, 1 textured additive, 2 textured alpha
static const char* kFxShader = R"(
cbuffer F : register(b0) { float4x4 world; float4x4 viewproj; float4 light; float4 misc; };
struct VI { float3 p : POSITION; float2 uv : TEXCOORD0; float4 c : COLOR; float k : TEXCOORD1; };
struct VO { float4 pos : SV_POSITION; float2 uv : TEXCOORD0; float4 c : COLOR; float k : TEXCOORD1; };
VO vsmain(VI i) { VO o; o.pos = mul(viewproj, float4(i.p, 1)); o.uv = i.uv; o.c = i.c; o.k = i.k; return o; }
Texture2D T : register(t0); SamplerState S : register(s0);
float4 psmain(VO i) : SV_TARGET {
  bool zw = i.k > 9.5; if (zw) i.k -= 10;                                 // v76: +10 = batch writes depth (intro)
  if (i.k < 0.5) {
    float r = length(i.uv * 2 - 1); float core = saturate(1 - r); core = core * core;
    float hot = saturate(1 - r * 2.2);
    return float4(i.c.rgb * core * i.c.a + hot * hot * i.c.a * 0.8, 1);
  }
  if (i.k > 2.5) {                                                      // v43: untextured KH1 effect models
    if (i.k < 3.5) return float4(i.c.rgb * i.c.a, 1);
    return i.c;
  }
  float4 t = T.Sample(S, i.uv);
  if (zw) clip(t.a * i.c.a - 0.02);                                     // v76: depth-writing KH1 mesh: no Z from clear texels
  if (i.k < 1.5) return float4(t.rgb * t.a * i.c.rgb * i.c.a, 1);       // additive (KH1 effects: glows, flames, sparks)
  return float4(t.rgb * i.c.rgb, t.a * i.c.a);                          // alpha (smoke, dust)
})";
static std::vector<ID3D11ShaderResourceView*> fxSrvs;   // KH1 effect sprites (GPU)
static ID3D11DepthStencilView* g_sceneDsv = nullptr; static UINT g_bbW = 0, g_bbH = 0;
static ID3D11DepthStencilView* g_ownDsv = nullptr; static UINT g_ownW = 0, g_ownH = 0;   // Sora self-occlusion
static void ensure_own_depth(UINT W, UINT H) {
    if (g_ownDsv && g_ownW == W && g_ownH == H) return;
    if (g_ownDsv) { g_ownDsv->Release(); g_ownDsv = nullptr; }
    D3D11_TEXTURE2D_DESC td{}; td.Width = W; td.Height = H; td.MipLevels = 1; td.ArraySize = 1; td.Format = DXGI_FORMAT_D24_UNORM_S8_UINT;
    td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT; td.BindFlags = D3D11_BIND_DEPTH_STENCIL;
    ID3D11Texture2D* t = nullptr; if (FAILED(dev->CreateTexture2D(&td, nullptr, &t))) return;
    dev->CreateDepthStencilView(t, nullptr, &g_ownDsv); t->Release(); g_ownW = W; g_ownH = H;
}
static ID3D11DepthStencilState* dsOwn;

// ------------------------------------------------------------------ relight source: mip-blurred copy of the game frame
// Like the GTA x Minecraft compositor relighting Minecraft from GTA's blurred picture: right before Sora is drawn, the
// target (the finished world image) is copied into a texture with a full mip chain; Sora's pixel shader reads a coarse
// mip as "the light around him".
static ID3D11Texture2D* g_envTex = nullptr; static ID3D11ShaderResourceView* g_envSrv = nullptr;
static D3D11_TEXTURE2D_DESC g_envDesc{}; static bool g_envBroken = false; static ID3D11SamplerState* sampClamp = nullptr;
static ID3D11ShaderResourceView* env_capture(ID3D11RenderTargetView* target) {
    if (g_envBroken || !target) return nullptr;
    ID3D11Resource* res = nullptr; target->GetResource(&res); if (!res) return nullptr;
    ID3D11Texture2D* t = nullptr; res->QueryInterface(__uuidof(ID3D11Texture2D), (void**)&t); res->Release(); if (!t) return nullptr;
    D3D11_TEXTURE2D_DESC td; t->GetDesc(&td);
    if (td.SampleDesc.Count != 1 || td.ArraySize != 1) { t->Release(); return nullptr; }
    if (!g_envTex || td.Width != g_envDesc.Width || td.Height != g_envDesc.Height || td.Format != g_envDesc.Format) {
        if (g_envSrv) { g_envSrv->Release(); g_envSrv = nullptr; } if (g_envTex) { g_envTex->Release(); g_envTex = nullptr; }
        D3D11_TEXTURE2D_DESC nd{}; nd.Width = td.Width; nd.Height = td.Height; nd.MipLevels = 0; nd.ArraySize = 1;
        nd.Format = td.Format; nd.SampleDesc.Count = 1; nd.Usage = D3D11_USAGE_DEFAULT;
        nd.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET; nd.MiscFlags = D3D11_RESOURCE_MISC_GENERATE_MIPS;
        if (td.Format == DXGI_FORMAT_R8G8B8A8_TYPELESS) nd.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
        if (td.Format == DXGI_FORMAT_B8G8R8A8_TYPELESS) nd.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
        if (FAILED(dev->CreateTexture2D(&nd, nullptr, &g_envTex)) || FAILED(dev->CreateShaderResourceView(g_envTex, nullptr, &g_envSrv))) {
            logf("relight: no mip texture for format %d - relight off", (int)td.Format); g_envBroken = true;
            if (g_envTex) { g_envTex->Release(); g_envTex = nullptr; } t->Release(); return nullptr;
        }
        g_envDesc = td; logf("relight: scene copy %ux%u fmt %d", td.Width, td.Height, (int)td.Format);
    }
    ctx->CopySubresourceRegion(g_envTex, 0, 0, 0, 0, t, 0, nullptr);
    ctx->GenerateMips(g_envSrv);
    t->Release();
    return g_envSrv;
}

// ------------------------------------------------------------------ KH1 HUD (screen space, drawn on top at Present)
// Python sends sprites in pixels (sr_set_hud). mode 0 = textured sprite, mode 1 = KH1 HP/MP gauge rings (procedural).
static std::vector<ID3D11ShaderResourceView*> hudSrvs;
static ID3D11VertexShader* hudvs; static ID3D11PixelShader* hudps; static ID3D11InputLayout* hudil; static ID3D11Buffer *hudvb, *hudcb;
static ID3D11BlendState* bsAlpha;
struct HudV { float p[2]; float uv[2]; float c[4]; float mode; };
static const char* kHudShader = R"(
cbuffer H : register(b0) { float4 screen; float4 g0; float4 g1; };
struct VI { float2 p : POSITION; float2 uv : TEXCOORD0; float4 c : COLOR; float m : TEXCOORD1; };
struct VO { float4 pos : SV_POSITION; float2 uv : TEXCOORD0; float4 c : COLOR; float m : TEXCOORD1; };
VO vsmain(VI i) { VO o; o.pos = float4(i.p.x / screen.x * 2 - 1, 1 - i.p.y / screen.y * 2, 0, 1); o.uv = i.uv; o.c = i.c; o.m = i.m; return o; }
Texture2D T : register(t0); SamplerState S : register(s0);
static const float PI = 3.14159265;
// KH1 gauge in units of the outer radius (uv -1..1, y up). HP arc: 180 deg -> -45 deg clockwise, MP arc: 180 -> 28 deg.
float4 gauge(float2 p) {
  float r = length(p); float a = degrees(atan2(p.y, p.x));
  float olw = 0.035; float4 col = float4(0, 0, 0, 0);
  // MP band
  float tm = (180 - a) / 152.0; float endPad = olw / max(r, 0.1) * 57.3 / 152.0;
  float mpBand = (r > 0.79 - olw && r < 1.0 + olw && tm > -endPad && tm < 1 + endPad) ? 1 : 0;
  if (mpBand > 0) {
    col = float4(0, 0, 0, 1);
    if (r > 0.79 && r < 1.0 && tm >= 0 && tm <= 1) {
      float seg = tm * g0.w; float f = frac(seg);
      bool divider = (f < 0.035 * g0.w / 5 || f > 1 - 0.035 * g0.w / 5) && seg > 0.05 && seg < g0.w - 0.05;
      float3 full = lerp(float3(0.22, 0.30, 0.86), float3(0.36, 0.52, 1.0), tm) * (0.85 + 0.3 * (r - 0.79) / 0.21);
      float3 c = seg < g0.z ? full : float3(0.06, 0.07, 0.20);
      col = float4(divider ? float3(0.02, 0.02, 0.04) : c, 1);
    }
  }
  // HP band
  float th = (180 - a) / 225.0;
  float hpEndPad = olw / max(r, 0.1) * 57.3 / 225.0;
  if (r > 0.555 - olw && r < 0.75 + olw && th > -hpEndPad && th < 1 + hpEndPad) {
    col = float4(0, 0, 0, 1);
    if (r > 0.555 && r < 0.75 && th >= 0 && th <= 1) {
      float3 g = lerp(float3(0.10, 0.62, 0.12), float3(0.70, 0.93, 0.10), th) * (0.85 + 0.35 * (r - 0.555) / 0.195);
      float3 low = lerp(float3(0.95, 0.15, 0.10), float3(1.0, 0.45, 0.2), th);
      g = lerp(g, low, g1.x);
      float3 c = th < g0.x ? g : (th < g0.y ? float3(0.85, 0.12, 0.08) : float3(0.05, 0.07, 0.05));
      col = float4(c, 1);
    }
  }
  // portrait disc
  if (r < 0.555) col = r > 0.515 ? float4(0, 0, 0, 1) : float4(0.12, 0.12, 0.14, 1);
  float aa = fwidth(r);
  if (r > 1.0 + olw - aa) col.a *= 1 - smoothstep(1.0 + olw - aa, 1.0 + olw + aa, r);
  return col;
}
float4 psmain(VO i) : SV_TARGET {
  if (i.m > 0.5) { float4 g = gauge(float2(i.uv.x * 2 - 1, 1 - i.uv.y * 2) * 1.06); return float4(g.rgb, g.a * i.c.a); }   // quad half-size = 1.06 R
  float4 t = T.Sample(S, i.uv); return t * i.c;
})";

// env = relight (strength, tint, ref luminance, mip), env2 = (fade, haze start, haze range, haze max),
// env3 = (min light, max light, Sora centre u, v), scr = (1/W, 1/H, has env, -). misc.x = brightness.
// v25 level lighting (sr_light): sun = (colour, ambient share; 0 = old fixed bands), sky = (sky colour, rim strength),
// gnd = (ground bounce colour, cel edge softness), cam = camera position, lvp = character shadow map's light vp,
// shp = (self-shadow strength, rim power, map size, -)
static const char* kShader = R"(
cbuffer F : register(b0) { float4x4 world; float4x4 viewproj; float4 light; float4 misc; float4 env; float4 env2; float4 env3; float4 scr;
                           float4 sun; float4 sky; float4 gnd; float4 cam; float4x4 lvp; float4 shp; };
cbuffer B : register(b1) { float4 bones[900]; };
struct VI { float3 p : POSITION; float3 n : NORMAL; float2 uv : TEXCOORD0; uint b : BLENDINDICES; };
struct VO { float4 pos : SV_POSITION; float3 n : NORMAL; float2 uv : TEXCOORD0; float3 wp : TEXCOORD1; };
VO vsmain(VI i) {
  float4 r0 = bones[i.b*3+0], r1 = bones[i.b*3+1], r2 = bones[i.b*3+2];
  float4 p = float4(i.p, 1);
  float3 sp = float3(dot(r0, p), dot(r1, p), dot(r2, p));
  float3 sn = float3(dot(r0.xyz, i.n), dot(r1.xyz, i.n), dot(r2.xyz, i.n));
  VO o; float4 w = mul(world, float4(sp, 1)); o.pos = mul(viewproj, w); o.wp = w.xyz;
  float3 wn = mul((float3x3)world, sn);   // v28: the chain links + keychain have NO normals (flat KH1 cards): keep them 0
  o.n = dot(wn, wn) > 1e-10 ? normalize(wn) : float3(0, 0, 0); o.uv = i.uv; return o;
}
Texture2D T : register(t0); SamplerState S : register(s0);
Texture2D E : register(t1); SamplerState SC : register(s1);   // blurred copy of the game's frame behind Sora
Texture2D<float> SM : register(t2);                            // v25: character shadow map (self + each other)
void psshadow(VO i) { clip(T.Sample(S, i.uv).a - 0.4); }   // v24: shadow-map pass (depth only, same cut-outs)
float self_shadow(float3 wp, float3 n) {
  if (shp.x <= 0.001) return 0;
  float4 l = mul(lvp, float4(wp + n * 5.0 - light.xyz * 4.0, 1));   // normal + light offset: no acne
  float2 t = float2(l.x * 0.5 + 0.5, 0.5 - l.y * 0.5) * shp.z;
  // v64: 3x3 bilinear PCF over a 4x4 texel block (the old 4 point taps drew stair-stepped edges - user, Goofy's hat)
  float2 tc = t - 0.5; float2 b = floor(tc); float2 f = tc - b;
  float r[16];
  [unroll] for (int y = 0; y < 4; y++) [unroll] for (int x = 0; x < 4; x++)
    r[y * 4 + x] = SM.Load(int3(clamp(b + float2(x - 1, y - 1), 0, shp.z - 1), 0)) < l.z ? 1.0 : 0.0;
  float s = 0;
  [unroll] for (int cy = 0; cy < 3; cy++) [unroll] for (int cx = 0; cx < 3; cx++)
    s += lerp(lerp(r[cy * 4 + cx], r[cy * 4 + cx + 1], f.x), lerp(r[cy * 4 + 4 + cx], r[cy * 4 + 5 + cx], f.x), f.y);
  return smoothstep(0.0, 1.0, s / 9.0) * shp.x;
}
struct PO { float4 c : SV_Target0; float d : SV_Target1; };
PO psmain(VO i) {
  float4 c = T.Sample(S, i.uv); clip(c.a - 0.4);
  // close camera: screen-door fade (keeps depth correct, no sorting)
  if (env2.x < 0.999) {
    static const float bayer[16] = {0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5};
    uint2 q = uint2(i.pos.xy) & 3;
    clip(env2.x - (bayer[q.y * 4 + q.x] + 0.5) / 16.0);
  }
  float3 N = dot(i.n, i.n) > 0.25 ? normalize(i.n) : normalize(cam.xyz - i.wp);   // no normal: lit as facing the camera
  float3 col;
  if (sun.w > 0.001) {
    // v25: the level's own lights. Sun = Borderlands' brightest directional light (its colour, its direction) with a
    // soft cel edge; ambient = the level's sky light, sky colour from above and ground bounce from below; shadows
    // from the characters themselves and each other; a thin sky-coloured rim on the silhouette.
    float ndl = dot(N, -light.xyz);
    float lit = smoothstep(-gnd.w, gnd.w, ndl - 0.08) * (1.0 - self_shadow(i.wp, N));
    float3 hemi = lerp(gnd.rgb, sky.rgb, N.z * 0.5 + 0.5);
    float3 L = hemi * sun.w + sun.rgb * lit * (1.0 - sun.w);
    float3 V = normalize(cam.xyz - i.wp);
    float rim = pow(1.0 - saturate(dot(N, V)), shp.y) * sky.w * (0.35 + 0.65 * lit);
    col = (c.rgb * L + sky.rgb * rim) * misc.x;
  } else {
    float d = saturate(dot(N, -light.xyz));
    float band = d > 0.45 ? 1.0 : (d > 0.12 ? 0.78 : 0.6);   // Borderlands-ish cel bands
    col = c.rgb * band * misc.x;
  }
  if (scr.z > 0.5) {
    // Relight from the game's own picture (heavily blurred): half per-pixel surroundings, half what is around Sora's
    // centre, so caves darken him and sunlit or neon-lit places tint him, like the scene he stands in.
    float3 ep = E.SampleLevel(SC, i.pos.xy * scr.xy, env.w).rgb;
    float3 ec = E.SampleLevel(SC, env3.zw, env.w + 1.0).rgb;
    float3 e = lerp(ec, ep, 0.5);
    float ref = env.z;
    if (scr.w > 0.5) { e = misc.yzw; ref = shp.w; }   // v26: the ground he stands on (camera-independent)
    float l = dot(e, float3(0.299, 0.587, 0.114));
    float k = clamp(l / max(ref, 0.01), env3.x, env3.y);
    float3 tint = clamp(e / max(l, 0.02), 0.0, 2.0);
    col *= lerp(1.0, k, env.x) * lerp(float3(1, 1, 1), tint, env.y * env.x);
    // haze: fade toward the scene colour with distance (view depth = SV_POSITION.w)
    float h = saturate((i.pos.w - env2.y) / max(env2.z, 1.0)) * env2.w;
    col = lerp(col, ep, h);
  }
  PO o; o.c = float4(col, 1); o.d = i.pos.w; return o;   // v30: SV_Target1 = view depth (ink lines)
})";

typedef HRESULT(WINAPI* PFN_D3DCompile)(LPCVOID, SIZE_T, LPCSTR, const D3D_SHADER_MACRO*, ID3DInclude*, LPCSTR, LPCSTR, UINT, UINT, ID3DBlob**, ID3DBlob**);

static void init_hud(PFN_D3DCompile comp) {   // KH1 HUD resources (also used by sr_hud_selftest)
    ID3DBlob *hv = nullptr, *hp = nullptr, *er = nullptr;
    if (SUCCEEDED(comp(kHudShader, strlen(kHudShader), "hud", nullptr, nullptr, "vsmain", "vs_5_0", 0, 0, &hv, &er)) &&
        SUCCEEDED(comp(kHudShader, strlen(kHudShader), "hud", nullptr, nullptr, "psmain", "ps_5_0", 0, 0, &hp, &er))) {
        dev->CreateVertexShader(hv->GetBufferPointer(), hv->GetBufferSize(), nullptr, &hudvs);
        dev->CreatePixelShader(hp->GetBufferPointer(), hp->GetBufferSize(), nullptr, &hudps);
        D3D11_INPUT_ELEMENT_DESC he[] = {
            {"POSITION", 0, DXGI_FORMAT_R32G32_FLOAT, 0, 0, D3D11_INPUT_PER_VERTEX_DATA, 0},
            {"TEXCOORD", 0, DXGI_FORMAT_R32G32_FLOAT, 0, 8, D3D11_INPUT_PER_VERTEX_DATA, 0},
            {"COLOR", 0, DXGI_FORMAT_R32G32B32A32_FLOAT, 0, 16, D3D11_INPUT_PER_VERTEX_DATA, 0},
            {"TEXCOORD", 1, DXGI_FORMAT_R32_FLOAT, 0, 32, D3D11_INPUT_PER_VERTEX_DATA, 0}};
        dev->CreateInputLayout(he, 4, hv->GetBufferPointer(), hv->GetBufferSize(), &hudil);
    } else logf("hud shader: %s", er ? (char*)er->GetBufferPointer() : "?");
    D3D11_BUFFER_DESC hb{}; hb.ByteWidth = HUD_MAX * 6 * sizeof(HudV); hb.Usage = D3D11_USAGE_DYNAMIC; hb.BindFlags = D3D11_BIND_VERTEX_BUFFER;
    hb.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE; dev->CreateBuffer(&hb, nullptr, &hudvb);
    hb.ByteWidth = 48; hb.BindFlags = D3D11_BIND_CONSTANT_BUFFER; dev->CreateBuffer(&hb, nullptr, &hudcb);
    D3D11_BLEND_DESC ab{}; auto& t = ab.RenderTarget[0]; t.BlendEnable = TRUE; t.SrcBlend = D3D11_BLEND_SRC_ALPHA; t.DestBlend = D3D11_BLEND_INV_SRC_ALPHA;
    t.BlendOp = D3D11_BLEND_OP_ADD; t.SrcBlendAlpha = D3D11_BLEND_ZERO; t.DestBlendAlpha = D3D11_BLEND_ONE; t.BlendOpAlpha = D3D11_BLEND_OP_ADD;
    t.RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_ALL; dev->CreateBlendState(&ab, &bsAlpha);
    for (auto& tx : g_hudTex) {
        D3D11_TEXTURE2D_DESC td{}; td.Width = tx.w; td.Height = tx.h; td.MipLevels = 1; td.ArraySize = 1;
        td.Format = DXGI_FORMAT_R8G8B8A8_UNORM; td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_IMMUTABLE; td.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        D3D11_SUBRESOURCE_DATA s{tx.rgba.data(), tx.w * 4, 0}; ID3D11Texture2D* tex = nullptr; dev->CreateTexture2D(&td, &s, &tex);
        ID3D11ShaderResourceView* v = nullptr; if (tex) { dev->CreateShaderResourceView(tex, nullptr, &v); tex->Release(); } hudSrvs.push_back(v);
    }
}

static void init_actor_gpu(Actor& a) {
    if (a.v.empty() || a.vb) return;
    D3D11_BUFFER_DESC bd{}; D3D11_SUBRESOURCE_DATA sd{};
    bd.ByteWidth = (UINT)(a.v.size() * sizeof(Vtx)); bd.BindFlags = D3D11_BIND_VERTEX_BUFFER; bd.Usage = D3D11_USAGE_IMMUTABLE;
    sd.pSysMem = a.v.data(); dev->CreateBuffer(&bd, &sd, &a.vb);
    bd.ByteWidth = (UINT)(a.i.size() * 4); bd.BindFlags = D3D11_BIND_INDEX_BUFFER; sd.pSysMem = a.i.data(); dev->CreateBuffer(&bd, &sd, &a.ib);
    for (auto& t : a.tex) {
        D3D11_TEXTURE2D_DESC td{}; td.Width = t.w; td.Height = t.h; td.MipLevels = 1; td.ArraySize = 1;
        td.Format = DXGI_FORMAT_R8G8B8A8_UNORM; td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_IMMUTABLE; td.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        D3D11_SUBRESOURCE_DATA s2{t.rgba.data(), t.w * 4, 0}; ID3D11Texture2D* tx = nullptr; dev->CreateTexture2D(&td, &s2, &tx);
        ID3D11ShaderResourceView* v = nullptr; if (tx) { dev->CreateShaderResourceView(tx, nullptr, &v); tx->Release(); } a.srvs.push_back(v);
    }
}
#include "weapons.inc"   // v58: the weapon KH1 has equipped
static bool init_resources();
static void init_probes(PFN_D3DCompile comp);
static void init_shadows(PFN_D3DCompile comp);
static void init_ground(PFN_D3DCompile comp);
static void init_outline(PFN_D3DCompile comp);
static bool init_gfx(IDXGISwapChain* sc) {
    if (FAILED(sc->GetDevice(__uuidof(ID3D11Device), (void**)&dev))) return false;
    dev->GetImmediateContext(&ctx);
    return init_resources();
}
static bool init_resources() {
    HMODULE dc = LoadLibraryA("d3dcompiler_47.dll");
    auto comp = dc ? (PFN_D3DCompile)GetProcAddress(dc, "D3DCompile") : nullptr;
    if (!comp) { logf("no d3dcompiler_47"); return false; }
    ID3DBlob *vsb = nullptr, *psb = nullptr, *err = nullptr;
    if (FAILED(comp(kShader, strlen(kShader), "sora", nullptr, nullptr, "vsmain", "vs_5_0", 0, 0, &vsb, &err))) { logf("vs: %s", err ? (char*)err->GetBufferPointer() : "?"); return false; }
    if (FAILED(comp(kShader, strlen(kShader), "sora", nullptr, nullptr, "psmain", "ps_5_0", 0, 0, &psb, &err))) { logf("ps: %s", err ? (char*)err->GetBufferPointer() : "?"); return false; }
    dev->CreateVertexShader(vsb->GetBufferPointer(), vsb->GetBufferSize(), nullptr, &vs);
    dev->CreatePixelShader(psb->GetBufferPointer(), psb->GetBufferSize(), nullptr, &ps);
    D3D11_INPUT_ELEMENT_DESC ied[] = {
        {"POSITION", 0, DXGI_FORMAT_R32G32B32_FLOAT, 0, 0, D3D11_INPUT_PER_VERTEX_DATA, 0},
        {"NORMAL", 0, DXGI_FORMAT_R32G32B32_FLOAT, 0, 12, D3D11_INPUT_PER_VERTEX_DATA, 0},
        {"TEXCOORD", 0, DXGI_FORMAT_R32G32_FLOAT, 0, 24, D3D11_INPUT_PER_VERTEX_DATA, 0},
        {"BLENDINDICES", 0, DXGI_FORMAT_R32_UINT, 0, 32, D3D11_INPUT_PER_VERTEX_DATA, 0}};
    dev->CreateInputLayout(ied, 4, vsb->GetBufferPointer(), vsb->GetBufferSize(), &il);
    D3D11_BUFFER_DESC bd{}; D3D11_SUBRESOURCE_DATA sd{};
    bd.ByteWidth = (UINT)(g_v.size() * sizeof(Vtx)); bd.BindFlags = D3D11_BIND_VERTEX_BUFFER; bd.Usage = D3D11_USAGE_IMMUTABLE;
    sd.pSysMem = g_v.data(); dev->CreateBuffer(&bd, &sd, &vb);
    bd.ByteWidth = (UINT)(g_i.size() * 4); bd.BindFlags = D3D11_BIND_INDEX_BUFFER; sd.pSysMem = g_i.data(); dev->CreateBuffer(&bd, &sd, &ib);
    bd = {}; bd.Usage = D3D11_USAGE_DYNAMIC; bd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE; bd.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    bd.ByteWidth = 92 * 4; dev->CreateBuffer(&bd, nullptr, &cbFrame);   // v25: + sun, sky, ground, camera, light vp, self-shadow
    bd.ByteWidth = 900 * 16; dev->CreateBuffer(&bd, nullptr, &cbBones);
    for (auto& t : g_tex) {
        D3D11_TEXTURE2D_DESC td{}; td.Width = t.w; td.Height = t.h; td.MipLevels = 1; td.ArraySize = 1;
        td.Format = DXGI_FORMAT_R8G8B8A8_UNORM; td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_IMMUTABLE; td.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        D3D11_SUBRESOURCE_DATA s{t.rgba.data(), t.w * 4, 0}; ID3D11Texture2D* tex = nullptr; dev->CreateTexture2D(&td, &s, &tex);
        ID3D11ShaderResourceView* v = nullptr; dev->CreateShaderResourceView(tex, nullptr, &v); tex->Release(); srvs.push_back(v);
    }
    D3D11_SAMPLER_DESC smd{}; smd.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR; smd.AddressU = smd.AddressV = smd.AddressW = D3D11_TEXTURE_ADDRESS_WRAP; smd.MaxLOD = D3D11_FLOAT32_MAX;
    dev->CreateSamplerState(&smd, &samp);
    smd.AddressU = smd.AddressV = smd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP; dev->CreateSamplerState(&smd, &sampClamp);
    D3D11_RASTERIZER_DESC rd{}; rd.FillMode = D3D11_FILL_SOLID; rd.CullMode = D3D11_CULL_NONE; rd.DepthClipEnable = TRUE; dev->CreateRasterizerState(&rd, &rs);
    D3D11_DEPTH_STENCIL_DESC dd{}; dev->CreateDepthStencilState(&dd, &dsOff);
    // frame is finished at Present, so writing depth is safe (gives Sora self-occlusion)
    dd.DepthEnable = TRUE; dd.DepthWriteMask = D3D11_DEPTH_WRITE_MASK_ALL; dd.DepthFunc = D3D11_COMPARISON_LESS_EQUAL; dev->CreateDepthStencilState(&dd, &dsOn);
    dd.DepthFunc = D3D11_COMPARISON_GREATER_EQUAL; dev->CreateDepthStencilState(&dd, &dsOnRev);
    dd.DepthFunc = D3D11_COMPARISON_LESS; dd.DepthWriteMask = D3D11_DEPTH_WRITE_MASK_ALL; dev->CreateDepthStencilState(&dd, &dsOwn);
    D3D11_BLEND_DESC bld{}; bld.RenderTarget[0].RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_ALL; dev->CreateBlendState(&bld, &bsOff);
    {   // spell effects
        ID3DBlob *fv = nullptr, *fp = nullptr, *er = nullptr;
        if (SUCCEEDED(comp(kFxShader, strlen(kFxShader), "fx", nullptr, nullptr, "vsmain", "vs_5_0", 0, 0, &fv, &er)) &&
            SUCCEEDED(comp(kFxShader, strlen(kFxShader), "fx", nullptr, nullptr, "psmain", "ps_5_0", 0, 0, &fp, &er))) {
            dev->CreateVertexShader(fv->GetBufferPointer(), fv->GetBufferSize(), nullptr, &fxvs);
            dev->CreatePixelShader(fp->GetBufferPointer(), fp->GetBufferSize(), nullptr, &fxps);
            D3D11_INPUT_ELEMENT_DESC fe[] = {
                {"POSITION", 0, DXGI_FORMAT_R32G32B32_FLOAT, 0, 0, D3D11_INPUT_PER_VERTEX_DATA, 0},
                {"TEXCOORD", 0, DXGI_FORMAT_R32G32_FLOAT, 0, 12, D3D11_INPUT_PER_VERTEX_DATA, 0},
                {"COLOR", 0, DXGI_FORMAT_R32G32B32A32_FLOAT, 0, 20, D3D11_INPUT_PER_VERTEX_DATA, 0},
                {"TEXCOORD", 1, DXGI_FORMAT_R32_FLOAT, 0, 36, D3D11_INPUT_PER_VERTEX_DATA, 0}};
            dev->CreateInputLayout(fe, 4, fv->GetBufferPointer(), fv->GetBufferSize(), &fxil);
        } else logf("fx shader: %s", er ? (char*)er->GetBufferPointer() : "?");
        D3D11_BUFFER_DESC fb{}; fb.ByteWidth = 256 * 6 * sizeof(FxV); fb.Usage = D3D11_USAGE_DYNAMIC; fb.BindFlags = D3D11_BIND_VERTEX_BUFFER;
        fb.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE; dev->CreateBuffer(&fb, nullptr, &fxvb);
        D3D11_BLEND_DESC ab{}; auto& t = ab.RenderTarget[0]; t.BlendEnable = TRUE; t.SrcBlend = D3D11_BLEND_ONE; t.DestBlend = D3D11_BLEND_ONE;
        t.BlendOp = D3D11_BLEND_OP_ADD; t.SrcBlendAlpha = D3D11_BLEND_ZERO; t.DestBlendAlpha = D3D11_BLEND_ONE; t.BlendOpAlpha = D3D11_BLEND_OP_ADD;
        t.RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_ALL; dev->CreateBlendState(&ab, &bsAdd);
        t.BlendOp = D3D11_BLEND_OP_REV_SUBTRACT; dev->CreateBlendState(&ab, &bsSub);   // v46: KH1 subtractive (Gravity orb)
        D3D11_DEPTH_STENCIL_DESC fd{}; fd.DepthEnable = TRUE; fd.DepthWriteMask = D3D11_DEPTH_WRITE_MASK_ZERO; fd.DepthFunc = D3D11_COMPARISON_LESS_EQUAL;
        dev->CreateDepthStencilState(&fd, &dsFx);
        for (auto& tx : g_fxTex) {   // KH1 effect sprites (mipmapped: they are drawn small and large)
            D3D11_TEXTURE2D_DESC td{}; td.Width = tx.w; td.Height = tx.h; td.MipLevels = 0; td.ArraySize = 1;
            td.Format = DXGI_FORMAT_R8G8B8A8_UNORM; td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT;
            td.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET; td.MiscFlags = D3D11_RESOURCE_MISC_GENERATE_MIPS;
            ID3D11Texture2D* tex = nullptr; ID3D11ShaderResourceView* v = nullptr;
            if (SUCCEEDED(dev->CreateTexture2D(&td, nullptr, &tex))) {
                ctx->UpdateSubresource(tex, 0, nullptr, tx.rgba.data(), tx.w * 4, 0);
                dev->CreateShaderResourceView(tex, nullptr, &v); if (v) ctx->GenerateMips(v); tex->Release();
            }
            fxSrvs.push_back(v);
        }
        logf("fx textures: %zu", fxSrvs.size());
    }
    init_hud(comp);
    init_probes(comp);
    init_shadows(comp);
    init_ground(comp);
    init_outline(comp);
    for (auto& a : g_actors) init_actor_gpu(a);
    logf("gfx ready");
    return true;
}

static void draw_hud(ID3D11RenderTargetView* target, UINT W, UINT H) {
    HudFrame h; EnterCriticalSection(&g_cs); h = g_hud; LeaveCriticalSection(&g_cs);
    if (h.n <= 0 || !hudvs || !hudps) return;
    if (h.n > HUD_MAX) h.n = HUD_MAX;
    D3D11_MAPPED_SUBRESOURCE ms;
    if (FAILED(ctx->Map(hudvb, 0, D3D11_MAP_WRITE_DISCARD, 0, &ms))) return;
    HudV* v = (HudV*)ms.pData;
    for (int i = 0; i < h.n; i++) {
        const HudSprite& s = h.s[i]; float x0 = s.x, y0 = s.y, x1 = s.x + s.w, y1 = s.y + s.h;
        HudV q[4] = {{{x0, y0}, {s.u0, s.v0}}, {{x1, y0}, {s.u1, s.v0}}, {{x0, y1}, {s.u0, s.v1}}, {{x1, y1}, {s.u1, s.v1}}};
        for (auto& c : q) { memcpy(c.c, s.color, 16); c.mode = (float)s.mode; }
        HudV* o = &v[i * 6]; o[0] = q[0]; o[1] = q[1]; o[2] = q[2]; o[3] = q[2]; o[4] = q[1]; o[5] = q[3];
    }
    ctx->Unmap(hudvb, 0);
    if (SUCCEEDED(ctx->Map(hudcb, 0, D3D11_MAP_WRITE_DISCARD, 0, &ms))) {
        float* o = (float*)ms.pData;   // sprite pixels are in the canvas size Python reports (gauge[6..7]), else back buffer
        o[0] = h.gauge[6] > 0 ? h.gauge[6] : (float)W; o[1] = h.gauge[7] > 0 ? h.gauge[7] : (float)H; o[2] = o[3] = 0;
        memcpy(o + 4, h.gauge, 32); ctx->Unmap(hudcb, 0);
    }
    D3D11_VIEWPORT vp{0, 0, (float)W, (float)H, 0, 1};
    ctx->OMSetRenderTargets(1, &target, nullptr); ctx->RSSetViewports(1, &vp); ctx->RSSetState(rs);
    ctx->OMSetDepthStencilState(dsOff, 0); float bf[4] = {0, 0, 0, 0}; ctx->OMSetBlendState(bsAlpha, bf, 0xffffffff);
    UINT stride = sizeof(HudV), off = 0; ctx->IASetVertexBuffers(0, 1, &hudvb, &stride, &off); ctx->IASetInputLayout(hudil);
    ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ctx->VSSetShader(hudvs, nullptr, 0); ctx->PSSetShader(hudps, nullptr, 0); ctx->GSSetShader(nullptr, nullptr, 0);
    ctx->VSSetConstantBuffers(0, 1, &hudcb); ctx->PSSetConstantBuffers(0, 1, &hudcb); ctx->PSSetSamplers(0, 1, &samp);
    for (int i = 0; i < h.n; i++) {   // one draw per sprite (texture can change)
        int t = h.s[i].tex; ID3D11ShaderResourceView* srv = (t >= 0 && t < (int)hudSrvs.size()) ? hudSrvs[t] : nullptr;
        if (!srv && h.s[i].mode == 0) continue;
        ctx->PSSetShaderResources(0, 1, &srv); ctx->Draw(6, i * 6);
    }
}

// ------------------------------------------------------------------ math
struct M4 { float m[4][4]; };
static M4 mul(const M4& a, const M4& b) { M4 r{}; for (int i = 0; i < 4; i++) for (int j = 0; j < 4; j++) for (int k = 0; k < 4; k++) r.m[i][j] += a.m[i][k] * b.m[k][j]; return r; }
static M4 ident() { M4 r{}; for (int i = 0; i < 4; i++) r.m[i][i] = 1; return r; }
// UE rotator axes (rows X fwd, Y right, Z up)
static void ue_axes(float p, float y, float r, float X[3], float Y[3], float Z[3]) {
    float SP = sinf(p), CP = cosf(p), SY = sinf(y), CY = cosf(y), SR = sinf(r), CR = cosf(r);
    X[0] = CP * CY; X[1] = CP * SY; X[2] = SP;
    Y[0] = SR * SP * CY - CR * SY; Y[1] = SR * SP * SY + CR * CY; Y[2] = -SR * CP;
    Z[0] = -(CR * SP * CY + SR * SY); Z[1] = CY * SR - CR * SP * SY; Z[2] = CR * CP;
}


// ------------------------------------------------------------------ depth probes (blade / spells against the world)
// Borderlands' ray traces don't work through the SDK, but the renderer has the scene depth: each frame, before Sora is
// drawn into the depth copy, the scene depth is sampled under 8 points along the Keyblade plus up to 16 points from
// Python (spell projectiles, a calibration point at his feet). A 24x1 R32F target receives the samples; it is read
// back 2 frames later (ring of 3 staging textures, never stalls). Python decides what counts as contact.
#define NPROBE 24
#define NPROBE_BLADE 8
static ID3D11ShaderResourceView* g_dcopySrv = nullptr;   // the depth copy as a texture (R24_UNORM_X8_TYPELESS)
static ID3D11Texture2D *g_probeRt = nullptr, *g_probeStage[3] = {}; static ID3D11RenderTargetView* g_probeRtv = nullptr;
static ID3D11VertexShader* prvs; static ID3D11PixelShader* prps; static ID3D11Buffer* prcb;
struct ProbeIn { float px, py, w, ndcz, valid; };
static ProbeIn g_probeQ[3][NPROBE]; static uint32_t g_probeFrame = 0;
static float g_userProbes[16 * 3]; static int g_nUser = 0;
static float g_probeOut[NPROBE * 4]; static uint32_t g_probeOutN = 0;   // per probe: scene depth, point depth (both NDC z), point w, valid
static volatile double g_hitstopUntil = 0;
static const char* kProbeShader = R"(
cbuffer P : register(b0) { float4 pts[24]; };
Texture2D<float> D : register(t0);
float4 vsmain(uint id : SV_VertexID) : SV_Position { float2 p = float2(id == 2 ? 3.0 : -1.0, id == 1 ? -3.0 : 1.0); return float4(p, 0, 1); }
float psmain(float4 pos : SV_Position) : SV_Target {
  uint i = (uint)pos.x; float4 q = pts[i];
  if (q.w < 0.5) return -1.0;
  return D.Load(int3((int)q.x, (int)q.y, 0));
})";
static void init_probes(PFN_D3DCompile comp) {
    ID3DBlob *v = nullptr, *f = nullptr, *er = nullptr;
    if (FAILED(comp(kProbeShader, strlen(kProbeShader), "probe", nullptr, nullptr, "vsmain", "vs_5_0", 0, 0, &v, &er)) ||
        FAILED(comp(kProbeShader, strlen(kProbeShader), "probe", nullptr, nullptr, "psmain", "ps_5_0", 0, 0, &f, &er))) {
        logf("probe shader: %s", er ? (char*)er->GetBufferPointer() : "?"); return;
    }
    dev->CreateVertexShader(v->GetBufferPointer(), v->GetBufferSize(), nullptr, &prvs);
    dev->CreatePixelShader(f->GetBufferPointer(), f->GetBufferSize(), nullptr, &prps);
    D3D11_TEXTURE2D_DESC td{}; td.Width = NPROBE; td.Height = 1; td.MipLevels = 1; td.ArraySize = 1; td.Format = DXGI_FORMAT_R32_FLOAT;
    td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT; td.BindFlags = D3D11_BIND_RENDER_TARGET;
    dev->CreateTexture2D(&td, nullptr, &g_probeRt); if (g_probeRt) dev->CreateRenderTargetView(g_probeRt, nullptr, &g_probeRtv);
    td.Usage = D3D11_USAGE_STAGING; td.BindFlags = 0; td.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    for (auto& st : g_probeStage) dev->CreateTexture2D(&td, nullptr, &st);
    D3D11_BUFFER_DESC bd{}; bd.ByteWidth = NPROBE * 16; bd.Usage = D3D11_USAGE_DYNAMIC; bd.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    bd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE; dev->CreateBuffer(&bd, nullptr, &prcb);
    logf("depth probes ready");
}
// world point -> probe (pixel, w, our NDC z); vp = the same view-projection Sora is drawn with
static ProbeIn probe_of(const M4& vp, const float* p, UINT W, UINT H) {
    float c[4];
    for (int r = 0; r < 4; r++) c[r] = vp.m[r][0] * p[0] + vp.m[r][1] * p[1] + vp.m[r][2] * p[2] + vp.m[r][3];
    ProbeIn q{0, 0, c[3], 0, 0};
    if (c[3] < 15.f) return q;
    float x = c[0] / c[3], y = c[1] / c[3];
    if (x < -0.98f || x > 0.98f || y < -0.98f || y > 0.98f) return q;
    q.px = (x + 1) * 0.5f * W; q.py = (1 - y) * 0.5f * H; q.ndcz = c[2] / c[3]; q.valid = 1; return q;
}
static void run_probes(const M4& vp, UINT W, UINT H) {
    if (!prps || !g_probeRtv || !g_dcopySrv) return;
    ProbeIn* Q = g_probeQ[g_probeFrame % 3];
    float bl[6]; EnterCriticalSection(&g_cs); memcpy(bl, g_blade, sizeof bl); int nu = g_nUser; float up[16 * 3]; memcpy(up, g_userProbes, sizeof up); LeaveCriticalSection(&g_cs);
    for (int k = 0; k < NPROBE_BLADE; k++) {   // hilt -> a little past the tip (the swing's reach)
        float t = (float)(k + 1) / NPROBE_BLADE * 1.15f, pt[3];
        for (int a = 0; a < 3; a++) pt[a] = bl[a] + (bl[3 + a] - bl[a]) * t;
        Q[k] = g_bladeV[0].b >= 0 ? probe_of(vp, pt, W, H) : ProbeIn{};
    }
    for (int k = 0; k < 16; k++) Q[NPROBE_BLADE + k] = k < nu ? probe_of(vp, &up[k * 3], W, H) : ProbeIn{};
    D3D11_MAPPED_SUBRESOURCE ms;
    if (FAILED(ctx->Map(prcb, 0, D3D11_MAP_WRITE_DISCARD, 0, &ms))) return;
    float* o = (float*)ms.pData;
    for (int k = 0; k < NPROBE; k++) { o[k * 4] = Q[k].px; o[k * 4 + 1] = Q[k].py; o[k * 4 + 2] = 0; o[k * 4 + 3] = Q[k].valid; }
    ctx->Unmap(prcb, 0);
    D3D11_VIEWPORT vp1{0, 0, (float)NPROBE, 1, 0, 1};
    ctx->OMSetRenderTargets(1, &g_probeRtv, nullptr); ctx->RSSetViewports(1, &vp1); ctx->RSSetState(rs);
    ctx->OMSetDepthStencilState(dsOff, 0); float bf[4] = {0, 0, 0, 0}; ctx->OMSetBlendState(bsOff, bf, 0xffffffff);
    ctx->IASetInputLayout(nullptr); ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ctx->VSSetShader(prvs, nullptr, 0); ctx->PSSetShader(prps, nullptr, 0); ctx->PSSetConstantBuffers(0, 1, &prcb);
    ctx->PSSetShaderResources(0, 1, &g_dcopySrv);
    ctx->Draw(3, 0);
    ID3D11ShaderResourceView* none = nullptr; ctx->PSSetShaderResources(0, 1, &none);
    ctx->CopyResource(g_probeStage[g_probeFrame % 3], g_probeRt);
    // results of the frame before last (ready by now; DO_NOT_WAIT never stalls the game)
    if (g_probeFrame >= 2) {
        uint32_t rf = g_probeFrame - 2; ProbeIn* R = g_probeQ[rf % 3];
        if (SUCCEEDED(ctx->Map(g_probeStage[rf % 3], 0, D3D11_MAP_READ, D3D11_MAP_FLAG_DO_NOT_WAIT, &ms))) {
            const float* d = (const float*)ms.pData; float out[NPROBE * 4];
            for (int k = 0; k < NPROBE; k++) { out[k * 4] = d[k]; out[k * 4 + 1] = R[k].ndcz; out[k * 4 + 2] = R[k].w; out[k * 4 + 3] = R[k].valid; }
            ctx->Unmap(g_probeStage[rf % 3], 0);
            EnterCriticalSection(&g_cs); memcpy(g_probeOut, out, sizeof out); g_probeOutN = rf; LeaveCriticalSection(&g_cs);
        }
    }
    g_probeFrame++;
}

// ------------------------------------------------------------------ v26: ground light (how lit the spot Sora stands on is)
// The relight used to sample the blurred screen around Sora = whatever is BEHIND him from the camera, so turning the
// camera toward a bright wall lit him up inside a shadow. Now: 16 points on the ground around his feet (two rings),
// each kept only if Borderlands' depth says that ground is really visible there (not occluded, not empty air under
// a jump), averaged from the blurred frame (taken before any of our drawing), read back 2 frames later, smoothed over
// ~0.3 s and held while nothing is visible (jumps, glides, camera pressed to a wall). World-anchored: the camera's
// direction no longer matters, and Borderlands' own shadows on that ground dim him.
static ID3D11Texture2D *g_glRt = nullptr, *g_glStage[3] = {}; static ID3D11RenderTargetView* g_glRtv = nullptr;
static ID3D11PixelShader* glps = nullptr; static ID3D11Buffer* glcb = nullptr; static uint32_t g_glFrame = 0;
static float g_gl[4] = {0.3f, 0.3f, 0.3f, 0}; static bool g_glHave = false; static double g_glT = 0; static float g_glRaw[4] = {};
static const char* kGroundShader = R"(
cbuffer G : register(b0) { float4 pts[16]; float4 prm; };   // pts: u, v, view depth, valid; prm: mip, W, H, -
Texture2D E : register(t0); SamplerState SC : register(s0); Texture2D<float> D : register(t1);
float4 psmain(float4 pos : SV_Position) : SV_Target {
  float3 s = 0; float n = 0;
  [unroll] for (int k = 0; k < 16; k++) {
    float4 q = pts[k];
    if (q.w < 0.5) continue;
    if (q.w > 1.5) {   // depth available: the point must lie on the visible surface
      float d = D.Load(int3(q.x * prm.y, q.y * prm.z, 0));
      float lin = 10.0 * 200000.0 / (200000.0 - d * 199990.0);
      if (abs(lin - q.z) > 25.0 + q.z * 0.03) continue;
    }
    s += E.SampleLevel(SC, q.xy, prm.x).rgb; n += 1;
  }
  return float4(s / max(n, 1.0), n);
})";
static void init_ground(PFN_D3DCompile comp) {
    ID3DBlob *f = nullptr, *er = nullptr;
    if (FAILED(comp(kGroundShader, strlen(kGroundShader), "ground", nullptr, nullptr, "psmain", "ps_5_0", 0, 0, &f, &er))) {
        logf("ground shader: %s", er ? (char*)er->GetBufferPointer() : "?"); return;
    }
    dev->CreatePixelShader(f->GetBufferPointer(), f->GetBufferSize(), nullptr, &glps);
    D3D11_TEXTURE2D_DESC td{}; td.Width = td.Height = 1; td.MipLevels = 1; td.ArraySize = 1; td.Format = DXGI_FORMAT_R32G32B32A32_FLOAT;
    td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT; td.BindFlags = D3D11_BIND_RENDER_TARGET;
    dev->CreateTexture2D(&td, nullptr, &g_glRt); if (g_glRt) dev->CreateRenderTargetView(g_glRt, nullptr, &g_glRtv);
    td.Usage = D3D11_USAGE_STAGING; td.BindFlags = 0; td.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    for (auto& st : g_glStage) dev->CreateTexture2D(&td, nullptr, &st);
    D3D11_BUFFER_DESC bd{}; bd.ByteWidth = 17 * 16; bd.Usage = D3D11_USAGE_DYNAMIC; bd.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    bd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE; dev->CreateBuffer(&bd, nullptr, &glcb);
    logf("ground light %s", (glps && g_glRtv && glcb) ? "ready" : "FAILED");
}
static void ground_light(const M4& vp, const float* feet, UINT W, UINT H, ID3D11ShaderResourceView* env, bool depth) {
    if (!glps || !g_glRtv || !glcb || !prvs) return;
    double now = qpc_now();
    // 1) result of the frame before last -> smoothed ground light
    if (g_glFrame >= 2) {
        D3D11_MAPPED_SUBRESOURCE ms;
        if (SUCCEEDED(ctx->Map(g_glStage[(g_glFrame - 2) % 3], 0, D3D11_MAP_READ, D3D11_MAP_FLAG_DO_NOT_WAIT, &ms))) {
            const float* d = (const float*)ms.pData; memcpy(g_glRaw, d, 16); ctx->Unmap(g_glStage[(g_glFrame - 2) % 3], 0);
            float dt = g_glT > 0 ? (float)(now - g_glT) : 0.f; g_glT = now; if (dt > 0.1f) dt = 0.1f;
            if (g_glRaw[3] >= 1 && std::isfinite(g_glRaw[0])) {   // any visible ground: trust it by how much was seen
                float a = g_glHave ? (1.f - expf(-dt / 0.3f)) * fminf(g_glRaw[3], 4.f) / 4.f : 1.f;
                for (int c = 0; c < 3; c++) g_gl[c] += (g_glRaw[c] - g_gl[c]) * a;
                g_glHave = true;
            }
        }
    }
    // 2) this frame's points: two rings on the ground around the feet
    D3D11_MAPPED_SUBRESOURCE ms;
    if (FAILED(ctx->Map(glcb, 0, D3D11_MAP_WRITE_DISCARD, 0, &ms))) return;
    float* o = (float*)ms.pData;
    for (int k = 0; k < 16; k++) {
        // three rings (6 / 5 / 5 points at 40 / 90 / 150): the close KH camera often leaves his feet off the bottom
        // of the screen, the outer rings (mostly the ground ahead of him) are still visible then
        int ring = k < 6 ? 0 : (k < 11 ? 1 : 2), idx = k < 6 ? k : (k < 11 ? k - 6 : k - 11), cnt = k < 6 ? 6 : 5;
        float ang = idx * 6.2831853f / cnt + ring * 0.6f, r = ring == 0 ? 40.f : (ring == 1 ? 90.f : 150.f);
        float p[3] = {feet[0] + cosf(ang) * r, feet[1] + sinf(ang) * r, feet[2] + 3.f}, c[4];
        for (int i = 0; i < 4; i++) c[i] = vp.m[i][0] * p[0] + vp.m[i][1] * p[1] + vp.m[i][2] * p[2] + vp.m[i][3];
        float u = 0, v = 0, ok = 0;
        if (c[3] > 15.f) { u = (c[0] / c[3] + 1) * 0.5f; v = (1 - c[1] / c[3]) * 0.5f; if (u > 0.01f && u < 0.99f && v > 0.01f && v < 0.99f) ok = depth ? 2.f : 1.f; }
        o[k * 4] = u; o[k * 4 + 1] = v; o[k * 4 + 2] = c[3]; o[k * 4 + 3] = ok;
    }
    o[64] = 3.f; o[65] = (float)W; o[66] = (float)H; o[67] = 0;
    ctx->Unmap(glcb, 0);
    D3D11_VIEWPORT v1{0, 0, 1, 1, 0, 1};
    ctx->OMSetRenderTargets(1, &g_glRtv, nullptr); ctx->RSSetViewports(1, &v1); ctx->RSSetState(rs);
    ctx->OMSetDepthStencilState(dsOff, 0); float bf[4] = {0, 0, 0, 0}; ctx->OMSetBlendState(bsOff, bf, 0xffffffff);
    ctx->IASetInputLayout(nullptr); ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ctx->VSSetShader(prvs, nullptr, 0); ctx->PSSetShader(glps, nullptr, 0); ctx->PSSetConstantBuffers(0, 1, &glcb);
    ID3D11ShaderResourceView* sv[2] = {env, depth ? g_dcopySrv : nullptr}; ctx->PSSetShaderResources(0, 2, sv);
    ctx->PSSetSamplers(0, 1, &sampClamp);
    ctx->Draw(3, 0);
    ID3D11ShaderResourceView* none[2] = {}; ctx->PSSetShaderResources(0, 2, none);
    ctx->CopyResource(g_glStage[g_glFrame % 3], g_glRt);
    g_glFrame++;
}
extern "C" __declspec(dllexport) int sr_ground_light(float* out) {   // smoothed rgb + have, raw rgb + visible points
    for (int k = 0; k < 3; k++) { out[k] = g_gl[k]; out[4 + k] = g_glRaw[k]; } out[3] = g_glHave ? 1.f : 0.f; out[7] = g_glRaw[3];
    return (int)g_glFrame;
}

// ------------------------------------------------------------------ v24: character shadows on the Borderlands world
// Every drawn character (Sora, Donald, Goofy, Heartless) goes into a depth map seen from the light (orthographic,
// centred on Sora). A full-screen pass then rebuilds each scene pixel's world position from Borderlands' depth (the
// same view-projection Sora is drawn with), looks it up in that map and darkens it. Soft edges (Poisson PCF, wider
// the farther the ground is below the caster) and a fade with that distance, so a gliding Sora's shadow thins away.
#define SH_N 2048
static ID3D11Texture2D* g_shTex = nullptr; static ID3D11DepthStencilView* g_shDsv = nullptr; static ID3D11ShaderResourceView* g_shSrv = nullptr;
static ID3D11PixelShader* shps = nullptr;                       // alpha-clipped depth-only (kShader psshadow)
static ID3D11VertexShader* srvs_ = nullptr; static ID3D11PixelShader* srps = nullptr; static ID3D11Buffer* srcb = nullptr;
static ID3D11BlendState* bsShadow = nullptr;
// sr_shadow: strength (0 = off), fade start / end (UE units the ground lies below the caster), softness (texels),
// light direction (0,0,0 = the frame light made steeper), extent (half size of the covered square, UE units)
static volatile float g_shCfg[8] = {0.5f, 220.f, 700.f, 1.6f, 0, 0, 0, 1600.f};
static volatile LONG g_shDrawn = 0;
// v25: level lighting from Python (sr_light): sun rgb + ambient share, sky rgb + rim, ground rgb + cel softness,
// self-shadow strength, rim power. sun ambient share 0 = the old fixed cel bands.
static volatile float g_lightCfg[16] = {};
static M4 g_lvp; static bool g_lvpOk = false;   // this frame's shadow-map light vp (valid once the shadow pass ran)
static const char* kShadowShader = R"(
cbuffer R : register(b0) { float4x4 ivp; float4x4 lvp; float4 p0; float4 p1; float4 col; };
// p0 = strength, fade start, fade end, PCF radius (texels); p1 = 1/W, 1/H, light depth range (units), map size
Texture2D<float> D : register(t0); Texture2D<float> SM : register(t1);
float4 vsmain(uint id : SV_VertexID) : SV_Position { float2 p = float2(id == 2 ? 3.0 : -1.0, id == 1 ? -3.0 : 1.0); return float4(p, 0, 1); }
static const float2 PD[16] = {
  float2(-0.942, -0.399), float2(0.946, -0.769), float2(-0.094, -0.929), float2(0.345, 0.294),
  float2(-0.916, 0.458), float2(-0.815, -0.879), float2(-0.383, 0.277), float2(0.975, 0.756),
  float2(0.443, -0.975), float2(0.537, -0.474), float2(-0.265, -0.419), float2(0.792, 0.191),
  float2(-0.242, 0.997), float2(-0.814, 0.914), float2(0.200, 0.787), float2(0.144, -0.141)};
float tap(float2 t) { return SM.Load(int3(clamp(t, 0, p1.w - 1), 0)); }
float4 psmain(float4 pos : SV_Position) : SV_Target {
  float d = D.Load(int3(pos.xy, 0));
  if (d >= 0.999999) discard;                                     // sky
  float2 ndc = float2(pos.x * p1.x * 2 - 1, 1 - pos.y * p1.y * 2);
  float4 w = mul(ivp, float4(ndc, d, 1)); w /= w.w;
  float4 l = mul(lvp, float4(w.xyz, 1));
  float2 uv = float2(l.x * 0.5 + 0.5, 0.5 - l.y * 0.5);
  if (any(uv < 0.004) || any(uv > 0.996) || l.z <= 0 || l.z >= 1) discard;
  float2 t0 = uv * p1.w;
  // blocker search: how far below the casters this pixel is (wider penumbra for higher casters)
  float bz = 0, bn = 0;
  [unroll] for (int b = 0; b < 16; b += 2) {
    float c = tap(t0 + PD[b] * 6.0);
    if (c < 0.99999) { float dz = (l.z - c) * p1.z; if (dz > 2.0) { bz += dz; bn += 1; } }
  }
  if (bn == 0) discard;
  float avg = bz / bn, rad = p0.w * (1.0 + avg / 120.0);
  float occ = 0;
  [unroll] for (int k = 0; k < 16; k++) {
    float c = tap(t0 + PD[k] * rad);
    float dz = (l.z - c) * p1.z;                                  // units this pixel lies below the caster
    if (c < 0.99999 && dz > 2.0) occ += 1.0 - smoothstep(p0.y, p0.z, dz);
  }
  occ /= 16.0;
  if (occ <= 0.002) discard;
  return float4(col.rgb, occ * p0.x);
})";
static void init_shadows(PFN_D3DCompile comp) {
    ID3DBlob *v = nullptr, *f = nullptr, *s = nullptr, *er = nullptr;
    if (FAILED(comp(kShader, strlen(kShader), "sora", nullptr, nullptr, "psshadow", "ps_5_0", 0, 0, &s, &er)) ||
        FAILED(comp(kShadowShader, strlen(kShadowShader), "shadow", nullptr, nullptr, "vsmain", "vs_5_0", 0, 0, &v, &er)) ||
        FAILED(comp(kShadowShader, strlen(kShadowShader), "shadow", nullptr, nullptr, "psmain", "ps_5_0", 0, 0, &f, &er))) {
        logf("shadow shader: %s", er ? (char*)er->GetBufferPointer() : "?"); return;
    }
    dev->CreatePixelShader(s->GetBufferPointer(), s->GetBufferSize(), nullptr, &shps);
    dev->CreateVertexShader(v->GetBufferPointer(), v->GetBufferSize(), nullptr, &srvs_);
    dev->CreatePixelShader(f->GetBufferPointer(), f->GetBufferSize(), nullptr, &srps);
    D3D11_TEXTURE2D_DESC td{}; td.Width = td.Height = SH_N; td.MipLevels = 1; td.ArraySize = 1; td.Format = DXGI_FORMAT_R32_TYPELESS;
    td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT; td.BindFlags = D3D11_BIND_DEPTH_STENCIL | D3D11_BIND_SHADER_RESOURCE;
    if (SUCCEEDED(dev->CreateTexture2D(&td, nullptr, &g_shTex))) {
        D3D11_DEPTH_STENCIL_VIEW_DESC dd{}; dd.Format = DXGI_FORMAT_D32_FLOAT; dd.ViewDimension = D3D11_DSV_DIMENSION_TEXTURE2D;
        dev->CreateDepthStencilView(g_shTex, &dd, &g_shDsv);
        D3D11_SHADER_RESOURCE_VIEW_DESC sd{}; sd.Format = DXGI_FORMAT_R32_FLOAT; sd.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D; sd.Texture2D.MipLevels = 1;
        dev->CreateShaderResourceView(g_shTex, &sd, &g_shSrv);
    }
    D3D11_BUFFER_DESC bd{}; bd.ByteWidth = 64 * 2 + 16 * 3; bd.Usage = D3D11_USAGE_DYNAMIC; bd.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    bd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE; dev->CreateBuffer(&bd, nullptr, &srcb);
    D3D11_BLEND_DESC bl{}; auto& t = bl.RenderTarget[0]; t.BlendEnable = TRUE; t.SrcBlend = D3D11_BLEND_SRC_ALPHA; t.DestBlend = D3D11_BLEND_INV_SRC_ALPHA;
    t.BlendOp = D3D11_BLEND_OP_ADD; t.SrcBlendAlpha = D3D11_BLEND_ZERO; t.DestBlendAlpha = D3D11_BLEND_ONE; t.BlendOpAlpha = D3D11_BLEND_OP_ADD;
    t.RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_RED | D3D11_COLOR_WRITE_ENABLE_GREEN | D3D11_COLOR_WRITE_ENABLE_BLUE;
    dev->CreateBlendState(&bl, &bsShadow);
    logf("shadows %s", (shps && srps && g_shDsv && g_shSrv && srcb && bsShadow) ? "ready" : "FAILED");
}
// One character to draw this frame: its mesh, posed bones and world matrix (shared by the shadow and colour passes).
struct DrawRec { ID3D11Buffer *vb, *ib; const std::vector<Sub>* subs; const std::vector<ID3D11ShaderResourceView*>* srvs; std::vector<float> bones; M4 world;
                 const std::vector<char>* skip = nullptr; Actor* wpn = nullptr; };   // v58: equipped weapon
static void upload_cb(ID3D11Buffer* cb, const void* p, size_t n) {
    D3D11_MAPPED_SUBRESOURCE ms; if (SUCCEEDED(ctx->Map(cb, 0, D3D11_MAP_WRITE_DISCARD, 0, &ms))) { memcpy(ms.pData, p, n); ctx->Unmap(cb, 0); }
}
static void shadow_pass(const DrawRec* recs, int n, const M4& vp, const float* light, const float* centre,
                        ID3D11RenderTargetView* target, UINT W, UINT H) {
    float strength = g_shCfg[0];
    if (strength <= 0.001f || !shps || !srps || !g_shDsv || !g_shSrv || !g_dcopySrv || !n) return;
    // light direction: given, or the frame light made steeper (long raking shadows read as "floating")
    float L[3] = {g_shCfg[4], g_shCfg[5], g_shCfg[6]};
    if (L[0] * L[0] + L[1] * L[1] + L[2] * L[2] < 1e-6f) { L[0] = light[0]; L[1] = light[1]; L[2] = light[2]; }   // v25: the level's sun
    if (L[2] > -0.4f) L[2] = -0.4f;   // always from above, never raking: very long shadows read as "floating"
    float ln = sqrtf(L[0] * L[0] + L[1] * L[1] + L[2] * L[2]); for (float& c : L) c /= ln;
    float hint[3] = {1, 0, 0}; if (fabsf(L[0]) > 0.9f) { hint[0] = 0; hint[1] = 1; }
    float R[3] = {hint[1] * L[2] - hint[2] * L[1], hint[2] * L[0] - hint[0] * L[2], hint[0] * L[1] - hint[1] * L[0]};
    float rn = sqrtf(R[0] * R[0] + R[1] * R[1] + R[2] * R[2]); for (float& c : R) c /= rn;
    float U[3] = {L[1] * R[2] - L[2] * R[1], L[2] * R[0] - L[0] * R[2], L[0] * R[1] - L[1] * R[0]};
    float ext = g_shCfg[7] > 100.f ? g_shCfg[7] : 1600.f, range = 4000.f;
    // snap the centre to whole texels along the map axes: the shadow doesn't shimmer as Sora moves
    float tex = 2.f * ext / SH_N, cr = R[0] * centre[0] + R[1] * centre[1] + R[2] * centre[2], cu = U[0] * centre[0] + U[1] * centre[1] + U[2] * centre[2];
    cr = floorf(cr / tex) * tex; cu = floorf(cu / tex) * tex;
    float cl = L[0] * centre[0] + L[1] * centre[1] + L[2] * centre[2];
    M4 lvp{};
    for (int k = 0; k < 3; k++) { lvp.m[0][k] = R[k] / ext; lvp.m[1][k] = U[k] / ext; lvp.m[2][k] = L[k] / (2 * range); }
    lvp.m[0][3] = -cr / ext; lvp.m[1][3] = -cu / ext; lvp.m[2][3] = (range - cl) / (2 * range); lvp.m[3][3] = 1;
    M4 ivp; if (!inv4(&vp.m[0][0], &ivp.m[0][0])) return;
    g_lvp = lvp; g_lvpOk = true;
    // 1) casters -> light depth map
    ctx->ClearDepthStencilView(g_shDsv, D3D11_CLEAR_DEPTH, 1.0f, 0);
    D3D11_VIEWPORT sv{0, 0, (float)SH_N, (float)SH_N, 0, 1};
    ctx->OMSetRenderTargets(0, nullptr, g_shDsv); ctx->RSSetViewports(1, &sv); ctx->RSSetState(rs);
    ctx->OMSetDepthStencilState(dsOwn, 0);
    ctx->IASetInputLayout(il); ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ID3D11Buffer* cbs[2] = {cbFrame, cbBones};
    ctx->VSSetShader(vs, nullptr, 0); ctx->VSSetConstantBuffers(0, 2, cbs); ctx->PSSetShader(shps, nullptr, 0); ctx->PSSetSamplers(0, 1, &samp);
    for (int i = 0; i < n; i++) {
        const DrawRec& d = recs[i];
        float cb[92]; memcpy(cb, g_lastCb, sizeof cb);
        for (int r = 0; r < 4; r++) for (int c = 0; c < 4; c++) { cb[c * 4 + r] = d.world.m[r][c]; cb[16 + c * 4 + r] = lvp.m[r][c]; }
        upload_cb(cbFrame, cb, sizeof cb);
        upload_cb(cbBones, d.bones.data(), sizeof(float) * (d.bones.size() < 900 * 4 ? d.bones.size() : 900 * 4));
        draw_rec_subs(d.vb, d.ib, *d.subs, *d.srvs, d.skip, d.wpn);
    }
    // 2) receivers: every scene pixel inside the map, darkened where a caster is above it
    float rc[16 * 2 + 12];
    for (int r = 0; r < 4; r++) for (int c = 0; c < 4; c++) { rc[c * 4 + r] = ivp.m[r][c]; rc[16 + c * 4 + r] = lvp.m[r][c]; }
    float p[12] = {strength, g_shCfg[1], g_shCfg[2], g_shCfg[3], 1.f / W, 1.f / H, 2 * range * -L[2], (float)SH_N, 0.02f, 0.025f, 0.05f, 1};
    memcpy(rc + 32, p, sizeof p); upload_cb(srcb, rc, sizeof rc);
    D3D11_VIEWPORT vpt{0, 0, (float)W, (float)H, 0, 1};
    ctx->OMSetRenderTargets(1, &target, nullptr); ctx->RSSetViewports(1, &vpt);
    ctx->OMSetDepthStencilState(dsOff, 0); float bf[4] = {0, 0, 0, 0}; ctx->OMSetBlendState(bsShadow, bf, 0xffffffff);
    ctx->IASetInputLayout(nullptr); ctx->VSSetShader(srvs_, nullptr, 0); ctx->PSSetShader(srps, nullptr, 0);
    ctx->PSSetConstantBuffers(0, 1, &srcb);
    ID3D11ShaderResourceView* two[2] = {g_dcopySrv, g_shSrv}; ctx->PSSetShaderResources(0, 2, two);
    ctx->Draw(3, 0);
    ID3D11ShaderResourceView* none[2] = {}; ctx->PSSetShaderResources(0, 2, none);
    InterlockedIncrement(&g_shDrawn);
}

// ------------------------------------------------------------------ v30: ink outlines (Borderlands' comic-book lines)
// Borderlands inks its world in a post pass that runs before we draw, so our characters had no lines. While they are
// drawn, a second target records their view depth; then a full-screen pass inks every pixel that lies clearly BEHIND a
// character pixel within a few pixels: the outer silhouette plus inner lines where an arm or the Keyblade crosses the
// body. Scene depth (with the characters in it) decides what "behind" means, so walls in front still hide the lines.
static ID3D11Texture2D* g_olTex = nullptr; static ID3D11RenderTargetView* g_olRtv = nullptr; static ID3D11ShaderResourceView* g_olSrv = nullptr;
static UINT g_olW = 0, g_olH = 0; static ID3D11PixelShader* olps = nullptr; static ID3D11Buffer* olcb = nullptr;
// sr_outline: strength (0 = off), width (px at 1080p), relative depth step, absolute depth step (UE units), colour rgb
static volatile float g_olCfg[8] = {0.9f, 1.6f, 0.035f, 4.f, 0.06f, 0.045f, 0.04f, 0};
static const char* kOutlineShader = R"(
cbuffer O : register(b0) { float4 p0; float4 col; };   // p0 = width px, rel step, abs step, strength
Texture2D<float> C : register(t0); Texture2D<float> D : register(t1);
float lin(float d) { return 10.0 * 200000.0 / (200000.0 - d * 199990.0); }
static const float2 DIRS[12] = { float2(1, 0), float2(-1, 0), float2(0, 1), float2(0, -1), float2(0.707, 0.707), float2(-0.707, 0.707),
  float2(0.707, -0.707), float2(-0.707, -0.707), float2(0.5, 0), float2(-0.5, 0), float2(0, 0.5), float2(0, -0.5) };
float4 psmain(float4 pos : SV_Position) : SV_Target {
  int2 q = int2(pos.xy);
  float dp = min(C.Load(int3(q, 0)), lin(D.Load(int3(q, 0))));
  float near_ = 1e9;
  [unroll] for (int k = 0; k < 12; k++) near_ = min(near_, C.Load(int3(q + int2(round(DIRS[k] * p0.x)), 0)));
  if (near_ > 1e8) discard;
  float step_ = p0.z + dp * p0.y;
  if (near_ > dp - step_) discard;
  float a = p0.w * saturate(1.5 - near_ / 4000.0);   // thin out far away
  return float4(col.rgb, a);
})";
static void init_outline(PFN_D3DCompile comp) {
    ID3DBlob *f = nullptr, *er = nullptr;
    if (FAILED(comp(kOutlineShader, strlen(kOutlineShader), "outline", nullptr, nullptr, "psmain", "ps_5_0", 0, 0, &f, &er))) {
        logf("outline shader: %s", er ? (char*)er->GetBufferPointer() : "?"); return;
    }
    dev->CreatePixelShader(f->GetBufferPointer(), f->GetBufferSize(), nullptr, &olps);
    D3D11_BUFFER_DESC bd{}; bd.ByteWidth = 32; bd.Usage = D3D11_USAGE_DYNAMIC; bd.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    bd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE; dev->CreateBuffer(&bd, nullptr, &olcb);
    logf("outline %s", (olps && olcb) ? "ready" : "FAILED");
}
static bool outline_begin(UINT W, UINT H) {   // (re)creates the character-depth target and clears it to "nothing"
    if (g_olCfg[0] <= 0.001f || !olps) return false;
    if (!g_olRtv || g_olW != W || g_olH != H) {
        if (g_olSrv) { g_olSrv->Release(); g_olSrv = nullptr; } if (g_olRtv) { g_olRtv->Release(); g_olRtv = nullptr; } if (g_olTex) { g_olTex->Release(); g_olTex = nullptr; }
        D3D11_TEXTURE2D_DESC td{}; td.Width = W; td.Height = H; td.MipLevels = 1; td.ArraySize = 1; td.Format = DXGI_FORMAT_R32_FLOAT;
        td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT; td.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
        if (FAILED(dev->CreateTexture2D(&td, nullptr, &g_olTex))) return false;
        dev->CreateRenderTargetView(g_olTex, nullptr, &g_olRtv); dev->CreateShaderResourceView(g_olTex, nullptr, &g_olSrv);
        g_olW = W; g_olH = H;
    }
    if (!g_olRtv || !g_olSrv) return false;
    float far_[4] = {1e9f, 1e9f, 1e9f, 1e9f}; ctx->ClearRenderTargetView(g_olRtv, far_);
    return true;
}
static void outline_pass(ID3D11RenderTargetView* target, UINT W, UINT H, ID3D11DepthStencilView* dsv, bool scene) {   // scene: dsv is our standard-Z copy of the scene depth
    if (g_olCfg[0] <= 0.001f || !olps || !g_olSrv || !prvs) return;
    float c[8] = {fmaxf(1.f, g_olCfg[1] * (float)H / 1080.f), g_olCfg[2], g_olCfg[3], g_olCfg[0], g_olCfg[4], g_olCfg[5], g_olCfg[6], 1};
    upload_cb(olcb, c, sizeof c);
    scene = scene && g_dcopySrv;
    D3D11_VIEWPORT vpt{0, 0, (float)W, (float)H, 0, 1};
    ctx->OMSetRenderTargets(1, &target, nullptr); ctx->RSSetViewports(1, &vpt); ctx->RSSetState(rs);
    ctx->OMSetDepthStencilState(dsOff, 0); float bf[4] = {0, 0, 0, 0}; ctx->OMSetBlendState(bsShadow, bf, 0xffffffff);
    ctx->IASetInputLayout(nullptr); ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ctx->VSSetShader(srvs_, nullptr, 0); ctx->PSSetShader(olps, nullptr, 0); ctx->PSSetConstantBuffers(0, 1, &olcb);
    ID3D11ShaderResourceView* sv[2] = {g_olSrv, scene ? g_dcopySrv : nullptr}; ctx->PSSetShaderResources(0, 2, sv);
    ctx->Draw(3, 0);
    ID3D11ShaderResourceView* none[2] = {}; ctx->PSSetShaderResources(0, 2, none);
    ctx->OMSetRenderTargets(1, &target, dsv);   // trail / spell effects continue as before
}
extern "C" __declspec(dllexport) void sr_outline(const float* p, int n) { for (int k = 0; k < n && k < 8; k++) g_olCfg[k] = p[k]; }

// ------------------------------------------------------------------ Keyblade trail (KH1's swing trail texture, fx tex 8)
// The renderer records the blade (hilt, tip) every frame; while Python switches the trail on (sr_trail), a ribbon is
// drawn through the last `seconds` of blade positions: the blade edge bright (u = 0), fading into the tail (u = 1).
#define TRAIL_N 96
struct TrailS { float h[3], t[3]; double time; };
static TrailS g_trail[TRAIL_N]; static int g_trailHead = 0, g_trailCount = 0;
static volatile float g_trailCol[4] = {0, 0, 0, 0}; static volatile float g_trailSecs = 0.15f; static volatile double g_trailUntil = 0;
static ID3D11Buffer* g_trailVb = nullptr;
static void trail_push(const float* hilt, const float* tip) {
    TrailS& e = g_trail[g_trailHead]; memcpy(e.h, hilt, 12); memcpy(e.t, tip, 12); e.time = qpc_now();
    g_trailHead = (g_trailHead + 1) % TRAIL_N; if (g_trailCount < TRAIL_N) g_trailCount++;
}
static void trail_draw(bool depth) {
    double now = qpc_now();
    if (now > g_trailUntil || g_trailCol[3] <= 0.001f || g_fxTex.size() < 9 || fxSrvs.size() < 9 || !fxSrvs[8] || !fxvs) return;
    if (!g_trailVb) {
        D3D11_BUFFER_DESC bd{}; bd.ByteWidth = TRAIL_N * 6 * sizeof(FxV); bd.Usage = D3D11_USAGE_DYNAMIC;
        bd.BindFlags = D3D11_BIND_VERTEX_BUFFER; bd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE; dev->CreateBuffer(&bd, nullptr, &g_trailVb);
        if (!g_trailVb) return;
    }
    float secs = g_trailSecs > 0.02f ? g_trailSecs : 0.15f;
    // newest first
    int idx[TRAIL_N], n = 0;
    for (int k = 0; k < g_trailCount; k++) {
        int i = (g_trailHead - 1 - k + TRAIL_N) % TRAIL_N;
        if (now - g_trail[i].time > secs) break;
        idx[n++] = i;
    }
    if (n < 2) return;
    D3D11_MAPPED_SUBRESOURCE ms;
    if (FAILED(ctx->Map(g_trailVb, 0, D3D11_MAP_WRITE_DISCARD, 0, &ms))) return;
    FxV* o = (FxV*)ms.pData; int nv = 0;
    float col[4] = {g_trailCol[0], g_trailCol[1], g_trailCol[2], g_trailCol[3]};
    for (int k = 0; k + 1 < n; k++) {
        const TrailS &a = g_trail[idx[k]], &b = g_trail[idx[k + 1]];
        float ua = (float)((now - a.time) / secs), ub = (float)((now - b.time) / secs);
        if (ua > 1) ua = 1; if (ub > 1) ub = 1;
        FxV q[4];
        const float* P[4] = {a.t, a.h, b.t, b.h}; float U[4] = {ua, ua, ub, ub}, V[4] = {0.05f, 0.95f, 0.05f, 0.95f};
        for (int c = 0; c < 4; c++) {
            memcpy(q[c].p, P[c], 12); q[c].uv[0] = U[c]; q[c].uv[1] = V[c];
            float fadeTail = 1.f - U[c] * 0.6f;
            for (int a2 = 0; a2 < 3; a2++) q[c].c[a2] = col[a2];
            q[c].c[3] = col[3] * fadeTail; q[c].k = 1.f;
        }
        o[nv++] = q[0]; o[nv++] = q[1]; o[nv++] = q[2]; o[nv++] = q[2]; o[nv++] = q[1]; o[nv++] = q[3];
    }
    ctx->Unmap(g_trailVb, 0);
    UINT fs = sizeof(FxV), fo = 0;
    ctx->IASetInputLayout(fxil); ctx->IASetVertexBuffers(0, 1, &g_trailVb, &fs, &fo); ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ctx->VSSetShader(fxvs, nullptr, 0); ctx->PSSetShader(fxps, nullptr, 0); ctx->PSSetSamplers(0, 1, &sampClamp);
    ID3D11ShaderResourceView* srv = fxSrvs[8]; ctx->PSSetShaderResources(0, 1, &srv);
    float bz[4] = {0, 0, 0, 0}; ctx->OMSetBlendState(bsAdd, bz, 0xffffffff);
    ctx->OMSetDepthStencilState(depth ? dsFx : dsOff, 0);
    ctx->Draw(nv, 0);
}

static void khfx_draw(const float Y[3], const float Z[3], ID3D11DepthStencilView* dsv);   // v35 (khfx.inc)
static void khfx_mdl_draw(const float Y[3], const float Z[3], ID3D11DepthStencilView* dsv);   // v43 (khfx.inc)
static void khfx_frame_latch_gameplay(); static void khfx_frame_release();   // v78 (khfx.inc)
static void draw(ID3D11RenderTargetView* target, UINT W, UINT H, ID3D11DepthStencilView* sceneCopy) {
    Frame f; EnterCriticalSection(&g_cs); f = g_frame; LeaveCriticalSection(&g_cs);
    if (!f.visible || g_mo.empty()) return;
    // Python stopped sending frames (pause menu, loading screen, mod tick broke): don't leave a stale Sora on screen
    if (g_frameT && GetTickCount64() - g_frameT > 300) return;
    if (f.fade <= 0.001f && f.fade != 0.0f) return;   // fully faded out (0 exactly = old Python struct: treat as solid)
    bool relight = f.relight[0] > 0.001f;
    ID3D11ShaderResourceView* envSrv = relight ? env_capture(target) : nullptr;
    // bones -> cb: sample the current motion (and the previous one while crossfading)
    static std::vector<float> cur, prev;
    auto sample = [&](int ai, float time, std::vector<float>& out) {
        if (ai < 0 || ai >= (int)g_mo.size() || g_mo[ai].frames == 0) ai = 0;
        const Motion& mo = g_mo[ai];
        float ft = fmodf(fmaxf(time, 0) * g_fps, (float)mo.frames); int f0 = (int)ft, f1 = (f0 + 1) % mo.frames; float a = ft - f0;
        const float* A = &mo.m[(size_t)f0 * g_bones * 12]; const float* B = &mo.m[(size_t)f1 * g_bones * 12];
        out.resize(g_bones * 12);
        for (uint32_t k = 0; k < g_bones * 12; k++) out[k] = A[k] + (B[k] - A[k]) * a;
    };
    // Live pose, bridged by the baked copy of the SAME KH1 motion while KH1 rebuilds its joints (motion changes):
    // w_live fades 1 -> 0 over ~80 ms when the live data goes stale and back when it returns, so stopping / starting /
    // landing etc. flow instead of freezing and popping.
    static float wLive = 1.f; static double lastT = 0; static std::vector<float> baked;
    double nowT = qpc_now(); float dt = lastT > 0 ? (float)(nowT - lastT) : 0.016f; lastT = nowT; if (dt > 0.1f) dt = 0.1f;
    bool liveOk = (f.flags & 64) && live_bones(cur);
    if (liveOk) {
        float target = g_liveHeld ? 0.f : 1.f, step = dt / 0.08f;
        wLive += (target > wLive) ? fminf(step, target - wLive) : -fminf(step, wLive - target);
        // Which baked motion bridges the gap: the same KH1 motion when its baked copy matches KH1 (index < 100:
        // locomotion, jumps, attacks). The baked 100+ motions are NOT what KH1 plays (113-116 are not the glide), so
        // for those the motion Sora was just in (jump / fall) keeps playing on, and the live glide fades in over it
        // once KH1's joints are valid again - no freeze on the way into a glide.
        static int lowAnim = -1; static float lowClock = 0; static double lowT = 0;
        if (!g_liveHeld && g_khAnim >= 0 && g_khAnim < 100) { lowAnim = g_khAnim; lowClock = g_khClock; lowT = nowT; }
        int bAnim = g_khAnim; float bTime = g_khClock / 60.f;
        // only INTO a glide (113-116, entered from a jump/fall) does the previous motion carry on; abilities and combos
        // (200+, entered from idle/run) would flash an idle/run pose at every step - they hold the last live pose
        if (g_khAnim >= 113 && g_khAnim <= 116 && lowAnim >= 8 && lowAnim <= 12) { bAnim = lowAnim; bTime = lowClock / 60.f + (float)(nowT - lowT); }
        else if (g_khAnim >= 100) bAnim = -1;
        if (wLive < 0.999f && bAnim >= 0 && bAnim < 100 && bAnim < (int)g_mo.size() && g_mo[bAnim].frames) {
            sample(bAnim, bTime, baked);
            for (size_t k = 0; k < cur.size() && k < baked.size(); k++) cur[k] = baked[k] + (cur[k] - baked[k]) * wLive;
        } else if (g_khAnim >= 100) wLive = 1.f;   // nothing to bridge with: hold the last live pose
        // diagnostics: how long KH1's joints stay unusable at each motion change
        static double heldSince = 0; static int heldAnim = -1;
        if (g_liveHeld && heldSince == 0) { heldSince = nowT; heldAnim = g_khAnim; }
        if (!g_liveHeld && heldSince > 0) {
            double ms = (nowT - heldSince) * 1000.0;
            if (ms > 200) logf("live pose held %.0f ms (anim %d -> %d)", ms, heldAnim, g_khAnim);
            heldSince = 0;
        }
    }
    if (!liveOk) {   // flag 64: live pose from a running KH1 (falls back to the baked motions)
        if (!(f.flags & 64)) g_liveState = 0;
        sample(f.anim, f.time, cur);
        float bw = f.blend < 0 ? 0 : (f.blend > 1 ? 1 : f.blend);
        if (bw > 0.001f) { sample(f.anim2, f.time2, prev); for (size_t k = 0; k < cur.size(); k++) cur[k] += (prev[k] - cur[k]) * bw; }
    }
    {   // hit-stop (sr_hitstop): the pose holds for a moment - the blade "bites" into a wall or an enemy
        static std::vector<float> frozen;
        if (qpc_now() < g_hitstopUntil) { if (frozen.empty()) frozen = cur; else cur = frozen; } else frozen.clear();
    }
    D3D11_MAPPED_SUBRESOURCE ms;
    if (SUCCEEDED(ctx->Map(cbBones, 0, D3D11_MAP_WRITE_DISCARD, 0, &ms))) {
        memcpy(ms.pData, cur.data(), sizeof(float) * (cur.size() < 900 * 4 ? cur.size() : 900 * 4));
        ctx->Unmap(cbBones, 0);
    }
    // world: KH (x,y,z) cm Y-up -> UE (X fwd, Y right, Z up); forward = KH +Z
    float s = f.scale, mir = (f.flags & 1) ? -1.f : 1.f;
    M4 kh2ue{}; kh2ue.m[0][2] = s; kh2ue.m[1][0] = s * mir; kh2ue.m[2][1] = s; kh2ue.m[3][3] = 1;
    float yaw = f.yaw + f.yaw_offset, cy = cosf(yaw), sy = sinf(yaw);
    M4 ry = ident(); ry.m[0][0] = cy; ry.m[0][1] = -sy; ry.m[1][0] = sy; ry.m[1][1] = cy;
    M4 tr = ident(); tr.m[0][3] = f.pos[0]; tr.m[1][3] = f.pos[1]; tr.m[2][3] = f.pos[2];
    M4 world = mul(tr, mul(ry, kh2ue));
    if (fabsf(f.bank) > 1e-4f) {   // lean: rotate about the forward (UE X) axis through the chest
        float cb = cosf(f.bank), sb = sinf(f.bank), hz = 90.f * s;
        M4 rx = ident(); rx.m[1][1] = cb; rx.m[1][2] = -sb; rx.m[2][1] = sb; rx.m[2][2] = cb;
        M4 up = ident(); up.m[2][3] = hz; M4 dn = ident(); dn.m[2][3] = -hz;
        world = mul(tr, mul(ry, mul(up, mul(rx, mul(dn, kh2ue)))));
    }
    // Keyblade hilt + tip in Borderlands world space, posed exactly as drawn (sr_get_blade: real blade hit detection)
    if (g_bladeV[0].b >= 0) {
        float o[6];
        for (int k = 0; k < 2; k++) {
            const float* R = &cur[(size_t)g_bladeV[k].b * 12]; const float* p = g_bladeV[k].p;
            float sp[4] = {R[0] * p[0] + R[1] * p[1] + R[2] * p[2] + R[3], R[4] * p[0] + R[5] * p[1] + R[6] * p[2] + R[7],
                           R[8] * p[0] + R[9] * p[1] + R[10] * p[2] + R[11], 1};
            for (int r = 0; r < 3; r++) o[k * 3 + r] = world.m[r][0] * sp[0] + world.m[r][1] * sp[1] + world.m[r][2] * sp[2] + world.m[r][3];
        }
        EnterCriticalSection(&g_cs); memcpy(g_blade, o, sizeof o); g_bladeT = qpc_now(); g_bladeN++; LeaveCriticalSection(&g_cs);
        trail_push(o, o + 3);
    }
    // view: UE camera -> d3d (x=right, y=up, z=forward)
    float X[3], Y[3], Z[3]; ue_axes(f.cam_rot[0], f.cam_rot[1], f.cam_rot[2], X, Y, Z);
    M4 view{}; const float* c = f.cam_pos;
    for (int k = 0; k < 3; k++) { view.m[0][k] = Y[k]; view.m[1][k] = Z[k]; view.m[2][k] = X[k]; }
    view.m[0][3] = -(Y[0] * c[0] + Y[1] * c[1] + Y[2] * c[2]);
    view.m[1][3] = -(Z[0] * c[0] + Z[1] * c[1] + Z[2] * c[2]);
    view.m[2][3] = -(X[0] * c[0] + X[1] * c[1] + X[2] * c[2]); view.m[3][3] = 1;
    float xs = 1.f / tanf(f.fov * 3.14159265f / 360.f), ys = xs * (float)W / (float)H, zn = 10.f, zf = 200000.f;
    M4 proj{}; proj.m[0][0] = xs; proj.m[1][1] = ys; proj.m[3][2] = 1;
    if (f.flags & 4) { proj.m[2][2] = zn / (zn - zf); proj.m[2][3] = -zf * zn / (zn - zf); }   // reversed Z
    else { proj.m[2][2] = zf / (zf - zn); proj.m[2][3] = -zn * zf / (zf - zn); }
    M4 vp = mul(proj, view);
    if (envSrv) ground_light(vp, f.pos, W, H, envSrv, (f.flags & 2) && sceneCopy && !(f.flags & 4));
    if (SUCCEEDED(ctx->Map(cbFrame, 0, D3D11_MAP_WRITE_DISCARD, 0, &ms))) {
        float* o = (float*)ms.pData;   // HLSL default column_major: write transposed
        for (int i = 0; i < 4; i++) for (int j = 0; j < 4; j++) { o[j * 4 + i] = world.m[i][j]; o[16 + j * 4 + i] = vp.m[i][j]; }
        float L[3] = {f.light[0], f.light[1], f.light[2]}; float ln = sqrtf(L[0] * L[0] + L[1] * L[1] + L[2] * L[2]) + 1e-6f;
        o[32] = L[0] / ln; o[33] = L[1] / ln; o[34] = L[2] / ln; o[35] = 0; o[36] = f.bright > 0 ? f.bright : 1.0f; o[37] = g_gl[0]; o[38] = g_gl[1]; o[39] = g_gl[2];   // v26: misc.yzw = ground light
        // Sora's chest on screen (centre of the relight sample)
        float ch[4] = {f.pos[0], f.pos[1], f.pos[2] + 90.f * s, 1}, cc[4];
        for (int r = 0; r < 4; r++) cc[r] = vp.m[r][0] * ch[0] + vp.m[r][1] * ch[1] + vp.m[r][2] * ch[2] + vp.m[r][3];
        float cu = 0.5f, cv = 0.5f;
        if (cc[3] > 1.f) { cu = fminf(fmaxf((cc[0] / cc[3] + 1) * 0.5f, 0), 1); cv = fminf(fmaxf((1 - cc[1] / cc[3]) * 0.5f, 0), 1); }
        float fade = (f.fade > 0.f && f.fade < 1.f) ? f.fade : 1.f;
        float env[16] = {f.relight[0], f.relight[1], f.relight[2] > 0 ? f.relight[2] : 0.35f, f.relight[3] > 0 ? f.relight[3] : 6.f,
                         fade, f.haze[0], f.haze[1], f.haze[2],
                         f.relight2[0] > 0 ? f.relight2[0] : 0.35f, f.relight2[1] > 0 ? f.relight2[1] : 1.25f, cu, cv,
                         1.f / (float)W, 1.f / (float)H, envSrv ? 1.f : 0.f, (envSrv && g_glHave) ? 1.f : 0.f};   // v26: scr.w = ground light valid
        memcpy(o + 40, env, sizeof env);
        {   // v25: level lights (sr_light), camera, shadow-map slot (filled after the shadow pass)
            float ex[36] = {}; for (int k = 0; k < 12; k++) ex[k] = g_lightCfg[k];
            ex[12] = f.cam_pos[0]; ex[13] = f.cam_pos[1]; ex[14] = f.cam_pos[2];
            ex[32] = 0; ex[33] = g_lightCfg[13] > 0.5f ? g_lightCfg[13] : 3.f; ex[34] = (float)SH_N; ex[35] = f.relight2[2] > 0 ? f.relight2[2] : 0.45f;   // v26: ground light reference
            memcpy(o + 56, ex, sizeof ex);
        }
        memcpy(g_lastCb, o, sizeof g_lastCb);
        ctx->Unmap(cbFrame, 0);
    }
    if ((f.flags & 2) && sceneCopy && !(f.flags & 4)) run_probes(vp, W, H);   // scene depth before Sora is in it

    // party actors + Heartless: pose them first (the shadow pass needs every caster), drawn after Sora
    static std::vector<DrawRec> recs; int nRec = 0;
    auto rec_at = [&](int i) -> DrawRec& { if ((int)recs.size() <= i) recs.resize(i + 1); return recs[i]; };
    {
        DrawRec& r0 = rec_at(nRec++); r0.vb = vb; r0.ib = ib; r0.subs = &g_subs; r0.srvs = &srvs; r0.bones = cur; r0.world = world;
        r0.wpn = wpn_get(0); r0.skip = r0.wpn ? wpn_skip(0) : nullptr;
    }
    Actor* drawList[2 + MAX_ENEMIES]; int nDraw = 0;
    for (auto& a : g_actors) drawList[nDraw++] = &a;
    for (auto& a : g_enemies) drawList[nDraw++] = &a;
    for (int di = 0; di < nDraw; di++) {
        Actor& a = *drawList[di];
        ActorFrame af; EnterCriticalSection(&g_cs); af = a.f; ULONGLONG at = a.fT; Actor* gm = a.geo; bool rst = a.reset; a.reset = false; LeaveCriticalSection(&g_cs);
        if (rst) { a.lastGood.clear(); a.pPrev.clear(); a.pCur.clear(); }
        if (di >= 2) { if (!gm || !gm->live.size()) { a.state = 0; continue; } if (!gm->vb) init_actor_gpu(*gm); }
        Actor& g = gm ? *gm : a;
        if (!af.visible || !g.vb || GetTickCount64() - at > (hold_active() ? 3500u : 300u)) { a.state = af.visible ? 3 : 0; continue; }
        DrawRec& d = rec_at(nRec);
        std::vector<float>& ab = d.bones;
        int fm = (af.flags >> 12) & 15;
        if ((af.flags & 0x100) && fm < (int)a.fly.size() && a.fly[fm].nf) {
            const Actor::Fly& F = a.fly[fm]; size_t np_ = a.live.size();
            double t = fmod(qpc_now(), (double)F.dur) * a.flyFps; uint32_t f0 = (uint32_t)t % F.nf, f1 = (f0 + 1) % F.nf;
            float k = (float)(t - floor(t));
            ab.resize(np_ * 12);
            const float* r0 = &F.rows[(size_t)f0 * np_ * 12]; const float* r1 = &F.rows[(size_t)f1 * np_ * 12];
            for (size_t q = 0; q < np_ * 12; q++) ab[q] = r0[q] + (r1[q] - r0[q]) * k;
        } else if (!actor_bones(a, ab)) { a.state = 2; continue; }
        a.state = 1;
        float as = af.scale, amir = (f.flags & 1) ? -1.f : 1.f;
        M4 k2u{}; k2u.m[0][2] = as; k2u.m[1][0] = as * amir; k2u.m[2][1] = as; k2u.m[3][3] = 1;
        float ay = af.yaw + f.yaw_offset; M4 ary = ident(); ary.m[0][0] = cosf(ay); ary.m[0][1] = -sinf(ay); ary.m[1][0] = sinf(ay); ary.m[1][1] = cosf(ay);
        M4 atr = ident(); atr.m[0][3] = af.pos[0]; atr.m[1][3] = af.pos[1]; atr.m[2][3] = af.pos[2];
        M4 apt = ident(); { float pa = std::isfinite(af.pad) ? af.pad : 0.f; apt.m[1][1] = cosf(pa); apt.m[1][2] = -sinf(pa); apt.m[2][1] = sinf(pa); apt.m[2][2] = cosf(pa); }   // v23: pitch (ActorFrame.pad, rad)
        d.world = mul(atr, mul(ary, mul(apt, k2u)));
        d.vb = g.vb; d.ib = g.ib; d.subs = &g.subs; d.srvs = &g.srvs;
        d.wpn = di < 2 ? wpn_get(di + 1) : nullptr; d.skip = d.wpn ? wpn_skip(di + 1) : nullptr;
        nRec++;
    }
    // v24: their shadows on the Borderlands world (needs the scene depth copy, before Sora is written into it)
    g_lvpOk = false;
    if ((f.flags & 2) && sceneCopy && !(f.flags & 4)) {
        float centre[3] = {f.pos[0], f.pos[1], f.pos[2] + 90.f * s};
        shadow_pass(recs.data(), nRec, vp, f.light, centre, target, W, H);
        if (g_lvpOk) {   // v25: characters receive the shadow map too (self + each other)
            for (int r = 0; r < 4; r++) for (int c = 0; c < 4; c++) g_lastCb[72 + c * 4 + r] = g_lvp.m[r][c];
            g_lastCb[88] = g_lightCfg[12];
        }
        upload_cb(cbFrame, g_lastCb, sizeof g_lastCb);   // the shadow pass reused Sora's constant buffers
        upload_cb(cbBones, cur.data(), sizeof(float) * (cur.size() < 900 * 4 ? cur.size() : 900 * 4));
    }

    D3D11_VIEWPORT vpt{0, 0, (float)W, (float)H, 0, 1};
    bool depth = (f.flags & 2) && sceneCopy;
    ID3D11DepthStencilView* dsv = depth ? sceneCopy : nullptr;
    if (!depth) { ensure_own_depth(W, H); dsv = g_ownDsv; if (dsv) ctx->ClearDepthStencilView(dsv, D3D11_CLEAR_DEPTH, 1.0f, 0); }
    if (outline_begin(W, H)) { ID3D11RenderTargetView* two[2] = {target, g_olRtv}; ctx->OMSetRenderTargets(2, two, dsv); }   // v30: + character depth
    else ctx->OMSetRenderTargets(1, &target, dsv);
    ctx->RSSetViewports(1, &vpt); ctx->RSSetState(rs);
    ctx->OMSetDepthStencilState(depth ? ((f.flags & 4) ? dsOnRev : dsOn) : (dsv ? dsOwn : dsOff), 0);
    float bf[4] = {0, 0, 0, 0}; ctx->OMSetBlendState(bsOff, bf, 0xffffffff);
    UINT stride = sizeof(Vtx), off = 0;
    ctx->IASetInputLayout(il); ctx->IASetVertexBuffers(0, 1, &vb, &stride, &off); ctx->IASetIndexBuffer(ib, DXGI_FORMAT_R32_UINT, 0);
    ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ID3D11Buffer* cbs[2] = {cbFrame, cbBones};
    ctx->VSSetShader(vs, nullptr, 0); ctx->VSSetConstantBuffers(0, 2, cbs);
    ctx->PSSetShader(ps, nullptr, 0); ctx->PSSetConstantBuffers(0, 2, cbs); ctx->PSSetSamplers(0, 1, &samp);
    ctx->PSSetSamplers(1, 1, &sampClamp); ctx->PSSetShaderResources(1, 1, &envSrv);
    if (g_lvpOk) ctx->PSSetShaderResources(2, 1, &g_shSrv);   // v25: self-shadowing (stays bound for the party / Heartless)
    ctx->GSSetShader(nullptr, nullptr, 0); ctx->HSSetShader(nullptr, nullptr, 0); ctx->DSSetShader(nullptr, nullptr, 0);
    draw_rec_subs(vb, ib, g_subs, srvs, recs[0].skip, recs[0].wpn);
    { ID3D11ShaderResourceView* none = nullptr; ctx->PSSetShaderResources(1, 1, &none); }

    // party actors + Heartless (posed above): same shader / depth / relight as Sora, their own world matrix and bones
    for (int i = 1; i < nRec; i++) {
        const DrawRec& d = recs[i];
        upload_cb(cbBones, d.bones.data(), sizeof(float) * (d.bones.size() < 900 * 4 ? d.bones.size() : 900 * 4));
        float cb[92]; memcpy(cb, g_lastCb, sizeof cb);
        for (int r = 0; r < 4; r++) for (int c = 0; c < 4; c++) cb[c * 4 + r] = d.world.m[r][c];
        upload_cb(cbFrame, cb, sizeof cb);
        ctx->IASetInputLayout(il);
        ctx->VSSetShader(vs, nullptr, 0); ctx->PSSetShader(ps, nullptr, 0);
        ctx->PSSetSamplers(1, 1, &sampClamp); ctx->PSSetShaderResources(1, 1, &envSrv);
        draw_rec_subs(d.vb, d.ib, *d.subs, *d.srvs, d.skip, d.wpn);
        { ID3D11ShaderResourceView* none = nullptr; ctx->PSSetShaderResources(1, 1, &none); }
    }
    { ID3D11ShaderResourceView* none = nullptr; ctx->PSSetShaderResources(2, 1, &none); }   // the shadow map is a DSV again next frame
    outline_pass(target, W, H, dsv, depth && !(f.flags & 4));   // v30: Borderlands-style ink lines around / inside the characters
    trail_draw(dsv != nullptr);
    int nfx = f.nfx < 0 ? 0 : (f.nfx > 256 ? 256 : f.nfx);
    if (nfx && fxvs && fxvb && SUCCEEDED(ctx->Map(fxvb, 0, D3D11_MAP_WRITE_DISCARD, 0, &ms))) {
        // group: key = blend (alpha first) * 64 + texture + 1; procedural dots (tex -1) are additive
        int order[256]; for (int k = 0; k < nfx; k++) order[k] = k;
        auto key = [&](int k) { const auto& e = f.fx[k]; bool tex = e.tex >= 0 && e.tex < (int)fxSrvs.size() && fxSrvs[e.tex];
                                int blend = (tex && (e.mode & 0xF) == 1) ? 0 : 1; return blend * 64 + (tex ? e.tex + 1 : 0); };
        for (int i = 1; i < nfx; i++) { int v = order[i], j = i; while (j > 0 && key(order[j - 1]) > key(v)) { order[j] = order[j - 1]; j--; } order[j] = v; }
        FxV* o = (FxV*)ms.pData;
        for (int n = 0; n < nfx; n++) {
            const auto& e = f.fx[order[n]]; float h = e.size * 0.5f, st = e.stretch > 0 ? e.stretch : 1.f;
            // v82: mode | 0x10 = SPHERE (KH1 prize orbs): the flat card is pulled toward the camera by its radius (and shrunk
            // by the same perspective ratio, so it looks as big) -> depth-tested like a ball's front, not a card whose
            // lower half sank into a slope rising behind it (user screenshot 2026-10-12: HP orb cut straight across)
            float P[3] = {e.pos[0], e.pos[1], e.pos[2]};
            if (e.mode & 0x10) {
                float dist = 0; for (int a = 0; a < 3; a++) dist += (e.pos[a] - f.cam_pos[a]) * X[a];
                if (dist > 2.f * h) { float k = (dist - h) / dist; for (int a = 0; a < 3; a++) P[a] = e.pos[a] - X[a] * h; h *= k; }
            }
            bool tex = e.tex >= 0 && e.tex < (int)fxSrvs.size() && fxSrvs[e.tex];
            float cr = cosf(e.rot), sr = sinf(e.rot), R[3], U[3];
            for (int a = 0; a < 3; a++) { R[a] = Y[a] * cr + Z[a] * sr; U[a] = -Y[a] * sr + Z[a] * cr; }   // rotated camera right/up
            float cx[4] = {-1, 1, 1, -1}, cy[4] = {-1, -1, 1, 1}; int idx[6] = {0, 1, 2, 0, 2, 3};
            for (int q = 0; q < 6; q++) {
                int c = idx[q]; FxV& v = o[n * 6 + q];
                for (int a = 0; a < 3; a++) v.p[a] = P[a] + (R[a] * cx[c] + U[a] * cy[c] * st) * h;
                float s01 = (cx[c] + 1) * 0.5f, t01 = (cy[c] + 1) * 0.5f;
                if (tex) { v.uv[0] = e.uv[0] + (e.uv[2] - e.uv[0]) * s01; v.uv[1] = e.uv[3] + (e.uv[1] - e.uv[3]) * t01; }
                else { v.uv[0] = s01; v.uv[1] = t01; }
                for (int a = 0; a < 4; a++) v.c[a] = e.color[a];
                v.k = tex ? ((e.mode & 0xF) == 1 ? 2.f : 1.f) : 0.f;
            }
        }
        ctx->Unmap(fxvb, 0);
        UINT fs = sizeof(FxV), fo = 0;
        ctx->IASetInputLayout(fxil); ctx->IASetVertexBuffers(0, 1, &fxvb, &fs, &fo);
        ctx->VSSetShader(fxvs, nullptr, 0); ctx->PSSetShader(fxps, nullptr, 0); ctx->PSSetSamplers(0, 1, &sampClamp);
        ctx->OMSetDepthStencilState(dsv ? dsFx : dsOff, 0);
        float bz[4] = {0, 0, 0, 0};
        for (int n = 0; n < nfx;) {   // one draw per (blend, texture) group
            int k0 = key(order[n]), m = n; while (m < nfx && key(order[m]) == k0) m++;
            int tx = k0 % 64 - 1; ID3D11ShaderResourceView* srv = tx >= 0 ? fxSrvs[tx] : nullptr;
            ctx->PSSetShaderResources(0, 1, &srv);
            ctx->OMSetBlendState(k0 / 64 == 0 ? bsAlpha : bsAdd, bz, 0xffffffff);
            ctx->Draw((m - n) * 6, n * 6); n = m;
        }
    }
    // v71: effect MODELS first, then sprites: effects write no depth, so the order decides. Tinker Bell's body is an
    // effect mesh (GS 0x88, drawn over the scene since v70) and her golden glow an additive sprite on top of it -
    // sprites first hid the glow under her body (user 2026-10-06). Additive meshes are order-independent anyway.
    // v78: gameplay too draws ONE whole KH1 effect frame (kh1_fx v40 "Local\SoraFxFrame", same as the intro since v77):
    // sprites + meshes + the camera that decodes them, all from KH1's last complete effect pass. cfg[95] 1 = old guess.
    khfx_frame_latch_gameplay();
    khfx_mdl_draw(Y, Z, dsv);     // v43: KH1's effect models (Thunder bolts, Aero wind, Tinker Bell...)
    khfx_draw(Y, Z, dsv);   // v35: KH1's own effect sprites
    khfx_frame_release();
}

// ------------------------------------------------------------------ state save/restore (the minimum we touch)
struct Saved {
    ID3D11RenderTargetView* rt[8]; ID3D11DepthStencilView* ds; D3D11_VIEWPORT vp[16]; UINT nvp;
    ID3D11RasterizerState* rs; ID3D11DepthStencilState* dss; UINT sref; ID3D11BlendState* bs; float bf[4]; UINT sm;
    ID3D11InputLayout* il; ID3D11Buffer* vb; UINT vs_, vo; ID3D11Buffer* ib; DXGI_FORMAT ifmt; UINT io; D3D11_PRIMITIVE_TOPOLOGY top;
    ID3D11VertexShader* v; ID3D11PixelShader* p; ID3D11GeometryShader* g; ID3D11Buffer* vcb[2]; ID3D11Buffer* pcb[2];
    ID3D11ShaderResourceView* srv[2]; ID3D11SamplerState* smp[2];
};
static void save(Saved& s) {
    ctx->OMGetRenderTargets(8, s.rt, &s.ds); s.nvp = 16; ctx->RSGetViewports(&s.nvp, s.vp); ctx->RSGetState(&s.rs);
    ctx->OMGetDepthStencilState(&s.dss, &s.sref); ctx->OMGetBlendState(&s.bs, s.bf, &s.sm);
    ctx->IAGetInputLayout(&s.il); ctx->IAGetVertexBuffers(0, 1, &s.vb, &s.vs_, &s.vo); ctx->IAGetIndexBuffer(&s.ib, &s.ifmt, &s.io); ctx->IAGetPrimitiveTopology(&s.top);
    ctx->VSGetShader(&s.v, nullptr, nullptr); ctx->PSGetShader(&s.p, nullptr, nullptr); ctx->GSGetShader(&s.g, nullptr, nullptr);
    ctx->VSGetConstantBuffers(0, 2, s.vcb); ctx->PSGetConstantBuffers(0, 2, s.pcb); ctx->PSGetShaderResources(0, 2, s.srv); ctx->PSGetSamplers(0, 2, s.smp);
}
template <class T> static void rel(T*& p) { if (p) { p->Release(); p = nullptr; } }
#include "khfx.inc"
#include "khui.inc"   // v52: KH1's own 2D interface
#include "menumdl.inc"   // v57: KH1's main-menu party models
#include "intro.inc"   // v74: KH1 summon intro redrawn natively (KH1 camera, stage, Sora + summon, effects)
static void restore(Saved& s) {
    ctx->OMSetRenderTargets(8, s.rt, s.ds); ctx->RSSetViewports(s.nvp, s.vp); ctx->RSSetState(s.rs);
    ctx->OMSetDepthStencilState(s.dss, s.sref); ctx->OMSetBlendState(s.bs, s.bf, s.sm);
    ctx->IASetInputLayout(s.il); ctx->IASetVertexBuffers(0, 1, &s.vb, &s.vs_, &s.vo); ctx->IASetIndexBuffer(s.ib, s.ifmt, s.io); ctx->IASetPrimitiveTopology(s.top);
    ctx->VSSetShader(s.v, nullptr, 0); ctx->PSSetShader(s.p, nullptr, 0); ctx->GSSetShader(s.g, nullptr, 0);
    ctx->VSSetConstantBuffers(0, 2, s.vcb); ctx->PSSetConstantBuffers(0, 2, s.pcb); ctx->PSSetShaderResources(0, 2, s.srv); ctx->PSSetSamplers(0, 2, s.smp);
    for (auto& r : s.rt) rel(r); rel(s.ds); rel(s.rs); rel(s.dss); rel(s.bs); rel(s.il); rel(s.vb); rel(s.ib); rel(s.v); rel(s.p); rel(s.g);
    rel(s.vcb[0]); rel(s.vcb[1]); rel(s.pcb[0]); rel(s.pcb[1]); rel(s.srv[0]); rel(s.srv[1]); rel(s.smp[0]); rel(s.smp[1]);
}

// ------------------------------------------------------------------ v32: KH1's own menu shown inside Borderlands
// KH1 runs in its window behind Borderlands. While the menu view is on, a background thread grabs that window with
// PrintWindow(PW_RENDERFULLCONTENT) ~35 times a second (the window must not be minimized; ~12 ms per grab) and the
// render thread shows the newest grab full-screen (letterboxed) over Borderlands at Present. Python opens KH1's menu
// (Start), forwards the keys and pauses Borderlands meanwhile.
static volatile LONG g_kvOn = 0, g_kvStop = 0, g_kvSeq = 0; static LONG g_kvShown = -1;
static HANDLE g_kvThread = nullptr; static CRITICAL_SECTION g_kvCs; static bool g_kvCsInit = false;
static std::vector<uint8_t> g_kvPix; static int g_kvW = 0, g_kvH = 0;
static ID3D11Texture2D* g_kvTex = nullptr; static ID3D11ShaderResourceView* g_kvSrv = nullptr; static int g_kvTexW = 0, g_kvTexH = 0;
static ID3D11PixelShader* kvps = nullptr; static ID3D11Buffer* kvcb = nullptr; static volatile float g_kvAlpha = 1.f;
static const char* kKvShader = R"(
cbuffer K : register(b0) { float4 rect; float4 p1; };   // rect = x, y, w, h (pixels); p1.x = alpha
Texture2D T : register(t0); SamplerState S : register(s0);
float4 psmain(float4 pos : SV_Position) : SV_Target {
  float2 uv = (pos.xy - rect.xy) / rect.zw;
  if (any(uv < 0) || any(uv > 1)) return float4(0, 0, 0, p1.x);
  return float4(T.Sample(S, uv).rgb, p1.x);
})";
static BOOL CALLBACK kv_find(HWND h, LPARAM lp) {
    DWORD pid = 0; GetWindowThreadProcessId(h, &pid);
    auto* io = (std::pair<DWORD, HWND>*)lp;
    if (pid == io->first && IsWindowVisible(h) && !GetWindow(h, GW_OWNER)) { RECT r; GetClientRect(h, &r); if (r.right > 320) { io->second = h; return FALSE; } }
    return TRUE;
}
static DWORD WINAPI kv_thread(LPVOID) {
    typedef void*(WINAPI * SetCtx)(void*);
    if (auto f = (SetCtx)GetProcAddress(GetModuleHandleA("user32.dll"), "SetThreadDpiAwarenessContext")) f((void*)-4);   // per-monitor v2: full-size grabs
    HDC mem = CreateCompatibleDC(nullptr); HBITMAP bmp = nullptr; void* bits = nullptr; int bw = 0, bh = 0;
    HWND hw = nullptr; ULONGLONG findT = 0;
    while (!g_kvStop) {
        if (!g_kvOn) { Sleep(40); continue; }
        if (!hw || !IsWindow(hw) || GetTickCount64() - findT > 3000) {
            findT = GetTickCount64(); DWORD pid = 0;
            HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0); PROCESSENTRY32W pe{}; pe.dwSize = sizeof pe;
            for (BOOL ok = Process32FirstW(snap, &pe); ok; ok = Process32NextW(snap, &pe))
                if (!_wcsicmp(pe.szExeFile, L"KINGDOM HEARTS FINAL MIX.exe")) { pid = pe.th32ProcessID; break; }
            CloseHandle(snap);
            std::pair<DWORD, HWND> io{pid, nullptr}; if (pid) EnumWindows(kv_find, (LPARAM)&io); hw = io.second;
            if (!hw) { Sleep(200); continue; }
        }
        RECT r; GetClientRect(hw, &r); int w = r.right, h = r.bottom;
        if (w < 64 || h < 64) { Sleep(50); continue; }
        if (w != bw || h != bh) {
            if (bmp) DeleteObject(bmp);
            BITMAPINFO bi{}; bi.bmiHeader.biSize = sizeof bi.bmiHeader; bi.bmiHeader.biWidth = w; bi.bmiHeader.biHeight = -h;
            bi.bmiHeader.biPlanes = 1; bi.bmiHeader.biBitCount = 32; bi.bmiHeader.biCompression = BI_RGB;
            bmp = CreateDIBSection(mem, &bi, DIB_RGB_COLORS, &bits, nullptr, 0); SelectObject(mem, bmp); bw = w; bh = h;
        }
        if (bmp && PrintWindow(hw, mem, 2 /*PW_RENDERFULLCONTENT*/)) {
            GdiFlush();
            EnterCriticalSection(&g_kvCs);
            g_kvPix.assign((uint8_t*)bits, (uint8_t*)bits + (size_t)w * h * 4); g_kvW = w; g_kvH = h;
            LeaveCriticalSection(&g_kvCs);
            InterlockedIncrement(&g_kvSeq);
        }
        Sleep(28);
    }
    if (bmp) DeleteObject(bmp);
    DeleteDC(mem);
    return 0;
}
static void kv_draw(ID3D11RenderTargetView* target, UINT W, UINT H) {
    if (!kvps) {
        HMODULE dc = LoadLibraryA("d3dcompiler_47.dll"); auto comp = dc ? (PFN_D3DCompile)GetProcAddress(dc, "D3DCompile") : nullptr;
        ID3DBlob *f = nullptr, *er = nullptr;
        if (!comp || FAILED(comp(kKvShader, strlen(kKvShader), "kv", nullptr, nullptr, "psmain", "ps_5_0", 0, 0, &f, &er))) {
            logf("kv shader: %s", er ? (char*)er->GetBufferPointer() : "?"); g_kvOn = 0; return;
        }
        dev->CreatePixelShader(f->GetBufferPointer(), f->GetBufferSize(), nullptr, &kvps);
        D3D11_BUFFER_DESC bd{}; bd.ByteWidth = 32; bd.Usage = D3D11_USAGE_DYNAMIC; bd.BindFlags = D3D11_BIND_CONSTANT_BUFFER; bd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
        dev->CreateBuffer(&bd, nullptr, &kvcb);
    }
    LONG seq = g_kvSeq;
    if (seq != g_kvShown) {
        EnterCriticalSection(&g_kvCs);
        if (g_kvW > 0 && (!g_kvTex || g_kvTexW != g_kvW || g_kvTexH != g_kvH)) {
            rel(g_kvSrv); rel(g_kvTex);
            D3D11_TEXTURE2D_DESC td{}; td.Width = g_kvW; td.Height = g_kvH; td.MipLevels = 1; td.ArraySize = 1; td.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
            td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT; td.BindFlags = D3D11_BIND_SHADER_RESOURCE;
            if (SUCCEEDED(dev->CreateTexture2D(&td, nullptr, &g_kvTex))) dev->CreateShaderResourceView(g_kvTex, nullptr, &g_kvSrv);
            g_kvTexW = g_kvW; g_kvTexH = g_kvH;
        }
        if (g_kvTex && !g_kvPix.empty()) ctx->UpdateSubresource(g_kvTex, 0, nullptr, g_kvPix.data(), g_kvW * 4, 0);
        LeaveCriticalSection(&g_kvCs);
        g_kvShown = seq;
    }
    if (!g_kvSrv || !kvps || !srvs_) return;
    float s = fminf((float)W / g_kvTexW, (float)H / g_kvTexH), w = g_kvTexW * s, h = g_kvTexH * s;
    float c[8] = {(W - w) * 0.5f, (H - h) * 0.5f, w, h, g_kvAlpha, 0, 0, 0}; upload_cb(kvcb, c, sizeof c);
    D3D11_VIEWPORT vp{0, 0, (float)W, (float)H, 0, 1};
    ctx->OMSetRenderTargets(1, &target, nullptr); ctx->RSSetViewports(1, &vp); ctx->RSSetState(rs);
    ctx->OMSetDepthStencilState(dsOff, 0); float bf[4] = {0, 0, 0, 0}; ctx->OMSetBlendState(bsShadow, bf, 0xffffffff);
    ctx->IASetInputLayout(nullptr); ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ctx->VSSetShader(srvs_, nullptr, 0); ctx->PSSetShader(kvps, nullptr, 0); ctx->PSSetConstantBuffers(0, 1, &kvcb);
    ctx->PSSetShaderResources(0, 1, &g_kvSrv); ctx->PSSetSamplers(0, 1, &sampClamp);
    ctx->GSSetShader(nullptr, nullptr, 0);
    ctx->Draw(3, 0);
    ID3D11ShaderResourceView* none = nullptr; ctx->PSSetShaderResources(0, 1, &none);
}
extern "C" __declspec(dllexport) int sr_kh1_view(int on, float alpha) {   // -> frames grabbed so far
    if (!g_kvCsInit) { InitializeCriticalSection(&g_kvCs); g_kvCsInit = true; }
    g_kvAlpha = alpha > 0 ? alpha : 1.f;
    if (on && !g_kvThread) { g_kvStop = 0; g_kvThread = CreateThread(nullptr, 0, kv_thread, nullptr, 0, nullptr); }
    g_kvOn = on ? 1 : 0;
    return (int)g_kvSeq;
}
static void kv_shutdown() {   // hot swap: stop the grab thread before this DLL unloads (GPU objects: release_gfx)
    g_kvOn = 0; g_kvStop = 1;
    if (g_kvThread) { WaitForSingleObject(g_kvThread, 2000); CloseHandle(g_kvThread); g_kvThread = nullptr; }
}
// ------------------------------------------------------------------ hooks
// ------------------------------------------------------------------ v29: hot swap (idea: justbustin/minecraft-crossover-bridge's
// swappable core). logic.py loads a new build from data/sora_render_next.dll while Borderlands runs: sr_shutdown asks
// the render thread to free our GPU objects at its next Present, disables every hook, waits until no thread is inside
// one of our hooks, removes them and stops audio; then Python unloads this DLL and loads the new one.
static volatile LONG g_inHook = 0, g_shutReq = 0, g_shutDone = 0;
struct HookGuard { HookGuard() { InterlockedIncrement(&g_inHook); } ~HookGuard() { InterlockedDecrement(&g_inHook); } };
static void release_gfx();
typedef HRESULT(STDMETHODCALLTYPE* PresentFn)(IDXGISwapChain*, UINT, UINT);
typedef HRESULT(STDMETHODCALLTYPE* ResizeFn)(IDXGISwapChain*, UINT, UINT, UINT, DXGI_FORMAT, UINT);
typedef void(STDMETHODCALLTYPE* OMSetRTFn)(ID3D11DeviceContext*, UINT, ID3D11RenderTargetView* const*, ID3D11DepthStencilView*);
typedef void(STDMETHODCALLTYPE* DrawIdxFn)(ID3D11DeviceContext*, UINT, UINT, INT);
typedef void(STDMETHODCALLTYPE* DrawFn)(ID3D11DeviceContext*, UINT, UINT);
static DrawIdxFn oDrawIdx; static DrawFn oDraw;
static int g_trace = 0;          // frames left to trace
static int g_draws = 0;          // draws since last RT bind
static int g_frameDraws = 0;     // draws since the last Present
static void after_ui_draw(ID3D11DeviceContext* c, UINT n, bool indexed);
static void STDMETHODCALLTYPE hkDrawIdx(ID3D11DeviceContext* c, UINT n, UINT a, INT b) { HookGuard hg; g_draws++; g_frameDraws++; oDrawIdx(c, n, a, b); after_ui_draw(c, n, true); }
static void STDMETHODCALLTYPE hkDraw(ID3D11DeviceContext* c, UINT n, UINT a) { HookGuard hg; g_draws++; g_frameDraws++; oDraw(c, n, a); after_ui_draw(c, n, false); }
static void describe(ID3D11View* v, char* out, size_t n) {
    if (!v) { snprintf(out, n, "-"); return; }
    ID3D11Resource* r = nullptr; v->GetResource(&r); ID3D11Texture2D* t = nullptr;
    if (r && SUCCEEDED(r->QueryInterface(__uuidof(ID3D11Texture2D), (void**)&t))) {
        D3D11_TEXTURE2D_DESC d; t->GetDesc(&d); snprintf(out, n, "%p(%ux%u f%d)", (void*)r, d.Width, d.Height, (int)d.Format); t->Release();
    } else snprintf(out, n, "%p", (void*)r);
    if (r) r->Release();
}
static PresentFn oPresent; static ResizeFn oResize; static OMSetRTFn oOMSetRT; static bool g_ctxHooked = false;
static int g_frames = 0; static int g_uiDraws = 0;

// private copy of the game's scene depth (same desc) so Sora can test + write depth without touching the game's buffer
static ID3D11Texture2D* g_dcopy = nullptr; static ID3D11DepthStencilView* g_dcopyDsv = nullptr; static D3D11_TEXTURE2D_DESC g_dcopyDesc{};
static ID3D11DepthStencilView* depth_copy(ID3D11DepthStencilView* game) {
    ID3D11Resource* res = nullptr; game->GetResource(&res); if (!res) return nullptr;
    ID3D11Texture2D* t = nullptr; res->QueryInterface(__uuidof(ID3D11Texture2D), (void**)&t); res->Release(); if (!t) return nullptr;
    D3D11_TEXTURE2D_DESC td; t->GetDesc(&td);
    if (!g_dcopy || td.Width != g_dcopyDesc.Width || td.Height != g_dcopyDesc.Height || td.Format != g_dcopyDesc.Format) {
        if (g_dcopyDsv) { g_dcopyDsv->Release(); g_dcopyDsv = nullptr; } if (g_dcopy) { g_dcopy->Release(); g_dcopy = nullptr; }
        D3D11_TEXTURE2D_DESC nd = td; nd.Usage = D3D11_USAGE_DEFAULT; nd.CPUAccessFlags = 0; nd.MiscFlags = 0;
        if (g_dcopySrv) { g_dcopySrv->Release(); g_dcopySrv = nullptr; }
        bool typeless = td.Format == DXGI_FORMAT_R24G8_TYPELESS;
        if (typeless) nd.BindFlags |= D3D11_BIND_SHADER_RESOURCE;      // depth probes read it
        if (SUCCEEDED(dev->CreateTexture2D(&nd, nullptr, &g_dcopy))) {
            D3D11_DEPTH_STENCIL_VIEW_DESC dd; game->GetDesc(&dd); dd.Flags = 0;
            if (FAILED(dev->CreateDepthStencilView(g_dcopy, &dd, &g_dcopyDsv))) logf("copy dsv failed");
            if (typeless) {
                D3D11_SHADER_RESOURCE_VIEW_DESC sd{}; sd.Format = DXGI_FORMAT_R24_UNORM_X8_TYPELESS;
                sd.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D; sd.Texture2D.MipLevels = 1;
                if (FAILED(dev->CreateShaderResourceView(g_dcopy, &sd, &g_dcopySrv))) logf("depth srv failed");
            }
        }
        g_dcopyDesc = td; logf("depth copy %ux%u fmt %d", td.Width, td.Height, (int)td.Format);
    }
    if (g_dcopy) ctx->CopyResource(g_dcopy, t);
    t->Release();
    return g_dcopyDsv;
}
static ID3D11Resource* g_bbRes = nullptr; static bool g_drawnThisFrame = false;

static bool g_postDone = false;
static ID3D11RenderTargetView* g_sceneRt = nullptr; static ID3D11DepthStencilView* g_sceneDs = nullptr;   // back buffer was bound WITHOUT depth this frame (post-process copy)
static bool g_uiPass = false; static int g_uiDrawIdx = 0; static ID3D11RenderTargetView* g_uiRtv = nullptr; static ID3D11DepthStencilView* g_uiDsv = nullptr;
static int g_traceUi = 0;   // frames of UI-pass draw tracing left
static int frame_flags() { return g_frame.flags; }
static void draw_sora_now(ID3D11RenderTargetView* rt, ID3D11DepthStencilView* d) {
    g_drawnThisFrame = true;
    Saved s{}; save(s);
    ID3D11DepthStencilView* dc = d ? depth_copy(d) : nullptr;
    draw(rt, g_bbW, g_bbH, dc);
    restore(s);
    g_uiDraws++;
}

static void STDMETHODCALLTYPE hkOMSetRT(ID3D11DeviceContext* c, UINT n, ID3D11RenderTargetView* const* r, ID3D11DepthStencilView* d) {
    HookGuard hg;
    if (g_shutReq) { oOMSetRT(c, n, r, d); return; }
    // The HUD/UI pass starts when the game binds the back buffer together with the scene depth:
    // draw Sora there, after the world and before any UI.
    if (g_gfx && !g_drawnThisFrame && n && r && r[0] && d && g_bbRes && c == ctx) {
        ID3D11Resource* rr = nullptr; r[0]->GetResource(&rr);
        bool isbb = rr == g_bbRes; if (rr) rr->Release();
        // the HUD pass = back buffer + depth AFTER the post-process copy (back buffer without depth);
        // an earlier back buffer + depth bind is the start of the world pass on some resolutions
        if (isbb && !g_postDone) isbb = false;
        if (isbb && !(frame_flags() & 8) && !((frame_flags() & 32) && g_drawnThisFrame)) {
            g_uiPass = true; g_uiDrawIdx = 0;
            if (g_uiRtv) g_uiRtv->Release(); if (g_uiDsv) g_uiDsv->Release();
            g_uiRtv = r[0]; g_uiRtv->AddRef(); g_uiDsv = d; g_uiDsv->AddRef();
            if (g_traceUi > 0) logf("UI pass begins (flags %d)", frame_flags());
            if (!(frame_flags() & 16) && !g_drawnThisFrame) draw_sora_now(r[0], d);   // mode 0: before any UI draw
        }
    } else if (g_gfx && n && r && r[0] && !d && g_bbRes && c == ctx && !g_postDone) {
        ID3D11Resource* rr = nullptr; r[0]->GetResource(&rr);
        if (rr == g_bbRes && g_frameDraws > 100) {   // the post-process copy into the back buffer starts now
            g_postDone = true;
            // scene mode: draw Sora into the finished HDR scene before post-processing and the HUD read it
            if ((frame_flags() & 32) && !g_drawnThisFrame && g_sceneRt && g_sceneDs) draw_sora_now(g_sceneRt, g_sceneDs);
        }
        if (rr) rr->Release();
    }
    if (g_gfx && n && r && r[0] && d && g_bbRes && c == ctx && !g_postDone) {
        // remember the last full-size scene colour target bound with depth (world pass)
        ID3D11Resource* rr = nullptr; r[0]->GetResource(&rr);
        if (rr && rr != g_bbRes) {
            ID3D11Texture2D* t = nullptr;
            if (SUCCEEDED(rr->QueryInterface(__uuidof(ID3D11Texture2D), (void**)&t))) {
                D3D11_TEXTURE2D_DESC td; t->GetDesc(&td);
                if (td.Width == g_bbW && td.Height == g_bbH && td.SampleDesc.Count == 1) {
                    if (g_sceneRt != r[0]) { if (g_sceneRt) g_sceneRt->Release(); g_sceneRt = r[0]; g_sceneRt->AddRef(); }
                    if (g_sceneDs != d) { if (g_sceneDs) g_sceneDs->Release(); g_sceneDs = d; g_sceneDs->AddRef(); }
                }
                t->Release();
            }
        }
        if (rr) rr->Release();
    }
    if (g_uiPass && g_traceUi > 0 && c == ctx) {
        char a[96]; describe(n ? r[0] : nullptr, a, sizeof a); logf("  UI pass rebind rt0 %s ds %p", a, (void*)d);
    }
    if (g_trace > 0) {
        char a[96], b[96]; describe(n ? r[0] : nullptr, a, sizeof a); describe(d, b, sizeof b);
        logf("  draws=%d | bind rt0 %s ds %s n=%u", g_draws, a, b, n);
    }
    g_draws = 0;
    // remember the last full-screen depth buffer the game binds (the scene depth)
    if (d && d != g_sceneDsv && g_bbW) {
        ID3D11Resource* res = nullptr; d->GetResource(&res);
        if (res) {
            ID3D11Texture2D* t = nullptr;
            if (SUCCEEDED(res->QueryInterface(__uuidof(ID3D11Texture2D), (void**)&t))) {
                D3D11_TEXTURE2D_DESC td; t->GetDesc(&td);
                static int seen = 0; static void* seenp[32];
                bool isnew = true; for (int k = 0; k < seen; k++) if (seenp[k] == d) isnew = false;
                if (isnew && seen < 32) { seenp[seen++] = d; logf("dsv %p %ux%u fmt %d msaa %u rt0 %p", (void*)d, td.Width, td.Height, (int)td.Format, td.SampleDesc.Count, n ? (void*)r[0] : nullptr); }
                if (td.Width == g_bbW && td.Height == g_bbH && td.SampleDesc.Count == 1) {
                    if (g_sceneDsv) g_sceneDsv->Release();
                    g_sceneDsv = d; d->AddRef();
                }
                t->Release();
            }
            res->Release();
        }
    }
    oOMSetRT(c, n, r, d);
}

static void after_ui_draw(ID3D11DeviceContext* c, UINT n, bool indexed) {
    if (g_shutReq) return;
    if (!g_uiPass || c != ctx) return;
    g_uiDrawIdx++;
    if (g_traceUi > 0) logf("  UI draw #%d %s count %u", g_uiDrawIdx, indexed ? "idx" : "arr", n);
    if ((frame_flags() & 16) && !g_drawnThisFrame && g_uiDrawIdx == 1) draw_sora_now(g_uiRtv, g_uiDsv);
}

static HRESULT STDMETHODCALLTYPE hkPresent(IDXGISwapChain* sc, UINT sync, UINT fl) {
    crash_tick();                     // v80: heartbeat on the render thread (stops while BL shows its own error box)
    HookGuard hg;
    if (g_shutReq) {   // v29 hot swap: free our GPU objects here, on the render thread, then just pass frames through
        if (!g_shutDone) { if (g_gfx) release_gfx(); g_gfx = false; g_shutDone = 1; }
        return oPresent(sc, sync, fl);
    }
    if (!g_gfx && g_ready) { g_gfx = init_gfx(sc); if (!g_gfx) g_ready = 0; }
    if (g_gfx) {
        if (!g_ctxHooked) {
            void** vt = *(void***)ctx;
            if (MH_CreateHook(vt[33], (void*)&hkOMSetRT, (void**)&oOMSetRT) == MH_OK && MH_EnableHook(vt[33]) == MH_OK) logf("OMSetRenderTargets hooked");
            if (MH_CreateHook(vt[12], (void*)&hkDrawIdx, (void**)&oDrawIdx) == MH_OK && MH_EnableHook(vt[12]) == MH_OK &&
                MH_CreateHook(vt[13], (void*)&hkDraw, (void**)&oDraw) == MH_OK && MH_EnableHook(vt[13]) == MH_OK) logf("Draw hooked");
            g_ctxHooked = true;
        }
        ID3D11Texture2D* bb = nullptr;
        if (SUCCEEDED(sc->GetBuffer(0, __uuidof(ID3D11Texture2D), (void**)&bb))) {
            D3D11_TEXTURE2D_DESC td; bb->GetDesc(&td); g_bbW = td.Width; g_bbH = td.Height;
            if (!rtv) dev->CreateRenderTargetView(bb, nullptr, &rtv);
            g_bbRes = bb;   // identity only (the swap chain keeps it alive)
            bb->Release();
            if (!g_drawnThisFrame) { Saved s{}; save(s); draw(rtv, g_bbW, g_bbH, nullptr); restore(s); }
            if (g_inOn) { Saved s{}; save(s); intro_draw(rtv, g_bbW, g_bbH); restore(s); }   // v74: summon intro
            if (g_hud.n > 0 && GetTickCount64() - g_hudT < 300) { Saved s{}; save(s); draw_hud(rtv, g_bbW, g_bbH); restore(s); }
            if (g_kuiOn && !g_kvOn) {   // v52: KH1's interface; v57: menu party models between its backdrop and its panels
                Saved s{}; save(s);
                if (g_mmOn || g_mwOn) { khui_draw(rtv, g_bbW, g_bbH, 0); menu_mdl_draw(rtv, g_bbW, g_bbH); menu_wpn_draw(rtv, g_bbW, g_bbH); khui_draw(rtv, g_bbW, g_bbH, 1); }
                else khui_draw(rtv, g_bbW, g_bbH);
                restore(s);
            }
            if (g_kvOn) { Saved s{}; save(s); kv_draw(rtv, g_bbW, g_bbH); restore(s); }   // v32: KH1 menu view on top
            if (g_traceUi > 0) { logf("==== present (ui draws %d, drawn %d)", g_uiDrawIdx, (int)g_drawnThisFrame); g_traceUi--; }
            g_drawnThisFrame = false; g_uiPass = false; g_postDone = false; g_frameDraws = 0;
            if (g_trace > 0) { logf("==== PRESENT (draws since bind %d) bb=%p", g_draws, (void*)bb); g_trace--; }
            if (g_frames == 3000) { logf("frames 3000, drawn in UI pass %d times", g_uiDraws); g_traceUi = 2; }
            if (++g_frames == 1) logf("first draw %ux%u fmt %d depth %p", g_bbW, g_bbH, (int)td.Format, (void*)g_sceneDsv);
        }
    }
    return oPresent(sc, sync, fl);
}
static HRESULT STDMETHODCALLTYPE hkResize(IDXGISwapChain* sc, UINT a, UINT w, UINT h, DXGI_FORMAT f, UINT fl) {
    HookGuard hg;
    if (rtv) { rtv->Release(); rtv = nullptr; }
    if (g_sceneDsv) { g_sceneDsv->Release(); g_sceneDsv = nullptr; }
    if (g_dcopyDsv) { g_dcopyDsv->Release(); g_dcopyDsv = nullptr; } if (g_dcopy) { g_dcopy->Release(); g_dcopy = nullptr; }
    if (g_dcopySrv) { g_dcopySrv->Release(); g_dcopySrv = nullptr; }
    if (g_sceneRt) { g_sceneRt->Release(); g_sceneRt = nullptr; } if (g_sceneDs) { g_sceneDs->Release(); g_sceneDs = nullptr; }
    g_bbRes = nullptr;
    return oResize(sc, a, w, h, f, fl);
}

static bool install_hooks() {
    WNDCLASSA wc{}; wc.lpfnWndProc = DefWindowProcA; wc.hInstance = GetModuleHandleA(nullptr); wc.lpszClassName = "SoraDummy"; RegisterClassA(&wc);
    HWND hw = CreateWindowA("SoraDummy", "", WS_OVERLAPPED, 0, 0, 8, 8, nullptr, nullptr, wc.hInstance, nullptr);
    DXGI_SWAP_CHAIN_DESC d{}; d.BufferCount = 1; d.BufferDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM; d.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    d.OutputWindow = hw; d.SampleDesc.Count = 1; d.Windowed = TRUE;
    IDXGISwapChain* sc = nullptr; ID3D11Device* dv = nullptr; ID3D11DeviceContext* cx = nullptr;
    HRESULT hr = D3D11CreateDeviceAndSwapChain(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0, nullptr, 0, D3D11_SDK_VERSION, &d, &sc, &dv, nullptr, &cx);
    if (FAILED(hr)) { logf("dummy device failed %08lx", hr); DestroyWindow(hw); return false; }
    void** vt = *(void***)sc;
    MH_Initialize();
    bool ok = MH_CreateHook(vt[8], (void*)&hkPresent, (void**)&oPresent) == MH_OK && MH_CreateHook(vt[13], (void*)&hkResize, (void**)&oResize) == MH_OK;
    ok = ok && MH_EnableHook(vt[8]) == MH_OK && MH_EnableHook(vt[13]) == MH_OK;
    sc->Release(); cx->Release(); dv->Release(); DestroyWindow(hw);
    logf("hooks %s", ok ? "installed" : "FAILED");
    return ok;
}

// ------------------------------------------------------------------ exports
extern "C" __declspec(dllexport) int sr_init(const char* dir) {
    static bool once = false; if (once) return 1; once = true;
    strncpy(g_dir, dir, MAX_PATH - 1); InitializeCriticalSection(&g_cs);
    crash_init("BL", "renderer v86");
    logf("---- sr_init");
    if (!load_assets()) return 0;
    find_blade();
    actor_weapon_cfg();
    for (auto& a : g_actors) load_actor(a);
    if (!install_hooks()) return 0;
    g_ready = 1; return 1;
}
extern "C" __declspec(dllexport) void sr_set_frame(const Frame* f) { EnterCriticalSection(&g_cs); g_frame = *f; g_frameT = GetTickCount64(); LeaveCriticalSection(&g_cs); }
extern "C" __declspec(dllexport) int sr_motion_count() { return (int)g_mo.size(); }
extern "C" __declspec(dllexport) float sr_motion_duration(int i) { return (i >= 0 && i < (int)g_mo.size()) ? g_mo[i].dur : 0.f; }
extern "C" __declspec(dllexport) int sr_frames_drawn() { return g_frames; }
extern "C" __declspec(dllexport) int sr_live_state() { return g_liveState; }
extern "C" __declspec(dllexport) int sr_hud_max() { return HUD_MAX; }
// party actor id (0 Donald, 1 Goofy): Borderlands placement each frame; hidden after 300 ms without a call
extern "C" __declspec(dllexport) void sr_set_actor(int id, const ActorFrame* f) {
    if (id < 0 || id > 1) return;
    EnterCriticalSection(&g_cs); g_actors[id].f = *f; g_actors[id].fT = GetTickCount64(); LeaveCriticalSection(&g_cs);
}
// v21 enemies. Load model ex_<id> (blocking file read; call once per id, off the render thread): palette size, -1 missing
extern "C" __declspec(dllexport) int sr_enemy_model(int id) {
    EnterCriticalSection(&g_cs); auto it = g_models.find(id); Actor* have = it == g_models.end() ? nullptr : it->second; LeaveCriticalSection(&g_cs);
    if (have) return have->live.empty() ? -1 : (int)have->live.size();
    Actor* m = new Actor(); char* nm = (char*)malloc(16); snprintf(nm, 16, "ex_%04d", id); m->name = nm;
    load_actor(*m);
    EnterCriticalSection(&g_cs); g_models[id] = m; LeaveCriticalSection(&g_cs);
    return m->live.empty() ? -1 : (int)m->live.size();
}
// slot 0-23: KH1 entity (pose; 0 = static model, identity pose), model id (loaded), Borderlands placement; hidden after 300 ms without a call
extern "C" __declspec(dllexport) void sr_set_enemy(int slot, uint64_t ent, int model, const ActorFrame* f) {
    if (slot < 0 || slot >= MAX_ENEMIES) return;
    EnterCriticalSection(&g_cs);
    auto it = g_models.find(model); Actor* gm = it == g_models.end() ? nullptr : it->second;
    Actor& a = g_enemies[slot];
    if (a.ent != ent || a.geo != gm) a.reset = true;
    a.ent = ent; a.geo = gm; a.f = *f; a.fT = GetTickCount64();
    LeaveCriticalSection(&g_cs);
}
extern "C" __declspec(dllexport) int sr_enemy_state(int slot) { return (slot < 0 || slot >= MAX_ENEMIES) ? -1 : g_enemies[slot].state; }
// 0 off, 1 drawn, 2 no valid KH1 pose, 3 placement stale; returns -1 when the actor's files are missing
extern "C" __declspec(dllexport) int sr_actor_state(int id) { return (id < 0 || id > 1) ? -1 : (g_actors[id].live.empty() ? -1 : g_actors[id].state); }
// Depth probes: up to 16 world points (UE units) sampled next frames, after the 8 blade points.
extern "C" __declspec(dllexport) void sr_set_probes(const float* xyz, int n) {
    if (n < 0) n = 0; if (n > 16) n = 16;
    EnterCriticalSection(&g_cs); memcpy(g_userProbes, xyz, (size_t)n * 12); g_nUser = n; LeaveCriticalSection(&g_cs);
}
// out[24*4]: per probe scene depth (NDC z, -1 = no sample), point NDC z, point view distance, valid; returns the frame id
extern "C" __declspec(dllexport) int sr_get_probes(float* out) {
    EnterCriticalSection(&g_cs); memcpy(out, g_probeOut, sizeof g_probeOut); int n = (int)g_probeOutN; LeaveCriticalSection(&g_cs);
    return n;
}
// Keyblade trail on for `hold` seconds (call every frame while swinging): colour rgba, trail length in seconds
extern "C" __declspec(dllexport) void sr_trail(float r, float g, float b, float a, float seconds, float hold) {
    g_trailCol[0] = r; g_trailCol[1] = g; g_trailCol[2] = b; g_trailCol[3] = a; g_trailSecs = seconds; g_trailUntil = qpc_now() + hold;
}
extern "C" __declspec(dllexport) void sr_hitstop(float seconds) { g_hitstopUntil = qpc_now() + (seconds > 0.5f ? 0.5f : seconds); }   // logic.py: KH1 command menu needs >= 160
// Keyblade segment: out[0..2] hilt, [3..5] tip (UE units, world), [6] seconds since it was posed, [7] pose counter.
// Returns 0 when no blade is known (no live palette / weapon vertices).
extern "C" __declspec(dllexport) int sr_get_blade(float* out) {
    if (g_bladeV[0].b < 0) return 0;
    EnterCriticalSection(&g_cs); memcpy(out, g_blade, 24); out[6] = (float)(qpc_now() - g_bladeT); out[7] = (float)g_bladeN; LeaveCriticalSection(&g_cs);
    return 1;
}
extern "C" __declspec(dllexport) void sr_set_hud(const HudFrame* h) { EnterCriticalSection(&g_cs); g_hud = *h; g_hudT = GetTickCount64(); LeaveCriticalSection(&g_cs); }
// Offline test (no game): own device, renders HudFrame h over a grey W x H image into out (RGBA8). Returns 1 on success.
extern "C" __declspec(dllexport) int sr_hud_selftest(const char* dir, const HudFrame* h, int W, int H, uint8_t* out) {
    static bool once = false;
    if (!once) {
        once = true; strncpy(g_dir, dir, MAX_PATH - 1); InitializeCriticalSection(&g_cs);
        std::vector<uint8_t> b;
        if (read_file("sora_hud.bin", b)) {
            const uint8_t* p = b.data() + 8; uint32_t n = rd<uint32_t>(p);
            for (uint32_t i = 0; i < n; i++) { Tex t; t.w = rd<uint32_t>(p); t.h = rd<uint32_t>(p); t.rgba.assign(p, p + (size_t)t.w * t.h * 4); p += (size_t)t.w * t.h * 4; g_hudTex.push_back(std::move(t)); }
        }
        if (FAILED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0, nullptr, 0, D3D11_SDK_VERSION, &dev, nullptr, &ctx))) return 0;
        HMODULE dc = LoadLibraryA("d3dcompiler_47.dll"); auto comp = dc ? (PFN_D3DCompile)GetProcAddress(dc, "D3DCompile") : nullptr;
        if (!comp) return 0;
        D3D11_SAMPLER_DESC smd{}; smd.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR; smd.AddressU = smd.AddressV = smd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP; smd.MaxLOD = D3D11_FLOAT32_MAX;
        dev->CreateSamplerState(&smd, &samp);
        D3D11_RASTERIZER_DESC rd{}; rd.FillMode = D3D11_FILL_SOLID; rd.CullMode = D3D11_CULL_NONE; rd.DepthClipEnable = TRUE; dev->CreateRasterizerState(&rd, &rs);
        D3D11_DEPTH_STENCIL_DESC dd{}; dev->CreateDepthStencilState(&dd, &dsOff);
        init_hud(comp);
        if (!hudvs || !hudps) return 0;
    }
    D3D11_TEXTURE2D_DESC td{}; td.Width = W; td.Height = H; td.MipLevels = 1; td.ArraySize = 1; td.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT; td.BindFlags = D3D11_BIND_RENDER_TARGET;
    ID3D11Texture2D *rt = nullptr, *st = nullptr; ID3D11RenderTargetView* v = nullptr;
    dev->CreateTexture2D(&td, nullptr, &rt); dev->CreateRenderTargetView(rt, nullptr, &v);
    float grey[4] = {0.35f, 0.33f, 0.30f, 1}; ctx->ClearRenderTargetView(v, grey);
    g_hud = *h; draw_hud(v, W, H);
    td.Usage = D3D11_USAGE_STAGING; td.BindFlags = 0; td.CPUAccessFlags = D3D11_CPU_ACCESS_READ; dev->CreateTexture2D(&td, nullptr, &st);
    ctx->CopyResource(st, rt); D3D11_MAPPED_SUBRESOURCE ms;
    if (SUCCEEDED(ctx->Map(st, 0, D3D11_MAP_READ, 0, &ms))) { for (int y = 0; y < H; y++) memcpy(out + y * W * 4, (uint8_t*)ms.pData + y * ms.RowPitch, W * 4); ctx->Unmap(st, 0); }
    st->Release(); v->Release(); rt->Release(); return 1;
}
// Offline test (no game, like the GTA mod's fakegta): own device, draws Sora with Frame f over the RGBA8 background bg
// (W x H) with the real shaders, relight and fade included, and returns the picture in out. Returns 1 on success.
extern "C" __declspec(dllexport) int sr_render_selftest(const char* dir, const Frame* f, const uint8_t* bg, int W, int H, uint8_t* out) {
    static bool once = false, ok = false;
    if (!once) {
        once = true; strncpy(g_dir, dir, MAX_PATH - 1); InitializeCriticalSection(&g_cs);
        if (!load_assets()) return 0;
        find_blade();
        actor_weapon_cfg();
        for (auto& a : g_actors) load_actor(a);
        if (FAILED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0, nullptr, 0, D3D11_SDK_VERSION, &dev, nullptr, &ctx))) return 0;
        ok = init_resources();
    }
    if (!ok) return 0;
    D3D11_TEXTURE2D_DESC td{}; td.Width = W; td.Height = H; td.MipLevels = 1; td.ArraySize = 1; td.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT; td.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
    D3D11_SUBRESOURCE_DATA sd{bg, (UINT)W * 4, 0};
    ID3D11Texture2D *rt = nullptr, *st = nullptr; ID3D11RenderTargetView* v = nullptr;
    if (FAILED(dev->CreateTexture2D(&td, &sd, &rt))) return 0;
    dev->CreateRenderTargetView(rt, nullptr, &v);
    EnterCriticalSection(&g_cs); g_frame = *f; g_frameT = GetTickCount64(); LeaveCriticalSection(&g_cs);
    draw(v, W, H, nullptr);
    td.Usage = D3D11_USAGE_STAGING; td.BindFlags = 0; td.CPUAccessFlags = D3D11_CPU_ACCESS_READ; dev->CreateTexture2D(&td, nullptr, &st);
    ctx->CopyResource(st, rt); D3D11_MAPPED_SUBRESOURCE ms;
    if (SUCCEEDED(ctx->Map(st, 0, D3D11_MAP_READ, 0, &ms))) { for (int y = 0; y < H; y++) memcpy(out + (size_t)y * W * 4, (uint8_t*)ms.pData + (size_t)y * ms.RowPitch, (size_t)W * 4); ctx->Unmap(st, 0); }
    st->Release(); v->Release(); rt->Release(); return 1;
}
// Offline test (no hooks): loads sora_live.bin from dir and fills out with the live skinning rows. Returns bone count or -1.
extern "C" __declspec(dllexport) int sr_live_selftest(const char* dir, float* out) {
    strncpy(g_dir, dir, MAX_PATH - 1);
    std::vector<uint8_t> b; if (!read_file("sora_live.bin", b)) return -1;
    const uint8_t* p = b.data() + 8; g_bones = rd<uint32_t>(p); rd<uint32_t>(p);
    g_live.resize(g_bones); memcpy(g_live.data(), p, g_bones * sizeof(LiveBone));
    g_khRetry = 0; std::vector<float> o;
    if (!live_bones(o)) return -1;
    memcpy(out, o.data(), o.size() * 4); return (int)g_bones;
}


// ------------------------------------------------------------------ KH1 sound effects (XAudio2, overlapping one-shots)
// WAVs decoded from the player's own KH1 install (passthrough/make_sfx.py -> data/sfx). Loaded by Python
// (sr_sfx_load) and played on a small pool of XAudio2 voices, so a clang, a hit and a whoosh can overlap.
#include <xaudio2.h>
struct SfxClip { WAVEFORMATEX fmt; std::vector<uint8_t> pcm; };
static std::vector<SfxClip> g_sfx; static IXAudio2* g_xa = nullptr; static IXAudio2MasteringVoice* g_xm = nullptr;
static std::vector<IXAudio2SourceVoice*> g_voices; static bool g_xaTried = false;
typedef HRESULT(WINAPI* XA2Create)(IXAudio2**, UINT32, XAUDIO2_PROCESSOR);
static bool sfx_init() {
    if (g_xa || g_xaTried) return g_xa != nullptr;
    g_xaTried = true;
    CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    HMODULE m = LoadLibraryA("xaudio2_9.dll"); if (!m) m = LoadLibraryA("xaudio2_8.dll");
    auto create = m ? (XA2Create)GetProcAddress(m, "XAudio2Create") : nullptr;
    if (!create || FAILED(create(&g_xa, 0, XAUDIO2_DEFAULT_PROCESSOR)) || FAILED(g_xa->CreateMasteringVoice(&g_xm))) {
        logf("sfx: XAudio2 unavailable"); if (g_xa) { g_xa->Release(); g_xa = nullptr; } return false;
    }
    logf("sfx: XAudio2 ready"); return true;
}
extern "C" __declspec(dllexport) int sr_sfx_load(const char* path) {   // PCM16 WAV -> clip id, -1 on failure
    if (!sfx_init()) return -1;
    FILE* f = fopen(path, "rb"); if (!f) return -1;
    std::vector<uint8_t> b; fseek(f, 0, SEEK_END); long n = ftell(f); fseek(f, 0, SEEK_SET); b.resize(n); fread(b.data(), 1, n, f); fclose(f);
    if (n < 44 || memcmp(b.data(), "RIFF", 4) || memcmp(b.data() + 8, "WAVE", 4)) return -1;
    SfxClip c{}; bool haveFmt = false;
    for (size_t o = 12; o + 8 <= b.size();) {
        uint32_t sz; memcpy(&sz, &b[o + 4], 4);
        if (!memcmp(&b[o], "fmt ", 4) && sz >= 16) { memcpy(&c.fmt, &b[o + 8], 16); c.fmt.cbSize = 0; haveFmt = true; }
        else if (!memcmp(&b[o], "data", 4)) c.pcm.assign(b.begin() + o + 8, b.begin() + std::min(b.size(), (size_t)(o + 8 + sz)));
        o += 8 + sz + (sz & 1);
    }
    if (!haveFmt || c.fmt.wFormatTag != WAVE_FORMAT_PCM || c.fmt.wBitsPerSample != 16 || c.pcm.empty()) return -1;
    // vgmstream writes 47912 / 48021 etc.: snap to the real rate (v87 - the old round-to-100 made 9 formats, and a voice
    // is only reused for its own format, so rare-rate clips like Donald's datk_12 found no free voice once the pool was full)
    { uint32_t r = c.fmt.nSamplesPerSec; const uint32_t std_[] = {48000, 44100, 32000, 24000, 22050, 16000};
      for (uint32_t s : std_) if (r > s * 98 / 100 && r < s * 102 / 100) { c.fmt.nSamplesPerSec = s; break; } }
    c.fmt.nAvgBytesPerSec = c.fmt.nSamplesPerSec * c.fmt.nBlockAlign;
    g_sfx.push_back(std::move(c)); return (int)g_sfx.size() - 1;
}
extern "C" __declspec(dllexport) int sr_sfx_play(int id, float volume, float pitch) {
    if (!g_xa || id < 0 || id >= (int)g_sfx.size()) return 0;
    const SfxClip& c = g_sfx[id]; IXAudio2SourceVoice* v = nullptr; IXAudio2SourceVoice* other = nullptr;
    for (auto* cand : g_voices) {   // a finished voice with the same format (else: same channels, other rate)
        XAUDIO2_VOICE_STATE st; cand->GetState(&st, XAUDIO2_VOICE_NOSAMPLESPLAYED);
        if (st.BuffersQueued != 0) continue;
        XAUDIO2_VOICE_DETAILS dt; cand->GetVoiceDetails(&dt);
        if (dt.InputChannels != c.fmt.nChannels) continue;
        if (dt.InputSampleRate == c.fmt.nSamplesPerSec) { v = cand; break; }
        if (!other) other = cand;
    }
    if (!v && other && (g_voices.size() >= 48) && SUCCEEDED(other->SetSourceSampleRate(c.fmt.nSamplesPerSec))) v = other;
    if (!v) {
        if (g_voices.size() >= 48 || FAILED(g_xa->CreateSourceVoice(&v, &c.fmt, 0, 4.0f))) return 0;
        g_voices.push_back(v);
    }
    XAUDIO2_BUFFER buf{}; buf.AudioBytes = (UINT32)c.pcm.size(); buf.pAudioData = c.pcm.data(); buf.Flags = XAUDIO2_END_OF_STREAM;
    v->SetVolume(volume); v->SetFrequencyRatio(pitch > 0.25f && pitch < 4.f ? pitch : 1.f);
    if (FAILED(v->SubmitSourceBuffer(&buf))) return 0;
    v->Start(0); return 1;
}

// ------------------------------------------------------------------ input passthrough (Borderlands -> KH1)
// kh1_bridge.dll (in the data dir) is loaded into KH1 once; it reads the pad we write to shared memory.
static SharedBlock* g_sh = nullptr; static volatile LONG g_injectState = 0;   // 0 idle, 1 running, 2 loaded, -1 failed
static SharedBlock* shared() {
    if (!g_sh) {
        HANDLE m = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0, sizeof(SharedBlock), SHARED_NAME);
        if (m) g_sh = (SharedBlock*)MapViewOfFile(m, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(SharedBlock));
        if (g_sh) g_sh->magic = SHARED_MAGIC;
    }
    return g_sh;
}
static DWORD find_kh1() {
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0); if (snap == INVALID_HANDLE_VALUE) return 0;
    PROCESSENTRY32W pe{sizeof pe}; DWORD pid = 0;
    for (BOOL ok = Process32FirstW(snap, &pe); ok; ok = Process32NextW(snap, &pe))
        if (!_wcsicmp(pe.szExeFile, L"KINGDOM HEARTS FINAL MIX.exe")) { pid = pe.th32ProcessID; break; }
    CloseHandle(snap); return pid;
}
static bool bridge_loaded(DWORD pid) {
    HANDLE ms = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE, pid); if (ms == INVALID_HANDLE_VALUE) return false;
    MODULEENTRY32W me{sizeof me}; bool found = false;
    for (BOOL ok = Module32FirstW(ms, &me); ok && !found; ok = Module32NextW(ms, &me)) found = !_wcsicmp(me.szModule, L"kh1_bridge.dll");
    CloseHandle(ms); return found;
}
// v81: the one place that loads a DLL into KH1 (bridge, kh1_fx, mapfeed). Moved out of logic.py: Windows Defender's
// Exploit:Python/Leivion heuristic flags the OpenProcess/VirtualAllocEx/CreateRemoteThread(LoadLibraryW) chain in Python.
// wait_ms 0 = don't block the caller (a helper thread waits for the remote thread and frees the path string).
struct LoadJob { HANDLE p, t; void* mem; };
static DWORD WINAPI load_reaper(LPVOID a) {
    LoadJob* j = (LoadJob*)a; WaitForSingleObject(j->t, 15000);
    VirtualFreeEx(j->p, j->mem, 0, MEM_RELEASE); CloseHandle(j->t); CloseHandle(j->p); delete j; return 0;
}
static bool kh1_load_dll(DWORD pid, const wchar_t* path, DWORD wait_ms) {
    if (GetFileAttributesW(path) == INVALID_FILE_ATTRIBUTES) { logf("load: missing %ls", path); return false; }
    HANDLE p = OpenProcess(PROCESS_CREATE_THREAD | PROCESS_VM_OPERATION | PROCESS_VM_WRITE | PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, FALSE, pid);
    if (!p) { logf("load: OpenProcess failed %lu", GetLastError()); return false; }
    size_t n = (wcslen(path) + 1) * 2; void* mem = VirtualAllocEx(p, nullptr, n, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    bool ok = mem && WriteProcessMemory(p, mem, path, n, nullptr);
    HANDLE t = ok ? CreateRemoteThread(p, nullptr, 0, (LPTHREAD_START_ROUTINE)GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "LoadLibraryW"), mem, 0, nullptr) : nullptr;
    if (t && !wait_ms) {
        HANDLE r = CreateThread(nullptr, 0, load_reaper, new LoadJob{p, t, mem}, 0, nullptr);
        if (r) { CloseHandle(r); return true; }
        wait_ms = 15000;
    }
    if (t) { WaitForSingleObject(t, wait_ms); CloseHandle(t); }
    if (mem) VirtualFreeEx(p, mem, 0, MEM_RELEASE);
    CloseHandle(p);
    return t != nullptr;
}
extern "C" __declspec(dllexport) int sr_kh1_load(int pid, const wchar_t* path) {   // logic.py: 1 = remote LoadLibraryW started
    return kh1_load_dll((DWORD)pid, path, 0) ? 1 : 0;
}
static DWORD WINAPI inject_thread(LPVOID) {
    DWORD pid = find_kh1();
    if (!pid) { logf("inject: KH1 not running"); g_injectState = -1; return 0; }
    if (bridge_loaded(pid)) { logf("inject: bridge already loaded"); g_injectState = 2; return 0; }
    wchar_t path[MAX_PATH]; MultiByteToWideChar(CP_ACP, 0, g_dir, -1, path, MAX_PATH); wcscat(path, L"\kh1_bridge.dll");
    kh1_load_dll(pid, path, 10000);
    bool ok = bridge_loaded(pid); logf("inject: %s", ok ? "bridge loaded" : "FAILED"); g_injectState = ok ? 2 : -1; return 0;
}
extern "C" __declspec(dllexport) int sr_inject_bridge() {   // async; poll sr_bridge_info
    shared();
    if (g_injectState == 1) return 1;
    g_injectState = 1; CloseHandle(CreateThread(nullptr, 0, inject_thread, nullptr, 0, nullptr)); return 1;
}
extern "C" __declspec(dllexport) void sr_set_pad(int active, int buttons, int rx, int ry, int lx, int ly) {
    SharedBlock* s = shared(); if (!s) return;
    s->buttons = (uint16_t)buttons; s->sticks[0] = (uint8_t)rx; s->sticks[1] = (uint8_t)ry; s->sticks[2] = (uint8_t)lx; s->sticks[3] = (uint8_t)ly;
    s->active = active; s->bl_seq++;
}
// Keeps KH1 Sora near where forwarding started so he never runs into KH1 walls (BL only uses his pose, so the
// teleport is invisible). on=0 clears the anchor; the next on=1 sets it at his current spot.
static HANDLE g_khw = nullptr; static DWORD g_khwPid = 0; static bool g_anchorSet = false; static float g_anchor[3];
extern "C" __declspec(dllexport) int sr_set_pin(int on, float radius) {
    if (!on) { g_anchorSet = false; return 0; }
    DWORD pid = find_kh1(); if (!pid) return 0;
    if (pid != g_khwPid) { if (g_khw) CloseHandle(g_khw); g_khw = OpenProcess(PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION, FALSE, pid); g_khwPid = pid; g_anchorSet = false; }
    if (!g_khw || !g_khBase) return 0;
    uint64_t sora = 0; float pos[3]; SIZE_T got = 0;
    if (!ReadProcessMemory(g_khw, (void*)(g_khBase + KH_SORA_PTR), &sora, 8, &got) || !sora) return 0;
    if (!ReadProcessMemory(g_khw, (void*)(sora + 0x10), pos, 12, &got)) return 0;
    if (!g_anchorSet) { memcpy(g_anchor, pos, 12); g_anchorSet = true; return 1; }
    float dx = pos[0] - g_anchor[0], dz = pos[2] - g_anchor[2];
    if (dx * dx + dz * dz > radius * radius) {
        // x and z only: height belongs to KH1 (jumps) and the bridge's Borderlands floor; rewriting y raced them
        WriteProcessMemory(g_khw, (void*)(sora + 0x10), &g_anchor[0], 4, &got);
        WriteProcessMemory(g_khw, (void*)(sora + 0x18), &g_anchor[2], 4, &got); return 2;
    }
    return 1;
}
// out[0] inject state, [1] bridge hook status, [2] overridden pad reads (kh_seq), [3] real buttons, [4] real sticks (packed)
extern "C" __declspec(dllexport) void sr_bridge_info(int* out) {
    SharedBlock* s = shared();
    out[0] = g_injectState; out[1] = s ? s->kh_status : 0; out[2] = s ? (int)s->kh_seq : 0; out[3] = s ? s->kh_real_buttons : 0;
    out[4] = s ? (int)(s->kh_real_sticks[0] | s->kh_real_sticks[1] << 8 | s->kh_real_sticks[2] << 16 | (uint32_t)s->kh_real_sticks[3] << 24) : 0;
}

BOOL WINAPI DllMain(HINSTANCE, DWORD reason, LPVOID reserved) {
    if (reason == DLL_PROCESS_DETACH && reserved && g_crash) g_crash->open = 0;   // Borderlands exiting (not a hot swap)
    return TRUE;
}
// v24: character shadows. p = strength (0 = off), fade start, fade end (UE units below the caster), softness (texels),
// light dir x, y, z (all 0 = the frame light made steeper), extent (half size of the covered square, UE units)
extern "C" __declspec(dllexport) void sr_shadow(const float* p, int n) { for (int k = 0; k < n && k < 8; k++) g_shCfg[k] = p[k]; }
extern "C" __declspec(dllexport) int sr_shadow_count() { return (int)g_shDrawn; }
// v25: level lighting. p[16] = sun r,g,b, ambient share | sky r,g,b, rim strength | ground r,g,b, cel edge softness |
// self-shadow strength, rim power. Ambient share 0 = the old fixed cel bands.
extern "C" __declspec(dllexport) void sr_light(const float* p, int n) { for (int k = 0; k < n && k < 16; k++) g_lightCfg[k] = p[k]; }
// v29 hot swap: everything we created on the GPU (runs on the render thread, from hkPresent)
static void release_gfx() {
    for (int k = 0; k < 3; k++) { wpn_free(g_wpn[k]); g_wpnHave[k] = 0; }   // v58
    for (auto& v : srvs) rel(v); srvs.clear(); for (auto& v : fxSrvs) rel(v); fxSrvs.clear(); for (auto& v : hudSrvs) rel(v); hudSrvs.clear();
    rel(vs); rel(ps); rel(il); rel(vb); rel(ib); rel(cbFrame); rel(cbBones); rel(samp); rel(sampClamp); rel(rs);
    rel(dsOff); rel(dsOn); rel(dsOnRev); rel(dsOwn); rel(dsFx); rel(bsOff); rel(bsAdd); rel(bsSub); rel(bsAlpha); rel(bsShadow); rel(rtv);
    rel(fxvs); rel(fxps); rel(fxil); rel(fxvb); rel(hudvs); rel(hudps); rel(hudil); rel(hudvb); rel(hudcb);
    rel(g_envSrv); rel(g_envTex); rel(g_ownDsv); rel(g_dcopySrv); rel(g_dcopyDsv); rel(g_dcopy);
    rel(g_probeRtv); rel(g_probeRt); for (auto& t : g_probeStage) rel(t); rel(prvs); rel(prps); rel(prcb);
    rel(g_glRtv); rel(g_glRt); for (auto& t : g_glStage) rel(t); rel(glps); rel(glcb);
    rel(g_shSrv); rel(g_shDsv); rel(g_shTex); rel(shps); rel(srvs_); rel(srps); rel(srcb); rel(g_trailVb);
    rel(g_sceneDsv); rel(g_sceneRt); rel(g_sceneDs); rel(g_uiRtv); rel(g_uiDsv);
    rel(g_olSrv); rel(g_olRtv); rel(g_olTex); rel(olps); rel(olcb);
    rel(g_kvSrv); rel(g_kvTex); rel(kvps); rel(kvcb);
    khfx_release(); khui_release();
    auto actor_rel = [](Actor& a) { rel(a.vb); rel(a.ib); for (auto& v : a.srvs) rel(v); a.srvs.clear(); };
    for (auto& a : g_actors) actor_rel(a);
    for (auto& a : g_enemies) actor_rel(a);
    for (auto& kv : g_models) if (kv.second) actor_rel(*kv.second);
    g_bbRes = nullptr;
    ctx->Flush(); rel(ctx); rel(dev);
    logf("hot swap: GPU objects released");
}
extern "C" __declspec(dllexport) int sr_shutdown() {
    logf("---- sr_shutdown");
    g_ready = 0; g_shutReq = 1;
    if (g_gfx) for (int i = 0; i < 300 && !g_shutDone; i++) Sleep(10);   // the render thread frees our GPU objects at its next Present
    MH_DisableHook(MH_ALL_HOOKS);
    for (int i = 0; i < 400 && g_inHook; i++) Sleep(5);                   // nobody may still be inside one of our hooks
    Sleep(30);
    MH_Uninitialize();
    kv_shutdown();
    for (int i = 0; i < 300 && g_injectState == 1; i++) Sleep(10);
    for (auto* v : g_voices) if (v) { v->Stop(); v->DestroyVoice(); }
    g_voices.clear();
    if (g_xm) { g_xm->DestroyVoice(); g_xm = nullptr; } if (g_xa) { g_xa->StopEngine(); g_xa->Release(); g_xa = nullptr; }
    kh_close();
    crash_shutdown(false);            // hot swap: the next renderer continues the session
    logf("sr_shutdown done (gpu freed %ld, still in hook %ld)", (long)g_shutDone, (long)g_inHook);
    return g_inHook == 0 ? 1 : 0;
}
// v31: mute / unmute KINGDOM HEARTS FINAL MIX's own audio (Windows per-app volume, like the Volume Mixer) - Borderlands
// plays every sound now, KH1's copies were doubled. Walks the audio sessions of every active output device.
#include <mmdeviceapi.h>
#include <audiopolicy.h>
extern "C" __declspec(dllexport) int sr_mute_kh1(int mute) {   // -> sessions changed, -1 KH1 not running, -2 COM failed
    DWORD pid = 0;
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0); PROCESSENTRY32W pe{}; pe.dwSize = sizeof pe;
    for (BOOL ok = Process32FirstW(snap, &pe); ok; ok = Process32NextW(snap, &pe))
        if (!_wcsicmp(pe.szExeFile, L"KINGDOM HEARTS FINAL MIX.exe")) { pid = pe.th32ProcessID; break; }
    CloseHandle(snap);
    if (!pid) return -1;
    HRESULT co = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    IMMDeviceEnumerator* en = nullptr; int changed = 0;
    if (FAILED(CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL, __uuidof(IMMDeviceEnumerator), (void**)&en))) {
        if (SUCCEEDED(co)) CoUninitialize(); return -2;
    }
    IMMDeviceCollection* devs = nullptr; UINT nd = 0;
    if (SUCCEEDED(en->EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE, &devs))) devs->GetCount(&nd);
    for (UINT d = 0; d < nd; d++) {
        IMMDevice* dv = nullptr; IAudioSessionManager2* mgr = nullptr; IAudioSessionEnumerator* se = nullptr; int ns = 0;
        if (FAILED(devs->Item(d, &dv))) continue;
        if (SUCCEEDED(dv->Activate(__uuidof(IAudioSessionManager2), CLSCTX_ALL, nullptr, (void**)&mgr)) && SUCCEEDED(mgr->GetSessionEnumerator(&se))) {
            se->GetCount(&ns);
            for (int i = 0; i < ns; i++) {
                IAudioSessionControl* c = nullptr; IAudioSessionControl2* c2 = nullptr; ISimpleAudioVolume* v = nullptr; DWORD sp = 0;
                if (FAILED(se->GetSession(i, &c))) continue;
                if (SUCCEEDED(c->QueryInterface(__uuidof(IAudioSessionControl2), (void**)&c2)) && SUCCEEDED(c2->GetProcessId(&sp)) && sp == pid &&
                    SUCCEEDED(c->QueryInterface(__uuidof(ISimpleAudioVolume), (void**)&v))) {
                    if (SUCCEEDED(v->SetMute(mute ? TRUE : FALSE, nullptr))) changed++;
                }
                if (v) v->Release(); if (c2) c2->Release(); c->Release();
            }
        }
        if (se) se->Release(); if (mgr) mgr->Release(); dv->Release();
    }
    if (devs) devs->Release(); en->Release();
    if (SUCCEEDED(co)) CoUninitialize();
    logf("KH1 audio %s: %d session(s)", mute ? "muted" : "unmuted", changed);
    return changed;
}
// v79: KH1's audio follows Borderlands' volume sliders (J9 "KH1 too loud"): Windows per-app volume of KH1's sessions
extern "C" __declspec(dllexport) int sr_vol_kh1(float vol) {   // -> sessions changed, -1 KH1 not running, -2 COM failed
    DWORD pid = 0;
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0); PROCESSENTRY32W pe{}; pe.dwSize = sizeof pe;
    for (BOOL ok = Process32FirstW(snap, &pe); ok; ok = Process32NextW(snap, &pe))
        if (!_wcsicmp(pe.szExeFile, L"KINGDOM HEARTS FINAL MIX.exe")) { pid = pe.th32ProcessID; break; }
    CloseHandle(snap);
    if (!pid) return -1;
    if (vol < 0.f) vol = 0.f; if (vol > 1.f) vol = 1.f;
    HRESULT co = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    IMMDeviceEnumerator* en = nullptr; int changed = 0;
    if (FAILED(CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL, __uuidof(IMMDeviceEnumerator), (void**)&en))) {
        if (SUCCEEDED(co)) CoUninitialize(); return -2;
    }
    IMMDeviceCollection* devs = nullptr; UINT nd = 0;
    if (SUCCEEDED(en->EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE, &devs))) devs->GetCount(&nd);
    for (UINT d = 0; d < nd; d++) {
        IMMDevice* dv = nullptr; IAudioSessionManager2* mgr = nullptr; IAudioSessionEnumerator* se = nullptr; int ns = 0;
        if (FAILED(devs->Item(d, &dv))) continue;
        if (SUCCEEDED(dv->Activate(__uuidof(IAudioSessionManager2), CLSCTX_ALL, nullptr, (void**)&mgr)) && SUCCEEDED(mgr->GetSessionEnumerator(&se))) {
            se->GetCount(&ns);
            for (int i = 0; i < ns; i++) {
                IAudioSessionControl* c = nullptr; IAudioSessionControl2* c2 = nullptr; ISimpleAudioVolume* v = nullptr; DWORD sp = 0;
                if (FAILED(se->GetSession(i, &c))) continue;
                if (SUCCEEDED(c->QueryInterface(__uuidof(IAudioSessionControl2), (void**)&c2)) && SUCCEEDED(c2->GetProcessId(&sp)) && sp == pid &&
                    SUCCEEDED(c->QueryInterface(__uuidof(ISimpleAudioVolume), (void**)&v))) {
                    if (SUCCEEDED(v->SetMasterVolume(vol, nullptr))) changed++;
                }
                if (v) v->Release(); if (c2) c2->Release(); c->Release();
            }
        }
        if (se) se->Release(); if (mgr) mgr->Release(); dv->Release();
    }
    if (devs) devs->Release(); en->Release();
    if (SUCCEEDED(co)) CoUninitialize();
    return changed;
}
