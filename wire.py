"""Bounded binary chunk framing compressed with Zstandard."""

import json
import struct

import zstandard as zstd

MAGIC = b"CDP3"
LENGTH = struct.Struct("!I")
DEFAULT_LEVEL = 6
DEFAULT_MAX_WIRE = 128 * 1024 * 1024


def frame(payload):
    entries = list(payload.get("data", {}).items())
    metadata = dict(payload)
    metadata["data"] = [[key, len(value)] for key, value in entries]
    header = json.dumps(metadata, separators=(",", ":")).encode()
    return LENGTH.pack(len(header)) + header + b"".join(value for key, value in entries)


def pack(payload, level=DEFAULT_LEVEL):
    return MAGIC + zstd.ZstdCompressor(level=level, write_checksum=True).compress(frame(payload))


def unpack(data, max_wire=DEFAULT_MAX_WIRE):
    if not data.startswith(MAGIC):
        raise ValueError("unsupported wire format")
    compressed = data[len(MAGIC):]
    try:
        size = zstd.frame_content_size(compressed)
        if size in {zstd.CONTENTSIZE_UNKNOWN, zstd.CONTENTSIZE_ERROR} or size > max_wire:
            raise ValueError("invalid or oversized envelope")
        raw = zstd.ZstdDecompressor().decompress(compressed, max_output_size=max_wire, allow_extra_data=False)
    except zstd.ZstdError as error:
        raise ValueError("invalid compressed envelope") from error
    if len(raw) < LENGTH.size:
        raise ValueError("missing metadata length")
    header_size = LENGTH.unpack_from(raw)[0]
    cursor = LENGTH.size + header_size
    if cursor > len(raw):
        raise ValueError("truncated metadata")
    metadata = json.loads(raw[LENGTH.size:cursor])
    if not isinstance(metadata, dict) or not isinstance(metadata.get("data"), list):
        raise ValueError("invalid metadata")
    chunks = {}
    for entry in metadata["data"]:
        if not isinstance(entry, list) or len(entry) != 2:
            raise ValueError("invalid chunk descriptor")
        key, length = entry
        if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key) or key in chunks:
            raise ValueError("invalid chunk hash")
        if type(length) is not int or not 0 < length <= 32768 or cursor + length > len(raw):
            raise ValueError("invalid chunk length")
        chunks[key] = raw[cursor:cursor + length]
        cursor += length
    if cursor != len(raw):
        raise ValueError("trailing chunk bytes")
    metadata["data"] = chunks
    return metadata
