// melty_setup.exe - Melty's "Setting up" step for Sora in Borderlands.
// Runs installer\setup.ps1 -Melty (next to this exe) with this exe's own arguments and returns its exit code.
// Build: zig cc -target x86_64-windows-gnu -O2 -o melty_setup.exe melty_setup.c
#include <windows.h>
#include <stdio.h>
#include <wchar.h>

int wmain(void) {
    wchar_t self[MAX_PATH], cmd[32768];
    GetModuleFileNameW(NULL, self, MAX_PATH);
    wchar_t *slash = wcsrchr(self, L'\\');
    if (slash) *slash = 0;
    // the arguments after our own exe name, passed through as they are
    const wchar_t *args = GetCommandLineW();
    if (*args == L'"') { args = wcschr(args + 1, L'"'); args = args ? args + 1 : L""; }
    else { while (*args && *args != L' ') args++; }
    swprintf(cmd, 32768, L"powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"%ls\\setup.ps1\" -Melty%ls", self, args);
    STARTUPINFOW si = { sizeof(si) };
    PROCESS_INFORMATION pi;
    if (!CreateProcessW(NULL, cmd, NULL, NULL, FALSE, 0, NULL, self, &si, &pi)) return 2;
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);
    return (int)code;
}
