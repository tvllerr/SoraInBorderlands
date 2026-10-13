// Shared memory between kh1_fx.dll (KH1) and Borderlands (sora_render.dll / logic.py): KH1's drawn effect quads.
#pragma once
#include <cstdint>
#define FX_SHARED_NAME L"Local\\SoraFx"
#define FX_MAGIC 0x31584653u   // 'SFX1'
#define FX_VERSION 13u
#define FX_RING 8192
#pragma pack(push, 1)
struct FxQuad {                 // 224 bytes
    volatile uint32_t seq;      // @0   write index + 1 once complete (0 = being written)
    uint32_t pk_flags;          // @4   packet +0x38
    float m[16];                // @8   KH1 world matrix (row vectors: p' = p * M)
    float col[4];               // @72  rgba, PS2 scale (128 = 1.0)
    float xy[8];                // @88  4 corners in the shape's plane (z = 0), KH1 units
    float uv[4];                // @120 u0 v0 u1 v1 in PS2 texels
    uint64_t tex0;              // @136 GS TEX0
    uint64_t texobj;            // @144 Axa::EntryTex* in KH1 (0 = none)
    float qf;                   // @152 per-quad float
    uint8_t b4, b5, clut, shape_flags;   // @156
    uint8_t gs[48];             // @160 GS context registers at draw time (+0 ALPHA, +8 TEX1, +0x20 TEST?, +0x28 TEX0)
    int64_t time_us;            // @208
    uint64_t shape;             // @216
};
struct FxShared {
    uint32_t magic, version, ring_size, rec_size;   // @0
    volatile uint32_t enabled;  // @16 Borderlands sets 1 to capture
    volatile uint32_t hooked;   // @20
    volatile uint32_t write;    // @24 next ring slot (monotonic)
    volatile uint32_t faults;   // @28
    volatile uint64_t quads_total;   // @32
    volatile uint32_t unload;   // @40 1 = unhook and free kh1_fx.dll (hot swap); the DLL clears it when done
    volatile uint32_t cam_seq;  // @44 bumped after each camera copy below
    volatile uint32_t alt_cam;  // @48 KH1 used its fixed matrix path (0x2E9D420 +0xDC != 0) for the last packet
    volatile uint32_t blank;    // @52 v13: bit0 = drop KH1's generic EndPrim draws, bit1 = drop effect sprites (tests)
    volatile uint32_t blank_dropped;   // @56 draws dropped
    uint8_t reserved[4];        // @60 .. 64
    float view[16];             // @64  KH1 0x2E9D480 (PS2 world -> camera)
    float proj[16];             // @128 KH1 0x2E9D600 (PS2 camera -> screen); world = M_packet * inverse(view * proj)
    // v3: KH1 command injection (cast a spell the way KH1's own menu does: command word 0x2D53A40 + target 0x2D53A44,
    // written for KH1 Sora right before KH1's executor FUN_1402a7510 reads it)
    volatile uint32_t cmd_req;    // @192 Borderlands bumps to request cmd_word
    volatile uint32_t cmd_done;   // @196 DLL sets = cmd_req once written into KH1
    volatile uint32_t cmd_word;   // @200 e.g. 0x2000000 | spell << 8 | 2 (magic)
    volatile uint32_t cmd_target; // @204 0 none, 1 Sora himself (Cure / Aero), 2 KH1's current lock-on target,
                                  //      3 the KH1 entity in cmd_target_ent (the stand-in of the Borderlands target)
    volatile uint32_t cmd_count;  // @208 commands written
    // v5: while a spell flies, KH1's room walls don't stop it (the Borderlands world is the real one)
    volatile uint32_t walls_all;  // @212 1 = drop steep (wall) triangles from EVERY KH1 collision gather
    volatile uint32_t walls_all_removed;   // @216 counter
    volatile uint32_t gather_calls;   // @220 diagnostics: KH1 collision gathers seen
    volatile uint32_t gather_tris;    // @224 triangles they returned
    uint32_t pad228;              // @228
    volatile uint64_t cmd_target_ent;   // @232 KH1 entity address for cmd_target 3
    // v11: KH1 music volume (sound captures with the music silenced): DLL applies bgm_vol to every playing BGM
    // (sound list 0x4D6618, entries with +0x3C == 0) via KH1's fade FUN_140056e60 when bgm_seq != bgm_done
    volatile float bgm_vol;       // @240
    volatile uint32_t bgm_seq;    // @244
    volatile uint32_t bgm_done;   // @248
    volatile uint32_t bgm_count;  // @252 BGM entries changed by the last request
    FxQuad ring[FX_RING];       // @256
};
#pragma pack(pop)
static_assert(sizeof(FxQuad) == 224, "FxQuad");
