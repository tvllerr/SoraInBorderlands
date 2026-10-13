// crashlog.h - release crash logger shared by kh1_fx.dll (in KH1) and sora_render.dll (in Borderlands).
// (SkyCraft #1 / release R1, 2026-10-12)
//
// A vectored handler records every SERIOUS exception (access violation, illegal instruction, stack overflow, divide by
// zero, ...) into a small ring in a FILE-BACKED mapping, Documents\SoraInBorderlands\crash_logs\session_<tag>.bin:
// time, thread, code, RIP + faulting address, registers and up to 16 return addresses, each as module+RVA. A heartbeat in
// the same file ticks every ~100 ms. Mapped pages reach the disk even when the process dies, so the NEXT start (any
// process with this logger, same tag) judges the previous session: it died within 2 s of a recorded exception ->
// crash_logs\<date>_<time>_<tag>.txt. First-chance exceptions that were handled (ours are __try-guarded, the games
// have their own) only churn the ring; the last one before death is the crash.
//
// No loader calls inside the handler: module names come from a table the heartbeat thread refreshes (K32EnumProcessModules).
// Usage: crash_init("KH1") once (any thread, repeat-safe: a hot-swapped DLL in the SAME process continues the session),
// crash_tick() every ~100 ms from a background thread, crash_shutdown() before FreeLibrary (removes the handler).
#pragma once
#include <windows.h>
#include <psapi.h>
#include <shlobj.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define CRASH_MAGIC 0x48535243u   // "CRSH"
#define CRASH_VERSION 1u
#define CRASH_RING 8
#define CRASH_FRAMES 16
#define CRASH_MODS 256   // session 9: KH1 has ~140 modules - 96 left D3DSCache unnamed ("?")

struct CrashFrame { char mod[28]; uint32_t rva; };
struct CrashRec {
    uint64_t time_ft;              // FILETIME (UTC)
    uint32_t tid, code;
    uint64_t rip, addr, rw;        // AV: addr = faulting address, rw = 0 read / 1 write / 8 execute
    CrashFrame where;              // module + RVA of RIP
    uint64_t reg[16];              // rax rcx rdx rbx rsp rbp rsi rdi r8..r15
    uint32_t nframes, pad;
    CrashFrame frames[CRASH_FRAMES];
};
struct CrashSession {
    uint32_t magic, version;
    uint32_t pid, open;            // open = 1 while the process runs (cleared on a clean crash_shutdown at process exit)
    uint64_t start_ft, beat_ft;    // session start + last heartbeat (FILETIME)
    char tag[16], exe[64], build[64];
    volatile LONG seq;             // records written (ring index = seq % CRASH_RING)
    uint32_t pad;
    CrashRec rec[CRASH_RING];
};
struct CrashModule { uint64_t base, size; char name[28]; uint32_t pad; };

static CrashSession* g_crash = nullptr;
static PVOID g_crashVeh = nullptr;
static CrashModule g_crashMods[2][CRASH_MODS];
static volatile LONG g_crashModSel = 0, g_crashModN[2] = {0, 0};
static ULONGLONG g_crashModT = 0;
static volatile LONG g_crashRate = 0; static ULONGLONG g_crashRateT = 0;
static char g_crashDir[MAX_PATH] = {0};

static uint64_t crash_now_ft() { FILETIME f; GetSystemTimeAsFileTime(&f); return ((uint64_t)f.dwHighDateTime << 32) | f.dwLowDateTime; }

static void crash_mods_refresh() {
    HMODULE m[CRASH_MODS]; DWORD need = 0;
    if (!K32EnumProcessModules(GetCurrentProcess(), m, sizeof m, &need)) return;
    int n = (int)(need / sizeof(HMODULE)); if (n > CRASH_MODS) n = CRASH_MODS;
    LONG w = 1 - g_crashModSel;
    for (int i = 0; i < n; i++) {
        MODULEINFO mi{}; K32GetModuleInformation(GetCurrentProcess(), m[i], &mi, sizeof mi);
        CrashModule& c = g_crashMods[w][i];
        c.base = (uint64_t)mi.lpBaseOfDll; c.size = mi.SizeOfImage;
        char path[MAX_PATH] = {0}; K32GetModuleBaseNameA(GetCurrentProcess(), m[i], path, sizeof path);
        strncpy(c.name, path, sizeof c.name - 1); c.name[sizeof c.name - 1] = 0;
    }
    g_crashModN[w] = n;
    InterlockedExchange(&g_crashModSel, w);
}
static bool crash_where(uint64_t a, CrashFrame* f) {
    LONG s = g_crashModSel; LONG n = g_crashModN[s];
    for (LONG i = 0; i < n; i++) {
        const CrashModule& c = g_crashMods[s][i];
        if (a >= c.base && a < c.base + c.size) { memcpy(f->mod, c.name, sizeof f->mod); f->rva = (uint32_t)(a - c.base); return true; }
    }
    return false;
}
static bool crash_serious(DWORD code) {
    switch (code) {
    case EXCEPTION_ACCESS_VIOLATION: case EXCEPTION_ILLEGAL_INSTRUCTION: case EXCEPTION_PRIV_INSTRUCTION:
    case EXCEPTION_STACK_OVERFLOW: case EXCEPTION_INT_DIVIDE_BY_ZERO: case EXCEPTION_IN_PAGE_ERROR:
    case EXCEPTION_ARRAY_BOUNDS_EXCEEDED: case EXCEPTION_DATATYPE_MISALIGNMENT: case 0xC0000409: case 0xC0000374:
        return true;
    }
    return false;
}
static LONG CALLBACK crash_veh(PEXCEPTION_POINTERS e) {
    CrashSession* s = g_crash;
    if (!s || !crash_serious(e->ExceptionRecord->ExceptionCode)) return EXCEPTION_CONTINUE_SEARCH;
    ULONGLONG t = GetTickCount64();                       // at most 64 records a second (a hot __try loop can't spin us)
    if (t - g_crashRateT > 1000) { g_crashRateT = t; g_crashRate = 0; }
    if (InterlockedIncrement(&g_crashRate) > 64) return EXCEPTION_CONTINUE_SEARCH;
    LONG i = InterlockedIncrement(&s->seq) - 1;
    CrashRec& r = s->rec[i % CRASH_RING];
    CONTEXT* c = e->ContextRecord;
    memset(&r, 0, sizeof r);
    r.time_ft = crash_now_ft(); r.tid = GetCurrentThreadId(); r.code = e->ExceptionRecord->ExceptionCode; r.rip = c->Rip;
    if (e->ExceptionRecord->NumberParameters >= 2) { r.rw = e->ExceptionRecord->ExceptionInformation[0]; r.addr = e->ExceptionRecord->ExceptionInformation[1]; }
    crash_where(c->Rip, &r.where);
    uint64_t regs[16] = {c->Rax, c->Rcx, c->Rdx, c->Rbx, c->Rsp, c->Rbp, c->Rsi, c->Rdi, c->R8, c->R9, c->R10, c->R11, c->R12, c->R13, c->R14, c->R15};
    memcpy(r.reg, regs, sizeof regs);
    // return-address candidates: qwords on THIS thread's stack (Rsp .. StackBase, always readable) that point into a module
    NT_TIB* tib = (NT_TIB*)NtCurrentTeb();
    uint64_t* p = (uint64_t*)(c->Rsp & ~7ull); uint64_t* end = (uint64_t*)tib->StackBase;
    if ((uint64_t)p >= (uint64_t)tib->StackLimit && p < end) {
        for (int k = 0; k < 2048 && p + k < end && r.nframes < CRASH_FRAMES; k++)
            if (crash_where(p[k], &r.frames[r.nframes]) && r.frames[r.nframes].rva >= 0x1000) r.nframes++;   // (skip image-base values)
    }
    return EXCEPTION_CONTINUE_SEARCH;
}

static void crash_ft_str(uint64_t ft, char* out, size_t n, bool forFile) {
    FILETIME u{(DWORD)ft, (DWORD)(ft >> 32)}, l; SYSTEMTIME st; FileTimeToLocalFileTime(&u, &l); FileTimeToSystemTime(&l, &st);
    if (forFile) snprintf(out, n, "%04d-%02d-%02d_%02d%02d%02d", st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond);
    else snprintf(out, n, "%04d-%02d-%02d %02d:%02d:%02d.%03d", st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);
}
static const char* crash_code_name(uint32_t c) {
    switch (c) {
    case EXCEPTION_ACCESS_VIOLATION: return "access violation"; case EXCEPTION_ILLEGAL_INSTRUCTION: return "illegal instruction";
    case EXCEPTION_PRIV_INSTRUCTION: return "privileged instruction"; case EXCEPTION_STACK_OVERFLOW: return "stack overflow";
    case EXCEPTION_INT_DIVIDE_BY_ZERO: return "divide by zero"; case EXCEPTION_IN_PAGE_ERROR: return "in-page error";
    case 0xC0000409: return "stack buffer overrun / fail fast"; case 0xC0000374: return "heap corruption";
    }
    return "exception";
}
// the previous session in this file: did that process die right after a serious exception? -> write a report
static void crash_judge(const CrashSession* s) {
    if (s->magic != CRASH_MAGIC || s->version != CRASH_VERSION || s->seq == 0) return;
    const CrashRec* last = &s->rec[(s->seq - 1) % CRASH_RING];
    int64_t gap_ms = ((int64_t)s->beat_ft - (int64_t)last->time_ft) / 10000;
    // died without a clean exit (open still 1: killed by Windows) -> up to 10 s; a clean-looking exit (the game's own
    // error box, then ExitProcess) -> only right after it. Longer = it kept running: handled, not the crash.
    if (gap_ms > (s->open ? 10000 : 2000)) return;
    char ts[32], fname[MAX_PATH]; crash_ft_str(last->time_ft, ts, sizeof ts, true);
    snprintf(fname, sizeof fname, "%s\\%s_%s.txt", g_crashDir, ts, s->tag);
    FILE* f = fopen(fname, "w"); if (!f) return;
    char a[40], b[40]; crash_ft_str(s->start_ft, a, sizeof a, false); crash_ft_str(last->time_ft, b, sizeof b, false);
    fprintf(f, "Sora in Borderlands - crash report (%s)\n", s->tag);
    fprintf(f, "process %s (pid %u), session started %s\nmod build: %s\n", s->exe, s->pid, a, s->build);
    fprintf(f, "the process stopped %lld ms after this exception (%s):\n\n", (long long)gap_ms,
            s->open ? "no clean exit - killed by Windows" : "closed itself right after it");
    int n = s->seq < CRASH_RING ? s->seq : CRASH_RING;
    for (int k = 0; k < n; k++) {                          // newest first
        const CrashRec& r = s->rec[(s->seq - 1 - k) % CRASH_RING];
        crash_ft_str(r.time_ft, b, sizeof b, false);
        fprintf(f, "%s #%d  %s  code %08X (%s)  thread %u\n", k ? "older" : "LAST ", k, b, r.code, crash_code_name(r.code), r.tid);
        fprintf(f, "  at %s+0x%X (rip %016llX)", r.where.mod[0] ? r.where.mod : "?", r.where.rva, (unsigned long long)r.rip);
        if (r.code == EXCEPTION_ACCESS_VIOLATION) fprintf(f, "  %s %016llX", r.rw == 1 ? "writing" : r.rw == 8 ? "executing" : "reading", (unsigned long long)r.addr);
        fprintf(f, "\n");
        if (k == 0) {
            static const char* rn[16] = {"rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi", "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15"};
            for (int q = 0; q < 16; q++) fprintf(f, "  %-3s %016llX%s", rn[q], (unsigned long long)r.reg[q], q % 4 == 3 ? "\n" : "");
        }
        fprintf(f, "  stack:");
        for (uint32_t q = 0; q < r.nframes; q++) fprintf(f, " %s+0x%X", r.frames[q].mod, r.frames[q].rva);
        fprintf(f, "\n\n");
    }
    fclose(f);
}

static void crash_init(const char* tag, const char* build) {
    if (g_crash) return;
    wchar_t* docs = nullptr; char base[MAX_PATH] = {0};
    if (SUCCEEDED(SHGetKnownFolderPath(FOLDERID_Documents, 0, nullptr, &docs)) && docs) { WideCharToMultiByte(CP_ACP, 0, docs, -1, base, MAX_PATH, nullptr, nullptr); CoTaskMemFree(docs); }
    else return;
    snprintf(g_crashDir, sizeof g_crashDir, "%s\\SoraInBorderlands", base); CreateDirectoryA(g_crashDir, nullptr);
    strncat(g_crashDir, "\\crash_logs", sizeof g_crashDir - strlen(g_crashDir) - 1); CreateDirectoryA(g_crashDir, nullptr);
    char path[MAX_PATH]; snprintf(path, sizeof path, "%s\\session_%s.bin", g_crashDir, tag);
    HANDLE fh = CreateFileA(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (fh == INVALID_HANDLE_VALUE) return;
    HANDLE mh = CreateFileMappingA(fh, nullptr, PAGE_READWRITE, 0, sizeof(CrashSession), nullptr);
    CloseHandle(fh);
    if (!mh) return;
    CrashSession* s = (CrashSession*)MapViewOfFile(mh, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(CrashSession));
    CloseHandle(mh);
    if (!s) return;
    crash_mods_refresh();
    bool same = s->magic == CRASH_MAGIC && s->version == CRASH_VERSION && s->pid == GetCurrentProcessId() && s->open;
    if (!same) {                                           // a new process: report the last one if it crashed, then reset
        crash_judge(s);
        memset(s, 0, sizeof *s);
        s->magic = CRASH_MAGIC; s->version = CRASH_VERSION; s->pid = GetCurrentProcessId(); s->start_ft = crash_now_ft();
        strncpy(s->tag, tag, sizeof s->tag - 1);
        char exe[MAX_PATH] = {0}; GetModuleFileNameA(nullptr, exe, MAX_PATH); const char* bn = strrchr(exe, '\\');
        strncpy(s->exe, bn ? bn + 1 : exe, sizeof s->exe - 1);
    }
    strncpy(s->build, build, sizeof s->build - 1);         // (a hot swap updates the build string, keeps the records)
    s->beat_ft = crash_now_ft(); s->open = 1;
    g_crash = s;
    g_crashVeh = AddVectoredExceptionHandler(0, crash_veh);   // last in line: every other handler sees it first
}
static void crash_tick() {
    if (!g_crash) return;
    g_crash->beat_ft = crash_now_ft();
    ULONGLONG t = GetTickCount64();
    if (t - g_crashModT > 5000) { g_crashModT = t; crash_mods_refresh(); }
}
static void crash_shutdown(bool processExit) {           // hot swap: false (the session continues in the next DLL)
    if (g_crashVeh) { RemoveVectoredExceptionHandler(g_crashVeh); g_crashVeh = nullptr; }
    if (g_crash) { if (processExit) g_crash->open = 0; g_crash->beat_ft = crash_now_ft(); FlushViewOfFile(g_crash, 0); UnmapViewOfFile(g_crash); g_crash = nullptr; }
}
