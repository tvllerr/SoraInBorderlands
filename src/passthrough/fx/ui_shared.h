// Shared memory "Local\SoraUi" (kh1_fx v10+): KH1's 2D UI sprites from the lock-on code (target cursor, "LOCK ON!"
// banner, target HP bar), copied at submit time FUN_1402764b0(sprite, n, list) with the submitting function's
// return address - the sprite buffer 0x284F900 is reused by several of them within a frame, so reading it from
// outside gave whichever was written last.
#pragma once
#include <cstdint>
#define UI_SHARED_NAME L"Local\\SoraUi"
#define UI_MAGIC 0x31495553u   // 'SUI1'
#define UI_RING 256
#define UI_SPR 0xE0
#pragma pack(push, 1)
struct UiSprite {               // 256 bytes
    volatile uint32_t seq;      // @0  write index + 1 once complete
    uint32_t caller;            // @4  return address RVA (e.g. inside FUN_140274b20 = target cursor)
    int64_t time_us;            // @8
    uint32_t count;             // @16 n sprites submitted in this call (first one copied)
    uint32_t frame;             // @20 KH1 frame counter (calls of the player update hook)
    uint64_t texobj;            // @24 Axa::EntryTex* for the sprite's TEX0 (name at +0x14)
    uint8_t spr[UI_SPR];        // @32 the sprite: +0 TEX0, +0x10 TEX0 b, +0x20 4x(u,v) i32, +0x60 4x RGBA i32, +0xA0 4x(x,y,z) i32
};
struct UiShared {
    uint32_t magic, ring_size, rec_size;   // @0
    volatile uint32_t write;    // @12
    volatile uint32_t frame;    // @16 current KH1 frame counter
    uint8_t reserved[44];       // @20 .. 64
    UiSprite ring[UI_RING];     // @64
};
#pragma pack(pop)
static_assert(sizeof(UiSprite) == 256, "UiSprite");
