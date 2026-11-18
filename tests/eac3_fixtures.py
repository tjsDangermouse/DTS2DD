"""Synthetic E-AC-3 JOC *signalling* fixtures for the tests.

``add_joc_signalling`` takes real E-AC-3 syncframes (encoded by FFmpeg) and
inserts additional bit stream information with flag_ec3_extension_type_a = 1
and a complexity index at the position atmos_inspector finds the addbsie bit.
The frame size and CRC are updated so the stream stays valid.

The result is NOT Atmos audio: there is no JOC/object payload. It only
signals JOC, which is exactly what FFmpeg, MediaInfo and the scanner read.
Because FFmpeg's own decoder then reports the "Dolby Digital Plus + Dolby
Atmos" profile, these fixtures also cross-check that the scanner's parser
finds the addbsi field at the same bit position as FFmpeg.
"""
import helpers  # noqa: F401  (sets sys.path)

from atmos_inspector import iter_ac3_frames, parse_ac3_frame


def crc16_ac3(data: bytes) -> int:
    """CRC-16 with polynomial 0x8005, initial 0, MSB first (AC-3 crc2)."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x8005) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def _bits(data: bytes) -> str:
    return "".join(f"{b:08b}" for b in data)


def _bytes(bits: str) -> bytes:
    return int(bits, 2).to_bytes(len(bits) // 8, "big")


def add_joc_signalling(frame: bytes, complexity: int = 16) -> bytes:
    info = parse_ac3_frame(frame)
    if not info.is_eac3 or info.addbsi_present:
        raise ValueError("needs an E-AC-3 frame without addbsi")
    bits = _bits(frame)
    pos = info.addbsie_bit
    assert bits[pos] == "0"
    inserted = "1" + f"{1:06b}" + "0000000" + "1" + f"{complexity:08b}"   # addbsie, addbsil=1, 2 bytes
    body = bits[:pos] + inserted + bits[pos + 1:len(bits) - 17]
    new_len = len(bits) + 32                     # grow by two 16-bit words
    body = body.ljust(new_len - 17, "0") + "0"   # pad, crcrsv = 0
    words = new_len // 16
    # frmsiz (11 bits at bit 21) = words - 1
    body = body[:21] + f"{words - 1:011b}" + body[32:]
    data = _bytes(body + "0" * 16)
    crc = crc16_ac3(data[2:-2])
    return data[:-2] + crc.to_bytes(2, "big")


def add_joc_to_stream(raw: bytes, complexity: int = 16) -> bytes:
    out = []
    for _, frame in iter_ac3_frames(raw):
        info = parse_ac3_frame(frame)
        if info.is_eac3 and info.strmtyp != 1 and info.substreamid == 0:
            out.append(add_joc_signalling(frame, complexity))
        else:
            out.append(frame)
    return b"".join(out)
