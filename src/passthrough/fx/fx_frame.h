// Shared memory "Local\SoraFxFrame" (kh1_fx v40): KH1's effect FRAMES. FUN_1401ecf30 is KH1's once-per-frame pass that
// runs every effect program (all sprite + effect-model draws happen inside it, on one thread). When a pass ends, kh1_fx
// publishes which records of the two rings it produced and the camera they were drawn with, so Borderlands draws one
// whole KH1 frame (sprites + meshes + camera from the SAME frame) instead of guessing frames from time gaps per ring
// (summon intros popped: the two rings' guesses landed on different KH1 frames).
// Read: s1 = seq (odd = being written) -> copy -> seq again == s1.
#pragma once
#include <cstdint>
#define FXF_SHARED_NAME L"Local\\SoraFxFrame"
#define FXF_MAGIC 0x46584653u   // 'SFXF'
#pragma pack(push, 1)
struct FxFrame {
    uint32_t magic;              // @0
    volatile uint32_t seq;       // @4  seqlock (odd while writing)
    uint32_t frame;              // @8  KH1 effect passes completed
    uint32_t spr_begin, spr_end; // @12 sprite ring records [begin, end) of that frame (FxShared.write values)
    uint32_t mdl_begin, mdl_end; // @20 effect-model ring batches [begin, end)
    uint32_t pad;                // @28
    int64_t time_us;             // @32 QPC time the pass ended
    float view[16];              // @40 KH1 0x2E9D480 at the end of the pass
    float proj[16];              // @104 KH1 0x2E9D600
};                               // 168
#pragma pack(pop)
static_assert(sizeof(FxFrame) == 168, "FxFrame");
