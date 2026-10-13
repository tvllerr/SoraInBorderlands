// Shared memory "Local\SoraMapFeed": Borderlands (logic.py, game thread) -> kh1_mapfeed.dll (in KH1).
// Borderlands samples its ground (native UWorld::SingleLineCheck) on a grid around Sora; the DLL turns the cells
// near each fed KH1 entity into KH1 collision polygons during that entity's collision query.
// Keep in sync with logic.py (MF_* offsets).
#pragma once
#include <cstdint>
#define MF_NAME L"Local\\SoraMapFeed"
#define MF_MAGIC 0x31464D53u   // 'SMF1'
#define MF_MAX_ENT 16
#define MF_MAX_N 96
#pragma pack(push, 1)
struct FeedShared {
    uint32_t magic;               // @0
    uint32_t version;             // @4   DLL version
    volatile uint32_t hooked;     // @8   DLL: 1 = hooks on
    volatile uint32_t unload;     // @12  BL: 1 = unhook + free the DLL (hot swap)
    volatile uint32_t enabled;    // @16  BL: 1 = feed the listed entities
    volatile uint32_t seq;        // @20  BL seqlock: odd while writing the fields below
    volatile uint32_t nent;       // @24
    uint32_t pad0;                // @28
    volatile uint64_t ent[MF_MAX_ENT];   // @32 KH1 entities that collide with Borderlands' ground
    float cell;                   // @160 BL units per grid cell
    int32_t gx0, gy0;             // @164 grid vertex (0,0) = BL (gx0 * cell, gy0 * cell)
    int32_t n;                    // @172 grid is n x n vertices
    float Fx, Fy;                 // @176 BL anchor (BL Sora feet x, y)
    float Kx, Kz;                 // @184 KH1 anchor (KH1 Sora x, z)
    float m00, m01, m10, m11;     // @192 BL offset = M * KH1 offset (x, z)
    float ref_bz, ref_ky, sc;     // @208 KH1 y = ref_ky - (BL z - ref_bz) / sc   (KH1 y grows down)
    volatile uint32_t q_fed;      // @220 DLL stats: fed queries
    volatile uint32_t q_polys;    // @224 polygons handed out (total)
    volatile uint32_t q_empty;    // @228 fed queries with no known ground cells
    volatile uint32_t gathers;    // @232 gathers replaced
    volatile int32_t floor_poly;  // @236 room polygon whose flags our polygons copy (-1 none)
    volatile uint32_t last_polys; // @240 polygons in the last fed gather
    uint32_t max_cells;           // @244 BL: cap per gather (0 -> 400)
    volatile uint32_t faults;     // @248 DLL: exceptions caught in the hooks
    uint32_t pad1;                // @252
    float h[MF_MAX_N * MF_MAX_N]; // @256 BL ground z per grid vertex [j * n + i] (NaN = unknown)
};
#pragma pack(pop)
static_assert(sizeof(FeedShared) == 256 + 4 * MF_MAX_N * MF_MAX_N, "FeedShared layout");
