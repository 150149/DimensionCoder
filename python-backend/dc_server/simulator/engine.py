# -*- coding: utf-8 -*-
"""模拟引擎封装：SimSession——加载装配、执行、插桩、快照/重放、trace、动态代码捕获。"""
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
    # 2026-08-19：FS_BASE 移除——unicorn 2.1.4 已废弃（id 250，reg_read/write
    # no-op + 警告）；FS_BASE 由 env 装配固定（TEB 段基址），执行中不修改，
    # 快照/恢复无需保存（否则 _restore_now 恢复它时警告且丢失，TEB 寻址错乱）
]

TRACE_LIMIT = 200000          # 执行流记录上限（防内存爆炸）
WRITES_LIMIT = 20000          # 写入记录上限


class SimSession:
    """一次程序模拟会话（按 task_id 存于 orchestrator 侧，跨工具轮保持）。"""

    def __init__(self, task_id: str):
        self.task_id = task_id
        self.uc: Optional[Uc] = None
        self.image = None
        self.stubs: ApiStubs | None = None
        self.exe_path = ""
        self.cmdline = ""
        self.inputs: list[str] = []       # 输入队列（name/serial/...）
        self.output: list[str] = []       # 程序输出（WriteFile/printf/ODS）
        self.clock = 0                    # 模拟时钟（时间类 API/rdtsc）
        self.pid = 0x1A2B                 # 伪造 pid
        self.parent_pid = 0x1F24          # 伪造父进程（explorer 系）
        self.api_calls: dict[str, int] = {}
        self.stop_reason = ""             # exit / error / breakpoint / timeout
        self.exit_code = 0
        self.last_error = ""
        self.last_error_code = 0
        self._heap_ptr = ENV.HEAP_BASE + 0x1000
        self._trace: list[int] = []
        self._writes: list = []
        self._executed: set[int] = set()
        self._dyncode: list = []          # [(addr, size, bytes)]
        self._trace_on = False
        self._snap = None

    # ── 模拟堆 ───────────────────────────────────────────────────
    def heap_alloc(self, size: int) -> int:
        size = (size + 0xFFF) & ~0xFFF
        if size < 0x1000:
            size = 0x1000
        addr = self._heap_ptr
        self._heap_ptr += size
        try:
            self.uc.mem_map(addr, size)
        except UcError:
            pass  # 已映射（同页重复分配）
        return addr

    # ── 加载装配 ─────────────────────────────────────────────────
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
        # 映射 image（页对齐）
        base = img.image_base
        size = (img.size_of_image + 0xFFF) & ~0xFFF
        self.uc.mem_map(base, size)
        for va, vs, ro, rs, _nm in img.sections:
            if ro:
                self.uc.mem_write(base + va, raw[ro:ro + rs])
        # 环境装配
        ENV.build_env(self.uc, img, exe_path, args)
        # wow64 64 位线程环境（子执行器 GS_BASE/PEB64）
        try:
            self._teb64, self._peb64 = ENV.build_env64(self.uc, img, exe_path, args)
        except Exception:
            self._teb64 = self._peb64 = 0
        # API stub
        self.stubs = ApiStubs(self.uc, img, self)
        self.stubs.install()
        # hook：动态代码/执行流/写入/特殊指令
        # 指令首字节窗口缓存（2026-08-19：_on_code 每指令 mem_read(1) 判特殊指令
        # 占 CODE hook 开销约 45%——顺序执行一次读 32 字节服务连续多条指令）
        self._win_start = -1
        self._win_data = b""
        self._ff_auto_cnt = 0        # 自动快进成功计数（防失控）
        self._ff_auto_active = False  # 兼容保留（快进段统一由 run 层控制）
        self._ff_auto_pending = False  # 兼容保留（不再使用）
        self._ff_dyn_addrs: set = set()  # 自学习：快进段异常指令地址（保留 hook 集合）
        self._code_hook = self.uc.hook_add(UC_HOOK_CODE, self._on_code)
        self._write_hook = self.uc.hook_add(UC_HOOK_MEM_WRITE, self._on_write)
        # 2026-08-19：删除 INSN hook 注册——本 unicorn 2.1.4 构建的
        # hook_add(htype, cb, user_data, begin, end, aux1, aux2) 中 INSN 仅支持
        # IN/OUT/SYSCALL/SYSENTER/CPUID（指令 id 经 aux1），原写法
        # (INS_RDTSC, 1, 0) 参数错位致 aux1=0 全部抛 UC_ERR_ARG 被吞——
        # 从未生效的死代码；RDTSC/CPUID 原生可执行，IN/特权指令由 _on_code
        # 特殊字节分支与异常容错处理，无需 INSN hook。
        # 初始状态
        self.uc.reg_write(x86_const.UC_X86_REG_EIP, base + img.entry_point)
        self.uc.reg_write(x86_const.UC_X86_REG_ESP, ENV.STACK_BASE + ENV.STACK_SIZE)
        for _n, r in X86_REGS:
            if r not in (x86_const.UC_X86_REG_EIP, x86_const.UC_X86_REG_ESP,
                         x86_const.UC_X86_REG_FS_BASE):
                try:
                    self.uc.reg_write(r, 0)
                except UcError:
                    pass
        # 入口快照（重放用：在 TLS/入口执行前）
        self._snap = self._snapshot_now()
        # 异常驱动控制流支持（CrackMe 反调试：call 高位未映射 → VEH → NtContinue）
        self.veh_handlers: list[int] = []
        self.uef_handler = 0             # SetUnhandledExceptionFilter 注册的处理器
        self._force_eip: Optional[int] = None
        self._ctx_buf = 0x7F000000          # 异常分发用 EXCEPTION_RECORD/CONTEXT 缓冲
        self._veh_ret_stub = 0              # VEH 返回 stub（ret 8 + int3）
        self._uef_ret_stub = 0              # UEF 返回 stub（ret 4 + 从 CONTEXT 恢复 EIP/ESP）
        self._seh_ret_stub = 0              # SEH 返回 stub（cmp eax,-1 → 恢复/下一帧哨兵）
        self._no_dispatch = False           # 链尾无处理器：停止分发（保持默认异常行为）
        try:
            self.uc.mem_map(self._ctx_buf, 0x1000)
        except UcError:
            pass
        self.uc.hook_add(UC_HOOK_MEM_FETCH_UNMAPPED, self._on_mem_unmapped)
        self.uc.hook_add(UC_HOOK_MEM_READ_UNMAPPED, self._on_mem_unmapped)
        self.uc.hook_add(UC_HOOK_MEM_WRITE_UNMAPPED, self._on_mem_unmapped)
        # 取指权限错误（0x0 页 RW 无 X → 真实 #PF 语义）：同样走异常分发
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

    # ── 执行 ─────────────────────────────────────────────────────
    def run(self, until_addr: Optional[int] = None, steps_limit: int = 0,
            timeout_ms: int = 0, trace_on: bool = False) -> dict:
        if self.uc is None:
            return {"error": "未加载（先 load）"}
        # steps_limit 穿透：调用方指定大步数（如一次跑过大量 xorshift 洗牌）时，
        # 64 位子执行器单轮 count 也跟随放大（默认 800 万/轮；上限 1 亿防失控）
        if steps_limit > 0:
            self.w64_count = min(steps_limit, 100000000)
        self._trace_on = trace_on
        self.stop_reason = ""
        eip = self.uc.reg_read(x86_const.UC_X86_REG_EIP)
        until = until_addr or (self.image.image_base + self.image.size_of_image)
        t0 = _time.time()
        _rem_ms = timeout_ms  # 剩余毫秒（自动快进段不占 timeout，主段递减）
        _seg = 5_000_000      # 主循环分段步数（自动快进检测粒度：分段正常返回后
                              # eip 落动态区即触发快进段；开销 ~0.1ms/段可忽略）
        _remain = steps_limit or 0
        while True:
            _cnt = min(_remain, _seg) if _remain else _seg
            try:
                # 2026-08-19 修复：unicorn 2.1.4 的 emu_start timeout 按微秒解释
                # （实测 timeout=30000 → 实际 31ms）。本 API 保持毫秒语义（调用方
                # tool.py 按 timeout_seconds*1000、测试按毫秒传参），内部 ×1000 转
                # 微秒——此前 timeout_ms=30000 实际 30ms 即超时，run 每 30-46ms
                # 返回 range_end（误标 timeout→range_end），全程需 96 次 run。
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
                break  # 不在动态区：正常执行路径（含 until/exit 到达）
            # —— 自动快进段（2026-08-19）：0x401160 障眼桥循环（KCTF5 洗牌防爆破
            # 111 万轮 ≈ 2.8 亿指令，每轮 Python 回调 → 全程 ~500s 的根源）。
            # 主循环分段正常返回后 eip 落动态区（洗牌循环内）→ 快照兜底 + 卸全域
            # CODE/WRITE hook（保留障眼桥/哨兵单点）→ 1 亿步纯 C 推进；异常
            # （int3/特权指令/未映射等需 hook 语义的事件）→ 回滚到段结束处（状态
            # 与未快进一致，不丢主循环推进）；成功则续跑（循环未完再快进）。
            # 注：回调内嵌套 emu_start / emu_stop 后重启均不被本 unicorn 支持
            # （实测抛 UcError），故全部在 run 层执行。 ——
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
            # 保留 hook = 固定障眼桥/哨兵 + 自学习地址（快进段异常指令经 _on_code
            # 完整处理——含字节检测分支，特殊指令不再抛 UC_ERR_EXCEPTION）。
            # 2026-08-19：本 unicorn 2.1.4 hook_add 参数顺序为
            # (htype, cb, user_data, begin, end)——原写法 (_a, _a, 0) 参数错位
            # 成 user_data=_a, begin=_a, end=0 → 全范围 hook（每指令 Python 回调，
            # "1 亿步纯 C"名存实亡 ≈1.2µs/步）；修正后真正单地址（≈0.02µs/步）。
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
                # 自学习（2026-08-19）：快进段遇动态障眼段特殊指令（int3/in/retf
                # 等）→ 记录异常指令地址（UC_ERR_EXCEPTION 时 RIP=指令起始+1）到
                # 保留集合——下次快进段在此地址触发完整 _on_code 处理，覆盖逐步
                # 扩大直到全速纯 C；上限 128 防失控（本次回滚，状态无损）。
                # 指令长度未知（int3=1 / int imm8=2 / retf=1...）→ RIP-1/2/3 多候选；
                # 只学 image 代码区（0x400000-0x900000）——模拟器哨兵区（VEH 返回
                # stub 的 int3）是异常驱动控制流核心，快进段无法承载完整 VEH 语义，
                # 学了也无效（保留 hook 反而多余）
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
                self._restore_now(_snap)  # 异常：回滚到段结束处（状态无损）
                self.stop_reason = ""
                break
            self._ff_auto_cnt += 1  # 仅成功计数（学习失败不计入上限）
            self.api_calls["__fast_auto__0x401160"] = \
                self.api_calls.get("__fast_auto__0x401160", 0) + 1
            eip = self.uc.reg_read(x86_const.UC_X86_REG_EIP)  # 快进后续跑点
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
        # P13（2026-08-20）：api_calls 过滤批量噪音标记（__wow64_*/__fast_auto__
        # 非真实 API 名——DB 实证 86 条 API 名误报；__int3__/__veh_dispatch/
        # __blackhole__ 等异常/VEH/黑洞诊断标记保留，AI 排查仍需可见）
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
        """安全区快跑（2026-08-19 用户定位：main 前 3 次 0x402100 hex 解析
        0x800 dword 乘加循环——每条指令都经 Python CODE hook 是慢的根源）：
        快照兜底 + 临时卸载引擎全域 CODE hook（保留 MEM 未映射 / INSN /
        apistub 精确 hook——API 调用与反 VM 语义不变）→ 大 count 纯 C 执行。

        兼容性保证（不影响解题功能性）：
        - 快跑只可能"碰巧"成功于无任何语义事件的纯计算区；int3/异常/
          wow64 桥/SEH 哨兵等在无 CODE hook 下执行必然抛 UC_ERR_* →
          恢复快照回滚 → 状态与未快跑完全一致（正确性由快照保证）。
        - 成功路径与普通 run 语义等价（纯计算区无 hook 语义差异）；
          诊断性状态（trace/_executed/_dyncode/api_calls 统计）快跑期间
          不记录——成功即纯计算区，本来无这些事件。"""
        if self.uc is None:
            return {"error": "未加载（先 load）"}
        snap = self._snapshot_now()  # 兜底快照（回滚保证状态无损）
        eip = self.uc.reg_read(x86_const.UC_X86_REG_EIP)
        until = until_addr or (self.image.image_base + self.image.size_of_image)
        hook = getattr(self, "_code_hook", None)
        if hook is not None:
            try:
                self.uc.hook_del(hook)
            except Exception:
                pass
        # 2026-08-19：只卸全域逐指令 hook——保留动态障眼桥/哨兵的单地址 hook
        # （0x4010E0 wow64 桥 / 0x401125 回程 / 0x401130/0x401160 障眼跳过 /
        # SEH/VEH/UEF 哨兵）。否则 KCTF5 洗牌主循环（0x401160 障眼桥 111 万轮）
        # 的真实动态代码（retf/垃圾）在无 hook 下执行必异常 → ff 永远回滚；
        # 保留后纯计算洗牌秒过，含 int3/异常/API 的循环仍回滚（语义不变）
        self._ff_keep: list = []
        # 2026-08-19 注：hook_add 参数顺序为本构建的 (htype, cb, user_data, begin, end)
        for _a in (0x4010E0, 0x401125, 0x401130, 0x401160,
                   0x7F000D00, 0x7F000D10, 0x7F000D20, 0x1000FF40):
            try:
                self._ff_keep.append(self.uc.hook_add(UC_HOOK_CODE, self._on_code,
                                                      0, _a, _a))
            except Exception:
                pass
        t0 = _time.time()
        try:
            # 同 run()：unicorn 2.1.4 timeout 为微秒——毫秒语义 ×1000（见 run）
            self.uc.emu_start(eip, until, timeout=timeout_ms * 1000,
                              count=count or 100_000_000)
            ok, err = True, ""
        except Exception as e:  # noqa: BLE001
            ok, err = False, str(e)
        # 恢复全域 CODE hook（无论成败）
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
        # 成功判定：无异常且 EIP 恰好到达 until（超时/count 用尽/提前退出
        # 都未到目标 → 一律回滚，保证状态与未快跑一致）
        if ok and eip2 == until:
            return {"eip": "0x%X" % eip2, "stop_reason": "fast_ok",
                    "elapsed_ms": round(elapsed * 1000, 1), "fast": True}
        # 回滚：恢复快照（状态无损）+ 清 stop_reason（与未快跑一致）
        self._restore_now(snap)
        self.stop_reason = ""
        return {"eip": "0x%X" % eip2,
                "stop_reason": f"fast_rollback: {err[:120]}",
                "elapsed_ms": round(elapsed * 1000, 1), "fast": True,
                "rollback": True}

    # ── hook 回调 ────────────────────────────────────────────────
    def _on_code(self, uc, address, size, user_data):
        if address == 0x7F000D00:
            # SEH 链推进哨兵：上一帧 handler 返回 CONTINUE_SEARCH(0) → 取 prev 帧继续分发
            self._seh_next_frame(uc)
            return
        if address == 0x7F000D10:
            # VEH→SEH 转换哨兵：VEH 返回 CONTINUE_SEARCH(0) → 从 SEH 链头重新分发
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
            # VEH 链推进哨兵：handler 返回 CONTINUE_SEARCH(0) → 下一个 VEH（真实语义：
            # 按注册顺序全试，全部返回 0 才转 SEH 链）
            code = _u32(self.uc.mem_read(0x7F000910, 4))
            addr = _u32(self.uc.mem_read(0x7F000914, 4))
            idx = _u32(self.uc.mem_read(0x7F000920, 4)) + 1
            if idx < len(self.veh_handlers):
                self.uc.reg_write(x86_const.UC_X86_REG_EIP, self._veh_dispatch(code, addr, idx))
            else:
                self.uc.reg_write(x86_const.UC_X86_REG_EIP, 0x7F000D10)  # VEH 全 0 → SEH
            return
        if address == 0x1000FF40:
            # UEF 返回 CONTINUE_SEARCH(0)：未处理异常 → 停止模拟（等价进程崩溃，
            # 而非死循环重试——UEF handler 返回 0 即放弃处理）
            self._unhandled = True
            uc.emu_stop()
            return
        if self._trace_on and len(self._trace) < TRACE_LIMIT:
            self._trace.append(address)
        if address >= self.image.image_base + self.image.size_of_image and \
                address not in (0x10000000, 0x11000000):
            self._executed.add(address)
        # 指令首字节分类（int3/int/retf/in 特殊指令检测）——2026-08-19 优化：
        # 每指令 mem_read(1) 占 CODE hook 开销约 45%（cProfile 实测）。顺序执行
        # 时一次读 32 字节窗口服务连续多条指令（跳转/回跳不命中才重读），mem_read
        # 次数降至约 1/10（kctf4 实测 miss 104 万→30 万级）；自修改代码由“跳转后
        # 重读”自然覆盖（向当前窗口写入并顺序执行的极端场景才可能读到旧字节）。
        if not (self._win_start <= address < self._win_start + 32):
            try:
                self._win_data = uc.mem_read(address, 32)
                self._win_start = address
            except UcError:
                try:
                    self._win_data = uc.mem_read(address, 1)  # 页尾回退
                    self._win_start = address
                except UcError:
                    self._win_start = -1
                    return
        b = self._win_data[address - self._win_start]
        if b == 0xCC:
            # int3 → EXCEPTION_BREAKPOINT(0x80000003) → VEH 分发（异常驱动控制流反调试）
            if self.veh_handlers and self._dispatch_exception(0x80000003, address):
                return  # VEH 接管：handler 改 CONTEXT → NtContinue 切新 RIP
            # 无 VEH 注册：断点异常被消费（继续下一条），模拟器容错——真实 Windows
            # 中 int3 通常由 VEH 消费（软件断点反调试），模拟器未发现其注册路径
            # （如经未执行/自修改代码注册）时，跳过等价于 VEH 的 ctx.Eip++ 效果；
            # 不再进入 SEH/UEF（否则 UEF 恢复 ctx 会死循环重试）
            uc.reg_write(x86_const.UC_X86_REG_EIP, address + 1)
            self.api_calls["__int3__0x%X" % address] = \
                self.api_calls.get("__int3__0x%X" % address, 0) + 1
            return
        # wow64 障眼桥（反调试）：
        # - 0x4010E0：x64 参数转发桥 call arg4(arg1..arg3)——用 64 位子执行器真实执行
        #   （.data 内 64 位动态验证函数，返回值 eax 决定成功/失败）
        # - 0x401130/0x401160：纯障眼段——跳过，eax 保持不变
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
            self._wow64_ret = eax  # 供 0x40112B 回程恢复（popal 覆盖 eax 后）
            uc.reg_write(x86_const.UC_X86_REG_EAX, eax)
            # 跳至桥的 32 位回程（movd xmm0,eax → popfd/popal → movd eax,xmm0 → ret）：
            # popal 恢复调用者寄存器后 movd 恢复 eax=返回值，ret 弹回调用者
            uc.reg_write(x86_const.UC_X86_REG_EIP, 0x401125)
            self.api_calls[f"__wow64_call__0x{arg4:X}(0x{arg1:X},0x{arg2:X},0x{arg3:X})->0x{eax:X}"] = \
                self.api_calls.get(f"__wow64_call__0x{arg4:X}(0x{arg1:X},0x{arg2:X},0x{arg3:X})->0x{eax:X}", 0) + 1
            return
        if address == 0x401125:
            # 0x4010E0 桥 32 位回程：特判跳过了入口的 pushal/pushfd（未压栈），
            # 故 [esp] 即返回地址——直接弹回调用者，eax=64 位子执行器返回值。
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
            # int imm8：int 0x29=__fastfail(0xC0000409)；int 0x03=断点(0x80000003)
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
            # in 指令（in eax,dx / in al,dx / in al,imm）——真实硬件 ring3 特权指令
            # → #GP(STATUS_PRIVILEGED_INSTRUCTION 0xC0000096) → SEH 链（反 VM 检查：
            # filter 0x40122F → handler → int3 → ... → 继续）。CODE hook 改 EIP 有效
            # （INSN hook 改 EIP 无效），因此在此拦截并跳过 in 执行。
            if self._dispatch_exception(0xC0000096, address):
                return
            # [二分实验] 还原：无处理器容错时清 EAX
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
            # 伪造物理机特征：leaf0 eax=1, ebx='Genu', edx='ineI', ecx='ntel'
            uc.reg_write(x86_const.UC_X86_REG_EAX, 1)
            uc.reg_write(x86_const.UC_X86_REG_EBX, 0x756E6547)
            uc.reg_write(x86_const.UC_X86_REG_EDX, 0x49656E69)
            uc.reg_write(x86_const.UC_X86_REG_ECX, 0x6C65746E)
        elif insn == x86_const.UC_X86_INS_IN:
            # in eax,dx——真实硬件 ring3 特权指令 → #GP(STATUS_PRIVILEGED_INSTRUCTION 0xC0000096)
            # → SEH 链（反 VM 检查的异常驱动部分：filter 0x40122F → handler → int3 → ...）
            # 无处理器时兕底跳过（#GP 后 EAX 不变，不清 EAX）
            self._dispatch_exception(0xC0000096, address)

    def _on_retf(self, uc, address, opcode: int) -> None:
        """retf/retf imm16：弹出 EIP+CS（wow64 0x23/0x33 段切换障眼法）。
        模拟内保持 32 位执行，仅取新 EIP（程序本体 32 位，段切换只是反调试手段）。
        CS=0x33（切 64 位）时，64 位短代码在 32 位模式解码错误（REX.R 前缀 0x44
        被解码为 inc esp 致 esp 偏移），用 capstone 64 位模式模拟该段至回切 retf。"""
        try:
            esp = uc.reg_read(x86_const.UC_X86_REG_ESP)
            eip = _u32(uc.mem_read(esp, 4))
            cs = _u32(uc.mem_read(esp + 4, 4))
            add = 8
            if opcode == 0xCA:  # retf imm16
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
        """模拟 wow64 障眼法段（CS=0x33 retf 进入 → 64 位短代码 → retf 回切）。
        64 位代码通常：寄存器垃圾指令 + 栈调整（add [esp],imm / mov [esp+4],imm）+ retf。
        用 capstone 64 位逐条解码，支持 call/out/in/mov/add 常见模式，执行到回切 retf。"""
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
                # 回切 32 位：保留弹回的 eip——落在低地址/未映射区则 FETCH_UNMAPPED
                # → 异常分发 → SEH 链 → filter → handler 块 → int3 → filter → 继续
                # （真实环境行为：wow64 段弹回 0x30 → #PF → SEH）
                uc.reg_write(x86_const.UC_X86_REG_ESP, esp + 8)
                uc.reg_write(x86_const.UC_X86_REG_EIP, neip)
                self.api_calls[f"__wow64_retf__0x{cur:X}->0x{neip:X}"] = \
                    self.api_calls.get(f"__wow64_retf__0x{cur:X}->0x{neip:X}", 0) + 1
                return
            if ins.mnemonic == "call":
                esp -= 4
                uc.mem_write(esp, (cur + ins.size).to_bytes(4, "little"))
            elif ins.mnemonic == "out":
                pass  # 特权指令障眼（#GP 由异常分发处理，模拟中跳过）
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
        """x64 子执行器：x64 约定 call target(arg1, arg2, arg3) → 返回 rax。
        0x4010E0 桥的 64 位段：xchg r14,rsp（切 64 位栈）→ mov r9,rdx / r8,rcx /
        rdx,rbx / rcx,rax → call r9 → xchg 恢复 → call 0x401118 → mov [rsp+4],0x23
        → add [rsp],0xd → retf 回 32 位（EIP=0x401125）。
        子执行器用独立 64 位 unicorn 实例：内存惰性从主实例拉取（未映射页），
        写操作同步回主实例（验证函数可能写参数缓冲）；64 位栈用 0x7FF00000 高区。
        验证函数 ret 到 0x401110（桥后半段起点）即停止——主模拟器从 0x401125
        （回程）继续。"""
        try:
            import unicorn as _uc
            from unicorn import x86_const as _xc
            from unicorn import unicorn_const as _ucc
            uc64 = _uc.Uc(_uc.UC_ARCH_X86, _uc.UC_MODE_64)
            # 本 unicorn 2.x 构建的 errno：READ_UNMAPPED=6 / WRITE_UNMAPPED=7 /
            # FETCH_UNMAPPED=8 / EXCEPTION=21（与文档默认不同，用常量而非硬编码）
            _ERR_RU, _ERR_WU, _ERR_FU = (_ucc.UC_ERR_READ_UNMAPPED,
                                          _ucc.UC_ERR_WRITE_UNMAPPED,
                                          _ucc.UC_ERR_FETCH_UNMAPPED)
            _um_cnt = 0  # 未映射访问计数（诊断：回绕区慢点定位）

            def on_unmapped(uc, access, address, size, value, user_data):
                nonlocal _um_cnt
                _um_cnt += 1
                page = address & ~0xFFF
                try:
                    data = self.uc.mem_read(page, 0x1000)
                    uc.mem_map(page, 0x1000, 7)  # RWX（UC_PROT_ALL 类型异常，用整数）
                    uc.mem_write(page, bytes(data))  # mem_read 返回 bytearray，需 bytes() 转换
                    return True
                except Exception:
                    # 主实例也未映射：
                    # - 数据访问（READ/WRITE）：映射空页容错（读 0）——真实 Windows 中栈顶
                    #   边界等页已提交，保证子执行器继续
                    # - 取指（FETCH）：不映射空页——真实进程该页未提交时访问 → #PF；
                    #   通用异常驱动跳板：ret 弹未映射地址时，真实 64 位 VEH 修正 RIP——
                    #   修正目标从栈帧推导：返回地址链在 [rsp-0x30..rsp+0x20] 内找
                    #   代码区地址（排除桥返回点 0x401110）；其余未映射取指返回 False
                    #   中断（避免 0x00 垃圾流）
                    if access == 0x15:  # UC_HOOK_MEM_FETCH_UNMAPPED
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

            # 纯跑模式（默认）：预拉取主实例全部已映射页到子执行器后，不注册任何
            # 内存 hook——零 Python 回调开销（unicorn 纯 C 执行，0.02-0.05µs/步）。
            # 未映射访问（READ/WRITE/FETCH）抛 UC_ERR_*_UNMAPPED，由执行循环 except
            # 分支动态重挂 hook 小步处理（拉取/空页容错/跳板修正）后移除；
            # 写同步改为返回前全量写回（on_write 不注册——Warning 链 wr=0 无需逐写）。
            # 旧模式（w64_codehook=True / w64_hooks=True）：保留逐指令 CODE hook
            # 与逐写同步（回退选项）。
            _use_hooks = getattr(self, "w64_hooks", False)
            if _use_hooks or getattr(self, "w64_codehook", False):
                uc64.hook_add(_uc.UC_HOOK_MEM_READ_UNMAPPED, on_unmapped)
                uc64.hook_add(_uc.UC_HOOK_MEM_WRITE_UNMAPPED, on_unmapped)
                uc64.hook_add(_uc.UC_HOOK_MEM_FETCH_UNMAPPED, on_unmapped)
                uc64.hook_add(_uc.UC_HOOK_MEM_WRITE, on_write)
            else:
                # 预拉取：主实例已映射页复制到子执行器（64 位栈区独立，跳过）
                for _rg in self.uc.mem_regions():
                    _ba, _en = _rg[0], _rg[1]
                    if _ba >= 0x7FE00000:
                        continue  # 64 位栈区独立（真实 wow64 栈不回写 32 位地址空间）
                    try:
                        _sz = _en - _ba
                        if _sz > 0x1000000:
                            continue  # 超大区域（不应出现）跳过
                        uc64.mem_map(_ba, (_sz + 0xFFF) & ~0xFFF, 7)  # size 须页对齐
                        uc64.mem_write(_ba, bytes(self.uc.mem_read(_ba, _sz)))
                    except Exception:
                        pass
                # 预映射“已知未映射访问区”（空页容错语义不变——读 0）：洗牌扫描
                # 带（低区探测/高区跳板槽/TEB 区），消除逐页小步重试的慢区（每页 2 次
                # emu_start 重启——r1448 后每轮 1.9s 的根源）
                _PREMAP = [(0x1000, 0x3FF000),   # 低区扫描带（到 image 前）
                           (0x841000, 0x7BF000),  # image 尾后扫描带
                           (0x80000000, 0x1000000),  # 高区跳板槽（0x8000D000 等）
                           (0xF0000000, 0x1000000),  # 0xF00E5FB4 区
                           (0xFFF00000, 0x100000),   # 0xFFFB6010/0xFFFC4010 区
                           (0x7FFE0000, 0x8000)]     # TEB/PEB 区
                for _ba, _sz in _PREMAP:
                    try:
                        uc64.mem_map(_ba, _sz, 7)
                    except Exception:
                        pass

            _dirty_pages: set = set()  # 旧模式：脏页延迟同步（BLOCK 模式逐写标脏）
            _wr_cnt = 0
            _exc_cnt = 0

            def on_write(uc, access, address, size, value, user_data):
                nonlocal _wr_cnt
                if 0x7FE00000 <= address < 0x80000000:
                    return  # 64 位栈区独立（真实 wow64 栈不回写 32 位地址空间）
                _wr_cnt += 1
                if getattr(self, "w64_codehook", False):
                    _safe_cache.pop(address, None)  # 自修改：该地址指令分类缓存失效
                    if _loop_cache:
                        _loop_cache.clear()
                    try:
                        # 旧模式逐写同步（value 可能为有符号——mask 后再 to_bytes）
                        self.uc.mem_write(address, (value & ((1 << (8 * size)) - 1)).to_bytes(size, "little"))
                    except Exception:
                        pass
                elif _use_hooks:
                    _dirty_pages.add(address & ~0xFFF)  # BLOCK 模式：只标脏页（轮末同步）

            if _use_hooks or getattr(self, "w64_codehook", False):
                uc64.hook_add(_uc.UC_HOOK_MEM_WRITE, on_write)

            # 64 位层反调试/特权指令容错：unicorn 64 位模式下 CODE hook 内改 RIP
            # 或改内存都无法阻止 int3/in 等执行（已取指，仍抛 UC_ERR_EXCEPTION），
            # 采用 emu_stop + 从下一地址重启（hook 内 emu_stop 立即停止，不执行当前指令）。
            # 注：本 unicorn 构建 64 位 UC_HOOK_INSN 不可用（仅 IN/OUT/CPUID/SYSCALL 有
            # 专用回调，其余 raise UcError），容错只能靠 CODE hook；_safe_cache 缓存
            # 指令分类（普通/特殊），循环体重复地址跳过 mem_read 加速 xorshift 洗牌
            _skip_next: list = []
            _safe_cache: dict = {}
            # 循环快进（xorshift 洗牌加速）：检测同地址重复执行 → 分析纯算术循环体 →
            # Python 批量模拟 N 次迭代 → emu_stop 从循环出口/循环头重启。通用：不写死
            # 程序地址，白名单外指令/复杂寻址/嵌套分支一律不快进（保守）。
            _loop_cache: dict = {}     # head -> (exit, insns, cnt_ins) 或 None（不可快进）
            _rip_seen: dict = {}       # address -> 最近执行序号（循环检测）
            _rip_seq = 0
            _FF_ALLOWED = {"mov", "movzx", "movsx", "lea", "add", "sub", "xor",
                           "shl", "shr", "sar", "and", "or", "inc", "dec",
                           "neg", "not", "cmp", "test"}
            _REG64 = {"rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rbp", "rsp",
                      "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15"}

            def _reg_base(name):
                """寄存器名 → (64 位基名, 写掩码)；未知返回 (None, 0)。"""
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
                if len(n) == 3 and n[0] == "r" and n[1:].isdigit():  # r8-r15
                    return n, 0xFFFFFFFFFFFFFFFF
                if len(n) == 4 and n[0] == "r" and n[1:-1].isdigit():  # r8d-r15d
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

            # 快进指令解析为纯数据元组（不依赖 capstone CsInsn/md 存活——
            # md GC 后 operands 变 None 导致 AttributeError）：
            # (addr, size, mnemonic, [(optype, regname, imm, memdisp, opsize), ...])
            # optype: 0=REG 1=IMM 2=MEM(仅 base=None/index=None 或 base=rip 绝对地址)

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
                            return None  # 复杂寻址（含 rip 相对）：保守不快进
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
                            # 快进模拟的写同步主实例（同 on_write；64 位栈区独立）
                            self.uc.mem_write(_addr, _raw)
                        except Exception:
                            pass

            def _sim_insn(uc, ins, regs):
                """模拟单条白名单指令（ins 为解析元组）。"""
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
                    return  # 白名单外（分析已拦截，防御）
                if _dst is not None:
                    _op_write(uc, _dst, _nv, regs)
                # flags：只模拟 ZF（xorshift 循环退出用 dec/sub; jnz）；mov/lea 不更新
                if _mn in ("add", "sub", "xor", "and", "or", "shl", "shr", "sar",
                           "inc", "dec", "neg", "not"):
                    _w = (_dst[4] * 8) if _dst is not None else 64
                    regs["_zf"] = ((_nv & ((1 << _w) - 1)) == 0)

            def _sim_loop(uc, insns, n):
                regs = {"_zf": False}
                for _ in range(n):
                    for ins in insns:
                        if ins[2] in ("jnz", "jne", "jz", "je", "jmp"):
                            continue  # 回跳：线性展开执行 N 次（循环体无其他分支）
                        _sim_insn(uc, ins, regs)

            def _read_cnt(uc, ins):
                """返回 (计数器当前值, 每迭代步长)；不可快进返回 None。"""
                _op = ins[3][0]
                if ins[2] == "dec":
                    _step = 1
                else:  # sub
                    _src = ins[3][1] if len(ins[3]) > 1 else None
                    if _src is None or _src[0] != 1:
                        return None  # sub reg,reg 步长未知：保守不快进
                    _step = _src[2]
                if _step <= 0:
                    return None
                if _op[0] == 0:
                    return _reg_read(uc, _op[1], {}) & 0xFFFFFFFFFFFFFFFF, _step
                if _op[0] == 2:
                    return int.from_bytes(uc.mem_read(_op[3] & 0xFFFFFFFFFFFFFFFF, _op[4]), "little"), _step
                return None

            def _analyze_loop(head):
                """反汇编 head 起窗口，找唯一回跳 head 的 jcc；白名单校验；
                返回 (insns, exit, cnt_ins) 或 (None, 0, None)。insns 为解析元组列表。"""
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
                                return None, 0, None  # 无 dec/sub 计数器：保守不快进
                            return _body, _ins.address + _sz, _cnt
                        return None, 0, None  # 非回跳分支 → 不可快进
                    if _mn not in _FF_ALLOWED:
                        return None, 0, None
                    _parsed = _parse_ins(_ins)
                    if _parsed is None:
                        return None, 0, None
                    _body.append(_parsed)
                    if _mn in ("dec", "sub") and _cnt is None:
                        _cnt = _parsed  # 计数器：回跳前的 dec/sub（sub 步长须 imm，_read_cnt 校验）
                    if _ins.address + _sz > head + 128:
                        return None, 0, None
                return None, 0, None

            def _run_ff(uc, head, cache):
                """快进；返回 True 表示已 emu_stop（调用方应 return）。"""
                exit_addr, insns, cnt_ins = cache
                _rc = _read_cnt(uc, cnt_ins)
                if _rc is None:
                    return False
                _val, _step = _rc
                _n = _val // _step  # 可完整迭代次数（剩余 < 步长时逐指令执行）
                if _n <= 0:
                    return False
                _n = min(_n, 1000000)  # 单次快进上限（防失控循环占满 CPU）
                _sim_loop(uc, insns, _n)
                if _read_cnt(uc, cnt_ins)[0] > 0:
                    _skip_next.append(head)  # 未耗尽：回循环头继续（下次再快进）
                else:
                    _skip_next.append(exit_addr)
                self.api_calls["__w64_ff__0x%X" % head] = \
                    self.api_calls.get("__w64_ff__0x%X" % head, 0) + 1
                uc.emu_stop()
                return True

            def _try_fast_forward(uc, head):
                """尝试快进；返回 True 表示已快进。"""
                try:
                    hit = _loop_cache.get(head)
                    if hit is None and head not in _loop_cache:
                        hit = _analyze_loop(head)
                        _loop_cache[head] = hit
                    if hit is None or hit[0] is None:
                        return False  # 不可快进（含缓存失败标记 None）
                    return _run_ff(uc, head, hit)
                except Exception:
                    return False

            def on_code(uc, address, size, user_data):
                nonlocal _rip_seq, _rip_seen
                if address == 0x401110:
                    uc.emu_stop()
                    return
                # 循环检测（每步 O(1)）：同一地址最近 2048 步内重复 → 快进尝试
                # （跳板链/洗牌循环体重复间隔可大于 128 步，窗口需覆盖）
                _rip_seq += 1
                _prev = _rip_seen.get(address)
                _rip_seen[address] = _rip_seq
                if _prev is not None and _rip_seq - _prev < 2048:
                    if _try_fast_forward(uc, address):
                        return  # 快进后 _skip_next 已置，重启走续跑
                if len(_rip_seen) > 8192:
                    _rip_seen = {k: v for k, v in _rip_seen.items()
                                 if _rip_seq - v < 2048}
                _c = _safe_cache.get(address)
                if _c is True:
                    return  # 已确认普通指令：零开销
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
                if b in (0xCC, 0xEC, 0xF4):  # int3 / in al,dx / hlt
                    nxt = address + 1
                elif b == 0xED:  # in eax,dx——#GP 后 RAX 不变（指令未执行），只跳过
                    nxt = address + 1
                elif b in (0xE4, 0xE5):  # in al/eax,imm8——同上，不清 RAX
                    nxt = address + 2
                elif b in (0xEE, 0xEF):  # out dx,al/eax
                    nxt = address + 1
                elif b in (0xE6, 0xE7):  # out imm8,al/eax
                    nxt = address + 2
                elif b in (0x6C, 0x6D, 0x6E, 0x6F):  # insb/ins/outsb/outsd（ring3 特权，跳过）
                    nxt = address + 1
                elif b in (0xF1, 0xFA, 0xFB):  # int1(cli/sti 前置花指令) / cli / sti——
                    # ring3 特权，真实由 VEH 消费；跳过等价不处理
                    nxt = address + 1
                elif b == 0xCB:  # retf——Heaven's Gate 段切换
                    try:
                        _rsp = uc.reg_read(_xc.UC_X86_REG_RSP)
                        _tgt = int.from_bytes(uc.mem_read(_rsp, 8), "little")
                        _cs = int.from_bytes(uc.mem_read(_rsp + 8, 8), "little")
                    except Exception:
                        _tgt, _cs = 0, 0
                    if _cs == 0x23:
                        # 回 32 位段：验证函数已完成，停止子执行器（RAX=返回值）
                        _skip_next.append(0x401110)
                        uc.emu_stop()
                        return
                    # 普通 retf：模拟 ret（弹目标地址，RSP+16 弹 CS）
                    uc.reg_write(_xc.UC_X86_REG_RSP, _rsp + 16)
                    _skip_next.append(_tgt)
                    uc.emu_stop()
                    return
                if nxt is not None:
                    _skip_next.append(nxt)
                    uc.emu_stop()

            def _exc_next(uc, addr, b):
                """BLOCK 模式异常容错：读异常指令字节（RIP-1）→ 返回下一条地址；
                不可处理返回 None（中断子执行器）。"""
                if b in (0xCC, 0xEC, 0xF4):  # int3 / in al,dx / hlt
                    return addr + 1
                if b == 0xED:  # in eax,dx——#GP 后 RAX 不变（指令未执行），只跳过
                    return addr + 1
                if b in (0xE4, 0xE5):  # in al/eax,imm8
                    return addr + 2
                if b in (0xEE, 0xEF):  # out dx,al/eax
                    return addr + 1
                if b in (0xE6, 0xE7):  # out imm8,al/eax
                    return addr + 2
                if b in (0x6C, 0x6D, 0x6E, 0x6F):  # insb/ins/outsb/outsd（ring3 特权）
                    return addr + 1
                if b in (0xF1, 0xFA, 0xFB):  # int1 / cli / sti
                    return addr + 1
                if b == 0xCB:  # retf——Heaven's Gate 段切换
                    try:
                        _rsp = uc.reg_read(_xc.UC_X86_REG_RSP)
                        _tgt = int.from_bytes(uc.mem_read(_rsp, 8), "little")
                        _cs = int.from_bytes(uc.mem_read(_rsp + 8, 8), "little")
                    except Exception:
                        return None
                    if _cs == 0x23:
                        return 0x401110  # 回 32 位段：验证函数已完成，停止子执行器
                    uc.reg_write(_xc.UC_X86_REG_RSP, _rsp + 16)  # 普通 retf：弹 CS
                    return _tgt
                return None

            if getattr(self, "w64_codehook", False):
                # 旧模式（w64_codehook=True）：逐指令 CODE hook（含循环快进）
                uc64.hook_add(_uc.UC_HOOK_CODE, on_code)
            # else：纯跑模式（默认）——不注册任何 CODE/MEM hook：纯 C 执行。
            # 特殊指令（int3/in/out/ins/outs/int1/cli/sti/hlt/retf）执行时 unicorn
            # 抛 UC_ERR_EXCEPTION（RIP=指令起始+1），由执行循环 except 分支读 RIP-1
            # 字节容错（_exc_next）；未映射访问由 except 分支按 errno 分类处理
            # 64 位栈（主实例 0x7F000000-0xFE000FFF 已映射，惰性拉取即可）
            rsp = 0x7FF00000 - 0x200
            # Python 侧 mem_write 不经过 UNMAPPED hook——先映射栈页
            # （unicorn 2.x UC_PROT_ALL 类型异常，用整数 7 = RWX）
            try:
                uc64.mem_map(rsp & ~0xFFF, 0x1000, 7)
            except _uc.UcError:
                pass
            # 压返回地址（0x401110，桥后半段）——写在 RSP 处（RSP = rsp - 8）：
            # 直接 ret 的代码弹 [RSP] → 0x401110 停止（call 压栈路径不受影响）
            uc64.mem_write(rsp - 8, (0x401110).to_bytes(8, "little"))
            # 2026-08-19 修复：验证函数地址（0x8386B8/0x7A6920）> 桥返回点 0x401110，
            # unicorn 2.1.4 对 begin > until 的处理是立即返回且 RIP=until（0 指令执行）
            # → 子执行器从未真正运行、RAX 未初始化返回 0 → main 短路 Failed!（用户发现 3）。
            # until 改用 64 位上限，"到达 0x401110 即停"语义由单地址 CODE hook 承担
            # （每轮最多触发一次，纯 C 执行零额外开销）。hook_add 参数顺序为本构建的
            # (htype, cb, user_data, begin, end)。
            def _stop_at_401110(uc, a, s, d):
                uc.emu_stop()

            uc64.hook_add(_uc.UC_HOOK_CODE, _stop_at_401110,
                          0, 0x401110, 0x401110)
            # 桥（0x4010e0 64 位段）只重排 arg1-4 到 rcx/rdx/r8/r9，其余 64 位寄存器
            # 保留 32 位入口值（花指令/验证逻辑依赖 rsi/rdi/rbx/rbp 的真实值）
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
            # wow64 64 位线程环境：GS_BASE=TEB64、r15=TEB64（真实 wow64 中 r15 指向 TEB64）；
            # gs:[0x60] → PEB64；Meng 写 PEB64+2 经 on_write 同步回主实例
            if getattr(self, "_teb64", 0):
                try:
                    uc64.reg_write(_xc.UC_X86_REG_GS_BASE, self._teb64)
                    uc64.reg_write(_xc.UC_X86_REG_R15, self._teb64)
                    # TEB64+0x60 = PEB64（每次调用前确保，防止被写坏）
                    uc64.mem_write(self._teb64 + 0x60,
                                   (self._peb64).to_bytes(8, "little"))
                except Exception:
                    pass
            _addr = target
            _prev = -1
            _rounds = 0
            _t0 = _time.time()
            _exc_last = -1       # 上一次异常 RIP（同 RIP 连续异常防护）
            _exc_same = 0        # 同 RIP 连续异常计数（≥3 中断，防矫正死循环）
            # 轮数/步数上限：xorshift 洗牌总量大（main 多对象洗牌），512 轮 × w64_count
            # = 40 亿步上限（约 10-15 分钟）；同 RIP 死循环检测仍生效
            _w64c = int(getattr(self, "w64_count", 8000000))
            _w64r = int(getattr(self, "w64_rounds", 512))
            # 2026-08-19 诊断/自适应：count 耗尽续跑计数 + break 原因（AI 从
            # __w64__ 统计可见验证函数为何提前返回）；长验证函数（如 0x8386B8
            # 数千万步）续跑 ≥2 次自动放大单轮步数（×2 上限 1 亿）；总步数预算
            # = 初始 800 万 × 512 不变（while 条件按累计步数截断，防伪循环失控）
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
                    # count 限制：子执行器内长循环（如 Warning 链 xorshift 洗牌）不无限跑；
                    # count 耗尽后按 RIP 续跑（验证函数需数千万步，见 0x8386B8）
                    uc64.emu_start(_addr, 0xFFFFFFFFFFFF, count=_w64c)
                except _uc.UcError as _e:
                    if _skip_next:
                        # 异常（如未映射取指）：_skip_next 已记录修正目标（retf/特权指令）——
                        # unicorn 重试会再抛，直接按记录目标重启
                        _addr = _skip_next.pop(0)
                        continue
                    _eno = _e.errno
                    if _eno in (_ERR_RU, _ERR_WU) and not (_use_hooks or getattr(self, "w64_codehook", False)):
                        # 纯跑模式：READ/WRITE 未映射——动态重挂 hook 小步处理（拉取/空页
                        # 容错）后移除（保留空页容错语义，避免中断依赖读 0 的路径）。
                        # 小步必须从异常 RIP 处开始（从 _addr 起点 2000 步可能到不了异常处）
                        _h_a = uc64.hook_add(_uc.UC_HOOK_MEM_READ_UNMAPPED, on_unmapped)
                        _h_b = uc64.hook_add(_uc.UC_HOOK_MEM_WRITE_UNMAPPED, on_unmapped)
                        try:
                            uc64.emu_start(uc64.reg_read(_xc.UC_X86_REG_RIP),
                                           0x401110, count=50000)
                        except Exception:
                            pass
                        _addr = uc64.reg_read(_xc.UC_X86_REG_RIP)  # 续跑点更新
                        try:
                            uc64.hook_del(_h_a)
                            uc64.hook_del(_h_b)
                        except Exception:
                            pass
                        continue
                    if _eno == _ERR_FU and not (_use_hooks or getattr(self, "w64_codehook", False)):
                        # 纯跑模式：FETCH 未映射——通用异常驱动跳板：ret 弹未映射地址时，
                        # 真实 64 位 VEH 修正 RIP——修正目标从栈帧推导（返回地址链在
                        # [rsp-0x30..rsp+0x20] 内找代码区地址，排除桥返回点 0x401110）
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
                                # 栈帧推导失败——指令矫正层兜底（R4 向后扫描/R5 跳转表）
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
                    # 特殊指令执行抛 UC_ERR_EXCEPTION，RIP=指令起始+1；
                    # 读 RIP-1 字节判断（int3/in/out/cli/sti/hlt/retf）→ 跳下一条重启
                    try:
                        _rp = uc64.reg_read(_xc.UC_X86_REG_RIP)
                        _b = uc64.mem_read(_rp - 1, 1)[0]
                    except Exception:
                        _w64_break = "rip1:fail@0x%X" % uc64.reg_read(_xc.UC_X86_REG_RIP)
                        break
                    _nx = _exc_next(uc64, _rp - 1, _b)
                    if _nx is None:
                        # 指令矫正层（fault-driven）：_exc_next 未命中（非特权指令）→
                        # R1-R5 规则（短跳垃圾/pushfd 包裹/恒定条件跳转/向后扫描/跳转表）
                        _dt, _dr = _deobf_correct(uc64, _rp - 1, 64)
                        if _dt is not None:
                            if _rp == _exc_last:
                                _exc_same += 1
                            else:
                                _exc_last, _exc_same = _rp, 1
                            if _exc_same >= 3:
                                _w64_break = "exc:same3@0x%X" % (_rp - 1)
                                break  # 同 RIP 连续矫正失败——中断，防死循环
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
                    _addr = _skip_next.pop(0)  # 从跳过的指令下一条重启
                    continue
                if uc64.reg_read(_xc.UC_X86_REG_RIP) == 0x401110:
                    break  # 正常到达桥后半段（完成）
                _nxt = uc64.reg_read(_xc.UC_X86_REG_RIP)  # count 耗尽：续跑
                if _nxt == _addr and _nxt == _prev:
                    _w64_break = "deadloop@0x%X" % _nxt
                    break  # 死循环：同一续跑点出现两次
                _prev, _addr = _addr, _nxt
                _cnt_exh += 1
                _steps_done += _w64c  # count 耗尽 = 本轮跑满 count（预算累计）
                if _cnt_exh >= 2:  # 长验证函数特征：自动放大单轮步数（总预算不变）
                    _w64c = min(_w64c * 2, 100_000_000)
            # 写回主实例：纯跑模式无 on_write——全量写回（64 位栈区独立跳过）；
            # 旧模式（_dirty_pages 脏页）按脏页写回
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
                    for _pa, _ps in _PREMAP:  # 预映射区主实例无对应页——跳过（省拷贝）
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
            # 黑洞探测（w64_blackhole=True 默认）：多轮纯 C 执行无异常、无矫正——
            # 疑似反调试算力黑洞（d810 SingleIterationLoop 证明思想 + 计数）：
            # 只报告不自动 patch（AI 决定；w64_blackhole_auto=True 可 patch ret）
            # 2026-08-19：条件按累计步数（原 8 轮 × 800 万 = 6400 万等效）——
            # count 自适应放大后轮数减少但步数语义不变
            if getattr(self, "w64_blackhole", True) and \
                    _steps_done >= 8 * 8000000 and _exc_cnt == 0:
                self.api_calls["__blackhole__0x%X" % target] = "rounds=%d cnt=%d" % (_rounds, _w64c)
                if getattr(self, "w64_blackhole_auto", False):
                    try:  # 自动 patch：入口写 ret（c3）——洗牌跳过诊断验证过安全
                        self.uc.mem_write(target, b"\xc3")
                    except Exception:
                        pass
            return uc64.reg_read(_xc.UC_X86_REG_RAX) & 0xFFFFFFFF
        except Exception:
            return 0

    def _on_mem_unmapped(self, uc, access, address, size, value, user_data):
        """未映射访问。
        unicorn 2.x 的 mem hook 返回 True 后「重试当前指令」（改 EIP 无效，会报 UC_ERR_MAP），
        因此：
        - 数据访问（READ/WRITE）：按页 mem_map，让重试指令直接成功（模拟真实进程已提交内存）；
        - 取指（FETCH）：异常驱动控制流（call 高位未映射 → SEH/VEH/UEF → NtContinue），
          在异常地址写 jmp handler 指令，重试执行 jmp 自然进入处理器。"""
        if getattr(self, "_no_dispatch", False):
            return False  # 链尾无处理器：保持默认异常行为（unicorn 报错停止模拟）
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
        """异常分发：VEH → SEH 链 → UEF（异常驱动控制流核心）。
        返回 True=处理器接管（unicorn 不应停止）；False=无处理器（保持默认异常行为）。
        write_jmp=True（mem hook 场景）：在 address 写 jmp handler 指令——unicorn 2.x
        mem hook 返回 True 后重试当前指令，改 EIP 无效（UC_ERR_MAP），jmp 才能切入 handler。"""
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
                # mem hook 场景：写 jmp handler（E9 rel32）——重试执行该指令即切入 handler。
                # 页权限提升 RWX：低地址页（FS 镜像）无 X 权限，jmp 需要可执行
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

    # ── 异常分发辅助（VEH/SEH/UEF 放行式调用）──────────────────
    def _build_rec_ctx(self, code: int, address: int) -> tuple:
        """构造 EXCEPTION_RECORD + CONTEXT（x86，缓冲 0x7F000000/0x400）。
        CONTEXT 为标准 i386 布局：Eip=0xB8、Esp=0xC4（handler 如 0x401190 直接
        inc [ctx+0xB8] 改 Eip 跳过 int3——偏移必须与真实 Windows 一致）。"""
        rec = self._ctx_buf
        ctx = self._ctx_buf + 0x400
        self.uc.mem_write(rec, (code & 0xFFFFFFFF).to_bytes(4, "little") + b"\x00\x00\x00\x00" +
                          b"\x00\x00\x00\x00" + (address & 0xFFFFFFFF).to_bytes(4, "little") +
                          b"\x00\x00\x00\x00")
        ctx_data = bytearray(0x2CC)
        _p32(ctx_data, 0x00, 0x10007)  # ContextFlags = CONTEXT_FULL
        # i386 CONTEXT：+0x8C SegGs..+0x98 SegDs；+0x9C Edi..+0xB4 Ebp；+0xB8 Eip；+0xBC SegCs；
        # +0xC0 EFlags；+0xC4 Esp；+0xC8 SegSs
        for reg, off in (("GS", 0x8C), ("FS", 0x90), ("ES", 0x94), ("DS", 0x98),
                         ("EDI", 0x9C), ("ESI", 0xA0), ("EBX", 0xA4), ("EDX", 0xA8),
                         ("ECX", 0xAC), ("EAX", 0xB0), ("EBP", 0xB4),
                         ("EFLAGS", 0xC0), ("ESP", 0xC4)):
            _p32(ctx_data, off, self.uc.reg_read(getattr(x86_const, "UC_X86_REG_" + reg)))
        # Eip 必须用异常地址：在哨兵 hook（0x7F000D00/0x7F000D20）内推进链时
        # reg_read(EIP) 是 hook 地址而非异常指令地址，快照会错
        _p32(ctx_data, 0xB8, address)
        self.uc.mem_write(ctx, bytes(ctx_data))
        return rec, ctx

    def _ctx_restore_stub_addr(self) -> int:
        """CONTEXT 恢复 stub（0x1000FF10）：mov eax,[ctx.Eip]；mov esp,[ctx.Esp]；jmp eax。
        VEH 返回 EXCEPTION_CONTINUE_EXECUTION(-1) / UEF 继续执行时恢复 handler 修改后的
        CONTEXT（handler 如 0x401190 已 inc ctx->Eip 跳过 int3）。
        与 _seh_ret_stub / _veh_ret_stub 的 je -1 目标共享。"""
        if not getattr(self, "_ctx_stub", 0):
            self._ctx_stub = 0x1000FF10
            # mov eax,[0x7F0004B8]（ctx.Eip）；mov esp,[0x7F0004C4]（ctx.Esp）；jmp eax
            stub = b"\xA1\xB8\x04\x00\x7F" + \
                   b"\x8B\x25\xC4\x04\x00\x7F" + \
                   b"\xFF\xE0" + b"\x90" * 6
            self.uc.mem_write(self._ctx_stub, stub)
        return self._ctx_stub

    def _veh_dispatch(self, code: int, address: int, idx: int = 0) -> int:
        """VEH：handler(PEXCEPTION_POINTERS) 放行式调用（NTAPI stdcall 单参数 ret 4）。
        真实签名：LONG NTAPI VectoredHandler(PEXCEPTION_POINTERS)（{rec, ctx} 打包）。
        返回 -1=EXCEPTION_CONTINUE_EXECUTION → stub 从 CONTEXT 恢复（handler 已改 Eip）；
        返回 0=EXCEPTION_CONTINUE_SEARCH → 0x7F000D20 哨兵推进下一个 VEH（注册顺序），
        全部返回 0 → 0x7F000D10 转 SEH 链分发。"""
        if idx >= len(self.veh_handlers):
            return 0
        handler = self.veh_handlers[idx]
        if not self._veh_ret_stub:
            self._veh_ret_stub = 0x1000FF00
            self._ctx_restore_stub_addr()  # 确保 je 目标（0x1000FF10）已创建
            # cmp eax,-1；je 0x1000FF10（恢复 ctx）；jmp 0x7F000D20（VEH 链推进哨兵）
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
        """UEF（SetUnhandledExceptionFilter）：handler(EXCEPTION_POINTERS*)——
        handler 修改 CONTEXT 后返回 CONTINUE_EXECUTION(-1) → stub 从 CONTEXT 恢复新 EIP；
        返回 CONTINUE_SEARCH(0) → 0x1000FF40 停止模拟（未处理异常崩溃，而非死循环重试）。"""
        handler = self.uef_handler
        if not self._uef_ret_stub:
            self._uef_ret_stub = 0x1000FF30
            self._ctx_restore_stub_addr()  # 确保 je 目标（0x1000FF10）已创建
            # cmp eax,-1；je 0x1000FF10（恢复 ctx）；jmp 0x1000FF40（未处理 → 停止）
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
        """SEH 链是否有效：fs:[0]（32 位模拟 FS 段基址为 0 → 线性地址 0x0）。"""
        try:
            frame = _u32(self.uc.mem_read(0x0, 4))
        except UcError:
            return False
        return frame not in (0, 0xFFFFFFFF)

    def _seh_ret_stub_addr(self) -> int:
        if not self._seh_ret_stub:
            self._seh_ret_stub = 0x1000FF20
            # cmp eax,-1；je 0x1000FF10（UEF 恢复 stub：从 CONTEXT 恢复 EIP/ESP）；
            # jmp 0x7F000D00（哨兵：handler 返回 CONTINUE_SEARCH=0 → 推进下一帧）
            self.uc.mem_write(self._seh_ret_stub,
                              b"\x83\xF8\xFF" + b"\x74\xEB" + b"\xE9\xD6\x0D\xFF\x6E")
        return self._seh_ret_stub

    def _seh_dispatch(self, code: int, address: int) -> Optional[int]:
        """SEH 链分发：fs:[0] 帧 → handler(rec, frame, ctx, disp) 放行式调用。
        返回 handler 地址；无有效帧返回 None（链尾 → UEF）。"""
        try:
            frame = _u32(self.uc.mem_read(0x0, 4))
        except UcError:
            return None
        if frame in (0, 0xFFFFFFFF):
            return None
        return self._seh_push_call(frame, code, address)

    def _seh_push_call(self, frame: int, code: int, address: int) -> int:
        """压 SEH 调用栈：返回 stub + rec/frame/ctx/disp 四参。
        handler（如 __except_handler3）改 CONTEXT 后返回 -1 → stub 恢复 ctx 切到 handler 块；
        返回 0（CONTINUE_SEARCH）→ 哨兵推进下一帧。"""
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
        """SEH 哨兵 0x7F000D00：上一帧返回 CONTINUE_SEARCH(0) → 取 prev 帧继续分发；
        链尾（prev=-1/0）→ UEF；无 UEF → 保持默认异常行为（unicorn 报错停止）。"""
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

    # ── 寄存器/内存 ──────────────────────────────────────────────
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
        # 十六进制展示（前 64 字节 + 尾部摘要）
        head = data[:64].hex(" ")
        return f"0x{addr:X} ({len(data)}B)\n{head}"

    def patch(self, addr: int, hexbytes: str) -> str:
        try:
            data = bytes.fromhex(hexbytes.replace(" ", "").replace(",", ""))
        except ValueError:
            return "[Error] err: 非法 hex"
        return self.mem_write(addr, data)

    # ── 快照/重放（unicorn 2.x 无快照 API，自实现：全量映射内存 + 寄存器）──
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
                if size > 0x4000000:      # 跳过超大区域（不应出现）
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
                # 2026-08-19：mem_read 存下的是 bytearray，unicorn 2.x
                # mem_write 只收 bytes（c_char_p）——不转会在恢复时抛
                # ctypes.ArgumentError（ff 快跑回滚路径首次触发此隐藏 bug）
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
        """改输入重跑（黑盒推导）：restore → 设新输入 → run。"""
        self.restore()
        self.inputs = list(inputs)
        return self.run(trace_on=False)

    # ── 执行流 / 动态代码 ────────────────────────────────────────
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
        """收集模拟内新写入且被执行的可执行代码块。"""
        # 写入记录与执行地址求交（按页聚合）
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
            # 该页是否有执行记录
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

    # ── 反调试检测点报告 ─────────────────────────────────────────
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

    # ── 输出 ─────────────────────────────────────────────────────
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
