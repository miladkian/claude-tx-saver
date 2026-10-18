import hashlib
import json
import unittest

import zstandard as zstd

from wire import LENGTH, MAGIC, pack, unpack


class WireTests(unittest.TestCase):
    def raw(self, metadata, body=b""):
        header = json.dumps(metadata).encode()
        return MAGIC + zstd.ZstdCompressor(write_checksum=True).compress(LENGTH.pack(len(header)) + header + body)

    def test_round_trip_arbitrary_bytes_and_unicode_headers(self):
        value = bytes(range(256)) * 100
        key = hashlib.sha256(value).hexdigest()
        payload = {"version": 3, "headers": [["future-header", "a\udcff"]], "data": {key: value}}
        self.assertEqual(unpack(pack(payload)), payload)

    def test_empty_body_has_no_chunks(self):
        payload = {"version": 3, "data": {}}
        self.assertEqual(unpack(pack(payload)), payload)

    def test_corrupt_truncated_and_extra_compressed_data_rejected(self):
        encoded = pack({"version": 3, "data": {}})
        corrupt = bytearray(encoded)
        corrupt[-1] ^= 1
        for invalid in [b"garbage", encoded[:-1], encoded + b"extra", encoded + encoded, bytes(corrupt)]:
            with self.subTest(invalid=invalid[:8]):
                with self.assertRaises(ValueError):
                    unpack(invalid)

    def test_declared_size_is_bounded_before_decompression(self):
        encoded = self.raw({"data": []}, b"a" * 10000)
        with self.assertRaises(ValueError):
            unpack(encoded, max_wire=100)

    def test_frames_without_declared_size_rejected(self):
        encoded = MAGIC + zstd.ZstdCompressor(write_content_size=False).compress(b"test")
        with self.assertRaises(ValueError):
            unpack(encoded)

    def test_invalid_chunk_lengths_and_hashes_rejected(self):
        key = "a" * 64
        for entries in [[[key, -1]], [[key, True]], [[key, 32769]], [[key, 3]],
                        [[key, 1], [key, 1]], [["bad-hash", 1]]]:
            with self.subTest(entries=entries):
                with self.assertRaises(ValueError):
                    unpack(self.raw({"data": entries}, b"aa"))

    def test_trailing_binary_bytes_rejected(self):
        with self.assertRaises(ValueError):
            unpack(self.raw({"data": []}, b"extra"))


if __name__ == "__main__":
    unittest.main()
