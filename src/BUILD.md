# Building the DLLs

The release zip ships these four DLLs prebuilt in `mod/sdk_mods/sora/data/`. This folder has their source.

| DLL | Source | Runs in | What it does |
|---|---|---|---|
| `sora_render.dll` | `render/sora_render.cpp` (+ `*.inc`) | Borderlands | Draws Sora, Donald, Goofy, Heartless, KH1 effects, HUD and menus in Borderlands' D3D11 frame; plays KH1 sounds (XAudio2); loads the KH1-side DLLs |
| `kh1_fx.dll` | `passthrough/fx/kh1_fx.cpp` | KH1 | Captures KH1's effects, HUD, sounds and hits; prize drops, loadout, equip, save-safety guard |
| `kh1_bridge.dll` | `passthrough/bridge/kh1_bridge.cpp` | KH1 | Feeds Borderlands' input to KH1 as its controller |
| `kh1_mapfeed_v3.dll` | `mapfeed/kh1_mapfeed.cpp` | KH1 | Gives KH1 collision polygons built from Borderlands' ground |

Addresses in the KH1-side code are for the Steam `KINGDOM HEARTS FINAL MIX.exe` 1.0.0.2 (SHA1 `E01FA57BC8DA71222E17E35926394C832BCE8107`). Names like `FUN_1402abc40` in comments are Ghidra's names for KH1 functions at those addresses.

## Toolchain
- [Zig 0.16](https://ziglang.org/) (used as the C++ compiler, targeting `x86_64-windows-gnu`)
- [MinHook](https://github.com/TsudaKageyu/minhook): build its C files once into objects:

```sh
zig cc -c -O2 -target x86_64-windows-gnu -I minhook/include minhook/src/buffer.c minhook/src/hook.c minhook/src/trampoline.c minhook/src/hde/hde64.c
# rename the four .o files to mh_buffer.o, mh_hook.o, mh_trampoline.o, mh_hde64.o
```

## Commands
Run from this `src/` folder, with MinHook at `minhook/` and the `mh_*.o` objects next to each source:

```sh
# Borderlands side
zig c++ -shared -O2 -target x86_64-windows-gnu -std=c++17 -w -I minhook/include render/sora_render.cpp mh_*.o \
    -ld3d11 -ldxgi -lole32 -lgdi32 -lwindowscodecs -lshell32 -luuid -lpsapi -o sora_render.dll

# KH1 side (SEH is used around game calls: -fms-extensions -fasync-exceptions)
zig c++ -shared -O2 -target x86_64-windows-gnu -std=c++17 -w -fms-extensions -fasync-exceptions -I minhook/include \
    passthrough/fx/kh1_fx.cpp mh_*.o -lpsapi -lole32 -o kh1_fx.dll
zig c++ -shared -O2 -target x86_64-windows-gnu -std=c++17 -w -fms-extensions -fasync-exceptions -I minhook/include \
    mapfeed/kh1_mapfeed.cpp mh_*.o -o kh1_mapfeed_v3.dll
zig c++ -shared -O2 -target x86_64-windows-gnu -std=c++17 -w -I minhook/include \
    passthrough/bridge/kh1_bridge.cpp mh_*.o -o kh1_bridge.dll
```

If a link step reports a missing Windows library, add it with `-l<name>`.

The Python side (`mod/sdk_mods/sora/logic.py`, `__init__.py`) needs no build: the SDK loads it directly, and `logic.py` hot-reloads when saved.
