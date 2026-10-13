// kh1_fx.dll - loaded into KINGDOM HEARTS FINAL MIX.exe (Steam 1.0.0.2) next to kh1_bridge.dll (no KH1 restart needed).
// Captures KH1's own effect sprites so Borderlands can draw them (vanilla KH1 effects instead of made-up ones).
//
// KH1's effect system ("ppp" programs from xm_*.mag / *.dpx) draws every sprite through one packet:
//   FUN_14031e210(shape, params, ...) builds a 0x110-byte packet, FUN_14031e920(packet, batch) draws it:
//   packet +0x30 shape*, +0x38 u32 flags, +0x44 world matrix A (4x4 f32, row vectors), +0x84 matrix B,
//   +0xC4 rgba f32 (PS2 scale, 128 = 1.0), +0xD4/+0xE4/+0xF4 vec4 shader constants.
//   shape: +0 u8 flags (8 = free quads, 24 B each; else rects 16 B), +2 i16 nquads, +4 i32 offset to per-quad info
//   (8 B: f32 q, u8 b4, u8 b5, u8 pad, u8 clut), +0x10 u64 GS TEX0 (CBP bits replaced per quad from the clut byte),
//   +0x20 quads: rect = x0 y0 x1 y1 u v w h (i16; xy * 1/16), free = 4 x (x, y) then u v w h.
// The texture behind a TEX0 is an Axa::EntryTex (FUN_14010d720): +0x14 name ("texcommon_dpd18d80.dds"),
// +0x68 file bytes in memory (dds/png), +0x78 byte size, +0x7C/+0x7E HD size, +0x94/+0x96 PS2 size.
// Each drawn quad -> one FxQuad in the ring of shared memory "Local\SoraFx" (see fx_shared.h).
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <cstdint>
#include <cstddef>
#include <cstdio>
#include <cstring>
#include <cstdarg>
#include <tlhelp32.h>
#include "MinHook.h"
#include "fx_shared.h"
#include "ui_shared.h"
#include "kui_shared.h"
#include "../crashlog.h"                 // v42: release crash logger (crash_logs\session_KH1.bin)

static char g_dir[MAX_PATH];
static void logf(const char* fmt, ...) {
    char path[MAX_PATH]; snprintf(path, sizeof path, "%s\\kh1_fx.log", g_dir);
    FILE* f = fopen(path, "a"); if (!f) return;
    va_list a; va_start(a, fmt); vfprintf(f, fmt, a); va_end(a); fputc('\n', f); fclose(f);
}

static uintptr_t g_base = 0;
static FxShared* g_sh = nullptr;
static UiShared* g_ui = nullptr;
static KuiShared* g_kui = nullptr;
static volatile uint32_t g_subCaller = 0; static volatile DWORD g_subTid = 0;   // v15: who called FUN_1402764b0

static const uintptr_t RVA_SHAPE_DRAW = 0x31E920, RVA_TEX_LOOKUP = 0x10D720, RVA_GS_CTX = 0x55A050, RVA_GS_CTXSEL = 0x55A0E0;
static const uint8_t SHAPE_DRAW_BYTES[] = {0x48, 0x89, 0x5c, 0x24, 0x10, 0x57, 0x48, 0x81, 0xec, 0xa0, 0x00, 0x00, 0x00};
typedef void(__fastcall* ShapeDraw)(uint8_t* packet, void* batch);
typedef uintptr_t(__fastcall* TexLookup)(const uint64_t* tex0);
static ShapeDraw oShapeDraw = nullptr;
static TexLookup g_texLookup = nullptr;
static LARGE_INTEGER g_qpf;

// ---- v8: effect MODELS (fx_mdl.h). FUN_140328ea0(packet, n) runs one effect-model packet (header = *(packet+0x10):
// +0x18 GS ALPHA, +0x58 i16 positions, +0x80 matrix A, +0x100 colour); its mesh draw functions fill KH1's generic
// BeginPrim FUN_1400f3040(maxverts, stride, 0) -> vertex buffer, EndPrim FUN_1400fb9c0(count, mode). While a model
// packet runs (per thread), every EndPrim's vertices are copied with that header's state.
#include "fx_mdl.h"
static FxMdlShared* g_mdl = nullptr;
static const uintptr_t RVA_MDL_RUN = 0x328EA0, RVA_BEGIN_PRIM = 0xF3040, RVA_END_PRIM = 0xFB9C0;
static const uint8_t MDL_RUN_BYTES[] = {0x48, 0x89, 0x5c, 0x24, 0x08, 0x48, 0x89, 0x6c, 0x24, 0x10};
static const uint8_t PRIM_BYTES[] = {0x48, 0x89, 0x5c, 0x24, 0x08, 0x48, 0x89, 0x74, 0x24, 0x10, 0x57};
typedef void(__fastcall* MdlRun)(uint8_t* packet, int n);
typedef float*(__fastcall* BeginPrim)(int maxv, int stride, int x);
typedef void(__fastcall* EndPrim)(int count, int mode, int x);
static MdlRun oMdlRun = nullptr; static BeginPrim oBeginPrim = nullptr; static EndPrim oEndPrim = nullptr;
// KH1 draws effects on one thread: plain globals + the owning thread id (no thread_local: this DLL hot-unloads)
static uint8_t* volatile t_hdr = nullptr; static volatile DWORD t_tid = 0;
static float* volatile t_buf = nullptr; static volatile int t_stride = 0;

static inline int64_t now_us() { LARGE_INTEGER t; QueryPerformanceCounter(&t); return t.QuadPart * 1000000 / g_qpf.QuadPart; }

static void capture(uint8_t* pk) {
    const uint8_t* shape = *(uint8_t**)(pk + 0x30);
    if (!shape) return;
    int n = *(const int16_t*)(shape + 2);
    if (n <= 0 || n > 256) return;
    bool free4 = (shape[0] & 8) != 0;
    const uint8_t* info = shape + *(const int32_t*)(shape + 4);
    const int16_t* q = (const int16_t*)(shape + 0x20);
    uint64_t base = *(const uint64_t*)(shape + 0x10);
    int ctx = *(int*)(g_base + RVA_GS_CTXSEL) == 1 ? 1 : 0;
    const uint8_t* gs = (const uint8_t*)(g_base + RVA_GS_CTX + ctx * 0x48);
    int64_t t = now_us();
    uintptr_t d420 = *(uintptr_t*)(g_base + 0x2E9D420);
    g_sh->alt_cam = (d420 && *(int*)(d420 + 0xDC)) ? 1 : 0;
    memcpy((void*)g_sh->view, (void*)(g_base + 0x2E9D480), 64);
    memcpy((void*)g_sh->proj, (void*)(g_base + 0x2E9D600), 64);
    g_sh->cam_seq++;
    for (int i = 0; i < n; i++, info += 8) {
        uint32_t w = InterlockedIncrement((volatile LONG*)&g_sh->write) - 1;
        FxQuad& r = g_sh->ring[w % FX_RING];
        r.seq = 0;                                   // invalid while being written
        memcpy(r.m, pk + 0x44, 64);
        memcpy(r.col, pk + 0xC4, 16);
        if (free4) {
            static const int ord[8] = {0, 1, 2, 3, 6, 7, 4, 5};   // KH1 order v0 v1 v3 v2 -> loop order
            for (int k = 0; k < 8; k++) r.xy[k] = q[ord[k]] * 0.0625f;
            r.uv[0] = q[8]; r.uv[1] = q[9]; r.uv[2] = (float)(q[8] + q[10]); r.uv[3] = (float)(q[9] + q[11]);
            q += 12;
        } else {
            float x0 = q[0] * 0.0625f, y0 = q[1] * 0.0625f, x1 = q[2] * 0.0625f, y1 = q[3] * 0.0625f;
            r.xy[0] = x0; r.xy[1] = y0; r.xy[2] = x1; r.xy[3] = y0; r.xy[4] = x1; r.xy[5] = y1; r.xy[6] = x0; r.xy[7] = y1;
            r.uv[0] = q[4]; r.uv[1] = q[5]; r.uv[2] = (float)(q[4] + q[6]); r.uv[3] = (float)(q[5] + q[7]);
            q += 8;
        }
        uint64_t tex0 = ((((uint64_t)info[7] << 37) + base) ^ base) & 0x7ffe000000000ull ^ base;
        r.tex0 = tex0;
        r.texobj = g_texLookup ? (uint64_t)g_texLookup(&tex0) : 0;
        r.qf = *(const float*)info;
        r.b4 = info[4]; r.b5 = info[5]; r.clut = info[7];
        r.shape_flags = shape[0];
        r.pk_flags = *(uint32_t*)(pk + 0x38);
        memcpy(r.gs, gs, sizeof r.gs);
        r.time_us = t;
        r.shape = (uint64_t)shape;
        MemoryBarrier();
        r.seq = w + 1;
    }
    g_sh->quads_total += n;
}

// v4: KH1 command injection. FUN_1402a6220(entity, ...) = KH1 Sora's per-frame player update: it runs the command
// executor FUN_1402a7510 only when the command word's low byte != 0 (and bit 0x1000000 clear). The executor reads the word
// 0x2D53A40 (type | param << 8 | flags), target handle 0x2D53A44, runs it (magic: FUN_14028efd0 unlock + MP check, cast
// motion 0x36 + spell) and clears it. KH1's menu / shortcuts fill the word earlier in the frame.
static const uintptr_t RVA_CMD_EXEC = 0x2A6220, RVA_CMD_WORD = 0x2D53A40, RVA_SORA_PTR = 0x2537E48;
static const uintptr_t RVA_TO_HANDLE = 0x38AD90, RVA_LOCK_ON = 0x2DB3510, RVA_LOCK_TGT = 0x2DB3518;
static const uint8_t CMD_EXEC_BYTES[] = {0x48, 0x89, 0x5c, 0x24, 0x18, 0x57, 0x48, 0x83, 0xec, 0x40};
typedef void(__fastcall* CmdExec)(uint8_t* ent, void* a2);
typedef uint32_t(__fastcall* ToHandle)(void* p);
static CmdExec oCmdExec = nullptr;
static volatile LONG g_inflight = 0;
// v5: KH1 world collision. FUN_140194720(box, mode) gathers the room triangles near a query box into the int list at
// 0x234E7D0 (count -> *(short*)(box + 0x18) and 0x23527D8); triangles: *(u8**)0x234E620 + idx*0x14 (+8 i16 normal
// index), normals *(i16**)0x234E650 + n*4. kh1_bridge.dll hooks the same function (for Sora); this hook chains on top.
static const uintptr_t RVA_GATHER = 0x194720, RVA_TRI_PTR = 0x234E620, RVA_NRM_PTR = 0x234E650, RVA_TRI_LIST = 0x234E7D0, RVA_TRI_COUNT = 0x23527D8;
typedef void(__fastcall* Gather)(float* box, int mode);
static Gather oGather = nullptr;
static void __fastcall hkGather(float* box, int mode) {
    InterlockedIncrement(&g_inflight);
    oGather(box, mode);
    if (g_sh) { g_sh->gather_calls++; g_sh->gather_tris += (uint32_t)*(short*)((uint8_t*)box + 0x18); }
    if (g_sh && g_sh->walls_all) {
        short* pcount = (short*)((uint8_t*)box + 0x18);
        int n = *pcount, w = 0;
        int* list = (int*)(g_base + RVA_TRI_LIST);
        uint8_t* tri = *(uint8_t**)(g_base + RVA_TRI_PTR);
        int16_t* nrm = *(int16_t**)(g_base + RVA_NRM_PTR);
        if (tri && nrm && n > 0) {
            for (int i = 0; i < n; i++) {
                int idx = list[i];
                int ni = *(int16_t*)(tri + (size_t)idx * 0x14 + 8);
                float nx = nrm[ni * 4], ny = nrm[ni * 4 + 1], nz = nrm[ni * 4 + 2], l2 = nx * nx + ny * ny + nz * nz;
                if (l2 > 0 && ny * ny < 0.30f * l2) continue;          // steep = wall
                list[w++] = idx;
            }
            if (w != n) { g_sh->walls_all_removed += (uint32_t)(n - w); *pcount = (short)w; *(int*)(g_base + RVA_TRI_COUNT) = w; }
        }
    }
    InterlockedDecrement(&g_inflight);
}
struct SndShared;
static void snd_mute_walk();   // v19 (defined with the sound hook below)
// ---- v22: LEVEL LINK (user: a Borderlands level-up = a KH1 level-up, shown the KH1 way). Borderlands bumps req with
// `levels`; on KH1's game thread (Sora's command exec) the DLL computes the EXP Sora needs to gain that many levels from
// KH1's own growth table and grants it through KH1's kill-reward path FUN_140284990(exp) (whole party, like a vanilla
// kill: level-up FUN_1402b51b0 = stats / abilities, FUN_1402b5360 = KH1's "LEVEL UP" event). Party block i =
// *(0x2868BA0) + 0x74*i: +4 level, +5..+0xB HP maxHP MP maxMP AP STR DEF, +0x40 EXP; Sora's EXP table id =
// u32 0x528028[byte +0x48D], others u32 0x528000[i]; table = u16 per-level EXP at 0x2D22D40 + id.
struct LvlShared {
    uint32_t magic;               // @0 'SLVL'
    volatile uint32_t req;        // @4 Borderlands bumps
    volatile uint32_t done;       // @8 DLL sets = req when handled
    volatile uint32_t levels;     // @12 Sora levels to gain (1..10)
    volatile int32_t granted;     // @16 EXP granted by the last request (-1 = refused)
    volatile uint32_t count;      // @20 requests handled
    uint8_t before[3][8];         // @24 Sora / Donald / Goofy: level HP maxHP MP maxMP AP STR DEF before
    uint8_t after[3][8];          // @48 ... after
    volatile uint32_t exp_after[3];   // @72
    volatile uint32_t block_kill_exp; // @84 v26: 1 = KH1 kills give no EXP (Borderlands levels drive KH1 levels)
    volatile uint32_t blocked;        // @88 kills whose EXP was dropped
    // v37: BL level loadout (logic kh1_loadout_*): KH1's own Keyblade equip + KH1's menu-close refresh, on the game thread
    volatile uint32_t lo_req;         // @92 Borderlands bumps
    volatile uint32_t lo_done;        // @96 DLL sets = lo_req when handled
    volatile uint32_t lo_item;        // @100 Sora weapon item to equip (0 = none)
    volatile uint32_t lo_flags;       // @104 1 = run FUN_1402a4e50 (abilities / stats rebuilt from the party blocks), v41: 2 = magic sync
    volatile uint32_t lo_result;      // @108 1 equip called, 2 refreshed, 0x10 equip busy/skipped, 0x100 equip watchdog, 0x8000 fault
    volatile uint32_t lo_count;       // @112 requests handled
};
static LvlShared* g_lvl = nullptr;
static volatile bool g_lvl_granting = false;
// v26: KH1's whole-party EXP award FUN_140284990(exp) (kill reward FUN_1402a4f40 -> here). While the level link drives
// KH1 levels from Borderlands, kill EXP is dropped; the level link's own grant passes (g_lvl_granting).
typedef void(__fastcall* ExpParty)(int exp, void* a2, void* a3, void* a4);   // pass every register through
static ExpParty oExpParty = nullptr;
static const uint8_t EXP_PARTY_BYTES[] = {0x48, 0x89, 0x5c, 0x24, 0x18, 0x89, 0x4c, 0x24, 0x08};
static void __fastcall hkExpParty(int exp, void* a2, void* a3, void* a4) {
    InterlockedIncrement(&g_inflight);
    if (g_lvl && g_lvl->block_kill_exp && !g_lvl_granting) { g_lvl->blocked++; InterlockedDecrement(&g_inflight); return; }
    oExpParty(exp, a2, a3, a4);
    InterlockedDecrement(&g_inflight);
}
static const uintptr_t RVA_PARTY_PTR = 0x2868BA0, RVA_EXP_PARTY = 0x284990;
static void lvl_snapshot(uint8_t (*dst)[8]) {
    uint8_t* party = *(uint8_t**)(g_base + RVA_PARTY_PTR);
    for (int i = 0; i < 3; i++) memcpy(dst[i], party + 0x74 * i + 4, 8);
}
// v37: KH1's Equipment-menu weapon equip FUN_140286720(item, done_cb): sets party+0x36, unloads the old weapon model, loads
// the new one asynchronously and calls done_cb when loaded. The menu brackets it with its busy counter 0x2E1F4E8++ and
// "equip loading" 0x2E9A0C4 = 1; its callback LAB_140307460 = 0x2E1F4E8-- + 0x2E9A0C4 = 0. Same here, plus a watchdog.
static const uintptr_t RVA_EQUIP = 0x286720, RVA_EQUIP_CB = 0x307460, RVA_MENU_BUSY = 0x2E1F4E8, RVA_EQUIP_LOADING = 0x2E9A0C4,
                       RVA_PARTY_REFRESH = 0x2A4E50, RVA_MENU_OPEN_FLAG = 0x2E1F470;
static const uint8_t EQUIP_CB_BYTES[] = {0xff, 0x0d, 0x82, 0x80, 0xb1, 0x02, 0xc7, 0x05, 0x54, 0x2c, 0xb9, 0x02};
static const uint8_t EQUIP_BYTES[] = {0x48, 0x89, 0x5c, 0x24, 0x18, 0x55, 0x56, 0x41, 0x56, 0x48, 0x83, 0xec};
static const uint8_t REFRESH_BYTES[] = {0x48, 0x89, 0x5c, 0x24, 0x10, 0x48, 0x89, 0x6c, 0x24, 0x18, 0x56, 0x48};
static int g_eq_wait = 0;     // frames since our equip started (0 = none pending)
// v41: KH1 MAGIC SYNC. KH1 keeps the spells it can cast in a runtime table 0x2D22B80 (7 x 0x38: +0 level whose data is
// loaded, +0x10 load buffer, +0x18 spell data (0 = not loaded), +0x30 done callback). Writing the save levels
// (party +0x492) alone left it stale (user 2026-10-12: magic menu "Fire Blizzard Thunder" at level 1 + greyed
// "Thundaga Curaga Graviga Stopga" rows, only 3 spells castable). KH1's own learn-spell (event opcode FUN_1401af730)
// calls FUN_14028f560(spell, level, cb): sets the levels + loads that spell's data (async, cb may be null). Here: every
// spell whose save level (!= 0) differs from the loaded level - what KH1's sync FUN_14028f480 does on its own.
// result: 0x40 synced, count of reloads << 16; 0x80 = bytes differ (not called).
static const uintptr_t RVA_SPELL_TABLE = 0x2D22B80, RVA_SPELL_SET = 0x28F560;
static const uint8_t SPELL_SET_BYTES[] = {0x48, 0x83, 0xec, 0x28, 0x4c, 0x63, 0xca};
static uint32_t magic_sync() {
    if (memcmp((void*)(g_base + RVA_SPELL_SET), SPELL_SET_BYTES, sizeof SPELL_SET_BYTES)) return 0x80;
    uint8_t* party = *(uint8_t**)(g_base + RVA_PARTY_PTR);
    if (!party) return 0;
    uint32_t n = 0;
    for (int i = 0; i < 7; i++) {
        uint8_t lv = party[0x492 + i];
        int32_t cur = *(int32_t*)(g_base + RVA_SPELL_TABLE + (size_t)i * 0x38);
        if (lv == 0 || lv > 3 || (int32_t)lv == cur) continue;
        ((int(__fastcall*)(int, int, void*))(g_base + RVA_SPELL_SET))(i, lv, nullptr);
        n++;
    }
    return 0x40 | (n << 16);
}
// v44 (session 9 KH1 crashes 21:27 + 22:53): an equip / refresh while KH1 was loading a room (the mod's home warp at a
// new BL session, or the guard firing mid-reload) ran KH1's weapon-attach FUN_1402a3980 on a party member whose model
// handle (+0x14c) resolved to nothing -> null write at 0x48. Every present party member needs its model (+0x14c) and
// data (+0x130) handles resolved by KH1's own resolver FUN_14038adc0, else "busy" (0x10: the callers retry).
static const uintptr_t RVA_RESOLVE = 0x38ADC0, RVA_PARTY_ENTS[3] = {0x2537E48, 0x2D37288, 0x2D37290};
static bool party_ready() {
    __try {
        auto res = (uint64_t(__fastcall*)(uint32_t))(g_base + RVA_RESOLVE);
        for (uintptr_t rva : RVA_PARTY_ENTS) {
            uint8_t* e = *(uint8_t**)(g_base + rva);
            if (!e) { if (rva == RVA_PARTY_ENTS[0]) return false; continue; }   // Sora must exist; D / G may be away
            if (!res(*(uint32_t*)(e + 0x14c)) || !res(*(uint32_t*)(e + 0x130))) return false;
        }
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}
static uint32_t lo_do(uint32_t item, uint32_t flags) {    // v38: shared by the loadout request and the guard restore
    uint32_t r = 0;
    if ((item || (flags & 1)) && !party_ready()) return 0x10 | 0x200;   // v44: room loading - nothing touched, retry
    volatile int32_t* busy = (int32_t*)(g_base + RVA_MENU_BUSY);
    volatile uint32_t* loading = (uint32_t*)(g_base + RVA_EQUIP_LOADING);
    if (item) {
        bool ok = !memcmp((void*)(g_base + RVA_EQUIP_CB), EQUIP_CB_BYTES, sizeof EQUIP_CB_BYTES) &&
                  !memcmp((void*)(g_base + RVA_EQUIP), EQUIP_BYTES, sizeof EQUIP_BYTES);
        if (ok && *loading == 0 && *(uint8_t*)(g_base + RVA_MENU_OPEN_FLAG) == 0 && g_eq_wait == 0) {
            (*busy)++;
            *loading = 1;
            g_eq_wait = 1;
            ((void(__fastcall*)(uint8_t, void*))(g_base + RVA_EQUIP))((uint8_t)item, (void*)(g_base + RVA_EQUIP_CB));
            r |= 1;
        } else r |= 0x10;
    }
    if (flags & 2) r |= magic_sync();                              // v41: before the refresh (it reads the spells too)
    if ((flags & 1) && memcmp((void*)(g_base + RVA_PARTY_REFRESH), REFRESH_BYTES, sizeof REFRESH_BYTES)) r |= 0x20;
    else if (flags & 1) {
        ((void(__fastcall*)())(g_base + RVA_PARTY_REFRESH))();
        r |= 2;
    }
    return r;
}
static void lo_handle() { g_lvl->lo_result = lo_do(g_lvl->lo_item, g_lvl->lo_flags); }
static void lo_watch() {    // once per KH1 frame: our equip's callback never came -> undo the menu bracket ourselves
    if (!g_eq_wait) return;
    volatile uint32_t* loading = (uint32_t*)(g_base + RVA_EQUIP_LOADING);
    if (*loading == 0) { g_eq_wait = 0; return; }
    if (++g_eq_wait > 600) {
        (*(volatile int32_t*)(g_base + RVA_MENU_BUSY))--;
        *loading = 0;
        g_eq_wait = 0;
        if (g_lvl) g_lvl->lo_result |= 0x100;
    }
}
// ---- v38: GUARD (release step 6 + J5). Borderlands' level loadout changes the player's KH1 party / inventory in live
// memory; only Borderlands could put the originals back, so a BL crash / quit left KH1 boosted (a KH1 save point would
// then write the boosted values into the player's save) and KH1's party kept fighting on its own. Now Borderlands keeps
// the ORIGINAL bytes of everything it changed here ("Local\SoraGuard") and bumps a heartbeat every tick. When the BL
// process is gone (or the heartbeat is stale for stale_ms: hung / stuck loading) KH1's game thread writes the originals
// back + runs KH1's own equip / refresh (lo_do), then the guard presses Start through the bridge's pad override
// ("Local\SoraPassthrough") so KH1 pauses (PAUSE screen in battle mode, main menu in the field). When the heartbeat
// comes back the guard disarms; Borderlands re-snapshots, re-applies and unpauses KH1 itself (paused_by_guard).
#define GUARD_PATCHES 16
#define GUARD_PATCH_MAX 0x80
struct GuardPatch {
    uint64_t addr;                // absolute KH1 address
    uint16_t len;                 // 1..GUARD_PATCH_MAX
    uint16_t kind;                // 0 raw bytes, 1 party block (0x74): current HP / MP kept, clamped to the original max
    uint32_t pad;
    uint8_t data[GUARD_PATCH_MAX];
};
struct GuardShared {
    uint32_t magic;               // @0 'SGRD' (Borderlands writes it)
    uint32_t version;             // @4 1
    volatile uint32_t bl_pid;     // @8 Borderlands process id (0 = guard off)
    volatile uint32_t hb;         // @12 Borderlands bumps every tick
    volatile uint32_t stale_ms;   // @16 heartbeat stale -> fire (0 = 20000)
    volatile uint32_t armed;      // @20 1 = the patches below hold originals to restore
    volatile uint32_t npatch;     // @24
    volatile uint32_t equip_item; // @28 original weapon items, byte 0 Sora, 1 Donald, 2 Goofy (0 = leave); v42: all three
    volatile uint32_t pause_on;   // @32 1 = pause KH1 after restoring
    volatile uint32_t start_mask; // @36 pad bit of Start (bridge layout)
    volatile uint32_t state;      // @40 DLL: 0 watching, 1 restoring, 2 pausing, 3 done (waiting for Borderlands)
    volatile uint32_t reason;     // @44 DLL: 1 BL process gone, 2 heartbeat stale
    volatile uint32_t fired;      // @48 DLL: times fired (Borderlands re-snapshots when this changes)
    volatile uint32_t result;     // @52 DLL: 1 patches written (game thread), equip/refresh flags << 8, 0x40 watch-thread
                                  //      fallback, 0x80 patch skipped, 0x8000 fault, 0x10000 paused, 0x20000 pause failed
    volatile uint32_t paused_by_guard;  // @56 DLL sets 1 when its Start press paused KH1; Borderlands clears after unpausing
    volatile uint32_t dll_alive;  // @60 DLL bumps every poll
    GuardPatch patch[GUARD_PATCHES];    // @64, 0x90 each
};
static_assert(sizeof(GuardPatch) == 0x90, "GuardPatch layout");
static_assert(offsetof(GuardShared, patch) == 64, "GuardShared layout");
static GuardShared* g_guard = nullptr;
static volatile LONG g_gphase = 0;        // 0 watching, 1 restore requested, 4 game thread restoring, 2 pausing, 3 done
static uint32_t g_ghb = 0, g_gpid = 0; static ULONGLONG g_ghb_t = 0, g_gfire_t = 0;
static HANDLE g_gproc = nullptr;
static volatile uint8_t* g_pt = nullptr;   // bridge block: @0 magic, @4 bl_seq, @8 active, @12 buttons u16, @14 sticks[4]
static int g_press_step = 0, g_press_tries = 0; static ULONGLONG g_press_t = 0;

static bool guard_writable(uint64_t a, uint32_t n) {
    MEMORY_BASIC_INFORMATION mi;
    if (!a || !VirtualQuery((void*)a, &mi, sizeof mi) || mi.State != MEM_COMMIT) return false;
    if (!(mi.Protect & (PAGE_READWRITE | PAGE_EXECUTE_READWRITE | PAGE_WRITECOPY))) return false;
    return a + n <= (uint64_t)mi.BaseAddress + mi.RegionSize;
}
static uint32_t guard_write_patches() {
    uint32_t r = 0, n = g_guard->npatch; if (n > GUARD_PATCHES) n = GUARD_PATCHES;
    for (uint32_t i = 0; i < n; i++) {
        GuardPatch* p = &g_guard->patch[i];
        if (!p->len || p->len > GUARD_PATCH_MAX || !guard_writable(p->addr, p->len) || (p->kind == 1 && p->len != 0x74)) { r |= 0x80; continue; }
        uint8_t* a = (uint8_t*)p->addr;
        uint8_t buf[GUARD_PATCH_MAX]; memcpy(buf, p->data, p->len);
        if (p->kind == 1) {        // party block: keep the current HP / MP, clamped to the original max
            uint8_t hp = a[5], mp = a[7];
            buf[5] = hp > buf[6] ? buf[6] : (hp < 1 ? 1 : hp);
            buf[7] = mp > buf[8] ? buf[8] : mp;
        }
        memcpy(a, buf, p->len);
    }
    return r;
}
// v42: the weapons to put back, one KH1 equip at a time (KH1 loads one weapon model at a time; its equip finds the
// character from the item). Decided from the weapons equipped BEFORE the patches: the party blocks hold +0x36 (weapon), so
// after writing them every original looked "already equipped" and no equip ran - KH1 kept drawing the banded weapon
// models (Donald / Goofy until the next room load; Sora's too).
static uint8_t g_gq[3]; static int g_gqn = 0, g_gqi = 0; static volatile int g_grefresh = 0;
static void guard_restore_gt() {      // KH1 game thread (hkCmdExec, Sora's entity)
    uint32_t r = 0;
    if (g_guard->armed) {
        uint8_t* party = *(uint8_t**)(g_base + RVA_PARTY_PTR);
        uint32_t items = g_guard->equip_item;
        g_gqn = g_gqi = 0;
        for (int i = 0; i < 3 && party; i++) {
            uint8_t want = (uint8_t)(items >> (8 * i)), cur = party[0x74 * i + 0x36];
            if (want && want != cur) g_gq[g_gqn++] = want;
        }
        r |= guard_write_patches() | 1;
        if (!party_ready()) g_grefresh = 1;          // v44: room loading - equips + refresh run per frame once ready
        else {
            uint8_t first = g_gqn ? g_gq[g_gqi++] : 0;
            r |= (lo_do(first, 1 | 2) & 0xffff) << 8;   // v41: + magic sync (originals' spell levels loaded again)
        }
        r |= (uint32_t)g_gqn << 24;                 // v42: weapons to re-equip
        g_guard->armed = 0;
    }
    g_guard->result = r;
}
static void guard_equip_next() {      // KH1 game thread, once per frame: the next queued weapon when KH1 is free
    if (g_grefresh) {                     // v44: the restore came during a room load - refresh + magic sync now
        if (g_eq_wait || !party_ready()) return;
        uint32_t r = lo_do(0, 1 | 2);
        if (!(r & 0x200)) { g_grefresh = 0; g_guard->result |= (r & 0xffff) << 8; }
        return;
    }
    if (g_gqi >= g_gqn || g_eq_wait) return;
    if (*(volatile uint32_t*)(g_base + RVA_EQUIP_LOADING) != 0) return;
    uint32_t r = lo_do(g_gq[g_gqi], 0);
    if (r & 1) { g_gqi++; g_guard->result += 1u << 28; }   // equips done (high nibble)
}
static void guard_pad(bool on, uint16_t buttons) {
    if (!g_pt || *(volatile uint32_t*)g_pt != 0x31545053u) return;
    *(volatile uint16_t*)(g_pt + 12) = buttons;
    for (int i = 0; i < 4; i++) g_pt[14 + i] = 0x80;
    *(volatile uint32_t*)(g_pt + 8) = on ? 1 : 0;
    (*(volatile uint32_t*)(g_pt + 4))++;
}
static bool kh1_halted() { return *(uint8_t*)(g_base + 0x2E1AC59) == 1 || *(uint8_t*)(g_base + RVA_MENU_OPEN_FLAG) != 0; }
static bool bl_dead() { return g_gproc && WaitForSingleObject(g_gproc, 0) == WAIT_OBJECT_0; }
static void guard_poll() {
    if (!g_guard || g_guard->magic != 0x44524753) return;
    g_guard->dll_alive++;
    ULONGLONG now = GetTickCount64();
    uint32_t hb = g_guard->hb, pid = g_guard->bl_pid;
    bool beat = hb != g_ghb;
    if (beat) { g_ghb = hb; g_ghb_t = now; }
    if (pid != g_gpid) {                  // new Borderlands session
        if (g_gproc) CloseHandle(g_gproc);
        g_gproc = pid ? OpenProcess(SYNCHRONIZE, FALSE, pid) : nullptr;
        g_gpid = pid; g_ghb_t = now;
    }
    if (!pid) return;
    if (g_gphase == 0) {
        uint32_t st = g_guard->stale_ms ? g_guard->stale_ms : 20000;
        bool dead = bl_dead(), stale = now - g_ghb_t > st;
        if (!dead && !stale) return;
        g_guard->reason = dead ? 1 : 2;
        g_guard->fired++;
        g_gfire_t = now; g_press_step = 0; g_press_tries = 0;
        logf("guard: fired (%s), armed %u, %u patches", dead ? "Borderlands gone" : "heartbeat stale", g_guard->armed, g_guard->npatch);
        if (g_guard->armed) { g_guard->state = 1; InterlockedExchange(&g_gphase, 1); }
        else { g_guard->state = 2; InterlockedExchange(&g_gphase, 2); }
        return;
    }
    if (g_gphase == 4) return;            // game thread is writing the originals right now
    if (beat && !bl_dead()) {             // Borderlands is back: disarm (it re-snapshots + unpauses KH1 itself)
        if (g_press_step) guard_pad(false, 0);
        logf("guard: Borderlands heartbeat back -> watching (paused_by_guard %u)", g_guard->paused_by_guard);
        g_press_step = 0; g_guard->state = 0;
        InterlockedExchange(&g_gphase, 0);
        return;
    }
    if (g_gphase == 1) {
        if (now - g_gfire_t < 3000) return;              // no game frame (KH1 paused / loading): write from this thread
        if (InterlockedCompareExchange(&g_gphase, 2, 1) != 1) return;
        g_guard->result = (g_guard->armed ? guard_write_patches() : 0) | 0x40;
        g_guard->armed = 0;
        logf("guard: restored from the watch thread (no game frame), result %x", g_guard->result);
        g_guard->state = 2;
        return;
    }
    if (g_gphase == 2) {
        if (g_gqi < g_gqn && now - g_gfire_t < 5000) return;   // v42: let KH1 load the re-equipped weapons first
        if (!g_guard->pause_on || !g_pt) { g_gphase = 3; g_guard->state = 3; return; }
        if (g_press_step == 0) {
            if (kh1_halted()) {
                if (g_press_tries) { g_guard->result |= 0x10000; g_guard->paused_by_guard = 1; logf("guard: KH1 paused"); }
                g_gphase = 3; g_guard->state = 3; return;
            }
            if (g_press_tries >= 3) { g_guard->result |= 0x20000; logf("guard: pause failed"); g_gphase = 3; g_guard->state = 3; return; }
            g_press_tries++; g_press_step = 1; g_press_t = now;
        }
        if (g_press_step == 1) {                         // hold Start 0.3 s
            guard_pad(true, (uint16_t)g_guard->start_mask);
            if (now - g_press_t >= 300) { g_press_step = 2; g_press_t = now; }
        } else if (g_press_step == 2) {                  // release 0.3 s
            guard_pad(true, 0);
            if (now - g_press_t >= 300) { g_press_step = 3; g_press_t = now; guard_pad(false, 0); }
        } else if (g_press_step == 3) {                  // give KH1 1.5 s to show the pause / menu
            if (kh1_halted() || now - g_press_t >= 1500) g_press_step = 0;
        }
    }
}
static void guard_poll_safe() { __try { guard_poll(); } __except (EXCEPTION_EXECUTE_HANDLER) {} }
static void lvl_grant() {
    uint8_t* party = *(uint8_t**)(g_base + RVA_PARTY_PTR);
    g_lvl->granted = -1;
    if (!party) return;
    uint32_t n = g_lvl->levels; if (n < 1) n = 1; if (n > 10) n = 10;
    uint32_t lvl = party[4];
    if (lvl >= 100) return;
    uint32_t id = *(uint32_t*)(g_base + 0x528028 + 4 * (uint32_t)party[0x48D]);
    const uint16_t* t = (const uint16_t*)(g_base + 0x2D22D40 + id);
    int64_t target = 0;
    for (uint32_t k = 0; k < lvl + n - 1 && k < 99; k++) target += t[k];     // EXP for level lvl + n
    int64_t need = target - *(int32_t*)(party + 0x40);
    if (need <= 0) need = 1;
    lvl_snapshot(g_lvl->before);
    g_lvl_granting = true;
    ((void(__fastcall*)(int))(g_base + RVA_EXP_PARTY))((int)need);   // (hooked: oExpParty when installed)
    g_lvl_granting = false;
    lvl_snapshot(g_lvl->after);
    for (int i = 0; i < 3; i++) g_lvl->exp_after[i] = *(uint32_t*)(party + 0x74 * i + 0x40);
    g_lvl->granted = (int32_t)need;
}


// ---- v23: hardware WRITE WATCH (RE helper, "Local\SoraWatch"): Borderlands sets addr + req; the DLL arms DR0 (write,
// 4 bytes) on every KH1 thread; a vectored handler logs each write: RIP + the first 12 return-address-looking qwords on
// the stack (inside KH1's image) + the new value. Off again when Borderlands sets req = 0. Used to find KH1's damage
// routine (who writes an entity's HP at stats +0x3C).
struct WatchHit { uint32_t seq, value; uint64_t rip, rsp; uint32_t rva_rip, nret; uint32_t ret[12]; };
struct WatchShared { uint32_t magic; volatile uint32_t req, armed, nhit; volatile uint64_t addr; WatchHit h[64]; };
static WatchShared* g_watch = nullptr;
static PVOID g_vehHandle = nullptr;
static volatile LONG g_crashLogged = 0;
static void crash_log(PEXCEPTION_POINTERS e) {
    // v27: KH1 access violation -> RIP, registers and KH1 return addresses on the stack into kh1_fx.log (once), then
    // KH1 crashes as it would have. Two crashes at 0x386AD3 (vector copy from null) on 2026-10-05.
    if (InterlockedExchange(&g_crashLogged, 1)) return;
    CONTEXT* c = e->ContextRecord;
    logf("CRASH code %08lx rip %p (rva %llx) addr %p", e->ExceptionRecord->ExceptionCode, (void*)c->Rip,
         (unsigned long long)(c->Rip - g_base), (void*)e->ExceptionRecord->ExceptionInformation[1]);
    logf("  rax %llx rbx %llx rcx %llx rdx %llx rsi %llx rdi %llx r8 %llx r9 %llx", c->Rax, c->Rbx, c->Rcx, c->Rdx, c->Rsi, c->Rdi, c->R8, c->R9);
    char buf[1024]; int n = 0;
    __try {
        for (int k = 0; k < 512 && n < 980; k++) {
            uint64_t v = ((uint64_t*)c->Rsp)[k];
            if (v > g_base + 0x1000 && v < g_base + 0x3B0000) n += snprintf(buf + n, sizeof buf - n, " %llx", (unsigned long long)(v - g_base));
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) {}
    buf[n] = 0;
    logf("  stack rvas:%s", buf);
}
static LONG CALLBACK watch_veh(PEXCEPTION_POINTERS e) {
    if (e->ExceptionRecord->ExceptionCode == EXCEPTION_ACCESS_VIOLATION) {
        uintptr_t rip = e->ContextRecord->Rip;
        if (rip > g_base && rip < g_base + 0x3B0000) crash_log(e);
        return EXCEPTION_CONTINUE_SEARCH;
    }
    if (e->ExceptionRecord->ExceptionCode != EXCEPTION_SINGLE_STEP || !g_watch) return EXCEPTION_CONTINUE_SEARCH;
    CONTEXT* c = e->ContextRecord;
    if (!(c->Dr6 & 1)) return EXCEPTION_CONTINUE_SEARCH;
    c->Dr6 = 0;
    uint32_t i = g_watch->nhit;
    if (i < 64) {
        WatchHit& w = g_watch->h[i];
        w.rip = c->Rip; w.rsp = c->Rsp; w.rva_rip = (uint32_t)(c->Rip - g_base);
        __try { w.value = *(uint32_t*)g_watch->addr; } __except (EXCEPTION_EXECUTE_HANDLER) { w.value = 0xFFFFFFFF; }
        w.nret = 0;
        uintptr_t lo = g_base, hi = g_base + 0x3B0000;
        __try {
            for (int k = 0; k < 256 && w.nret < 12; k++) {
                uint64_t v = ((uint64_t*)c->Rsp)[k];
                if (v > lo + 0x1000 && v < hi) w.ret[w.nret++] = (uint32_t)(v - g_base);
            }
        } __except (EXCEPTION_EXECUTE_HANDLER) {}
        w.seq = i + 1;
        g_watch->nhit = i + 1;
    }
    return EXCEPTION_CONTINUE_EXECUTION;
}
static void watch_apply(uint64_t addr, bool on) {
    DWORD me = GetCurrentThreadId(), pid = GetCurrentProcessId();
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
    if (snap == INVALID_HANDLE_VALUE) return;
    THREADENTRY32 te; te.dwSize = sizeof te;
    for (BOOL ok = Thread32First(snap, &te); ok; ok = Thread32Next(snap, &te)) {
        if (te.th32OwnerProcessID != pid || te.th32ThreadID == me) continue;
        HANDLE t = OpenThread(THREAD_GET_CONTEXT | THREAD_SET_CONTEXT | THREAD_SUSPEND_RESUME, FALSE, te.th32ThreadID);
        if (!t) continue;
        if (SuspendThread(t) != (DWORD)-1) {
            CONTEXT c; memset(&c, 0, sizeof c); c.ContextFlags = CONTEXT_DEBUG_REGISTERS;
            if (GetThreadContext(t, &c)) {
                if (on) { c.Dr0 = addr; c.Dr7 = (c.Dr7 & ~0xF0003ull) | 1ull | (1ull << 16) | (3ull << 18); }   // L0, write, 4 bytes
                else { c.Dr0 = 0; c.Dr7 &= ~0xF0003ull; }
                SetThreadContext(t, &c);
            }
            ResumeThread(t);
        }
        CloseHandle(t);
    }
    CloseHandle(snap);
}
static void watch_poll() {
    if (!g_watch) return;
    uint32_t want = g_watch->req ? 1 : 0;
    if (want && !g_watch->armed) { g_watch->nhit = 0; watch_apply(g_watch->addr, true); g_watch->armed = 1; logf("watch armed %p", (void*)g_watch->addr); }
    else if (!want && g_watch->armed) { watch_apply(0, false); g_watch->armed = 0; logf("watch off (%u hits)", g_watch->nhit); }
}


// ---- v24: KH1 HIT RECORDS (stray Borderlands shots -> a real KH1 hit on Donald / Goofy). KH1 keeps a queue of
// pending hits per entity (ent +0x3EC handle -> 0x40-byte records from the pool 0x2DE5E10 [64], next at +0x30):
// +0 flags (bit0 used), +4 attacker handle, +8 damage, +0xC type (low 5 bits = reaction kind; 1 = flinch strength
// from damage / max HP), +0x10 hit direction, +0x24 seq, +0x28 mask, +0x2C attack def handle. FUN_14029bbd0(ent)
// pops and applies them every frame: damage (FUN_1402beee0), KO when HP < 1, flinch / knockback by kind. Logged here
// (ring "Local\SoraHits") so a real record can be copied; Borderlands requests a hit -> on KH1's game thread a pool
// record = that copy with our damage + direction, linked with KH1's own FUN_1402bfe60(target, record).
static inline uint64_t kh_resolve(uint32_t h);
struct HitLog { uint32_t seq, pad; uint64_t ent; uint8_t rec[64]; };
struct HitShared {
    uint32_t magic; volatile uint32_t write;           // @0
    volatile uint32_t req, done;                       // @8  Borderlands bumps req
    volatile uint64_t target;                          // @16 KH1 entity (Donald / Goofy)
    volatile int32_t damage; volatile uint32_t result; // @24 result: 1 linked, 2 no template, 3 no free record, 4 bad target
    volatile float dir[4];                             // @32 hit direction (KH1 space, as in records +0x10)
    uint8_t tmpl[64]; volatile uint32_t have_tmpl;     // @48 template record (Borderlands may write one), @112
    uint32_t pad2;
    HitLog log[256];                                   // @120
};
static HitShared* g_hits = nullptr;
static const uintptr_t RVA_HIT_PROC = 0x29BBD0, RVA_HIT_LINK = 0x2BFE60, RVA_HIT_POOL = 0x2DE5E10;
static const uint8_t HIT_PROC_BYTES[] = {0x48, 0x8b, 0xc4, 0x55, 0x57, 0x41, 0x54, 0x41, 0x56, 0x41, 0x57, 0x48, 0x8d, 0x68, 0xb8};
// v28: KH1 calls it with more arguments (FUN_1402a43a0: (ent, a2), and its reaction handlers read R8) - the v24-v27
// hook declared ONE and clobbered RDX / R8 -> reaction handlers got garbage (crashes 0x386AD3 / 0x29D640 on 2026-10-05)
// v32: KH1 PRIZES without a death. FUN_1402abc40(table, pos, mode) is KH1's prize spawner (enemy death scripts reach it
// through AI script commands 63 / 156): table = 7 orb counts (HP / MP / munny kinds) then 3 x {chance, item} (items via
// FUN_1402bff00), pos = KH1 vec4, mode = (DAT_14298c9a0 != 0) for cmd 63. Every call is logged ("Local\SoraPrize") so
// Borderlands learns each Heartless kind's table from one real death; a request spawns that table's prizes at a
// stand-in on the game thread - the drop without killing the Heartless (killing them emptied the room -> reloads).
typedef void(__fastcall* PrizeSpawn)(int* table, float* pos, int mode, void* a4);
static PrizeSpawn oPrize = nullptr;
static const uintptr_t RVA_PRIZE = 0x2ABC40, RVA_PRIZE_MODE = 0x298C9A0;
static const uint8_t PRIZE_BYTES[] = {0x48, 0x8b, 0xc4, 0x48, 0x89, 0x48, 0x08, 0x41, 0x54, 0x41, 0x55, 0x41, 0x56, 0x48, 0x83, 0xec, 0x70};
struct PrizeLog { volatile uint32_t seq; int32_t mode; uint64_t table; float pos[4]; int32_t t[13]; uint32_t ours; };   // 88 B
struct PrizeShared {
    uint32_t magic; volatile uint32_t write;           // @0
    volatile uint32_t req, done;                       // @8  Borderlands bumps req
    int32_t t[13]; int32_t mode;                       // @16 table to spawn, mode (-1 = KH1's own cmd-63 rule)
    float pos[4];                                      // @72 KH1 position
    volatile uint32_t result; uint32_t pad;            // @88 result: 1 spawned, 2 fault
    PrizeLog log[64];                                  // @96
};
static PrizeShared* g_prize = nullptr;
static volatile LONG g_prize_ours = 0;
static void __fastcall hkPrize(int* table, float* pos, int mode, void* a4) {
    InterlockedIncrement(&g_inflight);
    if (g_prize) {
        __try {
            uint32_t i = (uint32_t)InterlockedIncrement((volatile LONG*)&g_prize->write) - 1;
            PrizeLog& L = g_prize->log[i % 64];
            L.seq = 0; L.mode = mode; L.table = (uint64_t)table; L.ours = (uint32_t)g_prize_ours;
            memcpy(L.pos, pos, 16); memcpy(L.t, table, 52); L.seq = i + 1;
        } __except (EXCEPTION_EXECUTE_HANDLER) {}
    }
    oPrize(table, pos, mode, a4);
    InterlockedDecrement(&g_inflight);
}
static void prize_service() {                          // game thread (called from hkHitProc)
    if (!g_prize || !oPrize || g_prize->req == g_prize->done) return;
    static int32_t tbl[16];
    static __declspec(align(16)) float pos[4];
    __try {
        memcpy(tbl, (const void*)g_prize->t, 52); memcpy(pos, (const void*)g_prize->pos, 16);
        int mode = g_prize->mode >= 0 ? g_prize->mode : (*(int*)(g_base + RVA_PRIZE_MODE) != 0);
        g_prize_ours = 1;
        oPrize(tbl, pos, mode, nullptr);
        g_prize_ours = 0;
        g_prize->result = 1;
    } __except (EXCEPTION_EXECUTE_HANDLER) { g_prize_ours = 0; g_prize->result = 2; }
    g_prize->done = g_prize->req;
}

typedef void(__fastcall* HitProc)(uint8_t* ent, void* a2, void* a3, void* a4);
static HitProc oHitProc = nullptr;
static void __fastcall hkHitProc(uint8_t* ent, void* a2, void* a3, void* a4) {
    InterlockedIncrement(&g_inflight);
    prize_service();
    if (g_hits) {
        __try {
            uint64_t r = kh_resolve(*(uint32_t*)(ent + 0x3EC));
            for (int n = 0; r && n < 8; n++) {
                uint32_t i = (uint32_t)InterlockedIncrement((volatile LONG*)&g_hits->write) - 1;
                HitLog& L = g_hits->log[i % 256];
                L.seq = 0; L.pad = 0; L.ent = (uint64_t)ent; memcpy(L.rec, (void*)r, 64); L.seq = i + 1;
                r = kh_resolve(*(uint32_t*)(r + 0x30));
            }
        } __except (EXCEPTION_EXECUTE_HANDLER) {}
    }
    oHitProc(ent, a2, a3, a4);
    InterlockedDecrement(&g_inflight);
}

// v30: KH1 Sora NEVER dies (user decision: Borderlands owns death / FFYL; KH1 Sora floors at 1 HP). The mod's HP
// mirror could be beaten by a big KH1 hit inside one frame -> KH1 Game Over (user, high-level enemies). Clamp in KH1's
// own HP change FUN_1402a4920(ent, delta): Sora's HP never goes below 1.
typedef uint32_t(__fastcall* HpAdd)(uint8_t* ent, int delta, void* a3, void* a4);
static HpAdd oHpAdd = nullptr;
static const uintptr_t RVA_HP_ADD = 0x2A4920;
static const uint8_t HP_ADD_BYTES[] = {0x48, 0x89, 0x74, 0x24, 0x10, 0x48, 0x89, 0x7c, 0x24, 0x18, 0x41, 0x56};
// v31: STAND-INS (Heartless standing in for Borderlands enemies) also floor at 1 HP, and every hit on one is counted
// with the damage KH1 MEANT to deal ("Local\SoraStand", Borderlands writes the entity list). Their KH1 HP now mirrors
// the Borderlands enemy's health (lock-on HP bar), so a hit can't be read from an HP drop any more (at 1 HP it's 0).
struct StandSlot { volatile uint64_t ent; volatile LONG dmg; volatile LONG hits; };
struct StandShared { uint32_t magic; volatile uint32_t on; uint32_t n, pad; StandSlot slot[32]; };
static StandShared* g_stand = nullptr;
static uint32_t __fastcall hkHpAdd(uint8_t* ent, int delta, void* a3, void* a4) {
    InterlockedIncrement(&g_inflight);
    __try {
        bool floor1 = false;
        if (delta < 0 && ent && ent == *(uint8_t**)(g_base + RVA_SORA_PTR)) floor1 = true;
        else if (delta < 0 && ent && g_stand && g_stand->on) {
            for (int i = 0; i < 32; i++) {
                if (g_stand->slot[i].ent == (uint64_t)ent) {
                    InterlockedExchangeAdd(&g_stand->slot[i].dmg, -delta);
                    InterlockedIncrement(&g_stand->slot[i].hits);
                    floor1 = true;
                    break;
                }
            }
        }
        if (floor1) {
            uint64_t stp = kh_resolve(*(uint32_t*)(ent + 0x6C));
            if (stp) { int hp = *(int*)(stp + 0x3C); if (hp + delta < 1) delta = hp > 1 ? 1 - hp : 0; }
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) {}
    uint32_t r = oHpAdd(ent, delta, a3, a4);
    InterlockedDecrement(&g_inflight);
    return r;
}

// v29: Sora's own Keyblade hits don't use the queue: FUN_1402b7b90(target, rec) applies a hit directly (attacker,
// reaction, damage). Logged into the same ring (HitLog.pad = 1) so a real Sora -> Heartless record can be a template.
typedef uint64_t(__fastcall* HitDirect)(uint8_t* ent, uint8_t* rec, void* a3, void* a4);
static HitDirect oHitDirect = nullptr;
static const uintptr_t RVA_HIT_DIRECT = 0x2B7B90;
static const uint8_t HIT_DIRECT_BYTES[] = {0x40, 0x57, 0x48, 0x83, 0xec, 0x40, 0x48, 0xc7, 0x44, 0x24, 0x30, 0xfe, 0xff, 0xff, 0xff};
static uint64_t __fastcall hkHitDirect(uint8_t* ent, uint8_t* rec, void* a3, void* a4) {
    InterlockedIncrement(&g_inflight);
    if (g_hits && rec) {
        __try {
            uint32_t i = (uint32_t)InterlockedIncrement((volatile LONG*)&g_hits->write) - 1;
            HitLog& L = g_hits->log[i % 256];
            L.seq = 0; L.pad = 1; L.ent = (uint64_t)ent; memcpy(L.rec, rec, 64); L.seq = i + 1;
        } __except (EXCEPTION_EXECUTE_HANDLER) {}
    }
    uint64_t r = oHitDirect(ent, rec, a3, a4);
    InterlockedDecrement(&g_inflight);
    return r;
}

static void hit_inject() {
    g_hits->result = 4;
    uint8_t* tgt = (uint8_t*)g_hits->target;
    if (!tgt) return;
    bool ok = false;
    for (int k = 0; k < 3; k++) if (*(uint8_t**)(g_base + 0x2D37280 + 8 * k) == tgt) ok = true;
    uint8_t* pool = (uint8_t*)(g_base + 0x2D372A0);                 // v26: any live actor of KH1's entity pool
    if (tgt >= pool && tgt < pool + 96 * 0x4B0 && (tgt - pool) % 0x4B0 == 0 && (*(uint32_t*)(tgt + 0x374) & 3) == 1) ok = true;
    if (!ok) return;
    if (!g_hits->have_tmpl) { g_hits->result = 2; return; }
    uint8_t* rec = nullptr;
    for (int k = 0; k < 64; k++) { uint8_t* r = (uint8_t*)(g_base + RVA_HIT_POOL + 0x40 * k); if (!(*(uint32_t*)r & 1)) { rec = r; break; } }
    if (!rec) { g_hits->result = 3; return; }
    memcpy(rec, g_hits->tmpl, 64);
    *(uint32_t*)rec |= 1;
    *(int32_t*)(rec + 8) = g_hits->damage;
    memcpy(rec + 0x10, (const void*)g_hits->dir, 16);
    *(uint32_t*)(rec + 0x30) = 0;                                   // end of queue
    ((void(__fastcall*)(uint8_t*, uint8_t*))(g_base + RVA_HIT_LINK))(tgt, rec);
    g_hits->result = 1;
}

static void __fastcall hkCmdExec(uint8_t* ent, void* a2) {
    InterlockedIncrement(&g_inflight);
    if (g_hits && g_hits->req != g_hits->done && ent == *(uint8_t**)(g_base + RVA_SORA_PTR)) {
        __try { hit_inject(); } __except (EXCEPTION_EXECUTE_HANDLER) { g_hits->result = 9; }
        g_hits->done = g_hits->req;
    }
    if (g_lvl && g_lvl->req != g_lvl->done && ent == *(uint8_t**)(g_base + RVA_SORA_PTR)) {
        __try { lvl_grant(); } __except (EXCEPTION_EXECUTE_HANDLER) { if (g_sh) g_sh->faults++; }
        g_lvl->count++;
        g_lvl->done = g_lvl->req;
    }
    if (g_lvl && g_lvl->lo_req != g_lvl->lo_done && ent == *(uint8_t**)(g_base + RVA_SORA_PTR)) {
        __try { lo_handle(); } __except (EXCEPTION_EXECUTE_HANDLER) { g_lvl->lo_result |= 0x8000; if (g_sh) g_sh->faults++; }
        g_lvl->lo_count++;
        g_lvl->lo_done = g_lvl->lo_req;
    }
    if (g_lvl && ent == *(uint8_t**)(g_base + RVA_SORA_PTR)) { __try { lo_watch(); } __except (EXCEPTION_EXECUTE_HANDLER) {} }
    if (g_guard && (g_gqi < g_gqn || g_grefresh) && ent == *(uint8_t**)(g_base + RVA_SORA_PTR)) { __try { guard_equip_next(); } __except (EXCEPTION_EXECUTE_HANDLER) { g_gqi = g_gqn; } }
    if (g_guard && g_gphase == 1 && ent == *(uint8_t**)(g_base + RVA_SORA_PTR) && InterlockedCompareExchange(&g_gphase, 4, 1) == 1) {
        __try { guard_restore_gt(); } __except (EXCEPTION_EXECUTE_HANDLER) { g_guard->result |= 0x8000; }
        g_guard->state = 2; g_press_step = 0; g_press_tries = 0;
        InterlockedExchange(&g_gphase, 2);
    }
    if (g_ui && ent == *(uint8_t**)(g_base + RVA_SORA_PTR)) g_ui->frame++;
    if (ent == *(uint8_t**)(g_base + RVA_SORA_PTR)) snd_mute_walk();   // v19: once per KH1 frame
    if (g_sh && g_sh->bgm_seq != g_sh->bgm_done && ent == *(uint8_t**)(g_base + RVA_SORA_PTR)) {
        __try {
            typedef void(__fastcall* Fade)(void* snd, float vol, int frames);
            uint32_t n = 0;
            for (uint8_t* e = *(uint8_t**)(g_base + 0x4D6618); e && n < 64; e = *(uint8_t**)(e + 8)) {
                if (*(int*)(e + 0x3C) == 0) { ((Fade)(g_base + 0x56E60))(e + 0x10, g_sh->bgm_vol, 0); n++; }
            }
            g_sh->bgm_count = n;
        } __except (EXCEPTION_EXECUTE_HANDLER) { g_sh->faults++; }
        g_sh->bgm_done = g_sh->bgm_seq;
    }
    if (g_sh && g_sh->cmd_req != g_sh->cmd_done && ent == *(uint8_t**)(g_base + RVA_SORA_PTR)) {
        __try {
            uint32_t* w = (uint32_t*)(g_base + RVA_CMD_WORD);
            uint32_t tgt = ((ToHandle)(g_base + RVA_TO_HANDLE))(nullptr);
            if (g_sh->cmd_target == 1) tgt = ((ToHandle)(g_base + RVA_TO_HANDLE))(ent);
            else if (g_sh->cmd_target == 2 && *(int*)(g_base + RVA_LOCK_ON) != 0) tgt = *(uint32_t*)(g_base + RVA_LOCK_TGT);
            else if (g_sh->cmd_target == 3 && g_sh->cmd_target_ent) tgt = ((ToHandle)(g_base + RVA_TO_HANDLE))((void*)g_sh->cmd_target_ent);
            w[0] = g_sh->cmd_word; w[1] = tgt; w[2] = 0;
            g_sh->cmd_count++;
        } __except (EXCEPTION_EXECUTE_HANDLER) { g_sh->faults++; }
        g_sh->cmd_done = g_sh->cmd_req;
    }
    oCmdExec(ent, a2);
    InterlockedDecrement(&g_inflight);
}
// v40 debug: FxShared reserved[0] = N -> log the KH1 call stacks of the next N effect draws (find KH1's frame loop)
static void stack_log(const char* what) {
    if (!g_sh || !g_sh->reserved[0]) return;
    g_sh->reserved[0]--;
    void* fr[24]; USHORT n = RtlCaptureStackBackTrace(0, 24, fr, nullptr);
    char buf[512]; int o = snprintf(buf, sizeof buf, "stack %s tid %lu:", what, GetCurrentThreadId());
    for (USHORT i = 0; i < n && o < 480; i++) {
        uintptr_t a = (uintptr_t)fr[i];
        if (a >= g_base && a < g_base + 0x4000000) o += snprintf(buf + o, sizeof buf - o, " %llx", (unsigned long long)(a - g_base));
    }
    logf("%s", buf);
}
static void __fastcall hkMdlRun(uint8_t* packet, int n) {
    InterlockedIncrement(&g_inflight);
    __try { stack_log("mdl"); } __except (EXCEPTION_EXECUTE_HANDLER) {}
    uint8_t* prev = t_hdr; DWORD prevTid = t_tid;
    __try { t_hdr = *(uint8_t**)(packet + 0x10); } __except (EXCEPTION_EXECUTE_HANDLER) { t_hdr = nullptr; }
    t_tid = GetCurrentThreadId(); t_buf = nullptr;
    oMdlRun(packet, n);
    t_hdr = prev; t_tid = prevTid; t_buf = nullptr;
    InterlockedDecrement(&g_inflight);
}
// ---- v14: UI primitive probe ("Local\SoraUiProbe"): EndPrim calls outside effect-model packets (KH1's 2D UI: HUD,
// menus) with the raw vertices, so their format can be read. Enabled by the probe header (off by default).
#pragma pack(push, 1)
struct UiProbeRec { volatile uint32_t seq; uint32_t tid, stride, count, mode, x; uint64_t texobj; int64_t time_us; uint64_t ret; uint8_t ctx[0x48]; uint8_t v[1024]; };
struct UiProbe { uint32_t magic; volatile uint32_t enabled, write, recsize; uint8_t pad[48]; UiProbeRec r[512]; };
#pragma pack(pop)
static UiProbe* g_probe = nullptr;
static float* volatile g_anyBuf = nullptr; static volatile int g_anyStride = 0; static volatile DWORD g_anyTid = 0;
static void ui_probe(int count, int mode, int x, uint64_t ra) {
    if (!g_probe || !g_probe->enabled || !g_anyBuf || g_anyTid != GetCurrentThreadId()) return;
    uint32_t w = InterlockedIncrement((volatile LONG*)&g_probe->write) - 1;
    UiProbeRec& r = g_probe->r[w % 512];
    r.seq = 0; r.tid = g_anyTid; r.stride = (uint32_t)g_anyStride; r.count = (uint32_t)count; r.mode = (uint32_t)mode; r.x = (uint32_t)x;
    int ctxi = *(int*)(g_base + RVA_GS_CTXSEL) == 1 ? 1 : 0;
    memcpy(r.ctx, (const void*)(g_base + RVA_GS_CTX + ctxi * 0x48), 0x48);
    uint64_t tex0 = *(uint64_t*)(r.ctx + 0x28);
    r.texobj = g_texLookup ? (uint64_t)g_texLookup(&tex0) : 0;
    size_t n = (size_t)count * (size_t)(g_anyStride > 0 ? g_anyStride : 0); if (n > sizeof r.v) n = sizeof r.v;
    memcpy(r.v, g_anyBuf, n);
    LARGE_INTEGER t; QueryPerformanceCounter(&t); r.time_us = t.QuadPart * 1000000 / g_qpf.QuadPart; r.ret = ra;
    MemoryBarrier(); r.seq = w + 1;
}
// v15: KH1's 2D interface (kui_shared.h): every EndPrim outside effect models that draws on screen, minus the world
// sprites (room glows 0x31d9b3 / 0x33c6f0), the viewport side bars (0x187968), degenerate weapon quads (0x29758d),
// off-screen targets (XYOFFSET x == y) and the target cursor (sprite list from FUN_140274b20: KH1 places it at its
// own enemy's screen position - Borderlands draws its own copy of that sprite at the Borderlands enemy).
// v36: Dumbo's Splash water stream ("tex_mizu0/1_tm240") is drawn triangle by triangle through EndPrim OUTSIDE any effect
// model packet (return address 0x27709E), stride 32: x y z w (homogeneous PS2 screen, the space effect-model matrices
// map to), u v (1/4096 of the texture like effect meshes), colour (TEX0 TFX 3 HIGHLIGHT2 + TCC: alpha from the
// texture, vertex alpha unused = 0). Copied into the effect-model ring as a batch with identity matrix: x/w y/w z/w.
static const uint32_t RVA_WATER_PRIM = 0x27709E;
static void water_capture(int count, int mode) {
    const uint8_t* src = (const uint8_t*)g_anyBuf; int stride = g_anyStride;
    if (!g_mdl || !g_mdl->enabled || !src || g_anyTid != GetCurrentThreadId() || count <= 0 || count > 4096 || stride < 32 || stride > 64) return;
    for (int i = 0; i < count; i++) { float wv = *(const float*)(src + (size_t)i * stride + 12); if (!(wv > 1e-4f)) return; }
    int ctx = *(int*)(g_base + RVA_GS_CTXSEL) == 1 ? 1 : 0;
    const uint8_t* gs = (const uint8_t*)(g_base + RVA_GS_CTX + ctx * 0x48);
    uint64_t tex0 = *(const uint64_t*)(gs + 0x28);
    bool texAlpha = ((tex0 >> 34) & 1) && (((tex0 >> 35) & 3) == 1 || ((tex0 >> 35) & 3) == 3);   // TCC + DECAL / HIGHLIGHT2
    uint32_t w = InterlockedIncrement((volatile LONG*)&g_mdl->write) - 1;
    uint32_t first = InterlockedExchangeAdd((volatile LONG*)&g_mdl->vwrite, count);
    FxMdlBatch& b = g_mdl->batch[w % FXM_BATCHES];
    b.seq = 0;
    for (int i = 0; i < count; i++) {
        const float* q = (const float*)(src + (size_t)i * stride); float* d = g_mdl->verts[(first + i) % FXM_VERTS];
        d[0] = q[0] / q[3]; d[1] = q[1] / q[3]; d[2] = q[2] / q[3];
        uint32_t c; memcpy(&c, src + (size_t)i * stride + 24, 4);
        if (texAlpha) c = (c & 0xffffff) | 0x80000000u;
        memcpy(&d[3], &c, 4); d[4] = q[4]; d[5] = q[5]; d[6] = 0.f;
    }
    b.nverts = (uint32_t)count; b.mode = (uint32_t)mode; b.first = first; b.stride = (uint32_t)stride;
    for (int i = 0; i < 16; i++) b.A[i] = (i % 5 == 0) ? 1.f : 0.f;
    for (int i = 0; i < 4; i++) b.col[i] = 1.f;
    memcpy(b.gs, gs, sizeof b.gs);
    memcpy(b.gs + 16, gs + 0x20, 8); b.pad = FXM_GS_CLAMP_MARK;
    b.alpha = *(const uint64_t*)(gs + 8);
    b.tex0 = tex0; b.texobj = g_texLookup ? (uint64_t)g_texLookup(&tex0) : 0;
    LARGE_INTEGER t; QueryPerformanceCounter(&t); b.time_us = t.QuadPart * 1000000 / g_qpf.QuadPart;
    b.header = RVA_WATER_PRIM;
    MemoryBarrier();
    b.seq = w + 1;
}
static void kui_capture(int count, int mode, uint32_t ra) {
    if (!g_kui || !g_kui->enabled || !g_anyBuf || g_anyTid != GetCurrentThreadId() || count <= 0 || count > 4096) return;
    if (ra == 0x31D9B3 || ra == 0x33C6F0 || ra == 0x187968 || ra == 0x29758D) return;
    int ctxi = *(int*)(g_base + RVA_GS_CTXSEL) == 1 ? 1 : 0;
    const uint8_t* ctx = (const uint8_t*)(g_base + RVA_GS_CTX + ctxi * 0x48);
    uint16_t ox = *(const uint16_t*)(ctx + 0x30), oy = *(const uint16_t*)(ctx + 0x34);
    if (ox == oy) return;
    uint32_t sub = (g_subTid == GetCurrentThreadId()) ? g_subCaller : 0;
    if (sub >= 0x274B20 && sub < 0x274E70) return;
    int stride = g_anyStride;
    if (stride < 20 || stride > 64) return;
    uint32_t w = InterlockedIncrement((volatile LONG*)&g_kui->write) - 1;
    uint32_t first = InterlockedExchangeAdd((volatile LONG*)&g_kui->vwrite, count);
    KuiBatch& b = g_kui->b[w % KUI_BATCHES];
    b.seq = 0;
    const uint8_t* src = (const uint8_t*)g_anyBuf;
    for (int i = 0; i < count; i++) {
        const uint8_t* q = src + (size_t)i * stride; float* d = g_kui->v[(first + i) % KUI_VERTS];
        memcpy(d, q, 16);
        if (stride >= 28) memcpy(d + 4, q + 16, 8); else { d[4] = 0; d[5] = 0; }
    }
    b.ret = ra; b.mode = (uint32_t)mode; b.count = (uint32_t)count; b.first = first; b.submit = sub; b.stride = (uint32_t)stride;
    b.ofx = ox / 16.f; b.ofy = oy / 16.f;
    b.alpha = *(const uint64_t*)(ctx + 8); b.clamp = *(const uint64_t*)(ctx + 0x20); b.tex0 = *(const uint64_t*)(ctx + 0x28);
    b.texobj = 0; b.pw = b.ph = 0;
    if (stride >= 28 && g_texLookup) {
        uint64_t t0 = b.tex0; uint64_t to = (uint64_t)g_texLookup(&t0); b.texobj = to;
        if (to) { b.pw = *(const uint16_t*)(to + 0x94); b.ph = *(const uint16_t*)(to + 0x96); }
    }
    LARGE_INTEGER t; QueryPerformanceCounter(&t); b.time_us = t.QuadPart * 1000000 / g_qpf.QuadPart;
    MemoryBarrier();
    b.seq = w + 1;
}
static float* __fastcall hkBeginPrim(int maxv, int stride, int x) {
    InterlockedIncrement(&g_inflight);
    float* r = oBeginPrim(maxv, stride, x);
    if (t_hdr && t_tid == GetCurrentThreadId()) { t_buf = r; t_stride = stride; }
    g_anyBuf = r; g_anyStride = stride; g_anyTid = GetCurrentThreadId();   // v14: every primitive (UI probe)
    InterlockedDecrement(&g_inflight);
    return r;
}
static void mdl_capture(int count, int mode) {
    uint8_t* h = t_hdr; float* src = t_buf; int stride = t_stride;
    if (!g_mdl || !g_mdl->enabled || !h || !src || count <= 0 || count > 4096 || stride < 24 || stride > 64) return;
    uint32_t w = InterlockedIncrement((volatile LONG*)&g_mdl->write) - 1;
    uint32_t first = InterlockedExchangeAdd((volatile LONG*)&g_mdl->vwrite, count);
    FxMdlBatch& b = g_mdl->batch[w % FXM_BATCHES];
    b.seq = 0;
    for (int i = 0; i < count; i++) memcpy(g_mdl->verts[(first + i) % FXM_VERTS], (uint8_t*)src + (size_t)i * stride, 28);
    b.nverts = (uint32_t)count; b.mode = (uint32_t)mode; b.first = first;
    memcpy(b.A, h + 0x80, 64); memcpy(b.col, h + 0x100, 16);
    int ctx = *(int*)(g_base + RVA_GS_CTXSEL) == 1 ? 1 : 0;
    const uint8_t* gs = (const uint8_t*)(g_base + RVA_GS_CTX + ctx * 0x48);
    // v39: KH1's depth state per batch (Simba's intro: the rock writes Z so the sky drawn after it stays behind it).
    // GS ctx +0 TEST (ZTE bit 16, ZTST bits 17-18), +0x10 ZBUF (ZMSK bit 32) -> stride bits 16.. (fx_mdl.h FXM_Z_*)
    uint64_t test = *(const uint64_t*)gs, zbuf = *(const uint64_t*)(gs + 0x10);
    b.stride = (uint32_t)stride | FXM_Z_VALID | (((zbuf >> 32) & 1) ? FXM_Z_NOWRITE : 0) | (uint32_t)((test >> 16) & 7) << 18;
    memcpy(b.gs, gs, sizeof b.gs);
    memcpy(b.gs + 16, gs + 0x20, 8); b.pad = FXM_GS_CLAMP_MARK;     // v12: GS CLAMP (texture wrap per axis)
    b.alpha = *(const uint64_t*)(gs + 8);
    uint64_t tex0 = *(const uint64_t*)(gs + 0x28);
    b.tex0 = tex0; b.texobj = g_texLookup ? (uint64_t)g_texLookup(&tex0) : 0;
    LARGE_INTEGER t; QueryPerformanceCounter(&t); b.time_us = t.QuadPart * 1000000 / g_qpf.QuadPart;
    b.header = (uint64_t)h;
    MemoryBarrier();
    b.seq = w + 1;
}
static void __fastcall hkEndPrim(int count, int mode, int x) {
    InterlockedIncrement(&g_inflight);
    if (g_sh && (g_sh->blank & 1)) {                              // v13 test: KH1 draws nothing through EndPrim
        g_sh->blank_dropped++; t_buf = nullptr; oEndPrim(0, mode, x);
        InterlockedDecrement(&g_inflight); return;
    }
    if (t_hdr && t_tid == GetCurrentThreadId()) {
        __try { mdl_capture(count, mode); } __except (EXCEPTION_EXECUTE_HANDLER) { if (g_mdl) g_mdl->faults++; }
        t_buf = nullptr;
    } else {
        uint64_t ra = (uint64_t)_ReturnAddress() - g_base;
        __try { ui_probe(count, mode, x, ra); } __except (EXCEPTION_EXECUTE_HANDLER) {}
        if (ra == RVA_WATER_PRIM) {                                    // v36: world-space water (Splash), not UI
            __try { water_capture(count, mode); } __except (EXCEPTION_EXECUTE_HANDLER) { if (g_mdl) g_mdl->faults++; }
        } else {
            __try { kui_capture(count, mode, (uint32_t)ra); } __except (EXCEPTION_EXECUTE_HANDLER) { if (g_kui) g_kui->faults++; }
        }
    }
    g_anyBuf = nullptr;
    oEndPrim(count, mode, x);
    InterlockedDecrement(&g_inflight);
}
// ---- v9: KH1 sound effects (se_shared.h). The retail build keeps every sound-library debug print as a call to the
// empty stub FUN_14011dc60(fmt, ...) ("SslSePlay : bank=%d no=%d id=%d [%s] -> ", ...). Hooking the stub and keeping
// the sound formats gives every sound KH1 starts: bank, sound number, bank name.
#include "se_shared.h"
#include <intrin.h>
static SeShared* g_se = nullptr;
static const uintptr_t RVA_DBG_PRINT = 0x11DC60;
static const uint8_t DBG_PRINT_BYTES[] = {0x48, 0x89, 0x54, 0x24, 0x10, 0x4c, 0x89, 0x44, 0x24, 0x18, 0x4c, 0x89, 0x4c, 0x24, 0x20, 0xc3};
typedef void(__fastcall* DbgPrint)(const char* fmt, uint64_t a, uint64_t b, uint64_t c, uint64_t d);
static DbgPrint oDbgPrint = nullptr;
struct SeFmt { uintptr_t rva; uint32_t kind; int layout; };   // layout 0: bank no id [name], 1: bank no id, 2: [name], 3: name bank
static const SeFmt SE_FMTS[] = {
    {0x3B2E78, SE_K_PLAY_NAMED, 0}, {0x3B2CE0, SE_K_CREATE2, 0}, {0x3B2E18, SE_K_MAP_PLAY, 0}, {0x3B2D10, SE_K_MAP_CREATE, 0},
    {0x3B2C18, SE_K_CREATE, 0}, {0x3B2D40, SE_K_PLAY, 1}, {0x3B2EA8, SE_K_STOP, 1}, {0x3B3080, SE_K_VOICE, 2},
    {0x3B2A78, SE_K_ENTRY, 3}, {0x3B2BF0, SE_K_BGM, 2},
};
static void se_copy_name(char* dst, uint64_t p) {
    dst[0] = 0;
    if (p < 0x10000) return;
    __try { const char* s = (const char*)p; int i = 0; for (; i < 55 && s[i]; i++) dst[i] = s[i]; dst[i] = 0; }
    __except (EXCEPTION_EXECUTE_HANDLER) { dst[0] = 0; }
}
static void __fastcall hkDbgPrint(const char* fmt, uint64_t a, uint64_t b, uint64_t c, uint64_t d) {
    InterlockedIncrement(&g_inflight);
    if (g_se) {
        g_se->calls++;
        uintptr_t r = (uintptr_t)fmt - g_base;
        if (r >= 0x3B2A00 && r < 0x3B3100) {
            for (const SeFmt& f : SE_FMTS) {
                if (f.rva != r) continue;
                uint32_t w = InterlockedIncrement((volatile LONG*)&g_se->write) - 1;
                SeEvent& e = g_se->ring[w % SE_RING];
                e.seq = 0;
                e.kind = f.kind; e.bank = e.no = e.id = -1;
                e.name[0] = 0;
                if (f.layout <= 1) { e.bank = (int32_t)a; e.no = (int32_t)b; e.id = (int32_t)c; if (f.layout == 0) se_copy_name(e.name, d); }
                else if (f.layout == 2) se_copy_name(e.name, a);
                else { se_copy_name(e.name, a); e.bank = (int32_t)b; }
                e.tid = GetCurrentThreadId();
                e.time_us = now_us();
                e.ret = (uint64_t)_ReturnAddress() - g_base;
                MemoryBarrier();
                e.seq = w + 1;
                break;
            }
        }
    }
    oDbgPrint(fmt, a, b, c, d);
    InterlockedDecrement(&g_inflight);
}
// ---- v10: KH1 lock-on UI sprites (ui_shared.h): FUN_1402764b0(sprite, n, list) turns a 2D sprite into GS packets.
// Copies the sprites submitted by the lock-on UI code (RVA 0x274400-0x275C00: target cursor FUN_140274b20, "LOCK ON!",
// target HP) - their shared sprite buffer can't be read reliably from outside.
static const uintptr_t RVA_SPR_SUBMIT = 0x2764B0;
static const uint8_t SPR_SUBMIT_BYTES[] = {0x40, 0x53, 0x48, 0x83, 0xec, 0x20, 0x45, 0x33, 0xd2};
typedef uint8_t*(__fastcall* SprSubmit)(uint8_t* spr, int n, uint8_t* list);
static SprSubmit oSprSubmit = nullptr;
static uint8_t* __fastcall hkSprSubmit(uint8_t* spr, int n, uint8_t* list) {
    InterlockedIncrement(&g_inflight);
    uintptr_t ra = (uintptr_t)_ReturnAddress() - g_base;
    if (g_ui && ra >= 0x274400 && ra < 0x275C00 && n > 0) {
        __try {
            uint32_t w = InterlockedIncrement((volatile LONG*)&g_ui->write) - 1;
            UiSprite& r = g_ui->ring[w % UI_RING];
            r.seq = 0;
            r.caller = (uint32_t)ra; r.count = (uint32_t)n; r.frame = g_ui->frame; r.time_us = now_us();
            memcpy(r.spr, spr, UI_SPR);
            uint64_t tex0 = *(uint64_t*)spr;
            r.texobj = g_texLookup ? (uint64_t)g_texLookup(&tex0) : 0;
            MemoryBarrier();
            r.seq = w + 1;
        } __except (EXCEPTION_EXECUTE_HANDLER) { if (g_sh) g_sh->faults++; }
    }
    uint32_t prevC = g_subCaller; DWORD prevT = g_subTid;
    g_subCaller = (uint32_t)ra; g_subTid = GetCurrentThreadId();
    uint8_t* res = oSprSubmit(spr, n, list);
    g_subCaller = prevC; g_subTid = prevT;
    InterlockedDecrement(&g_inflight);
    return res;
}
// ---- v16: kh_skel_eval(p1, skel, t, motion) @ 0x391C70 (RE_JOURNAL): every posed model, joint array = skel +0x1B4
// handle. While KH1's main menu is open (0x2E1F470) each call is logged to Local SoraSkel (who, skel, joints).
struct SkelRec { volatile uint32_t seq; uint32_t ret; uint64_t p1, skel, motion, joints; float t; uint32_t menu; int64_t time_us; };
struct SkelShared { uint32_t magic, pad; volatile uint32_t write, enabled; SkelRec r[1024]; };
static SkelShared* g_skel = nullptr;
static const uintptr_t RVA_SKEL_EVAL = 0x391C70;
static const uint8_t SKEL_EVAL_BYTES[] = {0x4c, 0x8b, 0xdc, 0x53, 0x56, 0x41, 0x54, 0x48, 0x81, 0xec, 0xf0, 0x00, 0x00, 0x00};
typedef uint64_t(__fastcall* SkelEval)(void* p1, uint8_t* skel, float t, void* motion);
static SkelEval oSkelEval = nullptr;
static inline uint64_t kh_resolve(uint32_t h) {
    if (!h) return 0;
    uint64_t b = *(uint64_t*)(g_base + 0x2EE3980 + (uint64_t)((h & 0x7FFFFFFF) >> 25) * 8);
    return b | (h & 0x1FFFFFF);
}
static uint64_t __fastcall hkSkelEval(void* p1, uint8_t* skel, float t, void* motion) {
    InterlockedIncrement(&g_inflight);
    uint64_t r = oSkelEval(p1, skel, t, motion);
    if (g_skel && (g_skel->enabled || *(int*)(g_base + 0x2E1F470) == 1)) {
        __try {
            uint32_t i = (uint32_t)InterlockedIncrement((volatile LONG*)&g_skel->write) - 1;
            SkelRec& o = g_skel->r[i % 1024];
            o.seq = 0; o.ret = (uint32_t)((uintptr_t)_ReturnAddress() - g_base);
            o.p1 = (uint64_t)p1; o.skel = (uint64_t)skel; o.motion = (uint64_t)motion; o.t = t;
            o.joints = skel ? kh_resolve(*(uint32_t*)(skel + 0x1B4)) : 0;
            o.menu = *(uint32_t*)(g_base + 0x2E1F470); o.time_us = now_us();
            o.seq = i + 1;
        } __except (EXCEPTION_EXECUTE_HANDLER) {}
    }
    InterlockedDecrement(&g_inflight);
    return r;
}

// ---- v17: every KH1 sound starts through FUN_1400598c0(snd = entry + 0x10, float, float). Called from the Ssl* sound
// effect / voice functions (SslSeCreate 0xde606, SslSeCreate2 0xde387, SslSePlay 0xdec16 / 0x118591 / 0x118851,
// SslSeVoiceCreate 0xdf3d1, IOP voice 0xdbde3, Voice Call 0xdc53d) and from the music / stream player (AxaSoundStream
// 0xe0ce7, ...). With se_mute on, a start from an effect / voice site is faded to silence at once with KH1's own fade
// (FUN_140056e60, like the BGM volume) - Borderlands plays those sounds itself (Local\SoraSe) - so KH1 can stay unmuted
// for its music. Every call site is counted (sites[]) to classify paths not listed yet.
struct SndSite { uint32_t ret, count, muted, pad; };
struct SndShared { uint32_t magic; volatile uint32_t se_mute; volatile uint32_t nsite; volatile uint32_t calls; SndSite sites[64];
                   volatile uint32_t walk_frames, walk_faded;      // v19 @1040
                   volatile uint32_t resume_muted;                 // v21 @1048
                   volatile uint32_t amb_stopped; };               // v34 @1052
static SndShared* g_snd = nullptr;
static const uintptr_t RVA_SND_PLAY = 0x598C0;
static const uint8_t SND_PLAY_BYTES[] = {0x40, 0x53, 0x56, 0x57, 0x48, 0x83, 0xec, 0x70, 0x48, 0xc7, 0x44, 0x24, 0x20, 0xfe, 0xff, 0xff};
typedef uint64_t(__fastcall* SndPlay)(void* snd, float a, float b);
static SndPlay oSndPlay = nullptr;
static const uint32_t SE_SITES[] = {0xde606, 0xde387, 0xdec16, 0x118591, 0x118851, 0xdf3d1, 0xdbde3, 0xdc53d};
static uint64_t __fastcall hkSndPlay(void* snd, float a, float b) {
    InterlockedIncrement(&g_inflight);
    uint32_t ret = (uint32_t)((uintptr_t)_ReturnAddress() - g_base);
    typedef void(__fastcall* Fade)(void* snd, float vol, int frames);
    bool se = false; for (uint32_t s : SE_SITES) if (s == ret) se = true;
    bool mute = g_snd && se && g_snd->se_mute && snd;
    if (mute) { __try { ((Fade)(g_base + 0x56E60))(snd, 0.0f, 0); } __except (EXCEPTION_EXECUTE_HANDLER) {} }   // v18: silent from the start
    uint64_t r = oSndPlay(snd, a, b);
    if (g_snd) {
        __try {
            if (mute) ((Fade)(g_base + 0x56E60))(snd, 0.0f, 0);
            g_snd->calls++;
            uint32_t n = g_snd->nsite, k = 0;
            for (; k < n && k < 64; k++) if (g_snd->sites[k].ret == ret) break;
            if (k == n && n < 64) { g_snd->sites[n].ret = ret; g_snd->nsite = n + 1; }
            if (k < 64) { g_snd->sites[k].count++; if (mute) g_snd->sites[k].muted++; }
        } __except (EXCEPTION_EXECUTE_HANDLER) {}
    }
    InterlockedDecrement(&g_inflight);
    return r;
}

// v20: room ambience loops (Traverse Town 3rd District: se021059 high buzz, se021008 crackle, se021003 wind) keep getting
// their volume re-set by KH1's ambience manager after our per-frame fade -> hook KH1's fade itself: any volume set on a
// sound-list entry that is an effect (+0x3C != 0) becomes 0 while se_mute; music entries and non-list objects untouched.
typedef void(__fastcall* FadeFn)(void* snd, float vol, int frames);
static FadeFn oFade = nullptr;
static const uintptr_t RVA_FADE = 0x56E60;
static const uint8_t FADE_BYTES[] = {0x40, 0x57, 0x48, 0x83, 0xec, 0x40, 0x48, 0xc7, 0x44, 0x24, 0x20, 0xfe, 0xff, 0xff, 0xff};
static bool snd_is_effect(void* snd) {
    __try {
        int n = 0;
        for (uint8_t* e = *(uint8_t**)(g_base + 0x4D6618); e && n < 256; e = *(uint8_t**)(e + 8), n++)
            if (e + 0x10 == (uint8_t*)snd) return *(int*)(e + 0x3C) != 0;
    } __except (EXCEPTION_EXECUTE_HANDLER) {}
    return false;
}
static void __fastcall hkFade(void* snd, float vol, int frames) {
    InterlockedIncrement(&g_inflight);
    if (g_snd && g_snd->se_mute && vol > 0.0f && snd_is_effect(snd)) { vol = 0.0f; g_snd->walk_faded++; }
    oFade(snd, vol, frames);
    InterlockedDecrement(&g_inflight);
}

// v21: SslSePlay on an EXISTING list entry (Shadows' sink/emerge cycle se003015/016/001, every ~10 s) never reaches
// FUN_1400598c0: it goes FUN_140056040 -> FUN_140056070(snd, vol, frames) = (re)start of a list entry. Same treatment
// as v18: an effect entry is faded to 0 before and after the start while se_mute.
typedef uint32_t(__fastcall* SndResume)(void* snd, int vol, int frames, void* p4);
static SndResume oSndResume = nullptr;
static const uintptr_t RVA_SND_RESUME = 0x56070;
static const uint8_t SND_RESUME_BYTES[] = {0x40, 0x55, 0x56, 0x57, 0x48, 0x83, 0xec, 0x30, 0x48, 0xc7, 0x44, 0x24, 0x20, 0xfe, 0xff, 0xff, 0xff};
// v33: a (re)start of an existing list entry never reaches the printf stub, so Borderlands never heard it (user: no
// spell CAST sound, only Sora's voice - KH1's spell sounds restart their loaded se000 entries). Log it into
// Local\SoraSe as SE_K_RESUME with the entry's bank (+0x30 from the entry = snd + 0x20), sound number (+0x34) and
// emitter id (+0x38); name = the bank file Borderlands keys its clips by (bank 2 = se000, bank -1 = one-sound file).
static void se_log_resume(void* snd) {
    if (!g_se || !snd) return;
    __try {
        const uint8_t* e = (const uint8_t*)snd - 0x10;
        int32_t bank = *(const int32_t*)(e + 0x30), no = *(const int32_t*)(e + 0x34), id = *(const int32_t*)(e + 0x38);
        int32_t type = *(const int32_t*)(e + 0x3C);
        if (type != 1 && type != 3) return;                            // music entry / not a sound-list entry (v35)
        if (bank < -1 || bank > 64 || no < 0) return;                  // v35: other callers pass non-list objects
        uint32_t w = InterlockedIncrement((volatile LONG*)&g_se->write) - 1;
        SeEvent& ev = g_se->ring[w % SE_RING];
        ev.seq = 0; ev.kind = SE_K_RESUME; ev.bank = bank; ev.no = no; ev.id = id; ev.name[0] = 0;
        if (bank == 2) memcpy(ev.name, "se000.win32.scd", 16);
        else if (bank < 0 && no > 0 && no < 1000000) {
            char* d = ev.name; const char* pre = "se"; for (int i = 0; i < 2; i++) *d++ = pre[i];
            char dig[6]; int v = no; for (int i = 5; i >= 0; i--) { dig[i] = (char)('0' + v % 10); v /= 10; }
            for (int i = 0; i < 6; i++) *d++ = dig[i];
            memcpy(d, ".win32.scd", 11);
        }
        ev.tid = GetCurrentThreadId(); ev.time_us = now_us(); ev.ret = (uint64_t)_ReturnAddress() - g_base;
        MemoryBarrier();
        ev.seq = w + 1;
    } __except (EXCEPTION_EXECUTE_HANDLER) {}
}
static uint32_t __fastcall hkSndResume(void* snd, int vol, int frames, void* p4) {
    InterlockedIncrement(&g_inflight);
    se_log_resume(snd);
    bool mute = g_snd && g_snd->se_mute && snd && snd_is_effect(snd);
    // v35: VOICE entries (type 3) must really (re)start, silently: refusing them left KH1's voice system "busy" forever
    // and KH1's summon waits for it before registering the summon's voice bank (FUN_140296480 / FUN_140178a30) - the
    // user's first Simba summon froze on its intro (2026-10-06). Effects (type 1) keep the v25 refusal.
    bool voice = false;
    if (mute) { __try { voice = *(const int*)((const uint8_t*)snd - 0x10 + 0x3C) == 3; } __except (EXCEPTION_EXECUTE_HANDLER) {} }
    if (mute && !voice) {   // v25: don't (re)start it at all - fading right after the start still let a short burst out (user)
        __try { oFade(snd, 0.0f, 0); g_snd->resume_muted++; } __except (EXCEPTION_EXECUTE_HANDLER) {}
        InterlockedDecrement(&g_inflight);
        return 0;
    }
    if (mute) { __try { oFade(snd, 0.0f, 0); } __except (EXCEPTION_EXECUTE_HANDLER) {} }   // voice: silent from the start
    uint32_t r = oSndResume(snd, vol, frames, p4);
    if (mute) { __try { oFade(snd, 0.0f, 0); g_snd->resume_muted++; } __except (EXCEPTION_EXECUTE_HANDLER) {} }
    InterlockedDecrement(&g_inflight);
    return r;
}

// v19: positional / looping effects (e.g. a room's electric sparks) get their volume recomputed by KH1 every frame,
// overriding the fade at start: once per KH1 frame every playing non-music entry of the sound list (0x4D6618,
// entry +0x3C != 0; music = 0) is faded to silence again.
static void snd_mute_walk() {
    if (!g_snd || !g_snd->se_mute) return;
    __try {
        typedef void(__fastcall* Fade)(void* snd, float vol, int frames);
        int n = 0, f = 0;
        // v34: Traverse Town room ambience loops keep sounding at volume 0 (user: fountain water = se021008, matched to
        // KH1's own output, NCC 0.72) -> STOP them with KH1's SslSeStop(bank, no, id) (FUN_1400def50: matches entry
        // +0x30 / +0x34 / +0x38, type 1, frees it), after the walk (it edits the list). A restart is blocked by v25.
        static const int AMB_STOP[] = {21008, 21003, 21059};
        int stop_no[8], stop_id[8], ns = 0;
        for (uint8_t* e = *(uint8_t**)(g_base + 0x4D6618); e && n < 256; e = *(uint8_t**)(e + 8), n++) {
            if (*(int*)(e + 0x3C) != 0) { ((Fade)(g_base + 0x56E60))(e + 0x10, 0.0f, 0); f++; }
            if (*(int*)(e + 0x3C) == 1 && *(int*)(e + 0x30) == -1 && ns < 8)
                for (int a : AMB_STOP) if (*(int*)(e + 0x34) == a) { stop_no[ns] = a; stop_id[ns] = *(int*)(e + 0x38); ns++; }
        }
        typedef uint64_t(__fastcall* SeStop)(int bank, int no, int id);
        for (int k = 0; k < ns; k++) { ((SeStop)(g_base + 0xDEF50))(-1, stop_no[k], stop_id[k]); g_snd->amb_stopped++; }
        g_snd->walk_frames++; g_snd->walk_faded += f;
    } __except (EXCEPTION_EXECUTE_HANDLER) {}
}

static HMODULE g_self = nullptr;
static void __fastcall hkShapeDraw(uint8_t* packet, void* batch) {
    InterlockedIncrement(&g_inflight);
    if (g_sh && (g_sh->blank & 2)) { g_sh->blank_dropped++; InterlockedDecrement(&g_inflight); return; }
    oShapeDraw(packet, batch);                                   // KH1 draws it as always
    __try { stack_log("spr"); } __except (EXCEPTION_EXECUTE_HANDLER) {}
    if (g_sh && g_sh->enabled) {
        __try { capture(packet); } __except (EXCEPTION_EXECUTE_HANDLER) { g_sh->faults++; }
    }
    InterlockedDecrement(&g_inflight);
}

// ---- v40: KH1 EFFECT FRAMES (fx_frame.h). FUN_1401ecf30 = KH1's per-frame pass over all effect programs (each one via
// FUN_14031c4f0); every effect sprite / model draw sits inside it (stacks recorded 2026-10-12). At its end the records
// it produced in both rings + the camera are published so Borderlands draws whole KH1 frames.
#include "fx_frame.h"
static FxFrame* g_frm = nullptr;
static const uintptr_t RVA_FX_PASS = 0x1ECF30;
static const uint8_t FX_PASS_BYTES[] = {0x48, 0x8b, 0xc4, 0x48, 0x89, 0x58, 0x08, 0x48, 0x89, 0x70, 0x10, 0x48, 0x89, 0x78, 0x18};
typedef void(__fastcall* FxPass)(void* a1, void* a2, void* a3, void* a4);   // void(void); pass every register through
static FxPass oFxPass = nullptr;
static uint32_t g_frmSpr = 0, g_frmMdl = 0; static bool g_frmInit = false;
static void frame_publish() {
    if (!g_frm || !g_sh || !g_mdl) return;
    uint32_t se = g_sh->write, me = g_mdl->write;
    if (!g_frmInit) { g_frmSpr = se; g_frmMdl = me; g_frmInit = true; return; }
    g_frm->seq++; MemoryBarrier();
    g_frm->spr_begin = g_frmSpr; g_frm->spr_end = se; g_frm->mdl_begin = g_frmMdl; g_frm->mdl_end = me;
    memcpy(g_frm->view, (void*)(g_base + 0x2E9D480), 64); memcpy(g_frm->proj, (void*)(g_base + 0x2E9D600), 64);
    g_frm->time_us = now_us(); g_frm->frame++;
    MemoryBarrier(); g_frm->seq++;
    g_frmSpr = se; g_frmMdl = me;
}
static void __fastcall hkFxPass(void* a1, void* a2, void* a3, void* a4) {
    InterlockedIncrement(&g_inflight);
    oFxPass(a1, a2, a3, a4);
    __try { frame_publish(); } __except (EXCEPTION_EXECUTE_HANDLER) {}
    InterlockedDecrement(&g_inflight);
}

static DWORD WINAPI init_thread(LPVOID) {
    g_base = (uintptr_t)GetModuleHandleW(nullptr);
    crash_init("KH1", "kh1_fx v44");
    QueryPerformanceFrequency(&g_qpf);
    HANDLE m = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0, sizeof(FxShared), FX_SHARED_NAME);
    if (!m) { logf("CreateFileMapping failed %lu", GetLastError()); return 0; }
    g_sh = (FxShared*)MapViewOfFile(m, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(FxShared));
    if (!g_sh) { logf("MapViewOfFile failed %lu", GetLastError()); return 0; }
    g_sh->magic = FX_MAGIC; g_sh->version = FX_VERSION; g_sh->ring_size = FX_RING; g_sh->rec_size = sizeof(FxQuad);
    g_texLookup = (TexLookup)(g_base + RVA_TEX_LOOKUP);
    MH_Initialize();
    void* target = (void*)(g_base + RVA_SHAPE_DRAW);
    logf("---- kh1_fx v%u: base %p", FX_VERSION, (void*)g_base);
    if (memcmp(target, SHAPE_DRAW_BYTES, sizeof SHAPE_DRAW_BYTES) != 0) { logf("shape_draw: bytes differ - not hooked"); return 0; }
    bool ok = MH_CreateHook(target, (void*)&hkShapeDraw, (void**)&oShapeDraw) == MH_OK && MH_EnableHook(target) == MH_OK;
    g_sh->hooked = ok ? 1 : 0;
    logf("shape_draw hook %s", ok ? "installed" : "FAILED");
    void* t2 = (void*)(g_base + RVA_CMD_EXEC);
    g_sh->cmd_done = g_sh->cmd_req;                               // nothing stale from before this DLL
    if (memcmp(t2, CMD_EXEC_BYTES, sizeof CMD_EXEC_BYTES) == 0) {
        bool ok2 = MH_CreateHook(t2, (void*)&hkCmdExec, (void**)&oCmdExec) == MH_OK && MH_EnableHook(t2) == MH_OK;
        if (ok2) g_sh->hooked |= 2;
        logf("cmd_exec hook %s", ok2 ? "installed" : "FAILED");
    } else logf("cmd_exec: bytes differ - not hooked");
    {   // chains on kh1_bridge's hook of the same function (MinHook relocates its leading jmp)
        void* t3 = (void*)(g_base + RVA_GATHER);
        bool ok3 = MH_CreateHook(t3, (void*)&hkGather, (void**)&oGather) == MH_OK && MH_EnableHook(t3) == MH_OK;
        if (ok3) g_sh->hooked |= 4;
        logf("gather hook %s (first byte %02x)", ok3 ? "installed" : "FAILED", *(uint8_t*)t3);
    }
    {   // v40: effect frames (needs g_sh; g_mdl is mapped just below - frame_publish waits for both)
        HANDLE fm = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0, sizeof(FxFrame), FXF_SHARED_NAME);
        g_frm = fm ? (FxFrame*)MapViewOfFile(fm, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(FxFrame)) : nullptr;
        void* fp = (void*)(g_base + RVA_FX_PASS);
        if (g_frm) { g_frm->magic = FXF_MAGIC; if (g_frm->seq & 1) g_frm->seq++; }
        if (g_frm && !memcmp(fp, FX_PASS_BYTES, sizeof FX_PASS_BYTES)) {
            bool okf = MH_CreateHook(fp, (void*)&hkFxPass, (void**)&oFxPass) == MH_OK && MH_EnableHook(fp) == MH_OK;
            logf("effect frame hook %s", okf ? "installed" : "FAILED");
        } else logf("effect frame: mapping or bytes differ - not hooked");
    }
    {   // v8: effect models
        HANDLE mm = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0, sizeof(FxMdlShared), FXM_SHARED_NAME);
        g_mdl = mm ? (FxMdlShared*)MapViewOfFile(mm, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(FxMdlShared)) : nullptr;
        if (g_mdl) { g_mdl->magic = FXM_MAGIC; g_mdl->version = FX_VERSION; g_mdl->nbatch = FXM_BATCHES; g_mdl->nvert = FXM_VERTS; }
        void* a = (void*)(g_base + RVA_MDL_RUN); void* bp = (void*)(g_base + RVA_BEGIN_PRIM); void* ep = (void*)(g_base + RVA_END_PRIM);
        if (g_mdl && !memcmp(a, MDL_RUN_BYTES, sizeof MDL_RUN_BYTES) && !memcmp(bp, PRIM_BYTES, sizeof PRIM_BYTES) && !memcmp(ep, PRIM_BYTES, sizeof PRIM_BYTES)) {
            bool o1 = MH_CreateHook(bp, (void*)&hkBeginPrim, (void**)&oBeginPrim) == MH_OK;
            bool o2 = MH_CreateHook(ep, (void*)&hkEndPrim, (void**)&oEndPrim) == MH_OK;
            bool o3 = MH_CreateHook(a, (void*)&hkMdlRun, (void**)&oMdlRun) == MH_OK;
            bool ok4 = o1 && o2 && o3 && MH_EnableHook(bp) == MH_OK && MH_EnableHook(ep) == MH_OK && MH_EnableHook(a) == MH_OK;
            if (ok4) g_sh->hooked |= 8;
            logf("model hooks %s", ok4 ? "installed" : "FAILED");
        } else logf("model hooks: mapping or bytes differ - not hooked");
    }
    {   // v9: sound effects
        HANDLE sm = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0, sizeof(SeShared), SE_SHARED_NAME);
        g_se = sm ? (SeShared*)MapViewOfFile(sm, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(SeShared)) : nullptr;
        if (g_se) { g_se->magic = SE_MAGIC; g_se->ring_size = SE_RING; g_se->rec_size = sizeof(SeEvent); }
        void* t = (void*)(g_base + RVA_DBG_PRINT);
        if (g_se && !memcmp(t, DBG_PRINT_BYTES, sizeof DBG_PRINT_BYTES)) {
            bool ok5 = MH_CreateHook(t, (void*)&hkDbgPrint, (void**)&oDbgPrint) == MH_OK && MH_EnableHook(t) == MH_OK;
            if (ok5) g_sh->hooked |= 16;
            logf("sound hook %s", ok5 ? "installed" : "FAILED");
        } else logf("sound hook: mapping or bytes differ - not hooked");
    }
    {   // v10: lock-on UI sprites
        HANDLE um = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0, sizeof(UiShared), UI_SHARED_NAME);
        g_ui = um ? (UiShared*)MapViewOfFile(um, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(UiShared)) : nullptr;
        if (g_ui) { g_ui->magic = UI_MAGIC; g_ui->ring_size = UI_RING; g_ui->rec_size = sizeof(UiSprite); }
        void* t = (void*)(g_base + RVA_SPR_SUBMIT);
        if (g_ui && !memcmp(t, SPR_SUBMIT_BYTES, sizeof SPR_SUBMIT_BYTES)) {
            bool ok6 = MH_CreateHook(t, (void*)&hkSprSubmit, (void**)&oSprSubmit) == MH_OK && MH_EnableHook(t) == MH_OK;
            if (ok6) g_sh->hooked |= 32;
            logf("ui sprite hook %s", ok6 ? "installed" : "FAILED");
        } else logf("ui sprite hook: mapping or bytes differ - not hooked");
    }
    {   // v14: UI primitive probe
        HANDLE pm = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0, sizeof(UiProbe), L"Local\\SoraUiProbe");
        g_probe = pm ? (UiProbe*)MapViewOfFile(pm, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(UiProbe)) : nullptr;
        if (g_probe) { g_probe->magic = 0x50495553; g_probe->recsize = sizeof(UiProbeRec); }
        logf("ui probe %s", g_probe ? "mapped" : "FAILED");
    }
    {   // v15: KH1 2D interface capture
        HANDLE km = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0, sizeof(KuiShared), KUI_SHARED_NAME);
        g_kui = km ? (KuiShared*)MapViewOfFile(km, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(KuiShared)) : nullptr;
        if (g_kui) { g_kui->magic = KUI_MAGIC; g_kui->version = 15; g_kui->nbatch = KUI_BATCHES; g_kui->nvert = KUI_VERTS; }
        logf("ui capture %s", g_kui ? "mapped" : "FAILED");
    }
    {   // v16: skeleton evaluations (menu party models probe)
        HANDLE sm2 = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0, sizeof(SkelShared), L"Local\\SoraSkel");
        g_skel = sm2 ? (SkelShared*)MapViewOfFile(sm2, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(SkelShared)) : nullptr;
        void* t = (void*)(g_base + RVA_SKEL_EVAL);
        if (g_skel && !memcmp(t, SKEL_EVAL_BYTES, sizeof SKEL_EVAL_BYTES)) {
            g_skel->magic = 0x4C4B5353;
            bool ok7 = MH_CreateHook(t, (void*)&hkSkelEval, (void**)&oSkelEval) == MH_OK && MH_EnableHook(t) == MH_OK;
            if (ok7) g_sh->hooked |= 64;
            logf("skel hook %s", ok7 ? "installed" : "FAILED");
        } else logf("skel hook: mapping or bytes differ - not hooked");
    }
    {   // v17: KH1 sound starts (mute KH1's sound effects / voices at the source, keep its music)
        HANDLE pm2 = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0, sizeof(SndShared), L"Local\\SoraSnd");
        g_snd = pm2 ? (SndShared*)MapViewOfFile(pm2, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(SndShared)) : nullptr;
        void* t = (void*)(g_base + RVA_SND_PLAY);
        if (g_snd && !memcmp(t, SND_PLAY_BYTES, sizeof SND_PLAY_BYTES)) {
            g_snd->magic = 0x444E5353;
            bool ok8 = MH_CreateHook(t, (void*)&hkSndPlay, (void**)&oSndPlay) == MH_OK && MH_EnableHook(t) == MH_OK;
            if (ok8) g_sh->hooked |= 128;
            logf("sound play hook %s", ok8 ? "installed" : "FAILED");
            void* tf = (void*)(g_base + RVA_FADE);
            if (!memcmp(tf, FADE_BYTES, sizeof FADE_BYTES)) {
                bool ok9 = MH_CreateHook(tf, (void*)&hkFade, (void**)&oFade) == MH_OK && MH_EnableHook(tf) == MH_OK;
                logf("sound fade hook %s", ok9 ? "installed" : "FAILED");
                void* tr = (void*)(g_base + RVA_SND_RESUME);
                if (ok9 && !memcmp(tr, SND_RESUME_BYTES, sizeof SND_RESUME_BYTES)) {
                    bool ok10 = MH_CreateHook(tr, (void*)&hkSndResume, (void**)&oSndResume) == MH_OK && MH_EnableHook(tr) == MH_OK;
                    logf("sound resume hook %s", ok10 ? "installed" : "FAILED");
                } else logf("sound resume hook: fade not hooked or bytes differ - not hooked");
            } else logf("sound fade hook: bytes differ - not hooked");
        } else logf("sound play hook: mapping or bytes differ - not hooked");
    }
    {   // v22: level link
        HANDLE lm = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0, sizeof(LvlShared), L"Local\\SoraLvl");
        LvlShared* l = lm ? (LvlShared*)MapViewOfFile(lm, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(LvlShared)) : nullptr;
        if (l) { l->magic = 0x4C564C53; l->done = l->req; l->lo_done = l->lo_req; g_lvl = l; }   // nothing stale from before this DLL
        logf("level link %s", g_lvl ? "mapped" : "FAILED");
        void* te = (void*)(g_base + RVA_EXP_PARTY);
        if (g_lvl && !memcmp(te, EXP_PARTY_BYTES, sizeof EXP_PARTY_BYTES)) {
            bool oke = MH_CreateHook(te, (void*)&hkExpParty, (void**)&oExpParty) == MH_OK && MH_EnableHook(te) == MH_OK;
            logf("kill exp hook %s", oke ? "installed" : "FAILED");
        } else logf("kill exp hook: bytes differ (%02x %02x %02x %02x) - not hooked", ((uint8_t*)te)[0], ((uint8_t*)te)[1], ((uint8_t*)te)[2], ((uint8_t*)te)[3]);
    }
    {   // v24: hit records
        HANDLE hm = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0, sizeof(HitShared), L"Local\\SoraHits");
        HitShared* hs = hm ? (HitShared*)MapViewOfFile(hm, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(HitShared)) : nullptr;
        void* t = (void*)(g_base + RVA_HIT_PROC);
        if (hs && !memcmp(t, HIT_PROC_BYTES, sizeof HIT_PROC_BYTES)) {
            hs->magic = 0x53544948; hs->done = hs->req; g_hits = hs;
            HANDLE sm3 = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0, sizeof(StandShared), L"Local\\SoraStand");
            StandShared* ss = sm3 ? (StandShared*)MapViewOfFile(sm3, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(StandShared)) : nullptr;
            if (ss) { ss->n = 32; ss->magic = 0x444E5453; g_stand = ss; }
            logf("stand-in hp floor %s", ss ? "mapped" : "FAILED");
            HANDLE pzm = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0, sizeof(PrizeShared), L"Local\\SoraPrize");
            PrizeShared* pz = pzm ? (PrizeShared*)MapViewOfFile(pzm, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(PrizeShared)) : nullptr;
            void* tp = (void*)(g_base + RVA_PRIZE);
            if (pz && !memcmp(tp, PRIZE_BYTES, sizeof PRIZE_BYTES)) {
                pz->done = pz->req;
                bool okz = MH_CreateHook(tp, (void*)&hkPrize, (void**)&oPrize) == MH_OK && MH_EnableHook(tp) == MH_OK;
                if (okz) { pz->magic = 0x5A495250; g_prize = pz; }
                logf("prize hook %s", okz ? "installed" : "FAILED");
            } else logf("prize hook: mapping or bytes differ - not hooked");
            bool okh =MH_CreateHook(t, (void*)&hkHitProc, (void**)&oHitProc) == MH_OK && MH_EnableHook(t) == MH_OK;
            logf("hit record hook %s", okh ? "installed" : "FAILED");
            void* th = (void*)(g_base + RVA_HP_ADD);
            if (!memcmp(th, HP_ADD_BYTES, sizeof HP_ADD_BYTES)) {
                bool okp = MH_CreateHook(th, (void*)&hkHpAdd, (void**)&oHpAdd) == MH_OK && MH_EnableHook(th) == MH_OK;
                logf("sora hp floor hook %s", okp ? "installed" : "FAILED");
            } else logf("sora hp floor hook: bytes differ - not hooked");
            void* td = (void*)(g_base + RVA_HIT_DIRECT);
            if (!memcmp(td, HIT_DIRECT_BYTES, sizeof HIT_DIRECT_BYTES)) {
                bool okd = MH_CreateHook(td, (void*)&hkHitDirect, (void**)&oHitDirect) == MH_OK && MH_EnableHook(td) == MH_OK;
                logf("direct hit hook %s", okd ? "installed" : "FAILED");
            } else logf("direct hit hook: bytes differ - not hooked");
        } else logf("hit records: mapping or bytes differ - not hooked");
    }
    {   // v23: write watch (RE helper)
        HANDLE wm = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0, sizeof(WatchShared), L"Local\\SoraWatch");
        g_watch = wm ? (WatchShared*)MapViewOfFile(wm, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(WatchShared)) : nullptr;
        if (g_watch) { g_watch->magic = 0x48435457; g_watch->armed = 0; g_vehHandle = AddVectoredExceptionHandler(1, watch_veh); }
        logf("write watch %s", g_watch ? "mapped" : "FAILED");
    }
    {   // v38: guard (Borderlands heartbeat -> restore the loadout + pause KH1 when Borderlands is gone)
        HANDLE gm = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0, sizeof(GuardShared), L"Local\\SoraGuard");
        g_guard = gm ? (GuardShared*)MapViewOfFile(gm, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(GuardShared)) : nullptr;
        if (g_guard) { g_guard->state = 0; g_guard->dll_alive = 1; }
        HANDLE pm3 = OpenFileMappingW(FILE_MAP_ALL_ACCESS, FALSE, L"Local\\SoraPassthrough");
        g_pt = pm3 ? (volatile uint8_t*)MapViewOfFile(pm3, FILE_MAP_ALL_ACCESS, 0, 0, 256) : nullptr;
        logf("guard %s, bridge pad %s", g_guard ? "mapped" : "FAILED", g_pt ? "mapped" : "missing");
    }
    // hot swap: Borderlands sets unload=1 -> unhook, wait until no KH1 thread is inside the hook, free this DLL
    while (!g_sh->unload) { watch_poll(); guard_poll_safe(); crash_tick(); Sleep(100); }
    if (g_press_step) guard_pad(false, 0);
    if (g_watch && g_watch->armed) { watch_apply(0, false); g_watch->armed = 0; }
    if (g_vehHandle) { RemoveVectoredExceptionHandler(g_vehHandle); g_vehHandle = nullptr; }
    crash_shutdown(false);                                         // hot swap: the next kh1_fx continues the session
    MH_DisableHook(MH_ALL_HOOKS);
    Sleep(1000);   // v43: most hooks are not in g_inflight - give threads still inside a detour time to leave (a v41 -> v42 swap crashed KH1 returning into freed code)
    for (int i = 0; i < 200 && g_inflight; i++) Sleep(10);
    MH_Uninitialize();
    g_sh->hooked = 0; g_sh->unload = 0;
    logf("unloaded");
    FreeLibraryAndExitThread(g_self, 0);
    return 0;
}

BOOL WINAPI DllMain(HINSTANCE h, DWORD reason, LPVOID reserved) {
    if (reason == DLL_PROCESS_DETACH && reserved && g_crash) g_crash->open = 0;   // KH1 exiting normally (not a hot swap)
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(h); g_self = h;
        GetModuleFileNameA(h, g_dir, MAX_PATH); char* s = strrchr(g_dir, '\\'); if (s) *s = 0;
        CloseHandle(CreateThread(nullptr, 0, init_thread, nullptr, 0, nullptr));
    }
    return TRUE;
}
