// kh1_bridge.dll — loaded into KINGDOM HEARTS FINAL MIX.exe (Steam 1.0.0.2) by sora_render.dll (Borderlands side).
// Hooks KH1's per-controller pad read (RVA 0x113D20: void read_pad(int idx, PadRec* rec)) and, while Borderlands is
// sending input, replaces controller 0 with the buttons/sticks Borderlands wrote to shared memory "Local\SoraPassthrough".
// KH1 ignores real input while unfocused; this hook runs after that check, so forwarded input works in the background.
// If Borderlands stops sending for 0.5 s, the override switches off and KH1's own controls work again.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include "MinHook.h"
#include "shared.h"

static char g_dir[MAX_PATH];
static void logf(const char* fmt, ...) {
    char path[MAX_PATH]; snprintf(path, sizeof path, "%s\\kh1_bridge.log", g_dir);
    FILE* f = fopen(path, "a"); if (!f) return;
    va_list a; va_start(a, fmt); vfprintf(f, fmt, a); va_end(a); fputc('\n', f); fclose(f);
}

static const uintptr_t RVA_READ_PAD = 0x113D20;
static const uint8_t READ_PAD_BYTES[] = {0x48, 0x89, 0x5c, 0x24, 0x08, 0x48, 0x89, 0x74, 0x24, 0x10, 0x57, 0x48, 0x83, 0xec, 0x20};
typedef void(__fastcall* ReadPad)(int idx, uint8_t* rec);
static ReadPad oReadPad = nullptr;
static SharedBlock* g_sh = nullptr;
static uint32_t g_lastSeq = 0; static ULONGLONG g_seqTime = 0; static bool g_overriding = false;
static uint16_t g_lastReal = 0;

static void __fastcall hkReadPad(int idx, uint8_t* rec) {
    oReadPad(idx, rec);
    if (idx != 0 || !g_sh || g_sh->magic != SHARED_MAGIC) return;
    ULONGLONG now = GetTickCount64();
    uint32_t seq = g_sh->bl_seq;
    if (seq != g_lastSeq) { g_lastSeq = seq; g_seqTime = now; }
    bool on = g_sh->active && now - g_seqTime < 500;
    if (on != g_overriding) { g_overriding = on; logf("override %s", on ? "ON" : "OFF"); }
    if (!on) {
        uint16_t real; memcpy(&real, rec, 2);   // learn the button layout from real presses
        if (real != g_lastReal) { logf("real pad: buttons %04x sticks %02x %02x %02x %02x", real, rec[2], rec[3], rec[4], rec[5]); g_lastReal = real; }
        g_sh->kh_real_buttons = real; memcpy((void*)g_sh->kh_real_sticks, rec + 2, 4);
        return;
    }
    uint16_t b = g_sh->buttons; memcpy(rec, &b, 2);
    rec[2] = g_sh->sticks[0]; rec[3] = g_sh->sticks[1]; rec[4] = g_sh->sticks[2]; rec[5] = g_sh->sticks[3];
    g_sh->kh_seq++;
}

// ------------------------------------------------------------------ KH1 world collision (Ghidra, see MODLOG)
// FUN_140195180(desc) = an actor's world-collision query (desc = entity + 0x2A0). Inside, FUN_140194720(box, n) gathers
// the room's collision triangles near the actor into the int list at 0x234E7D0 and writes the count to *(short*)(box+0x18).
// Triangle records: *(u8**)0x234E620 + idx*0x14 (+8 short = normal index); normals: *(i16**)0x234E650 + n*4 (x, y, z).
// For Sora's query only, and while Borderlands sets walls_off, the steep triangles (walls) are dropped from that list:
// he keeps KH1's floors (and ceilings) but no longer stops at KH1's walls - Borderlands supplies the walls instead.
static const uintptr_t RVA_COLQUERY = 0x195180, RVA_GATHER = 0x194720, RVA_SORA_PTR = 0x2537E48;
static const uintptr_t RVA_TRI_PTR = 0x234E620, RVA_NRM_PTR = 0x234E650, RVA_TRI_LIST = 0x234E7D0, RVA_TRI_COUNT = 0x23527D8;
static const uint8_t COLQUERY_BYTES[] = {0x40, 0x55, 0x53, 0x57, 0x41, 0x54, 0x48, 0x8d, 0xac, 0x24, 0xc8, 0xf6, 0xff, 0xff};
static const uint8_t GATHER_BYTES[] = {0x48, 0x89, 0x4c, 0x24, 0x08, 0x53, 0x55, 0x41, 0x54, 0x41, 0x55, 0x41, 0x56};
typedef unsigned long long(__fastcall* ColQuery)(float* desc);
typedef void(__fastcall* Gather)(float* box, int mode);
static ColQuery oColQuery = nullptr; static Gather oGather = nullptr;
static uintptr_t g_base = 0; static bool g_soraQuery = false; static bool g_dropAll = false;

// ---- v3: Borderlands ground. For Sora (and the proxy Heartless) in ground mode, the room's triangles are all dropped
// and the query result is rewritten as if KH1 had found a flat floor at floor_y (the Borderlands ground):
// desc (entity + 0x2A0, floats): [1] y, [9] height, [10] query flags (bit0 = air mode), [0xc] result flags
// (bit0 = floor too far below = airborne), [0xf] short floor polygon, [0x11] result y, [0x1a] floor height under him.
// Rules copied from FUN_140195180: ground mode snaps when the floor is within height * 0.5 below the result
// (DAT_14042f7ec = 0.5); air mode only lands when the result reaches the floor.
static const uintptr_t RVA_POLY_COUNT = 0x234E660;
static int g_floorPoly = -1; static uintptr_t g_floorPolyTab = 0; static int g_floorPolyCount = 0;
static uint32_t g_lastSnap = 0;
static int find_floor_poly() {   // a plain, flat room floor polygon (its attributes are what KH1 reads for "the floor")
    uint8_t* tri = *(uint8_t**)(g_base + RVA_TRI_PTR);
    int16_t* nrm = *(int16_t**)(g_base + RVA_NRM_PTR);
    int count = *(int*)(g_base + RVA_POLY_COUNT);
    if (!tri || !nrm || count <= 0 || count > 65535) return -1;
    if ((uintptr_t)tri == g_floorPolyTab && count == g_floorPolyCount) return g_floorPoly;
    g_floorPolyTab = (uintptr_t)tri; g_floorPolyCount = count; g_floorPoly = -1;
    for (int i = 0; i < count && g_floorPoly < 0; i++) {
        uint8_t* p = tri + (size_t)i * 0x14;
        uint32_t fl = *(uint32_t*)(p + 0x10);
        if ((fl & 1) || ((fl >> 4) & 0xF) || p[10]) continue;   // skipped / special type / attribute (water, damage...)
        int ni = *(int16_t*)(p + 8);
        float nx = nrm[ni * 4], ny = nrm[ni * 4 + 1], nz = nrm[ni * 4 + 2], l2 = nx * nx + ny * ny + nz * nz;
        if (l2 > 0 && ny * ny > 0.95f * l2) g_floorPoly = i;
    }
    logf("floor polygon for Borderlands ground: %d of %d", g_floorPoly, count);
    if (g_sh) g_sh->floor_poly = g_floorPoly;
    return g_floorPoly;
}
static bool is_proxy(uintptr_t ent) {
    uint32_t n = g_sh->nproxy; if (n > SHARED_MAX_PROXIES) n = SHARED_MAX_PROXIES;
    for (uint32_t i = 0; i < n; i++) if (g_sh->proxy[i] == ent) return true;
    return false;
}
static void apply_floor(float* d, bool sora) {
    uint32_t& res = *(uint32_t*)&d[0xc];
    bool airMode = (*(uint32_t*)&d[10]) & 1;
    float fy = g_sh->floor_y, y = d[0x11];
    int poly = find_floor_poly();
    if (sora && g_sh->snap_seq != g_lastSnap) {   // Borderlands landed: KH1 Sora lands too, wherever he is
        g_lastSnap = g_sh->snap_seq;
        if (g_sh->floor_on) { y = fy; g_sh->lift_total = 0; }
    }
    if (!g_sh->floor_on) {                        // Borderlands is falling: a bottomless floor keeps KH1 Sora falling
        if (sora && g_sh->fall_limit > 0 && y > fy + g_sh->fall_limit) { y -= g_sh->fall_lift; g_sh->lift_total += g_sh->fall_lift; }
        d[0x11] = y; res |= 1; d[0x1a] = y + 100000.f;
    } else {
        bool land = airMode ? (y >= fy) : (fy - y <= d[9] * 0.5f);
        if (land) { d[0x11] = fy; res &= ~1u; g_sh->ground_clamps++; }
        else { d[0x11] = y; res |= 1; }
        d[0x1a] = fy;
    }
    if (poly >= 0) *(int16_t*)&d[0xf] = (int16_t)poly;
    if (sora) g_sh->sora_air = res & 1;
}

static unsigned long long __fastcall hkColQuery(float* desc) {
    uintptr_t sora = *(uintptr_t*)(g_base + RVA_SORA_PTR);
    uintptr_t ent = (uintptr_t)desc - 0x2A0;
    bool isSora = sora && ent == sora;
    bool ground = g_sh && g_sh->ground_mode && (isSora || is_proxy(ent));
    bool prev = g_soraQuery, prevDrop = g_dropAll;
    g_soraQuery = !ground && g_sh && g_sh->walls_off && isSora;
    g_dropAll = ground;
    unsigned long long r = oColQuery(desc);
    g_soraQuery = prev; g_dropAll = prevDrop;
    if (ground) apply_floor(desc, isSora);
    return r;
}
static void __fastcall hkGather(float* box, int mode) {
    oGather(box, mode);
    if (g_dropAll) {   // ground mode: no KH1 room geometry at all (Borderlands supplies the floor and the walls)
        short* pc = (short*)((uint8_t*)box + 0x18);
        if (*pc) g_sh->walls_removed += (uint32_t)*pc;
        *pc = 0; *(int*)(g_base + RVA_TRI_COUNT) = 0;
        return;
    }
    if (!g_soraQuery) return;
    short* pcount = (short*)((uint8_t*)box + 0x18);
    int n = *pcount, w = 0;
    int* list = (int*)(g_base + RVA_TRI_LIST);
    uint8_t* tri = *(uint8_t**)(g_base + RVA_TRI_PTR);
    int16_t* nrm = *(int16_t**)(g_base + RVA_NRM_PTR);
    if (!tri || !nrm) return;
    for (int i = 0; i < n; i++) {
        int idx = list[i];
        int ni = *(int16_t*)(tri + (size_t)idx * 0x14 + 8);
        float nx = nrm[ni * 4], ny = nrm[ni * 4 + 1], nz = nrm[ni * 4 + 2];
        float len2 = nx * nx + ny * ny + nz * nz;
        if (len2 > 0 && ny * ny < 0.30f * len2) continue;   // steep (|ny| < ~0.55): a wall -> skip for Sora
        list[w++] = idx;
    }
    if (w != n) g_sh->walls_removed += (uint32_t)(n - w);
    *pcount = (short)w; *(int*)(g_base + RVA_TRI_COUNT) = w;
}

static bool hook_checked(uintptr_t rva, const uint8_t* bytes, size_t nb, void* detour, void** orig, const char* name) {
    void* target = (void*)(g_base + rva);
    if (memcmp(target, bytes, nb) != 0) { logf("%s: bytes differ (hooked already or other KH1 version) - skipped", name); return false; }
    bool ok = MH_CreateHook(target, detour, orig) == MH_OK && MH_EnableHook(target) == MH_OK;
    logf("%s hook %s", name, ok ? "installed" : "FAILED"); return ok;
}

static DWORD WINAPI init_thread(LPVOID) {
    uintptr_t base = (uintptr_t)GetModuleHandleW(nullptr); g_base = base;
    HANDLE m = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0, sizeof(SharedBlock), SHARED_NAME);
    if (!m) { logf("CreateFileMapping failed %lu", GetLastError()); return 0; }
    g_sh = (SharedBlock*)MapViewOfFile(m, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(SharedBlock));
    if (!g_sh) { logf("MapViewOfFile failed %lu", GetLastError()); return 0; }
    MH_Initialize();
    logf("---- kh1_bridge v3: base %p", (void*)base);
    // controller hook (skipped when an older bridge in this KH1 session already owns read_pad)
    if (hook_checked(RVA_READ_PAD, READ_PAD_BYTES, sizeof READ_PAD_BYTES, (void*)&hkReadPad, (void**)&oReadPad, "read_pad")) g_sh->kh_status = 1;
    bool w1 = hook_checked(RVA_COLQUERY, COLQUERY_BYTES, sizeof COLQUERY_BYTES, (void*)&hkColQuery, (void**)&oColQuery, "col_query");
    bool w2 = hook_checked(RVA_GATHER, GATHER_BYTES, sizeof GATHER_BYTES, (void*)&hkGather, (void**)&oGather, "tri_gather");
    g_sh->world_hooks = (w1 && w2) ? 1 : 0;
    g_sh->bridge_version = (w1 && w2) ? 3 : 0;
    return 0;
}

BOOL WINAPI DllMain(HINSTANCE h, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(h);
        GetModuleFileNameA(h, g_dir, MAX_PATH); char* s = strrchr(g_dir, '\\'); if (s) *s = 0;
        CloseHandle(CreateThread(nullptr, 0, init_thread, nullptr, 0, nullptr));
    }
    return TRUE;
}
