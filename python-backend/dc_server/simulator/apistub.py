# -*- coding: utf-8 -*-
import ctypes
import struct
from typing import Optional

from unicorn import Uc, x86_const, UC_HOOK_CODE
from unicorn.unicorn import UcError

from .loader import Image
from .apistub_table import APISTUB_TABLE
from . import env as ENV

STUB_BASE = 0x10000000
STUB_SIZE = 0x100000
NTDLL_EXPORT_BASE = 0x11000000
NTDLL_EXPORT_SIZE = 0x100000

CDECL_DLL_PREFIXES = ("msvcrt", "ucrtbase")
CDECL_NAMES = {
    "printf", "sprintf", "snprintf", "fprintf", "scanf", "sscanf", "fscanf",
    "vsprintf", "vsnprintf", "vfprintf", "vfscanf", "wprintf", "swprintf",
    "puts", "putchar", "gets", "getchar", "getch", "malloc", "free", "realloc",
    "calloc", "memcpy", "memset", "memmove", "memcmp", "strlen", "strcmp",
    "strncmp", "strcpy", "strncpy", "strcat", "strncat", "strchr", "strrchr",
    "strstr", "strtol", "strtoul", "strtod", "atoi", "atol", "atof", "exit",
    "abort", "rand", "srand", "qsort", "bsearch", "time", "clock", "getenv",
    "_stricmp", "_strnicmp", "_snprintf", "_snwprintf", "isalpha", "isdigit",
    "isalnum", "isxdigit", "islower", "isupper", "isspace", "toupper", "tolower",
    "__acrt_iob_func", "__stdio_common_vfprintf", "__stdio_common_vfscanf",
    "__stdio_common_vsnprintf", "__stdio_common_vsprintf", "__stdio_common_vswprintf",
    "_initterm", "_initterm_e", "atexit", "_atexit", "system", "getcwd",
}

ARGC_FIX = {
    "FlsAlloc": 1, "FlsFree": 1, "FlsGetValue": 1, "FlsSetValue": 2,
    "InitializeCriticalSectionEx": 3,
    "GetProcAddress": 2, "GetModuleHandleA": 1, "GetModuleHandleW": 1,
    "GetModuleHandleExA": 3, "GetModuleHandleExW": 3,
    "GetStartupInfoA": 1, "GetStartupInfoW": 1,
    "SetUnhandledExceptionFilter": 1, "UnhandledExceptionFilter": 1,
    "AddVectoredExceptionHandler": 2, "RtlAddVectoredExceptionHandler": 2,
    "RemoveVectoredExceptionHandler": 1, "RtlRemoveVectoredExceptionHandler": 1,
    "NtContinue": 2, "ZwContinue": 2,
    "IsProcessorFeaturePresent": 1, "GetCurrentProcessId": 0,
    "GetCurrentThreadId": 0, "GetSystemTimeAsFileTime": 1,
}

_STD = "stdcall"
_CDL = "cdecl"

def _u32(b: bytes) -> int:
    return ctypes.c_uint32.from_buffer_copy(b[:4]).value

def _s32(v: int) -> bytes:
    return ctypes.c_uint32(v & 0xFFFFFFFF).value.to_bytes(4, "little")

class ApiStubs:

    def __init__(self, uc: Uc, image: Image, session):
        self.uc = uc
        self.image = image
        self.session = session
        self.stub_map: dict[int, str] = {}
        self.api_id: dict[str, int] = {}
        self.handlers: dict[str, tuple] = {}
        self.dispatcher_addr = STUB_BASE
        self._next_stub = STUB_BASE + 0x10
        self._dll_of: dict[str, str] = {}
        for _n, _m in APISTUB_TABLE.items():
            self._dll_of[_n] = _m.get("dll", "")
        self.enc_to_stub: dict[int, tuple] = {}
        self.decrypt_fn: Optional[int] = None
        self._fallback_stub = 0
        self._module_stub = 0
        self._register_handlers()

    def _reg(self, name: str, argc: int, conv: str, fn) -> None:
        self.handlers[name] = (argc, conv, fn)

    def _register_handlers(self) -> None:
        S, C = _STD, _CDL
        h = self
        self._reg("ZwAllocateVirtualMemory", 6, S, h._alloc_vm)
        self._reg("NtAllocateVirtualMemory", 6, S, h._alloc_vm)
        self._reg("ZwFreeVirtualMemory", 4, S, h._ret0)
        self._reg("NtFreeVirtualMemory", 4, S, h._ret0)
        self._reg("ZwProtectVirtualMemory", 5, S, h._ret0)
        self._reg("NtProtectVirtualMemory", 5, S, h._ret0)
        self._reg("HeapAlloc", 3, S, h._alloc_vm)
        self._reg("HeapFree", 3, S, h._ret1)
        self._reg("HeapReAlloc", 4, S, h._ret0)
        self._reg("HeapSize", 3, S, h._ret0)
        self._reg("GetProcessHeap", 0, S, h._heap_base)
        self._reg("GetProcessHeaps", 2, S, h._ret0)
        self._reg("VirtualAlloc", 4, S, h._alloc_vm)
        self._reg("VirtualFree", 3, S, h._ret1)
        self._reg("VirtualProtect", 4, S, h._ret1)
        self._reg("VirtualQuery", 3, S, h._ret0)
        self._reg("malloc", 1, C, h._alloc_vm)
        self._reg("free", 1, C, h._ret0)
        self._reg("realloc", 2, C, h._alloc_vm)
        self._reg("calloc", 2, C, h._alloc_vm)
        self._reg("GlobalAlloc", 2, S, h._alloc_vm)
        self._reg("GlobalFree", 1, S, h._ret0)
        self._reg("LocalAlloc", 2, S, h._alloc_vm)
        self._reg("LocalFree", 1, S, h._ret0)
        self._reg("GetTickCount", 0, S, h._tick)
        self._reg("GetTickCount64", 0, S, h._tick)
        self._reg("timeGetTime", 0, S, h._tick)
        self._reg("QueryPerformanceCounter", 1, S, h._qpc)
        self._reg("QueryPerformanceFrequency", 1, S, h._qpc_freq)
        self._reg("GetSystemTimeAsFileTime", 1, S, h._filetime)
        self._reg("GetSystemTime", 1, S, h._ret0)
        self._reg("GetLocalTime", 1, S, h._ret0)
        self._reg("GetTimeZoneInformation", 1, S, h._ret_neg1)
        self._reg("IsDebuggerPresent", 0, S, h._ret0)
        self._reg("CheckRemoteDebuggerPresent", 2, S, h._check_remote_dbg)
        self._reg("NtQueryInformationProcess", 5, S, h._query_info_process)
        self._reg("ZwQueryInformationProcess", 5, S, h._query_info_process)
        self._reg("NtQuerySystemInformation", 4, S, h._query_sys_info)
        self._reg("ZwQuerySystemInformation", 4, S, h._query_sys_info)
        self._reg("NtSetInformationThread", 4, S, h._ret0)
        self._reg("ZwSetInformationThread", 4, S, h._ret0)
        self._reg("NtQueryObject", 5, S, h._ret_port_not_set)
        self._reg("ZwQueryObject", 5, S, h._ret_port_not_set)
        self._reg("NtQueryInformationThread", 5, S, h._ret0)
        self._reg("ZwQueryInformationThread", 5, S, h._ret0)
        self._reg("NtGetContextThread", 2, S, h._ret0)
        self._reg("NtSetContextThread", 2, S, h._ret0)
        self._reg("DebugActiveProcess", 1, S, h._ret0)
        self._reg("DebugActiveProcessStop", 1, S, h._ret0)
        self._reg("DebugBreak", 0, S, h._ret0)
        self._reg("IsProcessorFeaturePresent", 1, S, h._ret0)
        self._reg("IsWow64Process", 2, S, h._is_wow64)
        self._reg("GetCurrentProcess", 0, S, h._ret_neg1)
        self._reg("GetCurrentProcessId", 0, S, h._pid)
        self._reg("GetCurrentThread", 0, S, h._ret_neg2)
        self._reg("GetCurrentThreadId", 0, S, h._tid)
        self._reg("ExitProcess", 1, S, h._exit_process)
        self._reg("TerminateProcess", 2, S, h._exit_process)
        self._reg("ExitThread", 1, S, h._ret0)
        self._reg("CreateThread", 6, S, h._ret0)
        self._reg("Sleep", 1, S, h._ret0)
        self._reg("GetLastError", 0, S, h._get_last_error)
        self._reg("SetLastError", 1, S, h._set_last_error)
        self._reg("GetModuleHandleA", 1, S, h._get_module_handle)
        self._reg("GetModuleHandleW", 1, S, h._get_module_handle)
        self._reg("GetModuleHandleExA", 3, S, h._get_module_handle)
        self._reg("GetModuleHandleExW", 3, S, h._get_module_handle)
        self._reg("GetModuleFileNameA", 3, S, h._module_file_name)
        self._reg("GetModuleFileNameW", 3, S, h._module_file_name)
        self._reg("GetProcAddress", 2, S, h._get_proc_address)
        self._reg("GetCommandLineA", 0, S, h._cmdline_a)
        self._reg("GetCommandLineW", 0, S, h._cmdline_w)
        self._reg("GetStartupInfoA", 1, S, h._startup_info)
        self._reg("GetStartupInfoW", 1, S, h._startup_info)
        self._reg("GetEnvironmentVariableA", 3, S, h._ret0)
        self._reg("GetEnvironmentVariableW", 3, S, h._ret0)
        self._reg("RaiseException", 4, S, h._ret0)
        self._reg("RtlUnwind", 4, S, h._ret0)
        self._reg("UnhandledExceptionFilter", 1, S, h._ret0)
        self._reg("SetUnhandledExceptionFilter", 1, S, h._set_uef)
        self._reg("GetSystemInfo", 1, S, h._ret0)
        self._reg("GetNativeSystemInfo", 1, S, h._ret0)
        self._reg("GetVersionExA", 1, S, h._ret0)
        self._reg("GetVersionExW", 1, S, h._ret0)
        self._reg("GetVersion", 0, S, h._ret0)
        self._reg("GetProcessId", 1, S, h._pid)
        self._reg("GetThreadId", 1, S, h._tid)
        self._reg("GetExitCodeProcess", 2, S, h._ret0)
        self._reg("GetExitCodeThread", 2, S, h._ret0)
        self._reg("OpenProcess", 3, S, h._ret_neg1)
        self._reg("DecodePointer", 1, S, h._ret1)
        self._reg("EncodePointer", 1, S, h._ret1)
        self._reg("AddVectoredExceptionHandler", 2, S, h._add_veh)
        self._reg("RtlAddVectoredExceptionHandler", 2, S, h._add_veh)
        self._reg("RemoveVectoredExceptionHandler", 1, S, h._remove_veh)
        self._reg("RtlRemoveVectoredExceptionHandler", 1, S, h._remove_veh)
        self._reg("NtContinue", 2, S, h._nt_continue)
        self._reg("ZwContinue", 2, S, h._nt_continue)
        self._reg("GetErrorMode", 0, S, h._ret0)
        self._reg("SetErrorMode", 1, S, h._ret0)
        self._reg("GetStdHandle", 1, S, h._std_handle)
        self._reg("WriteFile", 5, S, h._write_file)
        self._reg("WriteConsoleA", 5, S, h._write_file)
        self._reg("WriteConsoleW", 5, S, h._write_file)
        self._reg("ReadFile", 5, S, h._read_file)
        self._reg("ReadConsoleA", 5, S, h._read_file)
        self._reg("ReadConsoleW", 5, S, h._read_file)
        self._reg("OutputDebugStringA", 1, S, h._output_debug)
        self._reg("OutputDebugStringW", 1, S, h._output_debug)
        self._reg("GetFileType", 1, S, h._ret2)
        self._reg("GetConsoleMode", 2, S, h._ret1)
        self._reg("SetConsoleMode", 2, S, h._ret1)
        self._reg("FlushFileBuffers", 1, S, h._ret1)
        self._reg("CloseHandle", 1, S, h._ret1)
        self._reg("CreateFileA", 7, S, h._ret_neg1)
        self._reg("CreateFileW", 7, S, h._ret_neg1)
        self._reg("GetFileSize", 2, S, h._ret0)
        self._reg("GetFileAttributesA", 1, S, h._ret_neg1)
        self._reg("GetFileAttributesW", 1, S, h._ret_neg1)
        self._reg("GetCurrentDirectoryA", 2, S, h._ret0)
        self._reg("GetCurrentDirectoryW", 2, S, h._ret0)
        self._reg("SetCurrentDirectoryA", 1, S, h._ret0)
        self._reg("SetCurrentDirectoryW", 1, S, h._ret0)
        self._reg("GetTempPathA", 3, S, h._ret0)
        self._reg("GetTempPathW", 3, S, h._ret0)
        self._reg("LoadLibraryA", 1, S, h._load_library)
        self._reg("LoadLibraryW", 1, S, h._load_library)
        self._reg("LoadLibraryExA", 3, S, h._load_library)
        self._reg("LoadLibraryExW", 3, S, h._load_library)
        self._reg("FreeLibrary", 1, S, h._ret1)
        self._reg("MultiByteToWideChar", 6, S, h._ret0)
        self._reg("WideCharToMultiByte", 8, S, h._ret0)
        self._reg("GetACP", 0, S, h._ret936)
        self._reg("GetOEMCP", 0, S, h._ret936)
        self._reg("GetConsoleOutputCP", 0, S, h._ret936)
        self._reg("SetConsoleOutputCP", 1, S, h._ret1)
        self._reg("GetSystemDefaultLCID", 0, S, h._ret0x804)
        self._reg("GetUserDefaultLCID", 0, S, h._ret0x804)
        self._reg("InitializeCriticalSection", 1, S, h._ret0)
        self._reg("DeleteCriticalSection", 1, S, h._ret0)
        self._reg("EnterCriticalSection", 1, S, h._ret0)
        self._reg("LeaveCriticalSection", 1, S, h._ret0)
        self._reg("TryEnterCriticalSection", 1, S, h._ret1)
        self._reg("InitializeCriticalSectionAndSpinCount", 2, S, h._ret1)
        self._reg("InitializeCriticalSectionEx", 3, S, h._ret1)
        self._reg("InitializeSListHead", 1, S, h._slist_init)
        self._reg("CompareStringW", 6, S, h._ret0)
        self._reg("FindClose", 1, S, h._ret1)
        self._reg("FindFirstFileExW", 6, S, h._ret0)
        self._reg("FindNextFileW", 2, S, h._ret0)
        self._reg("FreeEnvironmentStringsW", 1, S, h._ret0)
        self._reg("GetCPInfo", 2, S, h._ret1)
        self._reg("GetFileAttributesExW", 3, S, h._ret0)
        self._reg("GetFileSizeEx", 2, S, h._ret0)
        self._reg("GetStringTypeW", 4, S, h._ret0)
        self._reg("IsValidCodePage", 1, S, h._ret1)
        self._reg("LCMapStringW", 6, S, h._ret0)
        self._reg("SetFilePointerEx", 5, S, h._ret1)
        self._reg("SetStdHandle", 2, S, h._ret1)
        self._reg("SetCriticalSectionSpinCount", 2, S, h._ret0)
        self._reg("CreateEventA", 4, S, h._ret_neg1)
        self._reg("CreateEventW", 4, S, h._ret_neg1)
        self._reg("WaitForSingleObject", 2, S, h._ret0)
        self._reg("SetEvent", 1, S, h._ret1)
        self._reg("ResetEvent", 1, S, h._ret1)
        self._reg("FlsAlloc", 1, S, h._ret1)
        self._reg("FlsFree", 1, S, h._ret1)
        self._reg("FlsGetValue", 1, S, h._ret0)
        self._reg("FlsSetValue", 2, S, h._ret1)
        self._reg("FlsGetValue2", 1, S, h._ret0)
        self._reg("TlsAlloc", 1, S, h._ret1)
        self._reg("TlsFree", 1, S, h._ret1)
        self._reg("TlsGetValue", 1, S, h._ret0)
        self._reg("TlsSetValue", 2, S, h._ret1)
        self._reg("__security_init_cookie", 0, C, h._init_cookie)
        self._reg("__security_check_cookie", 1, C, h._ret0)
        self._reg("_initterm", 2, C, h._initterm)
        self._reg("_initterm_e", 2, C, h._initterm)
        self._reg("atexit", 1, C, h._ret0)
        self._reg("_atexit", 1, C, h._ret0)
        self._reg("exit", 1, C, h._exit_process)
        self._reg("_exit", 1, C, h._exit_process)
        self._reg("abort", 0, C, h._exit_process)
        self._reg("printf", 1, C, h._printf)
        self._reg("puts", 1, C, h._puts)
        self._reg("putchar", 1, C, h._putchar)
        self._reg("getchar", 0, C, h._getchar)
        self._reg("getch", 0, C, h._getchar)
        self._reg("_getch", 0, C, h._getchar)
        self._reg("fgetc", 1, C, h._getchar)
        self._reg("gets", 1, C, h._gets)
        self._reg("gets_s", 2, C, h._gets_s)
        self._reg("fgets", 3, C, h._fgets)
        self._reg("scanf", 2, C, h._scanf)
        self._reg("sscanf", 2, C, h._sscanf)
        self._reg("sprintf", 3, C, h._ret0)
        self._reg("snprintf", 3, C, h._ret0)
        self._reg("fprintf", 2, C, h._ret0)
        self._reg("__acrt_iob_func", 1, C, h._ret0)
        self._reg("__stdio_common_vfprintf", 5, C, h._ret0)
        self._reg("__stdio_common_vfscanf", 5, C, h._ret0)
        self._reg("__stdio_common_vsnprintf", 5, C, h._ret0)
        self._reg("__stdio_common_vsprintf", 5, C, h._ret0)
        self._reg("memcpy", 3, C, h._memcpy)
        self._reg("memset", 3, C, h._memset)
        self._reg("memmove", 3, C, h._memcpy)
        self._reg("memcmp", 3, C, h._memcmp)
        self._reg("strlen", 1, C, h._strlen)
        self._reg("strcmp", 2, C, h._strcmp)
        self._reg("strncmp", 3, C, h._strncmp)
        self._reg("_stricmp", 2, C, h._strcmp)
        self._reg("strcpy", 2, C, h._strcpy)
        self._reg("strncpy", 3, C, h._strcpy)
        self._reg("strchr", 2, C, h._ret0)
        self._reg("strstr", 2, C, h._ret0)
        self._reg("strtol", 3, C, h._ret0)
        self._reg("atoi", 1, C, h._ret0)
        self._reg("atol", 1, C, h._ret0)
        self._reg("rand", 0, C, h._rand)
        self._reg("srand", 1, C, h._ret0)
        self._reg("time", 1, C, h._tick)
        self._reg("clock", 0, C, h._tick)
        self._reg("getenv", 1, C, h._ret0)
        self._reg("system", 1, C, h._ret_neg1)
        self._reg("qsort", 4, C, h._ret0)
        self._reg("bsearch", 5, C, h._ret0)
        self._reg("isalpha", 1, C, h._isalpha)
        self._reg("isdigit", 1, C, h._isdigit)
        self._reg("isalnum", 1, C, h._isalnum)
        self._reg("isxdigit", 1, C, h._isxdigit)
        self._reg("islower", 1, C, h._islower)
        self._reg("isupper", 1, C, h._isupper)
        self._reg("isspace", 1, C, h._isspace)
        self._reg("toupper", 1, C, h._toupper)
        self._reg("tolower", 1, C, h._tolower)

    def install(self) -> None:
        uc = self.uc
        try:
            uc.mem_map(STUB_BASE, STUB_SIZE)
        except UcError:
            pass
        uc.mem_write(STUB_BASE, b"\x90" * 16)
        for imp in self.image.imports:
            if imp.name:
                addr = self._make_stub(imp.name, imp.dll)
                if imp.enc_val and not self._looks_like_addr(imp.enc_val):
                    self.enc_to_stub[imp.enc_val] = (imp.name, addr)
                uc.mem_write(imp.iat_addr, _s32(addr))
        for name in list(self.handlers):
            self._make_stub(name, self._dll_of.get(name, "kernel32.dll"))
        self._build_ntdll_exports()
        uc.hook_add(UC_HOOK_CODE, self._on_dispatcher, None,
                    self.dispatcher_addr, self.dispatcher_addr)
        self.decrypt_fn = self._discover_decrypt_fn()
        if self.decrypt_fn is not None:
            self._fallback_stub = self._next_stub
            self._next_stub += 16
            uc.mem_write(self._fallback_stub, b"\xC3" + b"\x90" * 15)
            uc.hook_add(UC_HOOK_CODE, self._on_decrypt_fn, None,
                        self.decrypt_fn, self.decrypt_fn)
        self._redirect_encoded_ptrs()

    def _redirect_encoded_ptrs(self) -> None:
        try:
            from capstone import Cs, CS_ARCH_X86, CS_MODE_32
        except ImportError:
            return
        img = self.image
        iat_slots = {imp.iat_addr for imp in img.imports}
        try:
            with open(self.session.exe_path, "rb") as f:
                raw = f.read()
        except OSError:
            return
        md = Cs(CS_ARCH_X86, CS_MODE_32)
        arg_counts: dict[int, int] = {}
        for va, vs, ro, rs, _nm in img.sections:
            if not ro or ro + rs > len(raw):
                continue
            try:
                insns = list(md.disasm(raw[ro:ro + rs], img.image_base + va))
            except Exception:
                continue
            for i, ins in enumerate(insns):
                if ins.mnemonic not in ("call", "jmp"):
                    continue
                op = ins.op_str
                if not op.startswith("dword ptr [0x") or not op.endswith("]"):
                    continue
                try:
                    slot = int(op[12:-1].replace("0x", ""), 16)
                except ValueError:
                    continue
                if not (img.image_base <= slot < img.image_base + img.size_of_image):
                    continue
                if slot in iat_slots:
                    continue
                n = 0
                for j in range(i - 1, max(-1, i - 24), -1):
                    p = insns[j]
                    if p.mnemonic == "push":
                        n += 1
                    elif p.mnemonic in ("call", "ret", "jmp", "int3"):
                        break
                    else:
                        break
                arg_counts[slot] = max(arg_counts.get(slot, 0), n)
        for slot, n in arg_counts.items():
            try:
                val = _u32(self.uc.mem_read(slot, 4))
            except UcError:
                continue
            if val == 0 or (img.image_base <= val < img.image_base + img.size_of_image):
                continue
            stub = self._next_stub
            self._next_stub += 16
            if n > 0:
                self.uc.mem_write(stub, b"\xC2" + (n * 4).to_bytes(2, "little") + b"\x90" * 13)
            else:
                self.uc.mem_write(stub, b"\xC3" + b"\x90" * 15)
            try:
                self.uc.mem_write(slot, _s32(stub))
            except UcError:
                continue
            self.session.api_calls[f"__encptr__0x{slot:X}({n})->0x{val:X}"] = 1

    @staticmethod
    def _looks_like_addr(v: int) -> bool:
        if v == 0:
            return True
        return (0x400000 <= v <= 0x7FFFFFFF) or v >= 0x7FF00000

    def _discover_decrypt_fn(self) -> Optional[int]:
        try:
            import capstone
            md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        except Exception:  # noqa: BLE001
            return None
        img = self.image
        base = img.image_base
        insns = []
        for va, vs, ro, rs, _nm in img.sections:
            if not ro:
                continue
            with open(self.image.path, "rb") as f:
                f.seek(ro)
                raw = f.read(rs)
            try:
                for ins in md.disasm(raw, base + va):
                    insns.append((ins.address, ins.mnemonic, ins.op_str))
            except Exception:  # noqa: BLE001
                continue
        for i, (addr, mn, op) in enumerate(insns):
            if mn != "ror" or op != "eax, cl":
                continue
            window = insns[max(0, i - 30):i]
            x_addr = None
            found_and = False
            mov_xs = set()
            for _a, _mn, _op in window:
                if _mn == "and" and _op == "ecx, 0x1f":
                    found_and = True
                elif _mn == "xor" and _op.startswith("eax, dword ptr ["):
                    try:
                        x_addr = int(_op[16:-1].replace("0x", ""), 16)
                    except ValueError:
                        x_addr = None
                elif _mn == "mov" and _op.startswith("ecx, dword ptr ["):
                    try:
                        mov_xs.add(int(_op[16:-1].replace("0x", ""), 16))
                    except ValueError:
                        pass
            if found_and and x_addr is not None and x_addr in mov_xs:
                entry = None
                for j in range(i - 1, max(0, i - 200), -1):
                    if insns[j][1].startswith("ret"):
                        entry = insns[j + 1][0]
                        break
                if entry is None:
                    entry = insns[max(0, i - 200)][0]
                return entry
        return None

    def _on_decrypt_fn(self, uc, address, size, user_data):
        esp = uc.reg_read(x86_const.UC_X86_REG_ESP)
        try:
            enc = _u32(uc.mem_read(esp + 4, 4))
        except UcError:
            enc = 0
        hit = self.enc_to_stub.get(enc)
        if hit is not None:
            name, stub = hit
            key = f"decrypt_iat->{name}"
        else:
            stub = self._fallback_stub
            key = f"decrypt_fallback(0x{enc:X})"
        self.session.api_calls[key] = self.session.api_calls.get(key, 0) + 1
        ret_addr = _u32(uc.mem_read(esp, 4))
        uc.reg_write(x86_const.UC_X86_REG_EAX, stub)
        uc.reg_write(x86_const.UC_X86_REG_ESP, esp + 4)
        uc.reg_write(x86_const.UC_X86_REG_EIP, ret_addr)

    def _make_stub(self, name: str, dll: str) -> int:
        for _a, _n in self.stub_map.items():
            if _n == name:
                return _a
        addr = self._next_stub
        self._next_stub += 16
        handler = self.handlers.get(name)
        if handler:
            sid = self.api_id.get(name)
            if sid is None:
                sid = len(self.api_id)
                self.api_id[name] = sid
            code = b"\xB8" + _s32(sid) + b"\xE9" + _s32(
                self.dispatcher_addr - (addr + 10)) + b"\x90" * 6
        else:
            info = APISTUB_TABLE.get(name)
            argc = ARGC_FIX.get(name)
            if argc is None:
                argc = (info or {}).get("argc", 0) or 0
            conv = _CDL if (dll.lower().startswith(CDECL_DLL_PREFIXES) or name in CDECL_NAMES) else _STD
            if conv == _STD and argc:
                code = b"\xC2" + ctypes.c_uint16(argc * 4).value.to_bytes(2, "little") + b"\x90" * 13
            else:
                code = b"\xC3" + b"\x90" * 15
        self.uc.mem_write(addr, code)
        self.stub_map[addr] = name
        return addr

    def _build_ntdll_exports(self) -> None:
        uc = self.uc
        try:
            uc.mem_map(NTDLL_EXPORT_BASE, NTDLL_EXPORT_SIZE)
        except UcError:
            pass
        names = [n for n in APISTUB_TABLE if APISTUB_TABLE[n].get("dll") == "ntdll"]
        names.sort()
        n = len(names)
        dir_size = 40
        funcs_off = dir_size
        names_off = funcs_off + 4 * n
        ords_off = names_off + 4 * n
        str_off = ords_off + 2 * n
        str_offsets = {}
        cur = str_off
        for nm in names:
            str_offsets[nm] = cur
            cur += len(nm) + 1
        total = cur
        data = bytearray(total)
        struct.pack_into("<11I", data, 0,
                         0, 0, 0, 0,
                         total,
                         1,
                         n, n,
                         funcs_off, names_off, ords_off)
        for i, nm in enumerate(names):
            stub = self._make_stub(nm, "ntdll.dll")
            struct.pack_into("<I", data, funcs_off + i * 4, (stub - NTDLL_EXPORT_BASE) & 0xFFFFFFFF)
            struct.pack_into("<I", data, names_off + i * 4, str_offsets[nm])
            struct.pack_into("<H", data, ords_off + i * 2, i)
            raw = nm.encode("ascii", "ignore") + b"\x00"
            data[str_offsets[nm]:str_offsets[nm] + len(raw)] = raw
        data += b"ntdll.dll\x00"
        uc.mem_write(NTDLL_EXPORT_BASE, bytes(data))

    def _on_dispatcher(self, uc, address, size, user_data):
        if address != self.dispatcher_addr:
            return
        sid = uc.reg_read(x86_const.UC_X86_REG_EAX)
        name = None
        for _n, _i in self.api_id.items():
            if _i == sid:
                name = _n
                break
        if name is None:
            return
        esp = uc.reg_read(x86_const.UC_X86_REG_ESP)
        ret_addr = _u32(uc.mem_read(esp, 4))
        argc, conv, fn = self.handlers.get(name, (0, _STD, None))
        args = []
        for i in range(argc):
            try:
                args.append(_u32(uc.mem_read(esp + 4 + i * 4, 4)))
            except UcError:
                args.append(0)
        self.session.api_calls[name] = self.session.api_calls.get(name, 0) + 1
        try:
            fn(args)
        except UcError as e:
            self.session.last_error = f"{name}: {e}"
        if getattr(self.session, "stop_reason", ""):
            return
        clean = argc * 4 if conv == _STD else 0
        uc.reg_write(x86_const.UC_X86_REG_ESP, esp + clean + 4)
        force = getattr(self.session, "_force_eip", None)
        if force is not None:
            self.session._force_eip = None
            uc.reg_write(x86_const.UC_X86_REG_EIP, force)
        else:
            uc.reg_write(x86_const.UC_X86_REG_EIP, ret_addr)

    def _add_veh(self, args):
        if len(args) > 1 and args[1]:
            handler = args[1]
            if handler not in self.session.veh_handlers:
                self.session.veh_handlers.append(handler)
                self.session.api_calls[f"__veh_add__0x{handler:X}"] = \
                    self.session.api_calls.get(f"__veh_add__0x{handler:X}", 0) + 1
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 1)

    def _remove_veh(self, args):
        handler = args[0] if args else 0
        try:
            self.session.veh_handlers.remove(handler)
            self.session.api_calls[f"__veh_remove__0x{handler:X}"] = \
                self.session.api_calls.get(f"__veh_remove__0x{handler:X}", 0) + 1
            self.uc.reg_write(x86_const.UC_X86_REG_EAX, 1)
        except ValueError:
            self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0)

    def _nt_continue(self, args):
        ctx = args[0] if args else 0
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0)
        if ctx:
            try:
                eip = _u32(self.uc.mem_read(ctx + 0xB8, 4))
                esp = _u32(self.uc.mem_read(ctx + 0xC4, 4))
                for reg, off in (("EBX", 0xA4), ("ECX", 0xAC), ("EDX", 0xA8),
                                 ("ESI", 0xA0), ("EDI", 0x9C), ("EBP", 0xB4), ("EAX", 0xB0)):
                    try:
                        v = _u32(self.uc.mem_read(ctx + off, 4))
                        self.uc.reg_write(getattr(x86_const, "UC_X86_REG_" + reg), v)
                    except UcError:
                        pass
                self.uc.reg_write(x86_const.UC_X86_REG_ESP, esp)
                self.session._force_eip = eip
                self.session.api_calls[f"__ntcontinue__0x{eip:X}"] = \
                    self.session.api_calls.get(f"__ntcontinue__0x{eip:X}", 0) + 1
            except UcError:
                pass

    def _set_uef(self, args):
        old = getattr(self.session, "uef_handler", 0)
        self.session.uef_handler = args[0] if args else 0
        if self.session.uef_handler:
            self.session.api_calls[f"__uef_set__0x{self.session.uef_handler:X}"] = \
                self.session.api_calls.get(f"__uef_set__0x{self.session.uef_handler:X}", 0) + 1
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, old)

    def _ret0(self, args):
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0)

    def _ret1(self, args):
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 1)

    def _ret2(self, args):
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 2)

    def _ret_neg1(self, args):
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, -1 & 0xFFFFFFFF)

    def _ret_neg2(self, args):
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, -2 & 0xFFFFFFFF)

    def _ret936(self, args):
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 936)

    def _ret0x804(self, args):
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0x804)

    def _ret_port_not_set(self, args):
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0xC0000353)

    def _heap_base(self, args):
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, ENV.HEAP_BASE)

    def _tick(self, args):
        self.session.clock += 16
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, self.session.clock & 0xFFFFFFFF)

    def _qpc(self, args):
        self.session.clock += 16
        if args:
            self.uc.mem_write(args[0], ctypes.c_uint64(self.session.clock).value.to_bytes(8, "little"))
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 1)

    def _qpc_freq(self, args):
        if args:
            self.uc.mem_write(args[0], ctypes.c_uint64(10000000).value.to_bytes(8, "little"))
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 1)

    def _filetime(self, args):
        t = self.session.clock + 0x1D48E46000
        if args:
            self.uc.mem_write(args[0], ctypes.c_uint64(t & 0xFFFFFFFFFFFFFFFF).value.to_bytes(8, "little"))
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0)

    def _pid(self, args):
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, self.session.pid)

    def _tid(self, args):
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, self.session.pid ^ 0x1000)

    def _check_remote_dbg(self, args):
        if len(args) > 1:
            try:
                self.uc.mem_write(args[1], b"\x00" * 4)
            except UcError:
                pass
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 1)

    def _query_info_process(self, args):
        cls = args[1] if len(args) > 1 else 0
        buf = args[2] if len(args) > 2 else 0
        if cls == 7 and buf:
            self.uc.mem_write(buf, b"\x00" * 4)
        elif cls == 0x1F and buf:
            self.uc.mem_write(buf, b"\x00" * 4)
        elif cls == 0x1E and buf:
            self.uc.mem_write(buf, b"\x01\x00\x00\x00")
        elif cls == 0 and buf:
            self.uc.mem_write(buf + 24,
                              ctypes.c_uint32(self.session.parent_pid).value.to_bytes(4, "little"))
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0)

    def _query_sys_info(self, args):
        cls = args[0] if args else 0
        buf = args[1] if len(args) > 1 else 0
        if cls == 0x23 and buf:
            self.uc.mem_write(buf, b"\x00\x00\x00\x00")
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0)

    def _is_wow64(self, args):
        if len(args) > 1 and args[1]:
            self.uc.mem_write(args[1], b"\x00" * 4)
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 1)

    def _alloc_vm(self, args):
        if len(args) >= 5 and args[1]:
            try:
                size = _u32(self.uc.mem_read(args[3], 4))
            except UcError:
                size = 0x1000
            base = self.session.heap_alloc(size or 0x1000)
            try:
                self.uc.mem_write(args[1], _s32(base))
                self.uc.mem_write(args[3], _s32(size or 0x1000))
            except UcError:
                pass
        else:
            n = len(args)
            if n == 1:
                size = args[0]
            elif n == 2:
                size = args[1]
            elif n == 3:
                size = args[2]
            elif n == 4:
                size = args[1]
            else:
                size = args[0] if args else 0x1000
            base = self.session.heap_alloc(size or 0x1000)
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, base)

    def _exit_process(self, args):
        self.session.stop_reason = "exit"
        self.session.exit_code = args[0] if args else 0
        try:
            self.uc.emu_stop()
        except Exception:
            pass

    def _get_last_error(self, args):
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, self.session.last_error_code)

    def _set_last_error(self, args):
        self.session.last_error_code = args[0] if args else 0
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0)

    def _get_module_handle(self, args):
        if not args or not args[0]:
            self.uc.reg_write(x86_const.UC_X86_REG_EAX, self.image.image_base)
            return
        try:
            raw = self.uc.mem_read(args[0], 64)
        except UcError:
            raw = b""
        nm = raw.split(b"\x00")[0]
        s = ""
        if len(nm) > 2 and nm[1] == 0:
            try:
                s = nm.decode("utf-16-le")
            except Exception:
                s = ""
        else:
            s = nm.decode("ascii", "ignore")
        low = s.lower().rstrip()
        exe_low = (self.session.exe_path.split("\\")[-1] or "").lower()
        if low.endswith((".exe", ".dll")) and exe_low and low == exe_low:
            self.uc.reg_write(x86_const.UC_X86_REG_EAX, self.image.image_base)
        elif low in ("ntdll.dll", "ntdll"):
            self.uc.reg_write(x86_const.UC_X86_REG_EAX, NTDLL_EXPORT_BASE)
        else:
            self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0)

    def _module_file_name(self, args):
        if len(args) > 1 and args[1]:
            path = self.session.exe_path.encode("utf-16-le")
            try:
                self.uc.mem_write(args[1], path + b"\x00\x00")
            except UcError:
                pass
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, len(path) // 2)

    def _get_proc_address(self, args):
        if len(args) < 2 or not args[1]:
            self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0)
            return
        try:
            raw = self.uc.mem_read(args[1], 128)
        except UcError:
            raw = b""
        nm = raw.split(b"\x00")[0].decode("ascii", "ignore")
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, self._lookup_api(nm))

    def _lookup_api(self, name: str) -> int:
        if name in self.api_id:
            for a, n in self.stub_map.items():
                if n == name:
                    return a
        if name in APISTUB_TABLE:
            return self._make_stub(name, APISTUB_TABLE[name].get("dll", ""))
        self.session.api_calls[f"__unknown__{name}"] = self.session.api_calls.get(f"__unknown__{name}", 0) + 1
        return self._make_stub(name, "")

    def _cmdline_a(self, args):
        buf = self.session.cmdline.encode("ascii", "ignore") + b"\x00"
        self.uc.mem_write(ENV.ARGS_ADDR + 0x300, buf)
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, ENV.ARGS_ADDR + 0x300)

    def _cmdline_w(self, args):
        buf = self.session.cmdline.encode("utf-16-le") + b"\x00\x00"
        self.uc.mem_write(ENV.ARGS_ADDR + 0x300, buf)
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, ENV.ARGS_ADDR + 0x300)

    def _std_handle(self, args):
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, (args[0] if args else -12) & 0xFFFFFFFF)

    def _slist_init(self, args):
        if args:
            try:
                self.uc.mem_write(args[0], b"\x00" * 8)
            except UcError:
                pass
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0)

    def _startup_info(self, args):
        if not args:
            return
        try:
            p = args[0]
            blk = getattr(self.session, "_startup_block", 0)
            if not blk:
                blk = self.session.heap_alloc(0x120)
                self.session._startup_block = blk
                data = bytearray(0x120)
                data[0:4] = _s32(0x100)
                data[4:8] = _s32(blk + 8)
                self.uc.mem_write(blk, bytes(data))
            si = bytearray(0x44)
            si[0:4] = _s32(0x44)
            si[0x34:0x38] = _s32(blk)
            si[0x38:0x3C] = _s32(0)
            si[0x3C:0x40] = _s32(0)
            si[0x40:0x44] = _s32(0)
            self.uc.mem_write(p, bytes(si))
        except UcError:
            pass
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0)

    def _write_file(self, args):
        if len(args) >= 3 and args[1]:
            try:
                data = self.uc.mem_read(args[1], min(args[2], 0x10000))
            except UcError:
                data = b""
            self.session.output.append(data.decode("utf-8", errors="replace"))
            if args[3]:
                try:
                    self.uc.mem_write(args[3], _s32(len(data)))
                except UcError:
                    pass
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 1)

    def _read_file(self, args):
        if len(args) >= 3 and args[1] and self.session.inputs:
            n = args[2] or 0x1000
            data = self.session.inputs[0]
            if n >= len(data) + 1:
                take = data + "\n"
                self.session.inputs.pop(0)
            else:
                take = data[:n]
                self.session.inputs[0] = data[n:]
            raw = take.encode("ascii", "ignore")
            try:
                self.uc.mem_write(args[1], raw + b"\x00" * (n - len(raw)))
            except UcError:
                raw = b""
            if args[3]:
                try:
                    self.uc.mem_write(args[3], _s32(len(raw)))
                except UcError:
                    pass
            self.uc.reg_write(x86_const.UC_X86_REG_EAX, 1)
        else:
            if len(args) >= 3 and args[1]:
                try:
                    self.uc.mem_write(args[1], b"\x00" * args[2])
                except UcError:
                    pass
            self.uc.reg_write(x86_const.UC_X86_REG_EAX, 1)

    def _output_debug(self, args):
        if args and args[0]:
            try:
                data = self.uc.mem_read(args[0], 0x400)
            except UcError:
                data = b""
            self.session.output.append(data.split(b"\x00")[0].decode("utf-8", errors="replace"))
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0)

    def _load_library(self, args):
        if self._module_stub == 0:
            self._module_stub = self._next_stub
            self._next_stub += 16
            self.uc.mem_write(self._module_stub, b"\xC3" + b"\x90" * 15)
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, self._module_stub)

    def _init_cookie(self, args):
        cookie = 0x0D0C0B0A
        if self.image.security_cookie:
            try:
                self.uc.mem_write(self.image.security_cookie,
                                  ctypes.c_uint32(cookie).value.to_bytes(4, "little"))
            except UcError:
                pass
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0)

    def _initterm(self, args):
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0)

    def _printf(self, args):
        if args and args[0]:
            try:
                fmt = self.uc.mem_read(args[0], 0x200)
            except UcError:
                fmt = b""
            txt = fmt.split(b"\x00")[0].decode("utf-8", errors="replace")
            out = txt
            if "%s" in txt and len(args) > 1:
                try:
                    s = self.uc.mem_read(args[1], 0x200).split(b"\x00")[0].decode("utf-8", errors="replace")
                    out = txt.replace("%s", s, 1)
                except UcError:
                    pass
            out = out.replace("%d", "0", 1).replace("%c", "?", 1)
            self.session.output.append(out)
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0)

    def _puts(self, args):
        if args and args[0]:
            try:
                s = self.uc.mem_read(args[0], 0x200).split(b"\x00")[0].decode("utf-8", errors="replace")
            except UcError:
                s = ""
            self.session.output.append(s + "\n")
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0)

    def _putchar(self, args):
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, (args[0] if args else 0) & 0xFF)

    def _getchar(self, args):
        if self.session.inputs:
            data = self.session.inputs[0]
            if data:
                self.session.inputs[0] = data[1:]
                self.uc.reg_write(x86_const.UC_X86_REG_EAX, ord(data[0]))
                return
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0xFFFFFFFF)

    def _gets(self, args):
        buf = args[0] if args else 0
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0)
        if not (buf and self.session.inputs):
            return
        data = self.session.inputs[0]
        nl = data.find("\n")
        if nl >= 0:
            line, self.session.inputs[0] = data[:nl], data[nl + 1:]
        else:
            line, self.session.inputs = data, self.session.inputs[1:]
        try:
            self.uc.mem_write(buf, line.encode("ascii", "ignore") + b"\x00")
            self.uc.reg_write(x86_const.UC_X86_REG_EAX, buf)
        except UcError:
            pass

    def _gets_s(self, args):
        buf, size = (args[0], args[1] or 0x1000) if len(args) > 1 else (args[0] if args else 0, 0)
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0)
        if not (buf and size and self.session.inputs):
            return
        data = self.session.inputs[0]
        nl = data.find("\n")
        if nl >= 0:
            line, self.session.inputs[0] = data[:nl], data[nl + 1:]
        else:
            line, self.session.inputs = data, self.session.inputs[1:]
        line = line[:size - 1]
        try:
            self.uc.mem_write(buf, line.encode("ascii", "ignore") + b"\x00")
            self.uc.reg_write(x86_const.UC_X86_REG_EAX, buf)
        except UcError:
            pass

    def _fgets(self, args):
        buf, size = (args[0], args[1] or 0x1000) if len(args) > 1 else (args[0] if args else 0, 0)
        self._gets_s([buf, size])

    def _scanf(self, args):
        if args and len(args) > 1 and self.session.inputs:
            data = self.session.inputs.pop(0).encode("ascii", "ignore") + b"\x00"
            try:
                self.uc.mem_write(args[1], data)
            except UcError:
                pass
            self.uc.reg_write(x86_const.UC_X86_REG_EAX, 1)
            self.session.api_calls[f"__scanf__0x{args[1]:X}"] = data[:64]
        else:
            self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0)

    def _sscanf(self, args):
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0)

    def _memcpy(self, args):
        if len(args) >= 3 and args[0] and args[1]:
            try:
                data = self.uc.mem_read(args[1], args[2])
                self.uc.mem_write(args[0], bytes(data))
            except UcError:
                pass
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, args[0] if args else 0)

    def _memset(self, args):
        if len(args) >= 3 and args[0]:
            try:
                self.uc.mem_write(args[0], bytes([args[1] & 0xFF]) * args[2])
            except UcError:
                pass
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, args[0])

    def _memcmp(self, args):
        r = 0
        if len(args) >= 3 and args[0] and args[1]:
            try:
                a = self.uc.mem_read(args[0], args[2])
                b = self.uc.mem_read(args[1], args[2])
                r = (a > b) - (a < b)
            except UcError:
                r = 0
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, r)

    def _strlen(self, args):
        n = 0
        if args and args[0]:
            try:
                data = self.uc.mem_read(args[0], 0x1000)
                n = data.find(b"\x00")
                if n == -1:
                    n = 0x1000
            except UcError:
                n = 0
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, n)

    def _strcmp(self, args):
        r = 0
        if len(args) >= 2 and args[0] and args[1]:
            try:
                a = self.uc.mem_read(args[0], 0x1000).split(b"\x00")[0]
                b = self.uc.mem_read(args[1], 0x1000).split(b"\x00")[0]
                r = (a > b) - (a < b)
            except UcError:
                r = 0
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, r)

    def _strncmp(self, args):
        r = 0
        if len(args) >= 3 and args[0] and args[1]:
            try:
                a = self.uc.mem_read(args[0], min(args[2], 0x1000))
                b = self.uc.mem_read(args[1], min(args[2], 0x1000))
                r = (a > b) - (a < b)
            except UcError:
                r = 0
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, r)

    def _strcpy(self, args):
        if len(args) >= 2 and args[0] and args[1]:
            try:
                data = self.uc.mem_read(args[1], 0x1000)
                end = data.find(b"\x00")
                self.uc.mem_write(args[0], bytes(data[: end + 1] if end != -1 else data))
            except UcError:
                pass
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, args[0] if args else 0)

    def _rand(self, args):
        self.session.clock = (self.session.clock * 1103515245 + 12345) & 0x7FFFFFFF
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, self.session.clock)

    def _isalpha(self, args):
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0)

    def _isdigit(self, args):
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0)

    def _isalnum(self, args):
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0)

    def _isxdigit(self, args):
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0)

    def _islower(self, args):
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0)

    def _isupper(self, args):
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0)

    def _isspace(self, args):
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, 0)

    def _toupper(self, args):
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, args[0] if args else 0)

    def _tolower(self, args):
        self.uc.reg_write(x86_const.UC_X86_REG_EAX, args[0] if args else 0)

    def stats(self) -> dict:
        by_dll = {}
        for _n, m in APISTUB_TABLE.items():
            d = m.get("dll", "")
            by_dll[d] = by_dll.get(d, 0) + 1
        return {
            "total_names": len(APISTUB_TABLE),
            "handlers": len(self.handlers),
            "by_dll": by_dll,
        }
