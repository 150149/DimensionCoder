import struct
import json
from collections import Counter, defaultdict



def _rol(x, k, bits=64):
    k %= bits
    return ((x << k) | (x >> (bits - k))) & ((1 << bits) - 1) if k else x


def _ror(x, k, bits=64):
    k %= bits
    return ((x >> k) | (x << (bits - k))) & ((1 << bits) - 1) if k else x


def _ror8(val, n):
    mask = 0xFF
    val &= mask
    return ((val >> n) | (val << (8 - n))) & mask


def _rol8(val, n):
    mask = 0xFF
    val &= mask
    return ((val << n) | (val >> (8 - n))) & mask



class DecryptChain:

    def __init__(self):
        self.ops = []

    def xor_key(self):
        self.ops.append(('xor_key', lambda t, k: t ^ k))
        return self

    def xor_const(self, c):
        self.ops.append(('xor_const', lambda t, k, c=c: t ^ c))
        return self

    def neg(self):
        self.ops.append(('neg', lambda t, k: (-t) & 0xFF))
        return self

    def not_(self):
        self.ops.append(('not', lambda t, k: (~t) & 0xFF))
        return self

    def ror(self, n):
        self.ops.append(('ror', lambda t, k, n=n: _ror8(t, n)))
        return self

    def rol(self, n):
        self.ops.append(('rol', lambda t, k, n=n: _rol8(t, n)))
        return self

    def add(self, c):
        self.ops.append(('add', lambda t, k, c=c: (t + c) & 0xFF))
        return self

    def sub(self, c):
        self.ops.append(('sub', lambda t, k, c=c: (t - c) & 0xFF))
        return self

    def custom(self, name, fn):
        self.ops.append((name, fn))
        return self

    def decrypt(self, ciphertext_byte, key_byte):
        t = ciphertext_byte
        for name, op in self.ops:
            t = op(t, key_byte)
        return t & 0xFF

    @classmethod
    def from_json(cls, spec):
        chain = cls()
        for item in spec:
            op = item['op']
            if op == 'xor_key':
                chain.xor_key()
            elif op == 'xor_const':
                chain.xor_const(item['c'])
            elif op == 'neg':
                chain.neg()
            elif op == 'not':
                chain.not_()
            elif op == 'ror':
                chain.ror(item['n'])
            elif op == 'rol':
                chain.rol(item['n'])
            elif op == 'add':
                chain.add(item['c'])
            elif op == 'sub':
                chain.sub(item['c'])
        return chain

    def to_json(self):
        return [name for name, _ in self.ops]

    def __repr__(self):
        return f"DecryptChain({[name for name, _ in self.ops]})"



class BytecodeDecoder:

    def __init__(self, pe_data, section_raw_offset, section_rva_start,
                 dispatch_table_rva, dispatch_table_size,
                 decrypt_chain, key_update='xor_plain',
                 cursor_delta=None, image_base=0):
        self.pe_data = pe_data
        self.sec_raw = section_raw_offset
        self.sec_rva = section_rva_start
        self.dt_rva = dispatch_table_rva
        self.dt_size = dispatch_table_size
        self.chain = decrypt_chain
        self.cursor_delta = cursor_delta or {}
        self.image_base = image_base

        if key_update == 'xor_plain':
            self.key_update = lambda k, p: (k ^ p) & 0xFF
        elif key_update == 'add_plain':
            self.key_update = lambda k, p: (k + p) & 0xFF
        elif key_update == 'ror_xor':
            self.key_update = lambda k, p: (_ror8(k, 1) ^ p) & 0xFF
        elif callable(key_update):
            self.key_update = key_update
        else:
            raise ValueError(f"Unknown key_update mode: {key_update}")

        self._load_dispatch_table()

    def _rva_to_raw(self, rva):
        return self.sec_raw + (rva - self.sec_rva)

    def _load_dispatch_table(self):
        raw_off = self._rva_to_raw(self.dt_rva)
        table_bytes = self.pe_data[raw_off:raw_off + self.dt_size * 4]
        self.dispatch_table = struct.unpack(f'<{self.dt_size}i', table_bytes)

    def _handler_for_opcode(self, opcode):
        offset = self.dispatch_table[opcode & 0xFF]
        return (self.dt_rva + offset) & 0xFFFFFFFF

    def _read_byte(self, rva):
        raw = self._rva_to_raw(rva)
        if 0 <= raw < len(self.pe_data):
            return self.pe_data[raw]
        return None

    def run(self, stream_rva, initial_key, num=64, default_delta=1):
        results = []
        cur = stream_rva
        key = initial_key
        for i in range(num):
            read_rva = cur - 1
            c = self._read_byte(read_rva)
            if c is None:
                break
            key_before = key
            opcode = self.chain.decrypt(c, key)
            key = self.key_update(key, opcode)
            handler_rva = self._handler_for_opcode(opcode)
            results.append({
                'seq': i, 'rva': read_rva, 'cipher': c,
                'opcode': opcode, 'handler': handler_rva,
                'key_before': key_before, 'key_after': key,
            })
            delta = self.cursor_delta.get(opcode, default_delta)
            if delta is None:
                break
            cur = read_rva + delta
        return results



class OperationFitter:

    def fit_all(self, traces, input_bytes_dict, n_steps=None):
        keys = list(traces.keys())
        if n_steps is None:
            n_steps = min(len(traces[k]) for k in keys)
        input_map = self._detect_inputs(traces, input_bytes_dict, keys, n_steps)
        ops = []
        fitted = 0
        for i in range(n_steps):
            step_op = self._fit_one_step(traces, keys, i, input_bytes_dict, input_map)
            h = traces[keys[0]][i]['h']
            w = traces[keys[0]][i]['w']
            if step_op is None and w:
                step_op = ('UNKNOWN',)
            if step_op and step_op[0] != 'UNKNOWN':
                fitted += 1
            entry = {
                'i': i, 'h': h, 'op': step_op,
                'dst': w[0][0] if w else None,
                'size': w[0][1] if w else 0,
            }
            ops.append(entry)
        return ops, fitted

    def _detect_inputs(self, traces, input_bytes_dict, keys, n_steps):
        input_map = {}
        for i in range(n_steps):
            for key in keys:
                inp = input_bytes_dict[key]
                st = traces[key][i]
                if not st['w']:
                    continue
                dst, sz, val = st['w'][0]
                mask = (1 << (8 * sz)) - 1
                for idx in range(min(32, len(inp))):
                    if (val & mask) == inp[idx]:
                        ok = True
                        for ok2 in range(8 * sz):
                            byte_val = (val >> ok2) & 0xFF
                            byte_idx = idx + ok2
                            if byte_idx >= 32 or byte_val != inp[byte_idx]:
                                ok = False
                                break
                        if ok:
                            input_map[i] = idx
                            break
                if i in input_map:
                    break
        return input_map

    def _fit_one_step(self, traces, keys, i, input_bytes_dict, input_map):
        sts = [traces[k][i] for k in keys]
        w0 = sts[0]['w']
        if not w0:
            return ('NOWRITE',)
        dst, size, _ = w0[0]
        outs = [st['w'][0][2] if st['w'] else 0 for st in sts]
        mask = (1 << (8 * size)) - 1
        outk = [o & mask for o in outs]

        cells = set()
        vals = []
        for st in sts:
            rd = {}
            for a, v in st['r']:
                if v is not None:
                    rd[a] = v
            vals.append(rd)
            cells |= set(rd.keys())
        cells = sorted(cells)

        if len(set(outk)) == 1:
            return ('CONST', outk[0])
        if i in input_map:
            return ('INPUT', input_map[i])
        for c in cells:
            if all(vals[k].get(c) is not None and
                   (vals[k][c] & mask) == outk[k] for k in range(len(keys))):
                return ('COPY', c)
        for c1 in cells:
            for c2 in cells:
                if c1 == c2:
                    continue
                a1 = [vals[k].get(c1) for k in range(len(keys))]
                a2 = [vals[k].get(c2) for k in range(len(keys))]
                if any(x is None for x in a1 + a2):
                    continue
                if all((a1[k] + a2[k]) & mask == outk[k] for k in range(len(keys))):
                    return ('ADD', c1, c2)
                if all((a1[k] ^ a2[k]) & mask == outk[k] for k in range(len(keys))):
                    return ('XOR', c1, c2)
                if all((a1[k] - a2[k]) & mask == outk[k] for k in range(len(keys))):
                    return ('SUB', c1, c2)
                if all((a1[k] & a2[k]) & mask == outk[k] for k in range(len(keys))):
                    return ('AND', c1, c2)
                if all((a1[k] | a2[k]) & mask == outk[k] for k in range(len(keys))):
                    return ('OR', c1, c2)
        M = (1 << 64) - 1
        for c in cells:
            a1 = [vals[k].get(c) for k in range(len(keys))]
            if any(x is None for x in a1):
                continue
            ks = [(outk[k] - a1[k]) & M for k in range(len(keys))]
            if len(set(ks)) == 1 and ks[0] != 0:
                return ('ADD_CONST', c, ks[0])
            kx = [outk[k] ^ a1[k] for k in range(len(keys))]
            if len(set(kx)) == 1 and kx[0] != 0:
                return ('XOR_CONST', c, kx[0])
            for k in range(1, 64):
                if all(((a1[j] << k) & mask) == outk[j] for j in range(len(keys))):
                    return ('SHL', c, k)
                if all(((a1[j] >> k) & mask) == outk[j] for j in range(len(keys))):
                    return ('SHR', c, k)
                if all((_rol(a1[j], k) & mask) == outk[j] for j in range(len(keys))):
                    return ('ROL', c, k)
                if all((_ror(a1[j], k) & mask) == outk[j] for j in range(len(keys))):
                    return ('ROR', c, k)
            for k in range(64):
                if all(((a1[j] >> k) & 1) == outk[j] for j in range(len(keys))):
                    return ('BIT', c, k)
            if all(((1 - a1[j]) & M) & mask == outk[j] for j in range(len(keys))):
                return ('NOT1', c)
        for c1 in cells:
            for c2 in cells:
                if c1 == c2:
                    continue
                a1 = [vals[k].get(c1) for k in range(len(keys))]
                a2 = [vals[k].get(c2) for k in range(len(keys))]
                if any(x is None for x in a1 + a2):
                    continue
                ok = all(((a1[k] & mask) == outk[k]) or
                         ((a2[k] & mask) == outk[k]) for k in range(len(keys)))
                if not ok:
                    continue
                for cb in cells:
                    bv = [vals[k].get(cb) for k in range(len(keys))]
                    if any(x is None for x in bv):
                        continue
                    good = True
                    for k in range(len(keys)):
                        bit = bv[k] & 1
                        want = a1[k] if bit else a2[k]
                        if (want & mask) != outk[k]:
                            good = False
                            break
                    if good:
                        return ('SELECT', c1, c2, cb)
                    good = True
                    for k in range(len(keys)):
                        bit = bv[k] & 1
                        want = a2[k] if bit else a1[k]
                        if (want & mask) != outk[k]:
                            good = False
                            break
                    if good:
                        return ('SELECT', c2, c1, cb)
        return None



def analyze_reg_roles(samples, reg_names):
    if len(samples) < 3:
        return {}
    reg_roles = {}
    for name in reg_names:
        values = [s.get(name, 0) for s in samples]
        deltas = [values[i + 1] - values[i] for i in range(len(values) - 1)]
        non_zero = [d for d in deltas if d != 0]

        if name in ('Rip', 'Eip'):
            reg_roles[name] = {"role": "HW_IP", "evidence": "instruction pointer"}
            continue
        if name in ('Rsp', 'Esp'):
            reg_roles[name] = {"role": "HW_SP", "evidence": "stack pointer"}
            continue
        if not non_zero:
            reg_roles[name] = {
                "role": "CONSTANT",
                "value": f"0x{values[0]:x}",
                "evidence": f"{len(values)} samples all identical"
            }
            continue
        change_ratio = len(non_zero) / len(deltas)
        unique_deltas = set(non_zero)
        if len(unique_deltas) == 1 and abs(non_zero[0]) == 8:
            reg_roles[name] = {
                "role": "VM_SP",
                "evidence": f"fixed stride {non_zero[0]:+d} (8B align), change={change_ratio:.0%}"
            }
        elif len(unique_deltas) <= 2 and change_ratio > 0.5:
            reg_roles[name] = {
                "role": "COUNTER",
                "evidence": f"stride={sorted(unique_deltas)}, change={change_ratio:.0%}"
            }
        elif change_ratio > 0.8:
            reg_roles[name] = {
                "role": "DATA",
                "evidence": f"change={change_ratio:.0%}, {len(unique_deltas)} strides"
            }
        elif change_ratio > 0.3:
            reg_roles[name] = {
                "role": "DATA_COND",
                "evidence": f"change={change_ratio:.0%}, {len(unique_deltas)} strides"
            }
        else:
            reg_roles[name] = {
                "role": "CONTEXT",
                "evidence": f"change={change_ratio:.0%}, low-freq"
            }
    return reg_roles



class DispatchAnalyzer:

    def __init__(self, pe_data, sections, image_base=0):
        self.pe_data = pe_data
        self.sections = sections
        self.image_base = image_base
        self.dt_size = 256
        self.handlers = {}

    def _rva_to_raw(self, rva):
        for s in self.sections:
            va = s['virtual_address']
            vsz = max(s.get('virtual_size', 0), s.get('raw_size', 0))
            if va <= rva < va + vsz:
                if s.get('raw_size', 0) and (rva - va) < s['raw_size']:
                    return s['raw_offset'] + (rva - va)
        return None

    def _is_code_rva(self, rva):
        for s in self.sections:
            va = s['virtual_address']
            vsz = max(s.get('virtual_size', 0), s.get('raw_size', 0))
            if va <= rva < va + vsz:
                if s.get('characteristics', 0) & 0x20000000:
                    return True
        return False

    def _read_int32(self, rva):
        raw = self._rva_to_raw(rva)
        if raw is not None and raw + 4 <= len(self.pe_data):
            return struct.unpack_from('<i', self.pe_data, raw)[0]
        return None

    def analyze(self, table_rva):
        self.handlers = {}
        valid = 0
        for opcode in range(self.dt_size):
            rva = table_rva + opcode * 4
            offset = self._read_int32(rva)
            if offset is None:
                continue
            handler_rva = (table_rva + offset) & 0xFFFFFFFF
            if self._is_code_rva(handler_rva):
                self.handlers[opcode] = handler_rva
                valid += 1
        return {
            'table_rva': table_rva,
            'valid_handlers': valid,
            'invalid_entries': self.dt_size - valid,
            'handlers': dict(self.handlers),
        }

    def scan_for_tables(self, code_section_rva=None, code_section_size=None,
                        min_valid_ratio=0.8):
        candidates = []
        if code_section_rva and code_section_size:
            scan_ranges = [(code_section_rva, code_section_size)]
        else:
            scan_ranges = []
            for s in self.sections:
                if s.get('characteristics', 0) & 0x20000000:
                    scan_ranges.append((s['virtual_address'], s.get('raw_size', 0)))
        for sec_rva, sec_size in scan_ranges:
            for offset in range(0, sec_size - self.dt_size * 4, 4):
                table_rva = sec_rva + offset
                valid = 0
                for i in range(self.dt_size):
                    entry_rva = table_rva + i * 4
                    val = self._read_int32(entry_rva)
                    if val is not None:
                        handler = (table_rva + val) & 0xFFFFFFFF
                        if self._is_code_rva(handler):
                            valid += 1
                ratio = valid / self.dt_size
                if ratio >= min_valid_ratio:
                    candidates.append({
                        'table_rva': table_rva,
                        'valid_count': valid,
                        'ratio': ratio,
                    })
        candidates.sort(key=lambda c: c['ratio'], reverse=True)
        return candidates



ANTIDBG_APIS = {
    'IsDebuggerPresent', 'CheckRemoteDebuggerPresent', 'NtQueryInformationProcess',
    'NtSetInformationThread',
    'GetTickCount', 'GetTickCount64', 'QueryPerformanceCounter', 'GetSystemTime',
    'GetLocalTime', 'GetSystemTimeAsFileTime', 'NtQuerySystemInformation',
    'GetThreadContext', 'SetThreadContext',
    'HeapSetInformation',
    'OutputDebugStringA', 'OutputDebugStringW',
    'AddVectoredExceptionHandler', 'SetUnhandledExceptionFilter',
    'CloseHandle',
    'CreateToolhelp32Snapshot', 'Process32FirstW', 'Process32NextW',
    'TlsSetValue',
    'ZwQueryInformationProcess',
}

ANTIDBG_OPCODES = {
    0xCC,
    0xF1,
}


def scan_antidbg(pe_data, sections, imports=None):
    result = {
        'suspicious_imports': [],
        'timing_apis': [],
        'exception_apis': [],
        'int3_locations': [],
        'segment_fs_refs': [],
        'segment_gs_refs': [],
    }

    timing_api_names = {
        'GetTickCount', 'GetTickCount64', 'QueryPerformanceCounter',
        'GetSystemTime', 'GetLocalTime', 'GetSystemTimeAsFileTime',
        'NtQuerySystemInformation',
    }
    exception_api_names = {
        'AddVectoredExceptionHandler', 'SetUnhandledExceptionFilter',
        'NtQueryInformationProcess', 'ZwQueryInformationProcess',
        'NtSetInformationThread',
    }

    if imports is None:
        imports = _extract_imports(pe_data, sections)

    for api in imports:
        api_clean = api.rstrip('AW')
        if api_clean in ANTIDBG_APIS or api in ANTIDBG_APIS:
            result['suspicious_imports'].append(api)
            if api_clean in timing_api_names:
                result['timing_apis'].append(api)
            if api_clean in exception_api_names:
                result['exception_apis'].append(api)

    for s in sections:
        if not (s.get('characteristics', 0) & 0x20000000):
            continue
        raw_off = s['raw_offset']
        raw_sz = min(s.get('raw_size', 0), len(pe_data) - raw_off)
        code = pe_data[raw_off:raw_off + raw_sz]
        for i in range(len(code)):
            b = code[i]
            if b in ANTIDBG_OPCODES:
                rva = s['virtual_address'] + i
                result['int3_locations'].append({
                    'rva': rva,
                    'opcode': b,
                })
                if len(result['int3_locations']) >= 50:
                    break

    for s in sections:
        if not (s.get('characteristics', 0) & 0x20000000):
            continue
        raw_off = s['raw_offset']
        raw_sz = min(s.get('raw_size', 0), len(pe_data) - raw_off)
        code = pe_data[raw_off:raw_off + raw_sz]
        for i in range(len(code) - 6):
            if code[i] == 0x64:
                if i + 7 <= len(code):
                    disp = struct.unpack_from('<I', code, i + 3)[0]
                    if disp == 0x30:
                        rva = s['virtual_address'] + i
                        result['segment_fs_refs'].append(rva)
                        if len(result['segment_fs_refs']) >= 20:
                            break
            elif code[i] == 0x65:
                if i + 7 <= len(code):
                    disp = struct.unpack_from('<I', code, i + 3)[0]
                    if disp == 0x60:
                        rva = s['virtual_address'] + i
                        result['segment_gs_refs'].append(rva)
                        if len(result['segment_gs_refs']) >= 20:
                            break

    result['total_findings'] = (
        len(result['suspicious_imports']) +
        min(len(result['int3_locations']), 50) +
        len(result['segment_fs_refs']) +
        len(result['segment_gs_refs'])
    )
    return result


def _extract_imports(pe_data, sections):
    imports = []
    try:
        if len(pe_data) < 64:
            return imports
        pe_off = struct.unpack_from('<I', pe_data, 0x3C)[0]
        if pe_off + 24 > len(pe_data):
            return imports
        opt_off = pe_off + 24
        magic = struct.unpack_from('<H', pe_data, opt_off)[0]
        is64 = (magic == 0x20B)
        if is64:
            imp_rva_off = opt_off + 120
        else:
            imp_rva_off = opt_off + 104
        if imp_rva_off + 8 > len(pe_data):
            return imports
        imp_rva = struct.unpack_from('<I', pe_data, imp_rva_off)[0]
        imp_size = struct.unpack_from('<I', pe_data, imp_rva_off + 4)[0]
        if imp_rva == 0:
            return imports

        desc_off = _rva_to_raw_imp(imp_rva, sections)
        if desc_off is None:
            return imports
        for i in range(256):
            d = desc_off + i * 20
            if d + 20 > len(pe_data):
                break
            ilt_rva = struct.unpack_from('<I', pe_data, d)[0]
            name_rva = struct.unpack_from('<I', pe_data, d + 12)[0]
            if name_rva == 0 and ilt_rva == 0:
                break
            name_raw = _rva_to_raw_imp(name_rva, sections)
            if name_raw is None:
                continue
            name_end = pe_data.index(b'\x00', name_raw)
            ilt_raw = _rva_to_raw_imp(ilt_rva, sections)
            if ilt_raw is None:
                continue
            entry_sz = 8 if is64 else 4
            ordinal_flag = 0x8000000000000000 if is64 else 0x80000000
            for j in range(1024):
                e = ilt_raw + j * entry_sz
                if e + entry_sz > len(pe_data):
                    break
                if is64:
                    val = struct.unpack_from('<Q', pe_data, e)[0]
                else:
                    val = struct.unpack_from('<I', pe_data, e)[0]
                if val == 0:
                    break
                if val & ordinal_flag:
                    continue
                hint_rva = val & 0x7FFFFFFF
                hint_raw = _rva_to_raw_imp(hint_rva, sections)
                if hint_raw is None or hint_raw + 2 >= len(pe_data):
                    continue
                name_start = hint_raw + 2
                name_end2 = pe_data.index(b'\x00', name_start)
                api_name = pe_data[name_start:name_end2].decode('ascii', errors='replace')
                if api_name:
                    imports.append(api_name)
    except Exception:
        pass
    return imports


def _rva_to_raw_imp(rva, sections):
    for s in sections:
        va = s['virtual_address']
        vsz = max(s.get('virtual_size', 0), s.get('raw_size', 0))
        if va <= rva < va + vsz:
            return s['raw_offset'] + (rva - va)
    return None



class VMAnalyzer:

    def __init__(self, session):
        self.session = session

    def discover(self):
        sess = self.session
        if not sess._trace:
            return "[Error] err: no trace data. Run first, then vm(discover)"

        trace = list(sess._trace)
        if len(trace) < 100:
            return f"[Error] err: trace too short ({len(trace)} insn). Run longer."

        counts = Counter(trace)
        transitions = defaultdict(list)
        for i in range(len(trace) - 1):
            transitions[trace[i]].append(trace[i + 1])

        fanout_data = {}
        for addr, cnt in counts.most_common(500):
            if cnt < 10:
                break
            nexts = transitions[addr]
            unique = len(set(nexts))
            fanout_data[addr] = {
                'total': cnt,
                'unique': unique,
                'ratio': unique / cnt if cnt > 0 else 0,
            }

        candidates = sorted(fanout_data.items(), key=lambda x: x[1]['unique'], reverse=True)

        dispatch_rva = None
        dispatch_type = None
        dispatch_count = 0
        min_fanout = 20

        for addr, info in candidates[:20]:
            if info['unique'] < min_fanout:
                break
            try:
                code = bytes(sess.uc.mem_read(sess.image.base + addr, 16))
                from capstone import Cs, CS_ARCH_X86, CS_MODE_64, CS_MODE_32
                md = Cs(CS_ARCH_X86, CS_MODE_64 if sess.image.is64 else CS_MODE_32)
                for ins in md.disasm(code, addr):
                    mn = ins.mnemonic
                    if mn == 'ret':
                        dispatch_rva = addr
                        dispatch_type = 'ret'
                        break
                    elif mn == 'jmp' and not ins.op_str.startswith('0x'):
                        dispatch_rva = addr
                        dispatch_type = 'jmp_indirect'
                        break
                    elif mn == 'jmp':
                        break
                    continue
            except Exception:
                pass
            if dispatch_rva is not None:
                break

        if dispatch_rva is None:
            for addr, info in candidates[:5]:
                if info['unique'] >= min_fanout:
                    dispatch_rva = addr
                    dispatch_type = 'high_fanout'
                    break

        if dispatch_rva is None:
            top5 = [(f"0x{a:x}(fan={info['unique']},cnt={info['total']})")
                    for a, info in candidates[:5]]
            return f"No dispatch point found. Top candidates: {', '.join(top5)}"

        dispatch_count = counts[dispatch_rva]

        hits = [i for i, a in enumerate(trace) if a == dispatch_rva]
        handler_entries = Counter()
        for i in hits:
            if i + 1 < len(trace):
                handler_entries[trace[i + 1]] += 1

        boundaries = []
        handler_codes = set()
        for i in range(len(hits) - 1):
            start = hits[i] + 1
            end = hits[i + 1]
            if start < end:
                boundaries.append((trace[start], trace[end - 1]))
                for j in range(start, end):
                    handler_codes.add(trace[j])

        trampoline = []
        if hits:
            first_hit = hits[0]
            for offset in range(-5, 0):
                idx = first_hit + offset
                if 0 <= idx < len(trace):
                    trampoline.insert(0, trace[idx])

        sess.vm_ctx = {
            'dispatch_rva': dispatch_rva,
            'dispatch_type': dispatch_type,
            'dispatch_count': dispatch_count,
            'handler_entries': handler_entries.most_common(50),
            'handler_boundaries': boundaries[:200],
            'handler_code_addrs': len(handler_codes),
            'trampoline': trampoline,
            'total_trace_len': len(trace),
        }

        lines = [
            f"=== VM Discovery ===",
            f"  Dispatch: RVA=0x{dispatch_rva:x} ({dispatch_type}, x{dispatch_count})",
            f"  Handler entries: {len(handler_entries)} unique targets",
            f"  Handler code addrs: {len(handler_codes)} unique",
            f"  Handler intervals: {len(boundaries)}",
        ]
        if trampoline:
            lines.append(f"  Trampoline: {' -> '.join(f'0x{a:x}' for a in trampoline)}")
        lines.append(f"  Top 5 handler entries:")
        for h, cnt in handler_entries.most_common(5):
            lines.append(f"    0x{h:x} (x{cnt})")
        return "\n".join(lines)

    def decode(self, args):
        sess = self.session
        chain_spec = _arg_json(args, "chain")
        if not chain_spec:
            return "[Error] err: decode needs chain=[{op:..},...]"

        stream_rva = int(_arg_str(args, "stream_rva", "0"), 0)
        initial_key = int(_arg_str(args, "initial_key", "0"), 0)
        num = int(_arg_str(args, "num", "64"))
        dt_rva = int(_arg_str(args, "dispatch_table_rva", "0"), 0)
        sec_raw = int(_arg_str(args, "section_raw", "0"), 0)
        sec_rva = int(_arg_str(args, "section_rva", "0"), 0)
        key_update = _arg_str(args, "key_update", "xor_plain")

        chain = DecryptChain.from_json(chain_spec)

        if not sess._raw:
            return "[Error] err: no PE data loaded"

        sections = _sess_sections(sess)

        if dt_rva == 0:
            dt_rva = sess.vm_ctx.get('dispatch_table_rva', 0)
            if dt_rva == 0:
                return "[Error] err: dispatch_table_rva unknown. Run discover first or provide it."

        decoder = BytecodeDecoder(
            pe_data=sess._raw,
            section_raw_offset=sec_raw,
            section_rva_start=sec_rva,
            dispatch_table_rva=dt_rva,
            dispatch_table_size=256,
            decrypt_chain=chain,
            key_update=key_update,
        )
        results = decoder.run(stream_rva, initial_key, num=num)

        lines = [f"=== Bytecode Decode ({len(results)} instructions) ==="]
        for r in results[:32]:
            lines.append(
                f"  #{r['seq']:02d} rva=0x{r['rva']:x} c=0x{r['cipher']:02x} "
                f"op=0x{r['opcode']:02x} h=0x{r['handler']:x} "
                f"k={r['key_before']:02x}->{r['key_after']:02x}"
            )
        if len(results) > 32:
            lines.append(f"  ... ({len(results) - 32} more)")
        return "\n".join(lines)

    def sym(self, handler_addr):
        return "[Info] sym: use run(isolate=0x%X) for handler isolation" % handler_addr

    def measure_handlers(self, handler_addrs, setup_regs=None):
        sess = self.session
        if sess.uc is None:
            return "[Error] err: no loaded session"

        from unicorn import x86_const
        reg_map = {
            'rax': x86_const.UC_X86_REG_RAX, 'rbx': x86_const.UC_X86_REG_RBX,
            'rcx': x86_const.UC_X86_REG_RCX, 'rdx': x86_const.UC_X86_REG_RDX,
            'rsi': x86_const.UC_X86_REG_RSI, 'rdi': x86_const.UC_X86_REG_RDI,
            'rbp': x86_const.UC_X86_REG_RBP, 'rsp': x86_const.UC_X86_REG_RSP,
            'r8': x86_const.UC_X86_REG_R8, 'r9': x86_const.UC_X86_REG_R9,
            'r10': x86_const.UC_X86_REG_R10, 'r11': x86_const.UC_X86_REG_R11,
            'r12': x86_const.UC_X86_REG_R12, 'r13': x86_const.UC_X86_REG_R13,
            'r14': x86_const.UC_X86_REG_R14, 'r15': x86_const.UC_X86_REG_R15,
            'rip': x86_const.UC_X86_REG_RIP if sess.image.is64 else x86_const.UC_X86_REG_EIP,
        }

        deltas = {}
        steps_per_handler = 500

        for h_rva in handler_addrs:
            snap = sess._snapshot_now()
            regs_before = {}
            for name, reg_id in reg_map.items():
                try:
                    regs_before[name] = sess.uc.reg_read(reg_id)
                except Exception:
                    pass

            rip_reg = reg_map['rip']
            sess.uc.reg_write(rip_reg, sess.image.base + h_rva)

            if setup_regs:
                for name, val in setup_regs.items():
                    r = reg_map.get(name.lower())
                    if r:
                        v = int(val, 16) if isinstance(val, str) and val.startswith('0x') else int(val)
                        try:
                            sess.uc.reg_write(r, v)
                        except Exception:
                            pass

            try:
                sess.uc.emu_start(sess.image.base + h_rva, 0, count=steps_per_handler)
            except Exception:
                pass

            regs_after = {}
            for name, reg_id in reg_map.items():
                try:
                    regs_after[name] = sess.uc.reg_read(reg_id)
                except Exception:
                    pass

            delta = {}
            for name in regs_before:
                if name in regs_after and name != 'rip':
                    d = regs_after[name] - regs_before[name]
                    if d != 0:
                        delta[name] = d

            deltas[h_rva] = {
                'regs_before': regs_before,
                'regs_after': regs_after,
                'delta': delta,
            }

            sess._restore_now(snap)

        sess.vm_ctx['handler_deltas'] = deltas

        lines = [f"=== Handler Effect Measurement ({len(handler_addrs)} handlers) ==="]
        for h_rva in handler_addrs:
            d = deltas[h_rva]['delta']
            if d:
                delta_str = ', '.join(f"{k}={v:+d}" for k, v in d.items())
                lines.append(f"  handler 0x{h_rva:x}: {delta_str}")
            else:
                lines.append(f"  handler 0x{h_rva:x}: (no change)")
        return "\n".join(lines)

    def derive_cursor_delta(self, vm_sp_reg='rbx'):
        sess = self.session
        handler_deltas = sess.vm_ctx.get('handler_deltas', {})
        if not handler_deltas:
            return "[Error] err: no handler_deltas. Run measure_handlers first."

        delta_groups = defaultdict(list)
        for h_rva, info in handler_deltas.items():
            sp_delta = info['delta'].get(vm_sp_reg, 0)
            delta_groups[sp_delta].append(h_rva)

        cursor_delta = {}
        lines = [f"=== Cursor Delta Derivation (vm_sp={vm_sp_reg}) ==="]

        for sp_delta, handlers in sorted(delta_groups.items()):
            if sp_delta == 0:
                for h in handlers:
                    cursor_delta[h] = 0
                lines.append(f"  delta=0 (JMP/LOOP): {len(handlers)} handlers")
            elif sp_delta > 0:
                cursor_advance = sp_delta // 8 if sp_delta >= 8 else 1
                for h in handlers:
                    cursor_delta[h] = cursor_advance
                lines.append(f"  delta=+{sp_delta} (POP): {len(handlers)} handlers -> cursor +{cursor_advance}")
            else:
                cursor_advance = (-sp_delta) // 8 if -sp_delta >= 8 else 1
                for h in handlers:
                    cursor_delta[h] = cursor_advance
                lines.append(f"  delta={sp_delta} (PUSH): {len(handlers)} handlers -> cursor +{cursor_advance}")

        sess.vm_ctx['cursor_delta'] = cursor_delta
        lines.append(f"\n  Total: {len(cursor_delta)} handlers mapped")
        return "\n".join(lines)

    def lift(self, args):
        sess = self.session
        traces = _arg_json(args, "traces") if args else None
        input_bytes = _arg_json(args, "input_bytes") if args else None

        if not traces or not input_bytes:
            return ("[Info] lift: requires traces and input_bytes.\n"
                    "Usage: Collect multi-input traces via run(capture=\"vm\") with different inputs,\n"
                    "then call vm(mode=\"lift\", traces={...}, input_bytes={...}).\n"
                    "OperationFitter will infer CONST/COPY/ADD/XOR/SUB/SHL/ROL/SELECT operations.")

        fitter = OperationFitter()
        ops, fitted = fitter.fit_all(traces, input_bytes)

        lines = [f"=== Lifted Program ({len(ops)} steps, {fitted} fitted) ==="]
        for op in ops[:50]:
            op_tuple = op['op']
            op_name = op_tuple[0] if op_tuple else 'UNKNOWN'
            dst = f"0x{op['dst']:x}" if op['dst'] else "?"
            lines.append(f"  step {op['i']:3d}: {op_name:12s} dst={dst} size={op['size']}")
        if len(ops) > 50:
            lines.append(f"  ... ({len(ops) - 50} more)")

        sess.vm_ctx['lifted_ops'] = ops
        return "\n".join(lines)

    def solve(self, args):
        sess = self.session
        lifted = sess.vm_ctx.get('lifted_ops')
        if not lifted:
            return ("[Info] solve: no lifted program. Run vm(mode=\"lift\") first,\n"
                    "or provide constraints directly via vm(mode=\"solve\", constraints=[...]).")

        try:
            import z3
        except ImportError:
            return ("[Info] solve: z3-solver not installed. Install via: pip install z3-solver\n"
                    "Alternative: export lifted program and solve externally.")

        return ("[Info] solve: z3 integration not yet wired.\n"
                f"Lifted program has {len(lifted)} operations. Export via vm_ctx['lifted_ops']\n"
                "and solve externally, or wait for full z3 integration in Step 6.")



def _arg_json(args, key):
    val = args.get(key) if isinstance(args, dict) else None
    if val is None:
        return None
    if isinstance(val, (list, dict)):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def _arg_str(args, key, default=""):
    if isinstance(args, dict):
        return str(args.get(key, default))
    return default


def _sess_sections(sess):
    sections = []
    if hasattr(sess, 'image') and hasattr(sess.image, 'sections'):
        for s in sess.image.sections:
            sections.append({
                'name': s.get('name', ''),
                'virtual_address': s.get('virtual_address', 0),
                'virtual_size': s.get('virtual_size', 0),
                'raw_size': s.get('raw_size', 0),
                'raw_offset': s.get('raw_offset', 0),
                'characteristics': s.get('characteristics', 0),
            })
    return sections
