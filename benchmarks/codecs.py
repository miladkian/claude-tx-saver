"""Compare codec CPU cost and wire size on the calibrated 100k context."""

import base64
import json
from pathlib import Path
import statistics
import sys
import time
import zlib

import brotli
import zstandard as zstd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy import chunks, digest
from wire import frame, pack
from workload import corpus, public_corpus, request_body


def main():
    root = Path(__file__).resolve().parent / "results"
    live = json.loads((root / "100k-live.json").read_text())
    context = public_corpus(live["context_reference_tokens"])
    original = request_body(context)
    old_hashes = {digest(part) for part in chunks(original)}
    results = []
    for case, body in [("cold", original), ("unchanged", original),
                       ("small_append", request_body(context, corpus(1000, seed=3000)))]:
        parts = {digest(part): part for part in chunks(body)}
        payload = {"version": 3, "method": "POST", "path": "/v1/messages", "headers": [],
                   "chunks": list(parts), "sha256": digest(body),
                   "data": {key: value for key, value in parts.items() if case == "cold" or key not in old_hashes}}

        def legacy():
            value = dict(payload)
            value["version"] = 2
            value["data"] = {key: base64.b64encode(part).decode() for key, part in payload["data"].items()}
            return zlib.compress(json.dumps(value, separators=(",", ":")).encode(), 3)

        codecs = [("legacy-json-base64-deflate-3", legacy)]
        for level in [1, 3, 6, 9]:
            codecs.append((f"binary-zstd-{level}", lambda level=level: pack(payload, level)))
        for quality in [4, 5, 6, 9]:
            codecs.append((f"binary-brotli-{quality}", lambda quality=quality: brotli.compress(frame(payload), quality=quality)))
        for name, encode in codecs:
            times = []
            for _ in range(32):
                started = time.perf_counter()
                encoded = encode()
                times.append((time.perf_counter() - started) * 1000)
            row = {"case": case, "codec": name, "bytes": len(encoded), "encode_ms_median": statistics.median(times)}
            results.append(row)
            print(json.dumps(row), flush=True)
    output = {"samples": results, "context_sha256": digest(context.encode()),
              "context_reference_tokens": live["context_reference_tokens"],
              "zstandard_version": zstd.__version__, "brotli_version": brotli.__version__,
              "notes": ["Includes framing/JSON work but excludes chunk scanning shared by all codecs.",
                        "Brotli measurements omit the four-byte magic that a wire implementation would need."]}
    (root / "codecs.json").write_text(json.dumps(output, indent=2) + "\n")


if __name__ == "__main__":
    main()
