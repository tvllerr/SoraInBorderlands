// kh1_mapfeed.dll - BORDERLANDS GROUND AS KH1 COLLISION (map feed v1). Loaded into KINGDOM HEARTS FINAL MIX.exe
// (Steam 1.0.0.2) next to kh1_bridge.dll; hot-swappable (shared "unload" flag, like kh1_fx).
//
// KH1 world collision (Ghidra, MODLOG 2026-10-06 night "MAP FEED"):
//   FUN_140195180(desc)        = an entity's collision query (desc = entity + 0x2A0)
//   FUN_140194720(box, mode)   = gathers room polygons near box (floats min xyz, max xyz) into the int list
//                                0x234E7D0, count 0x23527D8 and *(short*)(box + 0x18); per polygon it calls
//                                table 0x4E3360[mode](idx, &poly->flags) (box clip + layer filter, then append)
//   polygons  [0x234E620]: 0x14 B {s16 v[4] (v[3] < 0 = triangle), s16 normal, u8 attr, u8, f32 d, u32 flags}
//   vertices  [0x234E648]: f32 xyz;  normals [0x234E650]: s16 xyz (1/8192) + pad;  d = -dot(n, v)
//   winding: n = -normalize(cross(v1 - v0, v2 - v0)); KH1 y grows DOWN -> walkable floors have ny < 0
// For the entities Borderlands lists (party, stand-ins, summons ...), every gather inside their query is replaced
// by polygons built from Borderlands' ground grid (shared memory "Local\SoraMapFeed"); the global polygon / vertex /
// normal pointers point at our arrays for the rest of that query and are restored afterwards. KH1's own callback
// does the clip + filter, so its vanilla rules decide what our polygons are (floor, wall, slope).
// Hooks chain on top of kh1_bridge v3 (same two functions; MinHook relocates its jmp).
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cmath>
#include "MinHook.h"
#include "mapfeed_shared.h"

static const uint32_t VERSION = 3;   // v3: flat fallback at Sora's floor where no ground is known
// v2:   // v2: modes 0..10 (8 = the FLOOR search, 4 = walls; v1 skipped > 7)
static char g_dir[MAX_PATH];
static HMODULE g_self = nullptr;
static void logf(const char* fmt, ...) {
    char path[MAX_PATH]; snprintf(path, sizeof path, "%s\\kh1_mapfeed.log", g_dir);
    FILE* f = fopen(path, "a"); if (!f) return;
    va_list a; va_start(a, fmt); vfprintf(f, fmt, a); va_end(a); fputc('\n', f); fclose(f);
}

static const uintptr_t RVA_COLQUERY = 0x195180, RVA_GATHER = 0x194720;
static const uintptr_t RVA_POLY_PTR = 0x234E620, RVA_VTX_PTR = 0x234E648, RVA_NRM_PTR = 0x234E650;
static const uintptr_t RVA_POLY_COUNT = 0x234E660, RVA_LIST_COUNT = 0x23527D8, RVA_CB_CLIP = 0x4E3360;
static const uint8_t COLQUERY_BYTES[] = {0x40, 0x55, 0x53, 0x57, 0x41, 0x54, 0x48, 0x8d, 0xac, 0x24, 0xc8, 0xf6, 0xff, 0xff};
static const uint8_t GATHER_BYTES[] = {0x48, 0x89, 0x4c, 0x24, 0x08, 0x53, 0x55, 0x41, 0x54, 0x41, 0x55, 0x41, 0x56};
typedef unsigned long long(__fastcall* ColQuery)(float* desc);
typedef void(__fastcall* Gather)(float* box, int mode);
typedef void(__fastcall* GatherCb)(int idx, uint32_t* flags);
static ColQuery oColQuery = nullptr; static Gather oGather = nullptr;
static uintptr_t g_base = 0;
static FeedShared* g_sh = nullptr;
static volatile LONG g_inflight = 0;

#pragma pack(push, 1)
struct Poly { int16_t v[4]; int16_t ni; uint8_t attr, b11; float d; uint32_t flags; };
#pragma pack(pop)
static_assert(sizeof(Poly) == 0x14, "poly");
static const int MAX_POLY = 2048;
static Poly g_poly[MAX_POLY];
static float g_vtx[MAX_POLY * 3][3];
static int16_t g_nrm[MAX_POLY][4];

// snapshot of the shared grid (seqlock copy; KH1 only reads its own copy)
static FeedShared g_snap; static uint32_t g_snapSeq = 1; static bool g_snapOk = false;
static bool refresh_snapshot() {
    uint32_t s = g_sh->seq;
    if (s & 1) return g_snapOk;
    if (s == g_snapSeq) return g_snapOk;
    memcpy(&g_snap, (const void*)g_sh, sizeof(FeedShared));
    if (g_sh->seq != s) return g_snapOk;   // written meanwhile: keep the old copy this time
    g_snapSeq = s;
    float det = g_snap.m00 * g_snap.m11 - g_snap.m01 * g_snap.m10;
    g_snapOk = g_snap.n >= 2 && g_snap.n <= MF_MAX_N && g_snap.cell > 1.f && fabsf(det) > 1e-6f && g_snap.sc > 0.01f;
    return g_snapOk;
}

// current fed query (KH1 runs entity collision on one thread)
static uintptr_t g_fedEnt = 0; static DWORD g_fedThread = 0; static bool g_swapped = false; static bool g_usedOurs = false;
static uintptr_t g_savePoly, g_saveVtx, g_saveNrm;
static void swap_in() {
    if (g_swapped) return;
    g_savePoly = *(uintptr_t*)(g_base + RVA_POLY_PTR); g_saveVtx = *(uintptr_t*)(g_base + RVA_VTX_PTR);
    g_saveNrm = *(uintptr_t*)(g_base + RVA_NRM_PTR);
    *(uintptr_t*)(g_base + RVA_POLY_PTR) = (uintptr_t)g_poly; *(uintptr_t*)(g_base + RVA_VTX_PTR) = (uintptr_t)g_vtx;
    *(uintptr_t*)(g_base + RVA_NRM_PTR) = (uintptr_t)g_nrm;
    g_swapped = true;
}
static void swap_out() {
    if (!g_swapped) return;
    *(uintptr_t*)(g_base + RVA_POLY_PTR) = g_savePoly; *(uintptr_t*)(g_base + RVA_VTX_PTR) = g_saveVtx;
    *(uintptr_t*)(g_base + RVA_NRM_PTR) = g_saveNrm;
    g_swapped = false;
}

// flags our polygons copy from the current room (floor / wall layers), re-learned when the room changes
static uintptr_t g_roomTab = 0; static int g_roomCount = 0;
static uint32_t g_floorFlags = 0x100800, g_wallFlags = 0x10110; static int g_floorPoly = -1;
static void learn_room() {
    uint8_t* tri = *(uint8_t**)(g_base + RVA_POLY_PTR);
    int16_t* nrm = *(int16_t**)(g_base + RVA_NRM_PTR);
    int count = *(int*)(g_base + RVA_POLY_COUNT);
    if (!tri || !nrm || count <= 0 || count > 65535) return;
    if ((uintptr_t)tri == g_roomTab && count == g_roomCount) return;
    g_roomTab = (uintptr_t)tri; g_roomCount = count; g_floorPoly = -1;
    uint32_t fk[32] = {}, fn[32] = {}, wk[32] = {}, wn[32] = {}; int nf = 0, nw = 0;
    for (int i = 0; i < count; i++) {
        uint8_t* p = tri + (size_t)i * 0x14;
        uint32_t fl = *(uint32_t*)(p + 0x10);
        if ((fl & 1) || p[10]) continue;                        // skipped / attribute (water, damage ...)
        int ni = *(int16_t*)(p + 8);
        float nx = nrm[ni * 4], ny = nrm[ni * 4 + 1], nz = nrm[ni * 4 + 2], l = sqrtf(nx * nx + ny * ny + nz * nz);
        if (l <= 0) continue;
        ny /= l;
        uint32_t* k = nullptr; uint32_t* c = nullptr; int* n = nullptr;
        if (ny < -0.95f && !((fl >> 4) & 0xF)) { k = fk; c = fn; n = &nf; if (g_floorPoly < 0) g_floorPoly = i; }
        else if (fabsf(ny) < 0.3f) { k = wk; c = wn; n = &nw; }
        if (!k) continue;
        int j = 0; while (j < *n && k[j] != fl) j++;
        if (j == *n) { if (*n >= 32) continue; k[j] = fl; (*n)++; }
        c[j]++;
    }
    int bf = -1, bw = -1;
    for (int j = 0; j < nf; j++) if (bf < 0 || fn[j] > fn[bf]) bf = j;
    for (int j = 0; j < nw; j++) if (bw < 0 || wn[j] > wn[bw]) bw = j;
    if (bf >= 0) g_floorFlags = fk[bf];
    if (bw >= 0) g_wallFlags = wk[bw];
    g_sh->floor_poly = g_floorPoly;
    logf("room %p: %d polys, floor flags %08x (poly %d), wall flags %08x", tri, count, g_floorFlags, g_floorPoly, g_wallFlags);
}

static inline void to_kh(const FeedShared& s, float bx, float by, float bz, float* o) {
    float det = s.m00 * s.m11 - s.m01 * s.m10, dx = bx - s.Fx, dy = by - s.Fy;
    o[0] = s.Kx + (s.m11 * dx - s.m01 * dy) / det;
    o[2] = s.Kz + (-s.m10 * dx + s.m00 * dy) / det;
    o[1] = s.ref_ky - (bz - s.ref_bz) / s.sc;
}

static int g_npoly = 0, g_nvtx = 0;
static void add_tri(const float* a, const float* b, const float* c) {
    if (g_npoly >= MAX_POLY || g_nvtx + 3 > MAX_POLY * 3) return;
    float e1[3] = {b[0] - a[0], b[1] - a[1], b[2] - a[2]}, e2[3] = {c[0] - a[0], c[1] - a[1], c[2] - a[2]};
    float cx = e1[1] * e2[2] - e1[2] * e2[1], cy = e1[2] * e2[0] - e1[0] * e2[2], cz = e1[0] * e2[1] - e1[1] * e2[0];
    float l = sqrtf(cx * cx + cy * cy + cz * cz);
    if (l < 1e-4f) return;
    float nx = -cx / l, ny = -cy / l, nz = -cz / l;
    const float* v1 = b; const float* v2 = c;
    if (ny > 0) { nx = -nx; ny = -ny; nz = -nz; v1 = c; v2 = b; }   // face up (KH1 y down): swap the winding
    int vi = g_nvtx;
    memcpy(g_vtx[vi], a, 12); memcpy(g_vtx[vi + 1], v1, 12); memcpy(g_vtx[vi + 2], v2, 12);
    g_nvtx += 3;
    Poly& p = g_poly[g_npoly];
    int16_t qx = (int16_t)lrintf(nx * 8192.f), qy = (int16_t)lrintf(ny * 8192.f), qz = (int16_t)lrintf(nz * 8192.f);
    g_nrm[g_npoly][0] = qx; g_nrm[g_npoly][1] = qy; g_nrm[g_npoly][2] = qz; g_nrm[g_npoly][3] = 0;
    p.v[0] = (int16_t)vi; p.v[1] = (int16_t)(vi + 1); p.v[2] = (int16_t)(vi + 2); p.v[3] = -1;
    p.ni = (int16_t)g_npoly; p.attr = 0; p.b11 = 0;
    p.d = -(qx * a[0] + qy * a[1] + qz * a[2]) / 8192.f;
    p.flags = fabsf(ny) < 0.5f ? g_wallFlags : g_floorFlags;
    g_npoly++;
}

// polygons for the cells under a KH1 box (floats min xyz, max xyz); returns cells with known ground
static int build(const float* box) {
    const FeedShared& s = g_snap;
    g_npoly = 0; g_nvtx = 0;
    float xs[2] = {box[0], box[3]}, zs[2] = {box[2], box[5]};
    float bx0 = 1e30f, by0 = 1e30f, bx1 = -1e30f, by1 = -1e30f;
    for (float x : xs) for (float z : zs) {
        float ox = x - s.Kx, oz = z - s.Kz;
        float bx = s.Fx + s.m00 * ox + s.m01 * oz, by = s.Fy + s.m10 * ox + s.m11 * oz;
        bx0 = fminf(bx0, bx); bx1 = fmaxf(bx1, bx); by0 = fminf(by0, by); by1 = fmaxf(by1, by);
    }
    int i0 = (int)floorf(bx0 / s.cell) - s.gx0 - 1, i1 = (int)floorf(bx1 / s.cell) - s.gx0 + 1;
    int j0 = (int)floorf(by0 / s.cell) - s.gy0 - 1, j1 = (int)floorf(by1 / s.cell) - s.gy0 + 1;
    if (i0 < 0) i0 = 0; if (j0 < 0) j0 = 0; if (i1 > s.n - 2) i1 = s.n - 2; if (j1 > s.n - 2) j1 = s.n - 2;
    uint32_t cap = s.max_cells ? s.max_cells : 400;
    int cells = 0;
    for (int j = j0; j <= j1; j++) for (int i = i0; i <= i1; i++) {
        if ((uint32_t)cells >= cap) break;
        float z00 = s.h[j * s.n + i], z10 = s.h[j * s.n + i + 1], z01 = s.h[(j + 1) * s.n + i], z11 = s.h[(j + 1) * s.n + i + 1];
        if (std::isnan(z00) || std::isnan(z10) || std::isnan(z01) || std::isnan(z11)) continue;
        float x0 = (s.gx0 + i) * s.cell, x1 = x0 + s.cell, y0 = (s.gy0 + j) * s.cell, y1 = y0 + s.cell;
        float A[3], B[3], C[3], D[3];
        to_kh(s, x0, y0, z00, A); to_kh(s, x1, y0, z10, B); to_kh(s, x1, y1, z11, C); to_kh(s, x0, y1, z01, D);
        add_tri(A, B, C); add_tri(A, C, D);
        cells++;
    }
    return cells;
}

// no Borderlands ground known under the box (outside the grid / all holes): a flat floor at Sora's KH1 floor (= the
// bridge's old flat floor) instead of KH1's room, which lies 300 below (v1/v2 dropped entities there)
static int build_flat(const float* box) {
    g_npoly = 0; g_nvtx = 0;
    float y = g_snap.ref_ky, pad = 200.f;
    float A[3] = {box[0] - pad, y, box[2] - pad}, B[3] = {box[3] + pad, y, box[2] - pad};
    float C[3] = {box[3] + pad, y, box[5] + pad}, D[3] = {box[0] - pad, y, box[5] + pad};
    add_tri(A, B, C); add_tri(A, C, D);
    return g_npoly ? 1 : 0;
}

static bool is_fed(uintptr_t ent) {
    uint32_t n = g_snap.nent; if (n > MF_MAX_ENT) n = MF_MAX_ENT;
    for (uint32_t i = 0; i < n; i++) if (g_snap.ent[i] == ent) return true;
    return false;
}

static unsigned long long __fastcall hkColQuery(float* desc) {
    if (!g_sh || !g_sh->enabled || g_sh->unload) return oColQuery(desc);
    uintptr_t ent = (uintptr_t)desc - 0x2A0;
    if (!refresh_snapshot() || !is_fed(ent) || g_fedEnt) return oColQuery(desc);
    InterlockedIncrement(&g_inflight);
    __try { learn_room(); } __except (EXCEPTION_EXECUTE_HANDLER) { g_sh->faults++; }
    g_fedEnt = ent; g_fedThread = GetCurrentThreadId(); g_usedOurs = false;
    unsigned long long r = oColQuery(desc);
    swap_out();
    g_fedEnt = 0;
    // the floor polygon index KH1 keeps would point into OUR array; outside the query KH1 reads the room's -> hand
    // it the room's plain floor polygon (its attributes = "normal ground")
    if (g_usedOurs && g_floorPoly >= 0 && *(int16_t*)&desc[0xf] >= 0) *(int16_t*)&desc[0xf] = (int16_t)g_floorPoly;
    g_sh->q_fed++;
    InterlockedDecrement(&g_inflight);
    return r;
}

static void __fastcall hkGather(float* box, int mode) {
    if (!g_fedEnt || GetCurrentThreadId() != g_fedThread || mode < 0 || mode > 10) {
        if (g_swapped && g_fedEnt && GetCurrentThreadId() == g_fedThread) {   // odd mode inside a fed query
            swap_out(); oGather(box, mode); swap_in(); return;
        }
        oGather(box, mode); return;
    }
    swap_out();                    // the real gather walks the ROOM's groups (and sets KH1's box globals)
    oGather(box, mode);
    int cells = 0;
    __try { cells = build(box); } __except (EXCEPTION_EXECUTE_HANDLER) { g_sh->faults++; g_npoly = 0; }
    if (!cells) {
        g_sh->q_empty++;
        __try { cells = build_flat(box); } __except (EXCEPTION_EXECUTE_HANDLER) { g_sh->faults++; g_npoly = 0; }
        if (!cells) return;                    // (only if even that failed: KH1's room this time)
    }
    swap_in();
    *(int*)(g_base + RVA_LIST_COUNT) = 0;
    GatherCb cb = *(GatherCb*)(g_base + RVA_CB_CLIP + 8 * mode);
    __try { for (int i = 0; i < g_npoly; i++) cb(i, &g_poly[i].flags); }   // KH1's own clip + layer filter
    __except (EXCEPTION_EXECUTE_HANDLER) { g_sh->faults++; *(int*)(g_base + RVA_LIST_COUNT) = 0; }
    int n = *(int*)(g_base + RVA_LIST_COUNT);
    *(int16_t*)((uint8_t*)box + 0x18) = (int16_t)n;
    g_usedOurs = true;
    g_sh->gathers++; g_sh->q_polys += n; g_sh->last_polys = n;
}

static bool hook_any(uintptr_t rva, const uint8_t* bytes, size_t nb, void* detour, void** orig, const char* name) {
    uint8_t* target = (uint8_t*)(g_base + rva);
    bool vanilla = memcmp(target, bytes, nb) == 0, jmp = target[0] == 0xE9 || (target[0] == 0xFF && target[1] == 0x25);
    if (!vanilla && !jmp) { logf("%s: unknown bytes %02x %02x %02x - skipped", name, target[0], target[1], target[2]); return false; }
    bool ok = MH_CreateHook(target, detour, orig) == MH_OK && MH_EnableHook(target) == MH_OK;
    logf("%s hook %s (%s)", name, ok ? "installed" : "FAILED", vanilla ? "vanilla" : "on top of an existing jmp");
    return ok;
}

static DWORD WINAPI main_thread(LPVOID) {
    g_base = (uintptr_t)GetModuleHandleW(nullptr);
    HANDLE m = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0, sizeof(FeedShared), MF_NAME);
    if (!m) { logf("CreateFileMapping failed %lu", GetLastError()); FreeLibraryAndExitThread(g_self, 0); }
    g_sh = (FeedShared*)MapViewOfFile(m, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(FeedShared));
    if (!g_sh) { logf("MapViewOfFile failed"); FreeLibraryAndExitThread(g_self, 0); }
    g_sh->magic = MF_MAGIC; g_sh->version = VERSION; g_sh->unload = 0;
    MH_Initialize();
    logf("---- kh1_mapfeed v%u: base %p", VERSION, (void*)g_base);
    bool a = hook_any(RVA_GATHER, GATHER_BYTES, sizeof GATHER_BYTES, (void*)&hkGather, (void**)&oGather, "tri_gather");
    bool b = a && hook_any(RVA_COLQUERY, COLQUERY_BYTES, sizeof COLQUERY_BYTES, (void*)&hkColQuery, (void**)&oColQuery, "col_query");
    g_sh->hooked = (a && b) ? 1 : 0;
    while (!g_sh->unload) Sleep(100);
    g_sh->enabled = 0;
    MH_DisableHook(MH_ALL_HOOKS);
    Sleep(50);
    for (int i = 0; i < 200 && g_inflight; i++) Sleep(10);
    MH_Uninitialize();
    g_sh->hooked = 0; g_sh->unload = 0;
    logf("unloaded");
    FreeLibraryAndExitThread(g_self, 0);
    return 0;
}

BOOL WINAPI DllMain(HINSTANCE h, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        g_self = h;
        DisableThreadLibraryCalls(h);
        GetModuleFileNameA(h, g_dir, MAX_PATH); char* s = strrchr(g_dir, '\\'); if (s) *s = 0;
        CloseHandle(CreateThread(nullptr, 0, main_thread, nullptr, 0, nullptr));
    }
    return TRUE;
}
