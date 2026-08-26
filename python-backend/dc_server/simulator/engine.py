# -*- coding: utf-8 -*-
import ctypes
import time as _time

from typing import Optional

from unicorn import Uc, UC_ARCH_X86, UC_MODE_32, UC_HOOK_CODE, UC_HOOK_MEM_WRITE, \
    UC_HOOK_INSN, UC_HOOK_MEM_FETCH_UNMAPPED, UC_HOOK_MEM_READ_UNMAPPED, \
    UC_HOOK_MEM_WRITE_UNMAPPED, UC_HOOK_MEM_FETCH_INVALID, \
    UC_MEM_READ_UNMAPPED, UC_MEM_WRITE_UNMAPPED, UC_PROT_ALL, x86_const
from unicorn.unicorn import UcError

from .loader import load_pe, apply_relocs
from . import env as ENV
from .apistub import ApiStubs, NTDLL_EXPORT_BASE
from .deobf import correct_rip as _deobf_correct

def _u32(data, off=0) -> int:
    return int.from_bytes(data[off:off + 4], "little")

def _u16(data, off=0) -> int:
    return int.from_bytes(data[off:off + 2], "little")

def _p32(buf: bytearray, off: int, v: int) -> None:
    buf[off:off + 4] = (v & 0xFFFFFFFF).to_bytes(4, "little")

X86_REGS = [
    ("eax", x86_const.UC_X86_REG_EAX), ("ebx", x86_const.UC_X86_REG_EBX),
    ("ecx", x86_const.UC_X86_REG_ECX), ("edx", x86_const.UC_X86_REG_EDX),
    ("esi", x86_const.UC_X86_REG_ESI), ("edi", x86_const.UC_X86_REG_EDI),
    ("ebp", x86_const.UC_X86_REG_EBP), ("esp", x86_const.UC_X86_REG_ESP),
    ("eip", x86_const.UC_X86_REG_EIP), ("eflags", x86_const.UC_X86_REG_EFLAGS),
]

TRACE_LIMIT = 200000
WRITES_LIMIT = 20000

class SimSession:

    def __init__(self, task_id: str):
        self.task_id = task_id
        self.uc: Optional[Uc] = None
        self.image = None
        self.stubs: ApiStubs | None = None
        self.exe_path = ""
        self.cmdline = ""
        self.inputs: list[str] = []
        self.output: list[str] = []
        self.clock = 0
        self.pid = 0x1A2B
        self.parent_pid = 0x1F24
        self.api_calls: dict[str, int] = {}
        self.stop_reason = ""
        self.exit_code = 0
        self.last_error = ""
        self.last_error_code = 0
        self._heap_ptr = ENV.HEAP_BASE + 0x1000
        self._trace: list[int] = []
        self._writes: list = []
        self._executed: set[int] = set()
        self._dyncode: list = []
        self._trace_on = False
        self._snap = None

    def heap_alloc(self, size: int) -> int:
        size = (size + 0xFFF) & ~0xFFF
        if size < 0x1000:
            size = 0x1000
        addr = self._heap_ptr
        self._heap_ptr += size
        try:
            self.uc.mem_map(addr, size)
        except UcError:
            pass
        return addr

    def load(self, exe_path: str, args: Optional[list] = None,
             inputs: Optional[list] = None) -> str:
        args = args or []
        inputs = inputs or []
        self.uc = Uc(UC_ARCH_X86, UC_MODE_32)
        self.exe_path = exe_path
        self.cmdline = " ".join([exe_path] + args)
        self.inputs = list(inputs)
        self.output = []
        self.api_calls = {}
        self.stop_reason = ""
        self.exit_code = 0
        self._trace = []
        self._writes = []
        self._executed = set()
        self._dyncode = []
        self._snap = None

        img = load_pe(exe_path)
        if img.is64:
            raise NotImplementedError("x64 模拟尚未实现（本项目目标 32 位）")
        self.image = img
        with open(exe_path, "rb") as f:
            raw = f.read()
        raw = apply_relocs(img, raw, img.image_base)
        base = img.image_base
        size = (img.size_of_image + 0xFFF) & ~0xFFF
        self.uc.mem_map(base, size)
        for va, vs, ro, rs, _nm in img.sections:
            if ro:
                self.uc.mem_write(base + va, raw[ro:ro + rs])
        ENV.build_env(self.uc, img, exe_path, args)
        try:
            self._teb64, self._peb64 = ENV.build_env64(self.uc, img, exe_path, args)
        except Exception:
            self._teb64 = self._peb64 = 0
        self.stubs = ApiStubs(self.uc, img, self)
        self.stubs.install()
        self._win_start = -1
        self._win_data = b""
        self._ff_auto_cnt = 0
        self._ff_auto_active = False
        self._ff_auto_pending = False
        self._ff_dyn_addrs: set = set()
        self._code_hook = self.uc.hook_add(UC_HOOK_CODE, self._on_code)
        self._write_hook = self.uc.hook_add(UC_HOOK_MEM_WRITE, self._on_write)
        self.uc.reg_write(x86_const.UC_X86_REG_EIP, base + img.entry_point)
        self.uc.reg_write(x86_const.UC_X86_REG_ESP, ENV.STACK_BASE + ENV.STACK_SIZE)
        for _n, r in X86_REGS:
            if r not in (x86_const.UC_X86_REG_EIP, x86_const.UC_X86_REG_ESP,
                         x86_const.UC_X86_REG_FS_BASE):
                try:
                    self.uc.reg_write(r, 0)
                except UcError:
                    pass
        self._snap = self._snapshot_now()
        self.veh_handlers: list[int] = []
        self.uef_handler = 0
        self._force_eip: Optional[int] = None
        self._ctx_buf = 0x7F000000
        self._veh_ret_stub = 0
        self._uef_ret_stub = 0
        self._seh_ret_stub = 0
        self._no_dispatch = False
        try:
            self.uc.mem_map(self._ctx_buf, 0x1000)
        except UcError:
            pass
        self.uc.hook_add(UC_HOOK_MEM_FETCH_UNMAPPED, self._on_mem_unmapped)
        self.uc.hook_add(UC_HOOK_MEM_READ_UNMAPPED, self._on_mem_unmapped)
        self.uc.hook_add(UC_HOOK_MEM_WRITE_UNMAPPED, self._on_mem_unmapped)
        self.uc.hook_add(UC_HOOK_MEM_FETCH_INVALID, self._on_mem_unmapped)
        return self.describe()

    def describe(self) -> str:
        img = self.image
        lines = [
            f"[load] {self.exe_path}",
            f"  image_base=0x{img.image_base:X} entry=0x{img.image_base + img.entry_point:X}",
            f"  sections={len(img.sections)} imports={len(img.imports)} "
            f"tls_callbacks={[hex(t) for t in img.tls_callbacks]}",
            f"  inputs={self.inputs}",
            f"  api_stubs: total_names={self.stubs.stats()['total_names']} "
            f"handlers={self.stubs.stats()['handlers']}",
            f"  ntdll_export_base=0x{NTDLL_EXPORT_BASE:X} stub_base=0x10000000",
            "  [env] PEB=0x7FFDE000 BeingDebugged=0 NtGlobalFlag=0 FS=0x7FFD0000",
        ]
        return "\n".join(lines)

    def run(self, until_addr: Optional[int] = None, steps_limit: int = 0,
            timeout_ms: int = 0, trace_on: bool = False) -> dict:
        if self.uc is None:
            return {"error": "未加载（先 load）"}
        if steps_limit > 0:
            self.w64_count = min(steps_limit, 100000000)
        self._trace_on = trace_on
        self.stop_reason = ""
        eip = self.uc.reg_read(x86_const.UC_X86_REG_EIP)
        until = until_addr or (self.image.image_base + self.image.size_of_image)
        t0 = _time.time()
        _rem_ms = timeout_ms
        _seg = 5_000_000
        _remain = steps_limit or 0
        while True:
            _cnt = min(_remain, _seg) if _remain else _seg
            try:
                self.uc.emu_start(eip, until, timeout=_rem_ms * 1000, count=_cnt)
            except UcError as e:
                self.stop_reason = f"error: {e}"
            except Exception as e:  # noqa: BLE001
                self.stop_reason = f"error: {e}"
            if _remain:
                _remain -= _cnt
            eip2 = self.uc.reg_read(x86_const.UC_X86_REG_EIP)
            if self.stop_reason or self._ff_auto_cnt >= 8:
                break
            if not (0x4010E0 <= eip2 < 0x401260):
                break
            _snap = self._snapshot_now()
            _hook = getattr(self, "_code_hook", None)
            if _hook is not None:
                try:
                    self.uc.hook_del(_hook)
                except Exception:
                    pass
            _wh = getattr(self, "_write_hook", None)
            if _wh is not None:
                try:
                    self.uc.hook_del(_wh)
                except Exception:
                    _wh = None
            self._ff_keep = []
            for _a in ((0x4010E0, 0x401125, 0x401130, 0x401160,
                        0x7F000D00, 0x7F000D10, 0x7F000D20, 0x1000FF40)
                       + tuple(self._ff_dyn_addrs)[:128]):
                try:
                    self._ff_keep.append(self.uc.hook_add(UC_HOOK_CODE, self._on_code,
                                                          0, _a, _a))
                except Exception:
                    pass
            try:
                self.uc.emu_start(eip2, 0x841000,
                                  timeout=30_000_000, count=100_000_000)
                _ok = True
            except Exception as _e:  # noqa: BLE001
                _ok = False
                try:
                    _bad = self.uc.reg_read(x86_const.UC_X86_REG_EIP)
                    for _off in (1, 2, 3):
                        _cand = _bad - _off
                        if 0x400000 <= _cand < 0x900000 and len(self._ff_dyn_addrs) < 128:
                            self._ff_dyn_addrs.add(_cand)
                except Exception:
                    pass
            for _h in getattr(self, "_ff_keep", []):
                try:
                    self.uc.hook_del(_h)
                except Exception:
                    pass
            self._ff_keep = []
            try:
                self._code_hook = self.uc.hook_add(UC_HOOK_CODE, self._on_code)
            except Exception:
                self._code_hook = None
            if _wh is not None:
                try:
                    self._write_hook = self.uc.hook_add(UC_HOOK_MEM_WRITE, self._on_write)
                except Exception:
                    self._write_hook = None
            if not _ok:
                self._restore_now(_snap)
                self.stop_reason = ""
                break
            self._ff_auto_cnt += 1
            self.api_calls["__fast_auto__0x401160"] = \
                self.api_calls.get("__fast_auto__0x401160", 0) + 1
            eip = self.uc.reg_read(x86_const.UC_X86_REG_EIP)
            if timeout_ms:
                _rem_ms = max(0, int(timeout_ms - (_time.time() - t0) * 1000))
                if _rem_ms <= 0:
                    break
        elapsed = _time.time() - t0
        if not self.stop_reason:
            if getattr(self, "_unhandled", False):
                self.stop_reason = "unhandled_exception"
            else:
                self.stop_reason = "timeout" if timeout_ms and elapsed * 1000 >= timeout_ms - 1 else "range_end"
        eip = self.uc.reg_read(x86_const.UC_X86_REG_EIP)
        _api_calls = {k: v for k, v in self.api_calls.items()
                      if not (k.startswith("__wow64_") or k.startswith("__fast_auto__"))}
        return {
            "eip": hex(eip),
            "stop_reason": self.stop_reason,
            "exit_code": self.exit_code,
            "elapsed_ms": round(elapsed * 1000, 1),
            "trace_len": len(self._trace),
            "dyncode_blocks": len(self._dyncode),
            "api_calls": _api_calls,
            "output": "".join(self.output)[-4000:],
        }

    def fast_forward(self, until_addr: int, count: int = 0,
                     timeout_ms: int = 30000) -> dict:
        if self.uc is None:
            return {"error": "未加载（先 load）"}
        snap = self._snapshot_now()
        eip = self.uc.reg_read(x86_const.UC_X86_REG_EIP)
        until = until_addr or (self.image.image_base + self.image.size_of_image)
        hook = getattr(self, "_code_hook", None)
        if hook is not None:
            try:
                self.uc.hook_del(hook)
            except Exception:
                pass
        self._ff_keep: list = []
        for _a in (0x4010E0, 0x401125, 0x401130, 0x401160,
                   0x7F000D00, 0x7F000D10, 0x7F000D20, 0x1000FF40):
            try:
                self._ff_keep.append(self.uc.hook_add(UC_HOOK_CODE, self._on_code,
                                                      0, _a, _a))
            except Exception:
                pass
        t0 = _time.time()
        try:
            self.uc.emu_start(eip, until, timeout=timeout_ms * 1000,
                              count=count or 100_000_000)
            ok, err = True, ""
        except Exception as e:  # noqa: BLE001
            ok, err = False, str(e)
        for _h in getattr(self, "_ff_keep", []):
            try:
                self.uc.hook_del(_h)
            except Exception:
                pass
        self._ff_keep = []
        try:
            self._code_hook = self.uc.hook_add(UC_HOOK_CODE, self._on_code)
        except Exception:
            self._code_hook = None
        elapsed = _time.time() - t0
        eip2 = self.uc.reg_read(x86_const.UC_X86_REG_EIP)
        if ok and eip2 == until:
            return {"eip": "0x%X" % eip2, "stop_reason": "fast_ok",
                    "elapsed_ms": round(elapsed * 1000, 1), "fast": True}
        self._restore_now(snap)
        self.stop_reason = ""
        return {"eip": "0x%X" % eip2,
                "stop_reason": f"fast_rollback: {err[:120]}",
                "elapsed_ms": round(elapsed * 1000, 1), "fast": True,
                "rollback": True}

    def _on_code(self, uc, address, size, user_data):
        if address == 0x7F000D00:
            self._seh_next_frame(uc)
            return
        if address == 0x7F000D10:
            code = _u32(self.uc.mem_read(0x7F000910, 4))
            addr = _u32(self.uc.mem_read(0x7F000914, 4))
            handler = self._seh_dispatch(code, addr)
            if handler:
                self.uc.reg_write(x86_const.UC_X86_REG_EIP, handler)
            elif getattr(self, "uef_handler", 0):
                h2 = self._uef_dispatch(code, addr)
                self.uc.reg_write(x86_const.UC_X86_REG_EIP, h2)
            else:
                self._no_dispatch = True
            return
        if address == 0x7F000D20:
            code = _u32(self.uc.mem_read(0x7F000910, 4))
            addr = _u32(self.uc.mem_read(0x7F000914, 4))
            idx = _u32(self.uc.mem_read(0x7F000920, 4)) + 1
            if idx < len(self.veh_handlers):
                self.uc.reg_write(x86_const.UC_X86_REG_EIP, self._veh_dispatch(code, addr, idx))
            else:
                self.uc.reg_write(x86_const.UC_X86_REG_EIP, 0x7F000D10)
            return
        if address == 0x1000FF40:
            self._unhandled = True
            uc.emu_stop()
            return
        if self._trace_on and len(self._trace) < TRACE_LIMIT:
            self._trace.append(address)
        if address >= self.image.image_base + self.image.size_of_image and \
                address not in (0x10000000, 0x11000000):
            self._executed.add(address)
        if not (self._win_start <= address < self._win_start + 32):
            try:
                self._win_data = uc.mem_read(address, 32)
                self._win_start = address
            except UcError:
                try:
                    self._win_data = uc.mem_read(address, 1)
                    self._win_start = address
                except UcError:
                    self._win_start = -1
                    return
        b = self._win_data[address - self._win_start]
        if b == 0xCC:
            if self.veh_handlers and self._dispatch_exception(0x80000003, address):
                return
            uc.reg_write(x86_const.UC_X86_REG_EIP, address + 1)
            self.api_calls["__int3__0x%X" % address] = \
                self.api_calls.get("__int3__0x%X" % address, 0) + 1
            return
        if address == 0x4010E0:
            esp = uc.reg_read(x86_const.UC_X86_REG_ESP)
            try:
                ret_addr = _u32(uc.mem_read(esp, 4))
                arg1 = _u32(uc.mem_read(esp + 4, 4))
                arg2 = _u32(uc.mem_read(esp + 8, 4))
                arg3 = _u32(uc.mem_read(esp + 0xC, 4))
                arg4 = _u32(uc.mem_read(esp + 0x10, 4))
            except UcError:
                arg1 = arg2 = arg3 = arg4 = 0
            eax = self._wow64_call(arg1, arg2, arg3, arg4)
            self._wow64_ret = eax
            uc.reg_write(x86_const.UC_X86_REG_EAX, eax)
            uc.reg_write(x86_const.UC_X86_REG_EIP, 0x401125)
            self.api_calls[f"__wow64_call__0x{arg4:X}(0x{arg1:X},0x{arg2:X},0x{arg3:X})->0x{eax:X}"] = \
                self.api_calls.get(f"__wow64_call__0x{arg4:X}(0x{arg1:X},0x{arg2:X},0x{arg3:X})->0x{eax:X}", 0) + 1
            return
        if address == 0x401125:
            esp = uc.reg_read(x86_const.UC_X86_REG_ESP)
            try:
                ret_addr = _u32(uc.mem_read(esp, 4))
            except UcError:
                ret_addr = 0
            uc.reg_write(x86_const.UC_X86_REG_EAX, getattr(self, "_wow64_ret", 0))
            uc.reg_write(x86_const.UC_X86_REG_ESP, esp + 4)
            uc.reg_write(x86_const.UC_X86_REG_EIP, ret_addr)
            return
        if address in (0x401130, 0x401160):
            esp = uc.reg_read(x86_const.UC_X86_REG_ESP)
            try:
                ret_addr = _u32(uc.mem_read(esp, 4))
            except UcError:
                ret_addr = 0
            uc.reg_write(x86_const.UC_X86_REG_ESP, esp + 4)
            uc.reg_write(x86_const.UC_X86_REG_EIP, ret_addr)
            self.api_calls[f"__wow64_skip__0x{address:X}->0x{ret_addr:X}"] = \
                self.api_calls.get(f"__wow64_skip__0x{address:X}->0x{ret_addr:X}", 0) + 1
            return
        if b == 0xCD:
            try:
                imm = uc.mem_read(address + 1, 1)[0]
            except UcError:
                imm = 0
            code = 0xC0000409 if imm == 0x29 else (0x80000003 if imm == 0x03 else 0xC0000005)
            if self._dispatch_exception(code, address):
                return
            uc.reg_write(x86_const.UC_X86_REG_EIP, address + 2)
            self.api_calls["__int_0x%02X__0x%X" % (imm, address)] = \
                self.api_calls.get("__int_0x%02X__0x%X" % (imm, address), 0) + 1
            return
        if b in (0xCB, 0xCA):
            self._on_retf(uc, address, b)
        elif b in (0xED, 0xEC, 0xE4, 0xE5):
            if self._dispatch_exception(0xC0000096, address):
                return
            uc.reg_write(x86_const.UC_X86_REG_EAX, 0)
            self.api_calls[f"__in__0x{address:X}"] = \
                self.api_calls.get(f"__in__0x{address:X}", 0) + 1
            return

    def _on_write(self, uc, access, address, size, value, user_data):
        if len(self._writes) >= WRITES_LIMIT:
            return
        self._writes.append((address, size, value))

    def _on_insn(self, uc, address, size, user_data):
        insn = user_data
        if insn == x86_const.UC_X86_INS_RDTSC:
            self.clock += 16
            uc.reg_write(x86_const.UC_X86_REG_EAX, self.clock & 0xFFFFFFFF)
            uc.reg_write(x86_const.UC_X86_REG_EDX, 0)
        elif insn == x86_const.UC_X86_INS_CPUID:
            uc.reg_write(x86_const.UC_X86_REG_EAX, 1)
            uc.reg_write(x86_const.UC_X86_REG_EBX, 0x756E6547)
            uc.reg_write(x86_const.UC_X86_REG_EDX, 0x49656E69)
            uc.reg_write(x86_const.UC_X86_REG_ECX, 0x6C65746E)
        elif insn == x86_const.UC_X86_INS_IN:
            self._dispatch_exception(0xC0000096, address)

    def _on_retf(self, uc, address, opcode: int) -> None:
        try:
            esp = uc.reg_read(x86_const.UC_X86_REG_ESP)
            eip = _u32(uc.mem_read(esp, 4))
            cs = _u32(uc.mem_read(esp + 4, 4))
            add = 8
            if opcode == 0xCA:
                add += _u16(uc.mem_read(address + 1, 2))
            uc.reg_write(x86_const.UC_X86_REG_ESP, esp + add)
            uc.reg_write(x86_const.UC_X86_REG_EIP, eip)
            self.api_calls[f"__retf__cs0x{cs:X}->0x{eip:X}"] = \
                self.api_calls.get(f"__retf__cs0x{cs:X}->0x{eip:X}", 0) + 1
            if cs == 0x33:
                self._sim_wow64(uc, eip)
        except UcError:
            pass

    def _sim_wow64(self, uc, eip: int) -> None:
        import capstone as _cs
        if not hasattr(self, "_md64"):
            self._md64 = _cs.Cs(_cs.CS_ARCH_X86, _cs.CS_MODE_64)
            self._md64.detail = True
        md = self._md64
        cur = eip
        esp = uc.reg_read(x86_const.UC_X86_REG_ESP)
        for _ in range(64):
            try:
                code = uc.mem_read(cur, 16)
            except UcError:
                break
            ins = next(md.disasm(code, cur), None)
            if ins is None:
                break
            if ins.mnemonic == "retf":
                neip = _u32(uc.mem_read(esp, 4))
                ncs = _u32(uc.mem_read(esp + 4, 4))
                uc.reg_write(x86_const.UC_X86_REG_ESP, esp + 8)
                uc.reg_write(x86_const.UC_X86_REG_EIP, neip)
                self.api_calls[f"__wow64_retf__0x{cur:X}->0x{neip:X}"] = \
                    self.api_calls.get(f"__wow64_retf__0x{cur:X}->0x{neip:X}", 0) + 1
                return
            if ins.mnemonic == "call":
                esp -= 4
                uc.mem_write(esp, (cur + ins.size).to_bytes(4, "little"))
            elif ins.mnemonic == "out":
                pass
            elif ins.mnemonic == "in":
                uc.reg_write(x86_const.UC_X86_REG_EAX, 0)
            elif ins.mnemonic in ("mov", "add") and len(ins.operands) >= 2:
                d, s_ = ins.operands[0], ins.operands[1]
                if d.type == _cs.CS_OP_MEM and s_.type == _cs.CS_OP_IMM and \
                        d.mem.base in (x86_const.UC_X86_REG_ESP, x86_const.UC_X86_REG_RSP):
                    addr = esp + d.mem.disp
                    val = s_.imm
                    if ins.mnemonic == "add":
                        val = (_u32(uc.mem_read(addr, 4)) + val) & 0xFFFFFFFF
                    try:
                        uc.mem_write(addr, val.to_bytes(4, "little"))
                    except UcError:
                        pass
            cur += ins.size
        self.api_calls["__wow64_unresolved__0x%X" % eip] = \
            self.api_calls.get("__wow64_unresolved__0x%X" % eip, 0) + 1

    def _wow64_call(self, arg1: int, arg2: int, arg3: int, target: int) -> int:
        try:
            import unicorn as _uc
            from unicorn import x86_const as _xc
            from unicorn import unicorn_const as _ucc
            uc64 = _uc.Uc(_uc.UC_ARCH_X86, _uc.UC_MODE_64)
            _ERR_RU, _ERR_WU, _ERR_FU = (_ucc.UC_ERR_READ_UNMAPPED,
                                          _ucc.UC_ERR_WRITE_UNMAPPED,
                                          _ucc.UC_ERR_FETCH_UNMAPPED)
            _um_cnt = 0

            def on_unmapped(uc, access, address, size, value, user_data):
                nonlocal _um_cnt
                _um_cnt += 1
                page = address & ~0xFFF
                try:
                    data = self.uc.mem_read(page, 0x1000)
                    uc.mem_map(page, 0x1000, 7)
                    uc.mem_write(page, bytes(data))
                    return True
                except Exception:
                    if access == 0x15:
                        try:
                            _rsp = uc.reg_read(_xc.UC_X86_REG_RSP)
                            for _off in (0x00, 0x08, 0x10, 0x18, -0x08, -0x10,
                                        -0x18, -0x20, -0x28, 0x20):
                                _v = int.from_bytes(
                                    uc.mem_read(_rsp + _off, 8), "little")
                                if 0x400000 <= _v < 0x900000 and _v != 0x401110:
                                    _skip_next.append(_v)
                                    uc.emu_stop()
                                    return True
                        except Exception:
                            pass
                        return False
                    try:
                        uc.mem_map(page, 0x1000, 7)
                        return True
                    except Exception:
                        return False

            _use_hooks = getattr(self, "w64_hooks", False)
            if _use_hooks or getattr(self, "w64_codehook", False):
                uc64.hook_add(_uc.UC_HOOK_MEM_READ_UNMAPPED, on_unmapped)
                uc64.hook_add(_uc.UC_HOOK_MEM_WRITE_UNMAPPED, on_unmapped)
                uc64.hook_add(_uc.UC_HOOK_MEM_FETCH_UNMAPPED, on_unmapped)
                uc64.hook_add(_uc.UC_HOOK_MEM_WRITE, on_write)
            else:
                for _rg in self.uc.mem_regions():
                    _ba, _en = _rg[0], _rg[1]
                    if _ba >= 0x7FE00000:
                        continue
                    try:
                        _sz = _en - _ba
                        if _sz > 0x1000000:
                            continue
                        uc64.mem_map(_ba, (_sz + 0xFFF) & ~0xFFF, 7)
                        uc64.mem_write(_ba, bytes(self.uc.mem_read(_ba, _sz)))
                    except Exception:
                        pass
                _PREMAP = [(0x1000, 0x3FF000),
                           (0x841000, 0x7BF000),
                           (0x80000000, 0x1000000),
                           (0xF0000000, 0x1000000),
                           (0xFFF00000, 0x100000),
                           (0x7FFE0000, 0x8000)]
                for _ba, _sz in _PREMAP:
                    try:
                        uc64.mem_map(_ba, _sz, 7)
                    except Exception:
                        pass

            _dirty_pages: set = set()
            _wr_cnt = 0
            _exc_cnt = 0

            def on_write(uc, access, address, size, value, user_data):
                nonlocal _wr_cnt
                if 0x7FE00000 <= address < 0x80000000:
                    return
                _wr_cnt += 1
                if getattr(self, "w64_codehook", False):
                    _safe_cache.pop(address, None)
                    if _loop_cache:
                        _loop_cache.clear()
                    try:
                        self.uc.mem_write(address, (value & ((1 << (8 * size)) - 1)).to_bytes(size, "little"))
                    except Exception:
                        pass
                elif _use_hooks:
                    _dirty_pages.add(address & ~0xFFF)

            if _use_hooks or getattr(self, "w64_codehook", False):
                uc64.hook_add(_uc.UC_HOOK_MEM_WRITE, on_write)

            _skip_next: list = []
            _safe_cache: dict = {}
            _loop_cache: dict = {}
            _rip_seen: dict = {}
            _rip_seq = 0
            _FF_ALLOWED = {"mov", "movzx", "movsx", "lea", "add", "sub", "xor",
                           "shl", "shr", "sar", "and", "or", "inc", "dec",
                           "neg", "not", "cmp", "test"}
            _REG64 = {"rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rbp", "rsp",
                      "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15"}

            def _reg_base(name):
                n = name.lower()
                if n in _REG64:
                    return n, 0xFFFFFFFFFFFFFFFF
                if n[0] == "e" and n[1:] in _REG64:
                    return n[1:], 0xFFFFFFFF
                if n in ("ax", "bx", "cx", "dx", "si", "di", "bp", "sp"):
                    return {"ax": "rax", "bx": "rbx", "cx": "rcx", "dx": "rdx",
                            "si": "rsi", "di": "rdi", "bp": "rbp", "sp": "rsp"}[n], 0xFFFF
                if n in ("al", "bl", "cl", "dl", "sil", "dil", "bpl", "spl"):
                    return {"al": "rax", "bl": "rbx", "cl": "rcx", "dl": "rdx",
                            "sil": "rsi", "dil": "rdi", "bpl": "rbp", "spl": "rsp"}[n], 0xFF
                if len(n) == 3 and n[0] == "r" and n[1:].isdigit():
                    return n, 0xFFFFFFFFFFFFFFFF
                if len(n) == 4 and n[0] == "r" and n[1:-1].isdigit():
                    return n[:-1], 0xFFFFFFFF
                if len(n) == 4 and n[0] == "r" and n[1:-1].isdigit() and n[-1] == "w":
                    return n[:-1], 0xFFFF
                if len(n) == 4 and n[0] == "r" and n[1:-1].isdigit() and n[-1] == "b":
                    return n[:-1], 0xFF
                return None, 0

            def _reg_read(uc, name, regs):
                base, mask = _reg_base(name)
                if base is None:
                    return 0
                if base not in regs:
                    regs[base] = uc.reg_read(getattr(_xc, "UC_X86_REG_" + base.upper()))
                return regs[base] & mask

            def _reg_write(uc, name, val, regs):
                base, mask = _reg_base(name)
                if base is None:
                    return
                v = val & mask
                if base in regs:
                    regs[base] = (regs[base] & ~mask) | v
                else:
                    regs[base] = uc.reg_read(getattr(_xc, "UC_X86_REG_" + base.upper()))
                    regs[base] = (regs[base] & ~mask) | v
                uc.reg_write(getattr(_xc, "UC_X86_REG_" + base.upper()), regs[base])

            def _parse_ins(ins):
                _ops = []
                for _op in (ins.operands or []):
                    if _op.type == _cs.x86.X86_OP_REG:
                        _ops.append((0, ins.reg_name(_op.reg), 0, 0, _op.size))
                    elif _op.type == _cs.x86.X86_OP_IMM:
                        _ops.append((1, None, _op.imm & 0xFFFFFFFFFFFFFFFF, 0, _op.size))
                    elif _op.type == _cs.x86.X86_OP_MEM:
                        _mb = ins.reg_name(_op.mem.base) if _op.mem.base else None
                        if _op.mem.index or _mb is not None:
                            return None
                        _ops.append((2, None, 0, _op.mem.disp, _op.size))
                    else:
                        return None
                return (ins.address, len(ins.bytes), ins.mnemonic, _ops)

            def _op_val(uc, op, regs):
                if op[0] == 1:
                    return op[2]
                if op[0] == 0:
                    return _reg_read(uc, op[1], regs)
                if op[0] == 2:
                    return int.from_bytes(uc.mem_read(op[3] & 0xFFFFFFFFFFFFFFFF, op[4]), "little")
                return 0

            def _op_write(uc, op, val, regs):
                if op[0] == 0:
                    _reg_write(uc, op[1], val, regs)
                elif op[0] == 2:
                    _addr = op[3] & 0xFFFFFFFFFFFFFFFF
                    _raw = (val & ((1 << (8 * op[4])) - 1)).to_bytes(op[4], "little")
                    uc.mem_write(_addr, _raw)
                    if not (0x7FE00000 <= _addr < 0x80000000):
                        try:
                            self.uc.mem_write(_addr, _raw)
                        except Exception:
                            pass

            def _sim_insn(uc, ins, regs):
                _mn = ins[2]
                _ops = ins[3]
                _dst = _ops[0] if _ops else None
                _src = _ops[1] if len(_ops) > 1 else None
                _sv = _op_val(uc, _src, regs) if _src is not None else 0
                _dv = _op_val(uc, _dst, regs) if _dst is not None else 0
                if _mn == "mov":
                    _nv = _sv
                elif _mn in ("movzx", "movsx"):
                    _nv = _sv
                    if _mn == "movsx" and _src is not None and _src[4] < 8:
                        _ss = _src[4] * 8
                        if _sv >> (_ss - 1):
                            _nv = (_sv | (0xFFFFFFFFFFFFFFFF << _ss)) & 0xFFFFFFFFFFFFFFFF
                elif _mn == "lea":
                    _nv = (_src if _src is not None else _dst)[3] & 0xFFFFFFFFFFFFFFFF
                elif _mn == "add":
                    _nv = (_dv + _sv) & 0xFFFFFFFFFFFFFFFF
                elif _mn == "sub":
                    _nv = (_dv - _sv) & 0xFFFFFFFFFFFFFFFF
                elif _mn == "xor":
                    _nv = _dv ^ _sv
                elif _mn == "and":
                    _nv = _dv & _sv
                elif _mn == "or":
                    _nv = _dv | _sv
                elif _mn == "shl":
                    _nv = (_dv << _sv) & 0xFFFFFFFFFFFFFFFF
                elif _mn == "shr":
                    _nv = _dv >> _sv
                elif _mn == "sar":
                    _nv = _dv >> _sv
                    if _sv and (_dv >> 63):
                        _nv |= (0xFFFFFFFFFFFFFFFF << (64 - _sv)) & 0xFFFFFFFFFFFFFFFF
                elif _mn == "inc":
                    _nv = (_dv + 1) & 0xFFFFFFFFFFFFFFFF
                elif _mn == "dec":
                    _nv = (_dv - 1) & 0xFFFFFFFFFFFFFFFF
                elif _mn == "neg":
                    _nv = (-_dv) & 0xFFFFFFFFFFFFFFFF
                elif _mn == "not":
                    _nv = _dv ^ 0xFFFFFFFFFFFFFFFF
                elif _mn == "cmp":
                    regs["_zf"] = ((_dv - _sv) & 0xFFFFFFFFFFFFFFFF) == 0
                    return
                elif _mn == "test":
                    regs["_zf"] = ((_dv & _sv) == 0)
                    return
                else:
                    return
                if _dst is not None:
                    _op_write(uc, _dst, _nv, regs)
                if _mn in ("add", "sub", "xor", "and", "or", "shl", "shr", "sar",
                           "inc", "dec", "neg", "not"):
                    _w = (_dst[4] * 8) if _dst is not None else 64
                    regs["_zf"] = ((_nv & ((1 << _w) - 1)) == 0)

            def _sim_loop(uc, insns, n):
                regs = {"_zf": False}
                for _ in range(n):
                    for ins in insns:
                        if ins[2] in ("jnz", "jne", "jz", "je", "jmp"):
                            continue
                        _sim_insn(uc, ins, regs)

            def _read_cnt(uc, ins):
                _op = ins[3][0]
                if ins[2] == "dec":
                    _step = 1
                else:
                    _src = ins[3][1] if len(ins[3]) > 1 else None
                    if _src is None or _src[0] != 1:
                        return None
                    _step = _src[2]
                if _step <= 0:
                    return None
                if _op[0] == 0:
                    return _reg_read(uc, _op[1], {}) & 0xFFFFFFFFFFFFFFFF, _step
                if _op[0] == 2:
                    return int.from_bytes(uc.mem_read(_op[3] & 0xFFFFFFFFFFFFFFFF, _op[4]), "little"), _step
                return None

            def _analyze_loop(head):
                try:
                    _md2 = _cs.Cs(_cs.CS_ARCH_X86, _cs.CS_MODE_64)
                    _md2.detail = True
                    _data = bytes(uc64.mem_read(head, 160))
                except Exception:
                    return None, 0, None
                _body = []
                _cnt = None
                for _ins in _md2.disasm(_data, head):
                    _mn = _ins.mnemonic
                    _sz = len(_ins.bytes)
                    if _mn in ("jmp", "jne", "jnz", "je", "jz"):
                        if _ins.operands and _ins.operands[0].type == _cs.x86.X86_OP_IMM \
                                and _ins.operands[0].imm == head:
                            if _cnt is None:
                                return None, 0, None
                            return _body, _ins.address + _sz, _cnt
                        return None, 0, None
                    if _mn not in _FF_ALLOWED:
                        return None, 0, None
                    _parsed = _parse_ins(_ins)
                    if _parsed is None:
                        return None, 0, None
                    _body.append(_parsed)
                    if _mn in ("dec", "sub") and _cnt is None:
                        _cnt = _parsed
                    if _ins.address + _sz > head + 128:
                        return None, 0, None
                return None, 0, None

            def _run_ff(uc, head, cache):
                exit_addr, insns, cnt_ins = cache
                _rc = _read_cnt(uc, cnt_ins)
                if _rc is None:
                    return False
                _val, _step = _rc
                _n = _val // _step
                if _n <= 0:
                    return False
                _n = min(_n, 1000000)
                _sim_loop(uc, insns, _n)
                if _read_cnt(uc, cnt_ins)[0] > 0:
                    _skip_next.append(head)
                else:
                    _skip_next.append(exit_addr)
                self.api_calls["__w64_ff__0x%X" % head] = \
                    self.api_calls.get("__w64_ff__0x%X" % head, 0) + 1
                uc.emu_stop()
                return True

            def _try_fast_forward(uc, head):
                try:
                    hit = _loop_cache.get(head)
                    if hit is None and head not in _loop_cache:
                        hit = _analyze_loop(head)
                        _loop_cache[head] = hit
                    if hit is None or hit[0] is None:
                        return False
                    return _run_ff(uc, head, hit)
                except Exception:
                    return False

            def on_code(uc, address, size, user_data):
                nonlocal _rip_seq, _rip_seen
                if address == 0x401110:
                    uc.emu_stop()
                    return
                _rip_seq += 1
                _prev = _rip_seen.get(address)
                _rip_seen[address] = _rip_seq
                if _prev is not None and _rip_seq - _prev < 2048:
                    if _try_fast_forward(uc, address):
                        return
                if len(_rip_seen) > 8192:
                    _rip_seen = {k: v for k, v in _rip_seen.items()
                                 if _rip_seq - v < 2048}
                _c = _safe_cache.get(address)
                if _c is True:
                    return
                if _c is False:
                    _handle_special(uc, address)
                    return
                try:
                    b = uc.mem_read(address, 1)[0]
                except Exception:
                    return
                if b in (0xCC, 0xEC, 0xF4, 0xED, 0xE4, 0xE5, 0xEE, 0xEF, 0xE6, 0xE7,
                         0x6C, 0x6D, 0x6E, 0x6F, 0xF1, 0xFA, 0xFB, 0xCB):
                    _safe_cache[address] = False
                    _handle_special(uc, address)
                else:
                    _safe_cache[address] = True

            def _handle_special(uc, address):
                try:
                    b = uc.mem_read(address, 1)[0]
                except Exception:
                    return
                nxt = None
                if b in (0xCC, 0xEC, 0xF4):
                    nxt = address + 1
                elif b == 0xED:
                    nxt = address + 1
                elif b in (0xE4, 0xE5):
                    nxt = address + 2
                elif b in (0xEE, 0xEF):
                    nxt = address + 1
                elif b in (0xE6, 0xE7):
                    nxt = address + 2
                elif b in (0x6C, 0x6D, 0x6E, 0x6F):
                    nxt = address + 1
                elif b in (0xF1, 0xFA, 0xFB):
                    nxt = address + 1
                elif b == 0xCB:
                    try:
                        _rsp = uc.reg_read(_xc.UC_X86_REG_RSP)
                        _tgt = int.from_bytes(uc.mem_read(_rsp, 8), "little")
                        _cs = int.from_bytes(uc.mem_read(_rsp + 8, 8), "little")
                    except Exception:
                        _tgt, _cs = 0, 0
                    if _cs == 0x23:
                        _skip_next.append(0x401110)
                        uc.emu_stop()
                        return
                    uc.reg_write(_xc.UC_X86_REG_RSP, _rsp + 16)
                    _skip_next.append(_tgt)
                    uc.emu_stop()
                    return
                if nxt is not None:
                    _skip_next.append(nxt)
                    uc.emu_stop()

            def _exc_next(uc, addr, b):
                if b in (0xCC, 0xEC, 0xF4):
                    return addr + 1
                if b == 0xED:
                    return addr + 1
                if b in (0xE4, 0xE5):
                    return addr + 2
                if b in (0xEE, 0xEF):
                    return addr + 1
                if b in (0xE6, 0xE7):
                    return addr + 2
                if b in (0x6C, 0x6D, 0x6E, 0x6F):
                    return addr + 1
                if b in (0xF1, 0xFA, 0xFB):
                    return addr + 1
                if b == 0xCB:
                    try:
                        _rsp = uc.reg_read(_xc.UC_X86_REG_RSP)
                        _tgt = int.from_bytes(uc.mem_read(_rsp, 8), "little")
                        _cs = int.from_bytes(uc.mem_read(_rsp + 8, 8), "little")
                    except Exception:
                        return None
                    if _cs == 0x23:
                        return 0x401110
                    uc.reg_write(_xc.UC_X86_REG_RSP, _rsp + 16)
                    return _tgt
                return None

            if getattr(self, "w64_codehook", False):
                uc64.hook_add(_uc.UC_HOOK_CODE, on_code)
            rsp = 0x7FF00000 - 0x200
            try:
                uc64.mem_map(rsp & ~0xFFF, 0x1000, 7)
            except _uc.UcError:
                pass
            uc64.mem_write(rsp - 8, (0x401110).to_bytes(8, "little"))
            def _stop_at_401110(uc, a, s, d):
                uc.emu_stop()

            uc64.hook_add(_uc.UC_HOOK_CODE, _stop_at_401110,
                          0, 0x401110, 0x401110)
            for _r32, _r64 in (("EAX", "RAX"), ("ESI", "RSI"), ("EDI", "RDI"),
                               ("EBX", "RBX"), ("EBP", "RBP"), ("ECX", "RCX"),
                               ("EDX", "RDX")):
                try:
                    _v = self.uc.reg_read(getattr(x86_const, "UC_X86_REG_" + _r32))
                    uc64.reg_write(getattr(_xc, "UC_X86_REG_" + _r64), _v)
                except Exception:
                    pass
            uc64.reg_write(_xc.UC_X86_REG_RCX, arg1)
            uc64.reg_write(_xc.UC_X86_REG_RDX, arg2)
            uc64.reg_write(_xc.UC_X86_REG_R8, arg3)
            uc64.reg_write(_xc.UC_X86_REG_R9, target)
            uc64.reg_write(_xc.UC_X86_REG_R14, rsp)
            uc64.reg_write(_xc.UC_X86_REG_RSP, rsp - 8)
            if getattr(self, "_teb64", 0):
                try:
                    uc64.reg_write(_xc.UC_X86_REG_GS_BASE, self._teb64)
                    uc64.reg_write(_xc.UC_X86_REG_R15, self._teb64)
                    uc64.mem_write(self._teb64 + 0x60,
                                   (self._peb64).to_bytes(8, "little"))
                except Exception:
                    pass
            _addr = target
            _prev = -1
            _rounds = 0
            _t0 = _time.time()
            _exc_last = -1
            _exc_same = 0
            _w64c = int(getattr(self, "w64_count", 8000000))
            _w64r = int(getattr(self, "w64_rounds", 512))
            _cnt_exh = 0
            _w64_break = ""
            _steps_done = 0
            _W64_BUDGET = _w64c * _w64r
            _loop_i = 0
            while _loop_i < _w64r and _steps_done < _W64_BUDGET:
                _loop_i += 1
                _rounds += 1
                _s0 = _time.time()
                try:
                    uc64.emu_start(_addr, 0xFFFFFFFFFFFF, count=_w64c)
                except _uc.UcError as _e:
                    if _skip_next:
                        _addr = _skip_next.pop(0)
                        continue
                    _eno = _e.errno
                    if _eno in (_ERR_RU, _ERR_WU) and not (_use_hooks or getattr(self, "w64_codehook", False)):
                        _h_a = uc64.hook_add(_uc.UC_HOOK_MEM_READ_UNMAPPED, on_unmapped)
                        _h_b = uc64.hook_add(_uc.UC_HOOK_MEM_WRITE_UNMAPPED, on_unmapped)
                        try:
                            uc64.emu_start(uc64.reg_read(_xc.UC_X86_REG_RIP),
                                           0x401110, count=50000)
                        except Exception:
                            pass
                        _addr = uc64.reg_read(_xc.UC_X86_REG_RIP)
                        try:
                            uc64.hook_del(_h_a)
                            uc64.hook_del(_h_b)
                        except Exception:
                            pass
                        continue
                    if _eno == _ERR_FU and not (_use_hooks or getattr(self, "w64_codehook", False)):
                        try:
                            _rsp = uc64.reg_read(_xc.UC_X86_REG_RSP)
                            for _off in (0x00, 0x08, 0x10, 0x18, -0x08, -0x10,
                                        -0x18, -0x20, -0x28, 0x20):
                                _v = int.from_bytes(
                                    uc64.mem_read(_rsp + _off, 8), "little")
                                if 0x400000 <= _v < 0x900000 and _v != 0x401110:
                                    _skip_next.append(_v)
                                    _addr = _v
                                    break
                            else:
                                _dt, _dr = _deobf_correct(uc64, _rsp, 64)
                                if _dt is not None:
                                    _exc_cnt += 1
                                    _skip_next.append(_dt)
                                    _addr = _dt
                                    self.api_calls["__deobf_fu__0x%X" % _rsp] = _dr
                                    continue
                                _w64_break = "fu:no_tramp@0x%X" % uc64.reg_read(_xc.UC_X86_REG_RIP)
                                break
                            continue
                        except Exception:
                            _w64_break = "fu:exc@0x%X" % uc64.reg_read(_xc.UC_X86_REG_RIP)
                            break
                    try:
                        _rp = uc64.reg_read(_xc.UC_X86_REG_RIP)
                        _b = uc64.mem_read(_rp - 1, 1)[0]
                    except Exception:
                        _w64_break = "rip1:fail@0x%X" % uc64.reg_read(_xc.UC_X86_REG_RIP)
                        break
                    _nx = _exc_next(uc64, _rp - 1, _b)
                    if _nx is None:
                        _dt, _dr = _deobf_correct(uc64, _rp - 1, 64)
                        if _dt is not None:
                            if _rp == _exc_last:
                                _exc_same += 1
                            else:
                                _exc_last, _exc_same = _rp, 1
                            if _exc_same >= 3:
                                _w64_break = "exc:same3@0x%X" % (_rp - 1)
                                break
                            _exc_cnt += 1
                            _skip_next.append(_dt)
                            _addr = _dt
                            self.api_calls["__deobf__0x%X" % (_rp - 1)] = _dr
                            continue
                        _w64_break = "exc:unfix@0x%X" % (_rp - 1)
                        break
                    _exc_cnt += 1
                    _skip_next.append(_nx)
                    _addr = _nx
                    continue
                except Exception as _e2:
                    print("  [W64-EXC2] %r" % _e2, flush=True)
                    _w64_break = "exc2:%r" % _e2
                    break
                if _skip_next:
                    _addr = _skip_next.pop(0)
                    continue
                if uc64.reg_read(_xc.UC_X86_REG_RIP) == 0x401110:
                    break
                _nxt = uc64.reg_read(_xc.UC_X86_REG_RIP)
                if _nxt == _addr and _nxt == _prev:
                    _w64_break = "deadloop@0x%X" % _nxt
                    break
                _prev, _addr = _addr, _nxt
                _cnt_exh += 1
                _steps_done += _w64c
                if _cnt_exh >= 2:
                    _w64c = min(_w64c * 2, 100_000_000)
            if _use_hooks or getattr(self, "w64_codehook", False):
                if _dirty_pages:
                    for _pg in tuple(_dirty_pages):
                        try:
                            self.uc.mem_write(_pg, bytes(uc64.mem_read(_pg, 0x1000)))
                        except Exception:
                            pass
                    _dirty_pages.clear()
            else:
                for _rg in uc64.mem_regions():
                    _ba, _en = _rg[0], _rg[1]
                    if _ba >= 0x7FE00000:
                        continue
                    _pm = False
                    for _pa, _ps in _PREMAP:
                        if _ba >= _pa and _en <= _pa + _ps:
                            _pm = True
                            break
                    if _pm:
                        continue
                    try:
                        if _en - _ba > 0x1000000:
                            continue
                        self.uc.mem_write(_ba, bytes(uc64.mem_read(_ba, _en - _ba)))
                    except Exception:
                        pass
            _brk = (" break=%s" % _w64_break) if _w64_break else ""
            self.api_calls["__w64__0x%X" % target] = (
                "rounds=%d cnt=%d runs=%d steps=%d %.1fs%s"
                % (_rounds, _w64c, _cnt_exh, _steps_done,
                   _time.time() - _t0, _brk))
            if getattr(self, "w64_blackhole", True) and \
                    _steps_done >= 8 * 8000000 and _exc_cnt == 0:
                self.api_calls["__blackhole__0x%X" % target] = "rounds=%d cnt=%d" % (_rounds, _w64c)
                if getattr(self, "w64_blackhole_auto", False):
                    try:
                        self.uc.mem_write(target, b"\xc3")
                    except Exception:
                        pass
            return uc64.reg_read(_xc.UC_X86_REG_RAX) & 0xFFFFFFFF
        except Exception:
            return 0

    def _on_mem_unmapped(self, uc, access, address, size, value, user_data):
        if getattr(self, "_no_dispatch", False):
            return False
        if access in (UC_MEM_READ_UNMAPPED, UC_MEM_WRITE_UNMAPPED):
            try:
                uc.mem_map(address & ~0xFFF, 0x1000)
            except UcError:
                pass
            self.api_calls[f"__mem_map__0x{address & ~0xFFF:X}"] = \
                self.api_calls.get(f"__mem_map__0x{address & ~0xFFF:X}", 0) + 1
            return True
        return self._dispatch_exception(0xC0000005, address, write_jmp=True)

    def _dispatch_exception(self, code: int, address: int, write_jmp: bool = False) -> bool:
        has_veh = bool(getattr(self, "veh_handlers", None))
        has_seh = self._seh_has_frame()
        has_uef = bool(getattr(self, "uef_handler", 0))
        if not (has_veh or has_seh or has_uef):
            return False
        try:
            handler = None
            if has_veh:
                handler = self._veh_dispatch(code, address)
            elif has_seh:
                handler = self._seh_dispatch(code, address)
            if handler is None and has_uef:
                handler = self._uef_dispatch(code, address)
            if handler is None:
                return False
            if write_jmp:
                try:
                    self.uc.mem_map(address & ~0xFFF, 0x1000)
                except UcError:
                    pass
                try:
                    self.uc.mem_protect(address & ~0xFFF, 0x1000, UC_PROT_ALL)
                except UcError:
                    pass
                rel = (handler - (address + 5)) & 0xFFFFFFFF
                self.uc.mem_write(address, b"\xE9" + rel.to_bytes(4, "little"))
            else:
                self.uc.reg_write(x86_const.UC_X86_REG_EIP, handler)
            return True
        except UcError:
            return False

    def _build_rec_ctx(self, code: int, address: int) -> tuple:
        rec = self._ctx_buf
        ctx = self._ctx_buf + 0x400
        self.uc.mem_write(rec, (code & 0xFFFFFFFF).to_bytes(4, "little") + b"\x00\x00\x00\x00" +
                          b"\x00\x00\x00\x00" + (address & 0xFFFFFFFF).to_bytes(4, "little") +
                          b"\x00\x00\x00\x00")
        ctx_data = bytearray(0x2CC)
        _p32(ctx_data, 0x00, 0x10007)
        for reg, off in (("GS", 0x8C), ("FS", 0x90), ("ES", 0x94), ("DS", 0x98),
                         ("EDI", 0x9C), ("ESI", 0xA0), ("EBX", 0xA4), ("EDX", 0xA8),
                         ("ECX", 0xAC), ("EAX", 0xB0), ("EBP", 0xB4),
                         ("EFLAGS", 0xC0), ("ESP", 0xC4)):
            _p32(ctx_data, off, self.uc.reg_read(getattr(x86_const, "UC_X86_REG_" + reg)))
        _p32(ctx_data, 0xB8, address)
        self.uc.mem_write(ctx, bytes(ctx_data))
        return rec, ctx

    def _ctx_restore_stub_addr(self) -> int:
        if not getattr(self, "_ctx_stub", 0):
            self._ctx_stub = 0x1000FF10
            stub = b"\xA1\xB8\x04\x00\x7F" + \
                   b"\x8B\x25\xC4\x04\x00\x7F" + \
                   b"\xFF\xE0" + b"\x90" * 6
            self.uc.mem_write(self._ctx_stub, stub)
        return self._ctx_stub

    def _veh_dispatch(self, code: int, address: int, idx: int = 0) -> int:
        if idx >= len(self.veh_handlers):
            return 0
        handler = self.veh_handlers[idx]
        if not self._veh_ret_stub:
            self._veh_ret_stub = 0x1000FF00
            self._ctx_restore_stub_addr()
            rel = (0x7F000D20 - (self._veh_ret_stub + 10)) & 0xFFFFFFFF
            self.uc.mem_write(self._veh_ret_stub,
                              b"\x83\xF8\xFF" + b"\x74\x0B" +
                              b"\xE9" + rel.to_bytes(4, "little"))
        self.uc.mem_write(0x7F000920, (idx & 0xFFFFFFFF).to_bytes(4, "little"))
        rec, ctx = self._build_rec_ctx(code, address)
        self.uc.mem_write(0x7F000910, (code & 0xFFFFFFFF).to_bytes(4, "little"))
        self.uc.mem_write(0x7F000914, (address & 0xFFFFFFFF).to_bytes(4, "little"))
        ep = self._ctx_buf + 0x800
        self.uc.mem_write(ep, (rec & 0xFFFFFFFF).to_bytes(4, "little") +
                         (ctx & 0xFFFFFFFF).to_bytes(4, "little"))
        esp = self.uc.reg_read(x86_const.UC_X86_REG_ESP) - 8
        self.uc.mem_write(esp, (self._veh_ret_stub & 0xFFFFFFFF).to_bytes(4, "little"))
        self.uc.mem_write(esp + 4, (ep & 0xFFFFFFFF).to_bytes(4, "little"))
        self.uc.reg_write(x86_const.UC_X86_REG_ESP, esp)
        self.api_calls[f"__veh_dispatch__0x{address:X}(0x{code:X})->0x{handler:X}"] = \
            self.api_calls.get(f"__veh_dispatch__0x{address:X}(0x{code:X})->0x{handler:X}", 0) + 1
        return handler

    def _uef_dispatch(self, code: int, address: int) -> int:
        handler = self.uef_handler
        if not self._uef_ret_stub:
            self._uef_ret_stub = 0x1000FF30
            self._ctx_restore_stub_addr()
            rel = (0x1000FF40 - (self._uef_ret_stub + 10)) & 0xFFFFFFFF
            self.uc.mem_write(self._uef_ret_stub,
                              b"\x83\xF8\xFF" + b"\x74\xDB" +
                              b"\xE9" + rel.to_bytes(4, "little"))
        rec, ctx = self._build_rec_ctx(code, address)
        ep = self._ctx_buf + 0x800
        self.uc.mem_write(ep, (rec & 0xFFFFFFFF).to_bytes(4, "little") +
                         (ctx & 0xFFFFFFFF).to_bytes(4, "little"))
        esp = self.uc.reg_read(x86_const.UC_X86_REG_ESP) - 8
        self.uc.mem_write(esp, (self._uef_ret_stub & 0xFFFFFFFF).to_bytes(4, "little"))
        self.uc.mem_write(esp + 4, (ep & 0xFFFFFFFF).to_bytes(4, "little"))
        self.uc.reg_write(x86_const.UC_X86_REG_ESP, esp)
        self.api_calls[f"__uef_dispatch__0x{address:X}(0x{code:X})->0x{handler:X}"] = \
            self.api_calls.get(f"__uef_dispatch__0x{address:X}(0x{code:X})->0x{handler:X}", 0) + 1
        return handler

    def _seh_has_frame(self) -> bool:
        try:
            frame = _u32(self.uc.mem_read(0x0, 4))
        except UcError:
            return False
        return frame not in (0, 0xFFFFFFFF)

    def _seh_ret_stub_addr(self) -> int:
        if not self._seh_ret_stub:
            self._seh_ret_stub = 0x1000FF20
            self.uc.mem_write(self._seh_ret_stub,
                              b"\x83\xF8\xFF" + b"\x74\xEB" + b"\xE9\xD6\x0D\xFF\x6E")
        return self._seh_ret_stub

    def _seh_dispatch(self, code: int, address: int) -> Optional[int]:
        try:
            frame = _u32(self.uc.mem_read(0x0, 4))
        except UcError:
            return None
        if frame in (0, 0xFFFFFFFF):
            return None
        return self._seh_push_call(frame, code, address)

    def _seh_push_call(self, frame: int, code: int, address: int) -> int:
        handler = _u32(self.uc.mem_read(frame + 4, 4))
        self.uc.mem_write(0x7F000900, (frame & 0xFFFFFFFF).to_bytes(4, "little"))
        self.uc.mem_write(0x7F000910, (code & 0xFFFFFFFF).to_bytes(4, "little"))
        self.uc.mem_write(0x7F000914, (address & 0xFFFFFFFF).to_bytes(4, "little"))
        rec, ctx = self._build_rec_ctx(code, address)
        stub = self._seh_ret_stub_addr()
        esp = self.uc.reg_read(x86_const.UC_X86_REG_ESP) - 0x14
        self.uc.mem_write(esp, (stub & 0xFFFFFFFF).to_bytes(4, "little"))
        self.uc.mem_write(esp + 4, (rec & 0xFFFFFFFF).to_bytes(4, "little"))
        self.uc.mem_write(esp + 8, (frame & 0xFFFFFFFF).to_bytes(4, "little"))
        self.uc.mem_write(esp + 0xC, (ctx & 0xFFFFFFFF).to_bytes(4, "little"))
        self.uc.mem_write(esp + 0x10, 0x7F000D10.to_bytes(4, "little"))
        self.uc.reg_write(x86_const.UC_X86_REG_ESP, esp)
        self.api_calls[f"__seh_dispatch__0x{address:X}(0x{code:X})->0x{handler:X}"] = \
            self.api_calls.get(f"__seh_dispatch__0x{address:X}(0x{code:X})->0x{handler:X}", 0) + 1
        return handler

    def _seh_next_frame(self, uc) -> None:
        try:
            frame = _u32(uc.mem_read(0x7F000900, 4))
            prev = _u32(uc.mem_read(frame, 4))
        except UcError:
            self._no_dispatch = True
            uc.reg_write(x86_const.UC_X86_REG_EIP, 0x7F000E00)
            return
        code = _u32(uc.mem_read(0x7F000910, 4))
        address = _u32(uc.mem_read(0x7F000914, 4))
        if prev in (0, 0xFFFFFFFF):
            if getattr(self, "uef_handler", 0):
                handler = self._uef_dispatch(code, address)
                uc.reg_write(x86_const.UC_X86_REG_EIP, handler)
            else:
                self._no_dispatch = True
                uc.reg_write(x86_const.UC_X86_REG_EIP, 0x7F000E00)
            return
        handler = self._seh_push_call(prev, code, address)
        uc.reg_write(x86_const.UC_X86_REG_EIP, handler)

    def regs(self) -> dict:
        out = {}
        for name, r in X86_REGS:
            try:
                out[name] = hex(self.uc.reg_read(r))
            except UcError:
                out[name] = "?"
        return out

    def mem_read(self, addr: int, size: int) -> bytes:
        try:
            return self.uc.mem_read(addr, size)
        except UcError as e:
            return b""

    def mem_write(self, addr: int, data: bytes) -> str:
        try:
            self.uc.mem_write(addr, data)
            return f"ok {len(data)}B @ 0x{addr:X}"
        except UcError as e:
            return f"[Error] err: {e}"

    def dump(self, addr: int, size: int, out_file: str = "") -> str:
        data = self.mem_read(addr, size)
        if out_file:
            with open(out_file, "wb") as f:
                f.write(data)
            return f"dumped {len(data)}B -> {out_file}"
        head = data[:64].hex(" ")
        return f"0x{addr:X} ({len(data)}B)\n{head}"

    def patch(self, addr: int, hexbytes: str) -> str:
        try:
            data = bytes.fromhex(hexbytes.replace(" ", "").replace(",", ""))
        except ValueError:
            return "[Error] err: 非法 hex"
        return self.mem_write(addr, data)

    def _snapshot_now(self) -> dict:
        regs = {}
        for name, r in X86_REGS:
            try:
                regs[name] = self.uc.reg_read(r)
            except UcError:
                pass
        mem = []
        try:
            for region in self.uc.mem_regions():
                begin, end = region[0], region[1]
                size = end - begin
                if size > 0x4000000:
                    continue
                try:
                    mem.append((begin, self.uc.mem_read(begin, size)))
                except UcError:
                    pass
        except Exception:
            pass
        return {"regs": regs, "mem": mem}

    def _restore_now(self, snap: dict) -> None:
        for addr, data in snap.get("mem", []):
            try:
                self.uc.mem_write(addr, bytes(data))
            except UcError:
                pass
        for name, v in snap.get("regs", {}).items():
            try:
                self.uc.reg_write(dict(X86_REGS)[name], v)
            except (UcError, KeyError):
                pass

    def snapshot(self) -> str:
        if self.uc is None:
            return "[Error] err: 未加载"
        self._snap = self._snapshot_now()
        return "snapshot saved（当前状态，改输入后 restore + run 重放）"

    def restore(self) -> str:
        if self._snap is None:
            return "[Error] err: 无快照"
        self._restore_now(self._snap)
        self.output = []
        self.stop_reason = ""
        self.exit_code = 0
        self.api_calls = {}
        self._trace = []
        self._executed = set()
        self._dyncode = []
        return "restored（输入/输出已重置，可 run）"

    def replay(self, inputs: list[str]) -> dict:
        self.restore()
        self.inputs = list(inputs)
        return self.run(trace_on=False)

    def trace(self, limit: int = 200) -> str:
        if not self._trace:
            return "（无 trace——run 时需 trace_on=True）"
        lines = []
        for a in self._trace[:limit]:
            lines.append(f"  0x{a:X}")
        if len(self._trace) > limit:
            lines.append(f"  ... 共 {len(self._trace)} 条")
        return "\n".join(lines)

    def dyncode(self) -> str:
        by_page: dict[int, bytearray] = {}
        for addr, size, value in self._writes:
            if addr >= self.image.image_base + self.image.size_of_image and \
                    addr not in (0x10000000, 0x11000000):
                page = addr & ~0xFFF
                if page not in by_page:
                    by_page[page] = bytearray(0x1000)
                for i in range(size):
                    by_page[page][addr - page + i] = (value >> (8 * i)) & 0xFF
        blocks = []
        for page, data in by_page.items():
            if any(page <= e < page + 0x1000 for e in self._executed):
                blocks.append((page, bytes(data)))
        self._dyncode = blocks
        if not blocks:
            return "（无动态代码——未捕获到新写入且被执行的代码）"
        lines = [f"dyncode blocks: {len(blocks)}"]
        for page, data in blocks:
            nz = sum(1 for b in data if b != 0)
            lines.append(f"  0x{page:X} 页非零 {nz} 字节（执行过）")
        return "\n".join(lines)

    def antidbg_report(self) -> str:
        names = ("IsDebuggerPresent", "CheckRemoteDebuggerPresent",
                 "NtQueryInformationProcess", "NtQuerySystemInformation",
                 "NtSetInformationThread", "NtQueryObject", "OutputDebugStringA",
                 "DebugActiveProcess", "GetTickCount", "QueryPerformanceCounter",
                 "__unknown__NtQueryInformationProcess")
        hits = {n: c for n, c in self.api_calls.items()
                if any(k in n for k in names)}
        if not hits:
            return "（未检测到反调试 API 调用）"
        return "\n".join(f"  {n}: {c} 次" for n, c in sorted(hits.items()))

    def output_text(self) -> str:
        return "".join(self.output) if self.output else "（无输出）"

    def cleanup(self) -> None:
        try:
            if self.uc is not None:
                self.uc.emu_stop()
        except Exception:
            pass
        self.uc = None
        self.stubs = None
        self.image = None
