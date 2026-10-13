// Shared memory between sora_render.dll (Borderlands) and kh1_bridge.dll (KH1). Keep in sync with both and with
// logic.py (SH_* offsets).
#pragma once
#include <cstdint>
#define SHARED_NAME L"Local\\SoraPassthrough"
#define SHARED_MAGIC 0x31545053u   // 'SPT1'
#define SHARED_MAX_PROXIES 8
#pragma pack(push, 1)
struct SharedBlock {
    uint32_t magic;
    volatile uint32_t bl_seq;          // Borderlands bumps this every frame it sends input (heartbeat)
    volatile uint32_t active;          // 1 = override KH1 controller 0
    volatile uint16_t buttons;         // PS2-style mask as KH1 stores it (layout learned from real presses)
    volatile uint8_t sticks[4];        // PS2 order RX, RY, LX, LY; 0x80 = centre
    volatile uint32_t kh_seq;          // bridge bumps this each overridden pad read
    volatile uint32_t kh_status;       // 1 = hook installed
    volatile uint16_t kh_real_buttons; // the real pad while not overriding (for learning the layout)
    volatile uint8_t kh_real_sticks[4];
    volatile uint32_t walls_off;       // @32 1 = KH1 Sora ignores KH1 wall triangles (floors/ceilings kept)
    volatile uint32_t walls_removed;   // @36 bridge counter: wall triangles skipped for Sora (diagnostics)
    volatile uint32_t world_hooks;     // @40 bridge: 1 = collision hooks installed
    // ---- v3: Borderlands ground (like GTA's ground becoming Minecraft barrier blocks)
    volatile uint32_t ground_mode;     // @44 1 = Sora (and proxies) ignore ALL KH1 room triangles and stand on floor_y
    volatile uint32_t floor_on;        // @48 0 = no floor (Borderlands is falling: KH1 Sora keeps falling too)
    volatile float floor_y;            // @52 KH1 height of the Borderlands ground (KH cm, y down)
    volatile uint32_t snap_seq;        // @56 bump: put Sora on the floor now (Borderlands landed)
    volatile float lift_total;         // @60 bridge: cm Sora was lifted during long falls (virtual y = y + lift_total)
    volatile float fall_limit;         // @64 with no floor: lift Sora when he is this far below floor_y ...
    volatile float fall_lift;          // @68 ... by this much (keeps the KH1 fall loop going on long Borderlands drops)
    volatile uint32_t ground_clamps;   // @72 bridge counter: floor contacts emulated
    volatile int32_t floor_poly;       // @76 bridge: room floor polygon reported as "the floor" (attributes only)
    volatile uint32_t sora_air;        // @80 bridge: 1 = Sora above the floor (airborne) in the last query
    volatile uint32_t nproxy;          // @84 number of proxy entities below
    volatile uint64_t proxy[SHARED_MAX_PROXIES];   // @88 KH1 entities (Heartless stand-ins for Borderlands enemies)
    volatile uint32_t bridge_version;  // @152 bridge: 3 = this layout (ground mode + proxies)
    uint8_t reserved[100];             // @156 .. 256
};
#pragma pack(pop)
static_assert(sizeof(SharedBlock) == 256, "SharedBlock layout");
