
import os
import time
import struct

from .crypto_constants import CRYPTO_CONSTANTS_DWORD, CRYPTO_CONSTANTS_WORD, CRYPTO_SBOXES

TIMEOUT_SECONDS = 300

def _validate_and_read(file_path: str) -> tuple:
    if not os.path.isfile(file_path):
        return None, f"[ERROR] extract_constants: file not found: {file_path}"
    with open(file_path, 'rb') as f:
        data = f.read()
    if not data:
        return None, f"[ERROR] extract_constants: file is empty: {file_path}"
    return data, None

def _scan_dwords(data: bytes, start_time: float) -> tuple:
    dword_consts = {}
    data_len = len(data)
    for offset in range(data_len - 3):
        val = struct.unpack_from('<I', data, offset)[0]
        if val == 0 or val < 0x100:
            continue
        dword_consts.setdefault(val, []).append(offset)
        if (offset & 0xFFFF) == 0:
            if time.time() - start_time > TIMEOUT_SECONDS:
                return dword_consts, True
    return dword_consts, False

def _scan_words(data: bytes, start_time: float) -> tuple:
    word_consts = {}
    data_len = len(data)
    for offset in range(data_len - 1):
        val = struct.unpack_from('<H', data, offset)[0]
        if val == 0 or val < 0x10:
            continue
        word_consts.setdefault(val, []).append(offset)
        if (offset & 0xFFFF) == 0:
            if time.time() - start_time > TIMEOUT_SECONDS:
                return word_consts, True
    return word_consts, False

def _match_dword_constants(dword_consts: dict) -> list:
    matches = []
    for const_val, offsets in dword_consts.items():
        if const_val in CRYPTO_CONSTANTS_DWORD:
            name = CRYPTO_CONSTANTS_DWORD[const_val]
            for off in offsets:
                matches.append((off, const_val, name, 'DWORD'))
    return matches

def _match_word_constants(word_consts: dict) -> list:
    matches = []
    for const_val, offsets in word_consts.items():
        if const_val in CRYPTO_CONSTANTS_WORD:
            name = CRYPTO_CONSTANTS_WORD[const_val]
            for off in offsets:
                matches.append((off, const_val, name, 'WORD'))
    return matches

def _match_sboxes(data: bytes, start_time: float) -> tuple:
    sbox_matches = []
    for sbox_name, sbox_bytes in CRYPTO_SBOXES.items():
        if time.time() - start_time > TIMEOUT_SECONDS:
            return sbox_matches, True
        pos = 0
        while True:
            idx = data.find(sbox_bytes, pos)
            if idx == -1:
                break
            sbox_matches.append((idx, sbox_name, len(sbox_bytes)))
            pos = idx + 1
    return sbox_matches, False

def _format_value(val: int, typ: str) -> str:
    if typ == 'WORD':
        return f"0x{val:04X}"
    return f"0x{val:08X}"

def _format_matches_table(matches: list) -> list:
    lines = []
    lines.append(f"{'Offset':<12} | {'Value':<12} | {'Type':<6} | "
                 f"Crypto Constant")
    lines.append("-" * 12 + "-+-" + "-" * 12 + "-+-" + "-" * 6
                 + "-+-" + "-" * 40)
    for off, val, name, typ in sorted(matches):
        val_str = _format_value(val, typ)
        lines.append(f"0x{off:08X}   | {val_str:<12} | {typ:<6} | "
                     f"{name}")
    lines.append("")
    return lines

def _format_sbox_table(sbox_matches: list) -> list:
    lines = []
    lines.append(f"{'Offset':<12} | {'S-box Name':<35} | Size")
    lines.append("-" * 12 + "-+-" + "-" * 35 + "-+-" + "-" * 6)
    for off, name, size in sorted(sbox_matches):
        lines.append(f"0x{off:08X}   | {name:<35} | {size} bytes")
    lines.append("")
    return lines

def _format_top20(dword_consts: dict) -> list:
    lines = []
    lines.append("")
    lines.append("// Top-20 most frequent DWORD constants:")
    lines.append(f"{'Value':<12} | {'Count':<7} | First Offset")
    lines.append("-" * 12 + "-+-" + "-" * 7 + "-+-" + "-" * 12)
    freq_sorted = sorted(dword_consts.items(),
                         key=lambda x: len(x[1]), reverse=True)
    for val, offsets in freq_sorted[:20]:
        lines.append(f"0x{val:08X}   | {len(offsets):<7} | "
                     f"0x{offsets[0]:08X}")
    return lines

def _format_output(file_path: str, data: bytes, dword_consts: dict,
                   word_consts: dict, matches: list, sbox_matches: list,
                   timed_out: bool, start_time: float) -> str:
    lines = []
    lines.append(f"// Constants extracted from: {file_path}")
    lines.append(f"// File size: {len(data)} bytes")
    lines.append(f"// Unique DWORDs: {len(dword_consts)}, "
                 f"Unique WORDs: {len(word_consts)}")
    lines.append(f"// Crypto constant matches: {len(matches)}")
    lines.append(f"// S-box matches: {len(sbox_matches)}")
    if timed_out:
        elapsed = time.time() - start_time
        lines.append(
            f"// [TIMEOUT] Extraction timed out after {elapsed:.1f}s. "
            f"Results below are partial."
        )
    lines.append("")

    if matches:
        lines.extend(_format_matches_table(matches))

    if sbox_matches:
        lines.extend(_format_sbox_table(sbox_matches))

    if not matches and not sbox_matches:
        lines.append("// No known cryptographic constants found.")

    if dword_consts:
        lines.extend(_format_top20(dword_consts))

    return "\n".join(lines)

def _run_scan_and_match(data: bytes, start_time: float) -> tuple:
    dword_consts, timed_out = _scan_dwords(data, start_time)
    word_consts = {}
    if not timed_out:
        word_consts, timed_out = _scan_words(data, start_time)
    matches = _match_dword_constants(dword_consts)
    matches.extend(_match_word_constants(word_consts))
    sbox_matches, sbox_timed = _match_sboxes(data, start_time)
    if sbox_timed:
        timed_out = True
    return dword_consts, word_consts, matches, sbox_matches, timed_out

def extract_constants(*args: str) -> str:
    try:
        if len(args) < 1:
            return (f"[ERROR] extract_constants: expected 1 argument "
                    f"(file_path), got {len(args)}")

        file_path = args[0]
        data, err = _validate_and_read(file_path)
        if err:
            return err

        start_time = time.time()
        dword_consts, word_consts, matches, sbox_matches, timed_out = \
            _run_scan_and_match(data, start_time)

        return _format_output(file_path, data, dword_consts, word_consts,
                              matches, sbox_matches, timed_out, start_time)

    except Exception as e:
        return f"[ERROR] extract_constants: {type(e).__name__}: {e}"
