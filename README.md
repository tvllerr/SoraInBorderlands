# Sora in Borderlands

Play **Borderlands GOTY Enhanced** as **Sora from Kingdom Hearts Final Mix**: real KH1 combos, magic, summons, Donald and Goofy, KH1's own menus and HUD.

This is a *passthrough* mod. Kingdom Hearts runs in the background at the same time, and the mod links the two games: KH1 does the combat, animation, effects and sound, and Borderlands shows it in its own world.

> Unofficial fan mod. Not affiliated with Square Enix, Disney, Gearbox or 2K.
> **No Kingdom Hearts files are included.** The installer builds Sora, Donald, Goofy, the Heartless, weapons and sounds from *your own* KH1 install.

## Features
- Sora's real KH1 moveset: combos, finishers, dodge roll, glide, Sliding Dash and other abilities
- KH1 magic (Fire, Blizzard, Thunder, Cure, Gravity, Stop, Aero) and the six summons
- Donald and Goofy fight next to you with their own attacks, spells and voices
- KH1's lock-on, command menu, HP/MP gauges and full main menu (Items, Equipment, Abilities...) drawn in Borderlands
- KH1 prize orbs, munny and item drops from enemies you defeat
- Your Borderlands level sets Sora's KH1 level, stats, magic, abilities, summons and Keyblades. This is temporary and in memory only; your KH1 save file is never written.

## Requirements
- Windows 10 or 11
- On Steam, both installed:
  - **Borderlands Game of the Year Enhanced**
  - **KINGDOM HEARTS HD 1.5+2.5 ReMIX** (Steam version, not Epic)
- A KH1 Final Mix save where Donald and Goofy have joined you (after the first visit to Traverse Town)
- About 25 GB of free space on the drive Kingdom Hearts is on (only during the install)
- An internet connection during the install (it downloads the Borderlands mod loader and its build tools)

## Install
1. Download `SoraInBorderlands.zip` from [Releases](https://github.com/tvllerr/SoraInBorderlands/releases/latest).
2. Right-click it > **Extract All...** (don't run anything from inside the zip).
3. Close Borderlands and Kingdom Hearts.
4. Double-click **INSTALL.bat** and wait for "ALL DONE" (15-30 minutes the first time).

Full instructions, controls and troubleshooting: [HOW TO INSTALL.txt](HOW%20TO%20INSTALL.txt).

## Play
1. Start KINGDOM HEARTS FINAL MIX, load your save and stand in the field.
2. Start Borderlands and load your character.
3. Play in the Borderlands window. Leave KH1 open behind it (don't minimize it).

Options: Borderlands main menu > **Mods > Sora** (camera, gameplay, visuals, audio, keybinds).

## Update / uninstall
- **UPDATE.bat** checks this page for a newer release and installs it. Your settings are kept.
- **UNINSTALL.bat** removes the mod. Kingdom Hearts and your saves are never changed.

## Credits
Made by **tvllerr**. Thank you to everyone whose work this mod stands on:

**Used by the mod and its installer**
- [willow1 PythonSDK / mod manager](https://github.com/bl-sdk/willow1-mod-manager) (unrealsdk, pyunrealsdk, mods_base) by the bl-sdk team: the Borderlands mod loader everything runs on
- [KHPCPatchManager](https://github.com/AntonioDePau/KHPCPatchManager) by AntonioDePau: unpacks KH1's data from your own install
- [vgmstream](https://github.com/vgmstream/vgmstream) by the vgmstream team: decodes KH1's music, voices and sound effects (license in `LICENSES/`)
- [MinHook](https://github.com/TsudaKageyu/minhook) by Tsuda Kageyu: function hooking in the mod's DLLs (license in `LICENSES/`)
- [uv](https://github.com/astral-sh/uv) by Astral, [Python](https://www.python.org/), [NumPy](https://numpy.org/) and [Pillow](https://python-pillow.org/): run the build scripts that turn your KH1 files into the mod's models, textures and sounds
- [PCGamingWiki](https://www.pcgamingwiki.com/wiki/Borderlands:_Game_of_the_Year_Enhanced): the fix for skipping the 2K launcher

**Research and references**
- [OpenKH](https://github.com/OpenKH/OpenKh) by the OpenKH team: documentation and code for KH1's model (MDLS/CVBL), weapon and animation (MSET) formats
- [KH1-LUA-LIBRARY](https://github.com/gaithern/KH1-LUA-LIBRARY) by gaithern: the Steam KH1 memory address map and the enemy-spawn method
- **YG123**'s Borderlands 2 x Kingdom Hearts project and its reverse-engineering journal: KH1 animation, motion and command addresses that made the passthrough possible
- [borderlands-1-goty-mods](https://github.com/EerieGoesD/borderlands-1-goty-mods) by EerieGoesD: reference for SDK mods on BL1 Enhanced
- [SkyCraft](https://github.com/chasmlol/SkyCraft) by chasmlol: the passthrough idea (one game running inside another) and the crash-logger approach (MIT)

**Tools used to build it**
- [Ghidra](https://github.com/NationalSecurityAgency/ghidra) (NSA): reverse engineering KH1 and Borderlands
- [Zig](https://ziglang.org/): compiles the C++ DLLs
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper): labeling KH1's voice clips (which line is an attack shout, a hurt sound, a spell call)

## License
The mod's own code is open source under the [MIT License](LICENSE): use, copy, change and share any of it, in your own mods too, as long as the copyright notice comes along. The C++ source of the DLLs is in [`src/`](https://github.com/tvllerr/SoraInBorderlands/tree/main/src) on GitHub (build notes in [`src/BUILD.md`](https://github.com/tvllerr/SoraInBorderlands/blob/main/src/BUILD.md)). Third-party parts keep their own licenses (see `LICENSES/`), and no game content is included.

Kingdom Hearts is © Disney / Square Enix. Borderlands is © Gearbox Software / 2K. This is an unofficial fan project: it contains only original code, and all game content stays with its owners and is read from your own copies of the games.

## Made with Claude
This mod was made with the help of [Claude](https://claude.com/claude-code) (Anthropic's AI) in Claude Code. Claude wrote the code (the Python mod, the C++ renderer and KH1 bridge DLLs, the installer), reverse engineered KH1's and Borderlands' internals with Ghidra and live memory probes, and built the tools that convert KH1's files. tvllerr directed the project, playtested every change in game, and decided how it should look and feel.
