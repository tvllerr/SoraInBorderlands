// Shared memory "Local\SoraFxMdl": KH1's effect MODELS (Thunder bolts, Aero wind, Blizzard crystals...) as drawn.
// kh1_fx.dll (v8+) records every primitive batch KH1 submits while its effect-model packet runner FUN_140328ea0 runs.
// Vertices are KH1's own: 7 floats = model-space x y z, packed colour (u32 bits), u v (1/16 texels; 2048,2048 = no
// texture), unused. World = vertex * A * inverse(view * proj), exactly like the sprites (fx_shared.h).
#pragma once
#include <cstdint>
#define FXM_SHARED_NAME L"Local\\SoraFxMdl"
#define FXM_MAGIC 0x4D584653u   // 'SFXM'
#define FXM_BATCHES 2048
#define FXM_VERTS 131072
#define FXM_GS_CLAMP_MARK 0x4D414C43u   // 'CLAM'
// v39 (kh1_fx): FxMdlBatch.stride bits 0-15 = vertex stride, then KH1's depth state for the batch: FXM_Z_VALID,
// FXM_Z_NOWRITE (GS ZBUF ZMSK), bits 18-20 = GS TEST bits 16-18 (ZTE, ZTST: 0 never, 1 always, 2 gequal, 3 greater)
#define FXM_Z_VALID 0x10000u
#define FXM_Z_NOWRITE 0x20000u
#pragma pack(push, 1)
struct FxMdlBatch {              // 168 bytes
    volatile uint32_t seq;       // @0   write index + 1 once complete
    uint32_t nverts;             // @4
    uint32_t mode;               // @8   KH1 EndPrim mode: 5 = triangle list, 8 = quads (4 verts each)
    uint32_t first;              // @12  index of the first vertex in verts[] (mod FXM_VERTS)
    float A[16];                 // @16  model matrix (PS2 screen space, row vectors), header + 0x80
    float col[4];                // @80  header + 0x100 (shader constant 3)
    uint64_t alpha;              // @96  GS ALPHA at draw time (low byte 0x48 additive / 0x44 alpha)
    uint64_t tex0;               // @104 GS TEX0 at draw time
    uint64_t texobj;             // @112 Axa::EntryTex* for tex0 (0 = none)
    int64_t time_us;             // @120
    uint64_t header;             // @128 KH1 model header pointer (identity)
    uint32_t stride;             // @136 bytes per vertex KH1 used (copied as 28)
    uint32_t pad;                // @140 v12: FXM_GS_CLAMP_MARK when gs[16..24] holds GS CLAMP
    uint8_t gs[24];              // @144 GS ctx +0x00, +0x08 (ALPHA); v12: +0x10..+0x18 = ctx +0x20 = GS CLAMP
                                 //      (WMS bits 0-1, WMT bits 2-3: 0 repeat, 1 clamp, 2 region clamp, 3 region repeat)
};
struct FxMdlShared {
    uint32_t magic, version, nbatch, nvert;   // @0
    volatile uint32_t enabled;   // @16
    volatile uint32_t write;     // @20 next batch slot (monotonic)
    volatile uint32_t vwrite;    // @24 next vertex slot (monotonic)
    volatile uint32_t faults;    // @28
    uint8_t reserved[32];        // @32 .. 64
    FxMdlBatch batch[FXM_BATCHES];
    float verts[FXM_VERTS][7];
};
#pragma pack(pop)
static_assert(sizeof(FxMdlBatch) == 168, "FxMdlBatch");
