// Shared memory "Local\SoraSe": every sound effect KH1 starts (kh1_fx.dll v9+), so Borderlands can play the same
// KH1 sound. Source: KH1's sound library logs each call through the debug printf stub FUN_14011dc60 (an empty
// function in the retail build); kh1_fx hooks it and keeps the calls whose format string is a sound one.
#pragma once
#include <cstdint>
#define SE_SHARED_NAME L"Local\\SoraSe"
#define SE_MAGIC 0x31455353u   // 'SSE1'
#define SE_RING 1024
#pragma pack(push, 1)
struct SeEvent {                // 96 bytes
    volatile uint32_t seq;      // @0  write index + 1 once complete
    uint32_t kind;              // @4  SE_K_*
    int32_t bank, no, id;       // @8  KH1 bank number, sound number in the bank, play id
    uint32_t tid;               // @20 calling thread
    int64_t time_us;            // @24 QPC microseconds
    uint64_t ret;               // @32 return address of the printf call (who logged it)
    char name[56];              // @40 bank name ("[%s]"), voice name, or entry name
};
struct SeShared {
    uint32_t magic, ring_size, rec_size;   // @0
    volatile uint32_t write;    // @12 next slot (monotonic)
    volatile uint32_t calls;    // @16 printf stub calls seen (all formats)
    volatile uint32_t faults;   // @20
    uint8_t reserved[40];       // @24 .. 64
    SeEvent ring[SE_RING];      // @64
};
#pragma pack(pop)
static_assert(sizeof(SeEvent) == 96, "SeEvent");
enum {
    SE_K_PLAY_NAMED = 1,    // SslSePlay : bank no id [name]
    SE_K_CREATE2 = 2,       // SslSeCreate2 (3D positioned)
    SE_K_MAP_PLAY = 3,      // SslMapSePlay
    SE_K_MAP_CREATE = 4,    // SslMapSeCreate
    SE_K_CREATE = 5,        // SslSeCreate
    SE_K_PLAY = 6,          // SslSePlay (no name)
    SE_K_VOICE = 7,         // SslSeVoicePlay [name]
    SE_K_ENTRY = 8,         // Entry Sound -> name (Bank=n)
    SE_K_STOP = 9,          // SslSeStop
    SE_K_BGM = 10,          // ****** Play BGM name
    SE_K_RESUME = 11,       // v33: (re)start of an existing sound-list entry (FUN_140056070) - no printf
};
