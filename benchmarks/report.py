"""Build a readable report from captured measurements, without guessing results."""

import json
from pathlib import Path
import random
import statistics

ROOT = Path(__file__).resolve().parent / "results"
LABELS = {"cold": "First upload (cold chunk cache)", "unchanged": "Repeated identical request",
          "append_1k": "Small appended turn (~4 KiB)", "tool_10k": "Large appended result (~40 KiB)",
          "edit_1k": "Edit in middle (~4 KiB)", "metadata_churn": "Session metadata changed",
          "cache_loss": "Relay cache lost; resynchronize"}
ORDER = list(LABELS)


def ci_difference(left, right, paired=False):
    rng = random.Random(157)
    samples = []
    for _ in range(10000):
        if paired:
            indices = rng.choices(range(len(left)), k=len(left))
            samples.append(statistics.median(left[i] - right[i] for i in indices))
        else:
            samples.append(statistics.median(rng.choices(left, k=len(left))) -
                           statistics.median(rng.choices(right, k=len(right))))
    samples.sort()
    return samples[250], samples[9749]


def main():
    transport = json.loads((ROOT / "100k-public-code.json").read_text())
    live = json.loads((ROOT / "100k-live.json").read_text())
    synthetic = json.loads((ROOT / "100k-synthetic.json").read_text())
    codecs = json.loads((ROOT / "codecs.json").read_text())
    assert transport["transport_version"] == live["transport_version"] == 3, "transport versions do not match"
    assert transport["context_sha256"] == live["context_sha256"], "fixtures do not match"
    measured = [row for row in live["samples"] if row["phase"] == "measured"]
    actual = sorted({row["provider_context_tokens"] for row in measured})
    lines = ["# 100k-context proxy benchmark", "",
             "Measured over the real client–VPS HTTPS connection (host address redacted in public artifacts). "
             "The calibrated public-code fixture produced **" + ", ".join(f"{x:,}" for x in actual) +
             " input-context tokens**, reported by Anthropic through Claude Code. "
             "It contains only public aiohttp source, not user project data.", "",
             f"The source text is {transport['context_utf8_bytes']:,} UTF-8 bytes; the controlled HTTP request is "
             f"{transport['request_body_bytes']:,} bytes. Its cl100k reference count is {transport['context_reference_tokens']:,}; "
             "that reference tokenizer differs from Claude's tokenizer. Both tests use the same SHA-256-verified source fixture.", "",
             "## Controlled real-network transport test", "",
             f"Each row has {transport['repetitions']} measurements per mode. Mode order was shuffled within each trial. "
             "A temporary authenticated sidecar used the production proxy factories and an immediate local hash-checking upstream. "
             "Every reconstructed request matched its original SHA-256. TCP/TLS connections were warmed equally; "
             "cold refers to the chunk cache. No traffic shaping was applied.", "",
             f"Small-request RTT median: **{transport['ping_ms_median']:.1f} ms**. "
             f"Local chunk scanning alone: **{transport['chunk_scan_ms_median']:.1f} ms** per request.", "",
             "Upload sizes below include HTTP request lines and headers, but exclude TLS/TCP/IP framing and retransmissions. "
             "TTFB measures request submission to the first response body byte, including local processing and real network transfer, "
             "with no model inference. Response bodies are small and identical across modes.", "",
             "| Scenario | Full upload | Compression only | Delta upload | Upload saved vs full | Full TTFB | Compressed TTFB | Delta TTFB |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for case in ORDER:
        row = transport["summary"][case]
        a, b, c = (row[key] for key in ["full", "full_zstd", "delta"])
        lines.append(f"| {LABELS[case]} | {a['upload_http_bytes_median']/1024:.2f} KiB | "
                     f"{b['upload_http_bytes_median']/1024:.2f} KiB | {c['upload_http_bytes_median']/1024:.2f} KiB | "
                     f"{row['reduction']['upload_http_percent']:.2f}% | {a['ttfb_ms_median']:.1f} ms | "
                     f"{b['ttfb_ms_median']:.1f} ms | {c['ttfb_ms_median']:.1f} ms |")
    lines += ["", "Compression-only means the entire original JSON body uses Zstandard level 6 for every request; "
              "it has no deduplication. This distinguishes compression savings from reuse savings. "
              "Changes of ~1k/~10k reference tokens use generated snippets, about 4/40 KiB, "
              "and are not claimed as exact Claude token increments.", "",
              "Paired median TTFB improvements and bootstrap 95% intervals (positive means delta was faster):", "",
              "| Scenario | vs uncompressed full | 95% interval | vs compression only | 95% interval |",
              "|---|---:|---:|---:|---:|"]
    for case in ORDER:
        by_mode = {}
        for mode in ["full", "full_zstd", "delta"]:
            by_mode[mode] = [r["ttfb_ms"] for r in sorted(transport["samples"], key=lambda r: r["trial"])
                             if r["case"] == case and r["mode"] == mode]
        text = []
        for mode in ["full", "full_zstd"]:
            difference = statistics.median(a - b for a, b in zip(by_mode[mode], by_mode["delta"]))
            low, high = ci_difference(by_mode[mode], by_mode["delta"], paired=True)
            text += [f"{difference:+.1f} ms", f"[{low:+.1f}, {high:+.1f}] ms"]
        lines.append("| " + LABELS[case] + " | " + " | ".join(text) + " |")
    lines += ["", "These intervals describe repeated samples on this connection, not all networks or future conditions. "
              "Percentile/p95 estimates in the raw JSON have only 12 observations each. Cache loss requires two relay "
              "requests: the miss is rejected before upstream delivery, then the full body is resynchronized.", "",
              "## Actual Claude Code calls", "",
              "Both modes used Haiku, the existing Pro OAuth login, the same VPS, the same prompt, "
              "and an instruction to answer only OK. Per-invocation CLI settings selected the route. "
              "Each accepted call was observed by its selected proxy; an unobserved route fails validation. "
              "All measured calls used a warmed Anthropic prompt cache. "
              "First-token time includes Claude Code startup/auth, upload, Anthropic prefill, and generation.", "",
              "| Mode | Calls | Upload body median | First token median | First token range |",
              "|---|---:|---:|---:|---:|"]
    times = {}
    byte_sizes = {}
    for mode in ["full", "delta"]:
        rows = [r for r in measured if r["mode"] == mode]
        times[mode] = [r["first_token_ms"] for r in rows]
        byte_sizes[mode] = statistics.median(r["upload_body_bytes"] for r in rows)
        lines.append(f"| {mode} | {len(rows)} | {byte_sizes[mode]/1024:.2f} KiB | "
                     f"{statistics.median(times[mode])/1000:.3f} s | "
                     f"{min(times[mode])/1000:.3f}–{max(times[mode])/1000:.3f} s |")
    low, high = ci_difference(times["full"], times["delta"])
    difference = statistics.median(times["full"]) - statistics.median(times["delta"])
    lines += ["", f"Actual Claude Code body-upload reduction: **{100*(1-byte_sizes['delta']/byte_sizes['full']):.2f}%**. "
              f"Observed median first-token improvement: **{difference:+.1f} ms** "
              f"(bootstrap 95% interval [{low:+.1f}, {high:+.1f}] ms). "
              "A negative value means the delta calls were slower in this sample. "
              "These are small samples of a variable inference service; do not attribute a noisy difference entirely to transport.", "",
              "The first live pilot calibrated token counts but failed route verification because Claude's saved "
              "environment settings overrode the process environment. Its timings were excluded and retained only in "
              "`100k-live-calibration.json`. Later pilot and confirmation calls explicitly set the base URL through "
              "CLI settings and positively verified the route.", "",
              "## Compression choice", "",
              "The deployed transport uses binary chunk records with **Zstandard level 6**, checksums, "
              "and declared decompressed-size validation before allocation. Chunk data is not base64 encoded. "
              "The following cold-envelope codec measurements used the same calibrated source fixture and "
              "32 repetitions each; they exclude chunk scanning common to all codecs.", "",
              "| Codec/framing | Encoded bytes | Median encoding time |",
              "|---|---:|---:|"]
    for row in codecs["samples"]:
        if row["case"] == "cold":
            lines.append(f"| {row['codec']} | {row['bytes']:,} | {row['encode_ms_median']:.3f} ms |")
    lines += ["", "Level 6 trades a small ratio difference against Brotli for lower encoding CPU. "
              "Level 9 saved little additional data for extra CPU. "
              "Raw comparison data includes unchanged and appended-request cases as well.", "",
              "## Additional synthetic stress fixture", "",
              f"A separate {synthetic['context_reference_tokens']:,}-reference-token generated fixture "
              f"({synthetic['request_body_bytes']:,} request bytes) was tested with the same 12-trial matrix. "
              "It is more repetitive/compressible than actual source and is not provider-token calibrated. "
              "It used the previous Deflate/base64 transport. Its measurements are secondary and are not "
              "substituted for the current Zstandard public-code results above.", "",
              "## Interpretation and limits", "",
              "The benefit is largest for repeated large contexts over constrained upload links. "
              "Delta scanning still walks the entire request locally, and compression alone can beat it on latency "
              "when the full compressed request already fits comfortably in the available bandwidth. "
              "New sessions can modify metadata and boundary chunks, so the live CLI sends more than the minimal "
              "unchanged-request manifest. First requests and cache loss send a full compressed chunk set.", "",
              "Anthropic still receives and accounts for the full context; this does not reduce model input tokens "
              "or server inference time. Response-token traffic was deliberately small, so reported bandwidth savings "
              "concern uploads, not an entire session with large generated outputs. "
              "Existing production proxy configuration was preserved and the temporary sidecar was removed after testing.", "",
              "Raw data: [public-code transport](100k-public-code.json), [live calls](100k-live.json), "
              "[synthetic stress](100k-synthetic.json).", ""]
    (ROOT / "REPORT.md").write_text("\n".join(lines))
    print("Saved " + str(ROOT / "REPORT.md"))


if __name__ == "__main__":
    main()
