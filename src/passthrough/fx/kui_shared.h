// Shared memory "Local\SoraKhUi" (kh1_fx v15+): KH1's own 2D interface (HUD, command menu, menus) as KH1 draws it,
// so Borderlands can draw the real thing. Source: KH1's generic primitive submit EndPrim FUN_1400fb9c0 outside
// effect-model packets. Vertices are in PS2 screen space (x - 1728, y - 1824 = pixels of a 640x448 screen), u v in
// texels of the texture's own PS2 size (EntryTex +0x94), colour packed RGBA (128 = 1.0).
#pragma once
#include <cstdint>
#define KUI_SHARED_NAME L"Local\\SoraKhUi"
#define KUI_MAGIC 0x49554B53u   // 'SKUI'
#define KUI_BATCHES 4096
#define KUI_VERTS 262144
#pragma pack(push, 1)
struct KuiBatch {               // 80 bytes
    volatile uint32_t seq;      // @0  write index + 1 once complete
    uint32_t ret;               // @4  EndPrim return address RVA (who drew it)
    uint32_t mode, count;       // @8  EndPrim mode (5 tri list, 6 quads Z order, 7 strip, 8 quads loop order), vertices
    uint32_t first;             // @16 first vertex in v[] (mod KUI_VERTS)
    uint32_t submit;            // @20 caller of the 2D sprite list FUN_1402764b0 when drawn from one (RVA, else 0)
    float ofx, ofy;             // @24 GS XYOFFSET (1792/1856 = screen; ofx == ofy = off-screen target)
    uint64_t alpha, clamp;      // @32 GS ALPHA, CLAMP
    uint64_t tex0, texobj;      // @48 GS TEX0, Axa::EntryTex* (0 = none)
    int64_t time_us;            // @64
    uint16_t pw, ph;            // @72 texture's PS2 size (UV scale)
    uint32_t stride;            // @76 KH1's vertex stride (28 textured, 20 flat)
};
struct KuiShared {
    uint32_t magic, version, nbatch, nvert;     // @0
    volatile uint32_t enabled;  // @16 Borderlands sets 1 to capture
    volatile uint32_t write;    // @20 next batch (monotonic)
    volatile uint32_t vwrite;   // @24 next vertex (monotonic)
    volatile uint32_t faults;   // @28
    uint8_t reserved[32];       // @32 .. 64
    KuiBatch b[KUI_BATCHES];
    float v[KUI_VERTS][6];      // x y z, colour bits, u v
};
#pragma pack(pop)
static_assert(sizeof(KuiBatch) == 80, "KuiBatch");
