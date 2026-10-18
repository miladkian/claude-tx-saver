# 100k-context proxy benchmark

Measured over the real client–VPS HTTPS connection (host address redacted in public artifacts). The calibrated public-code fixture produced **100,126 input-context tokens**, reported by Anthropic through Claude Code. It contains only public aiohttp source, not user project data.

The source text is 279,355 UTF-8 bytes; the controlled HTTP request is 290,674 bytes. Its cl100k reference count is 59,647; that reference tokenizer differs from Claude's tokenizer. Both tests use the same SHA-256-verified source fixture.

## Controlled real-network transport test

Each row has 12 measurements per mode. Mode order was shuffled within each trial. A temporary authenticated sidecar used the production proxy factories and an immediate local hash-checking upstream. Every reconstructed request matched its original SHA-256. TCP/TLS connections were warmed equally; cold refers to the chunk cache. No traffic shaping was applied.

Small-request RTT median: **157.3 ms**. Local chunk scanning alone: **48.5 ms** per request.

Upload sizes below include HTTP request lines and headers, but exclude TLS/TCP/IP framing and retransmissions. TTFB measures request submission to the first response body byte, including local processing and real network transfer, with no model inference. Response bodies are small and identical across modes.

| Scenario | Full upload | Compression only | Delta upload | Upload saved vs full | Full TTFB | Compressed TTFB | Delta TTFB |
|---|---:|---:|---:|---:|---:|---:|---:|
| First upload (cold chunk cache) | 284.14 KiB | 60.94 KiB | 62.24 KiB | 78.10% | 575.9 ms | 377.2 ms | 432.3 ms |
| Repeated identical request | 284.14 KiB | 60.94 KiB | 1.16 KiB | 99.59% | 293.0 ms | 211.2 ms | 230.9 ms |
| Small appended turn (~4 KiB) | 288.25 KiB | 61.61 KiB | 6.97 KiB | 97.58% | 316.3 ms | 220.4 ms | 241.5 ms |
| Large appended result (~40 KiB) | 324.39 KiB | 64.26 KiB | 9.83 KiB | 96.97% | 297.3 ms | 211.1 ms | 252.4 ms |
| Edit in middle (~4 KiB) | 284.17 KiB | 60.96 KiB | 12.78 KiB | 95.50% | 287.5 ms | 212.7 ms | 245.8 ms |
| Session metadata changed | 284.15 KiB | 60.95 KiB | 4.49 KiB | 98.42% | 290.9 ms | 218.5 ms | 219.1 ms |
| Relay cache lost; resynchronize | 284.14 KiB | 60.94 KiB | 63.39 KiB | 77.69% | 296.9 ms | 210.8 ms | 477.6 ms |

Compression-only means the entire original JSON body uses Zstandard level 6 for every request; it has no deduplication. This distinguishes compression savings from reuse savings. Changes of ~1k/~10k reference tokens use generated snippets, about 4/40 KiB, and are not claimed as exact Claude token increments.

Paired median TTFB improvements and bootstrap 95% intervals (positive means delta was faster):

| Scenario | vs uncompressed full | 95% interval | vs compression only | 95% interval |
|---|---:|---:|---:|---:|
| First upload (cold chunk cache) | +142.1 ms | [+102.3, +163.8] ms | -54.8 ms | [-65.1, -37.4] ms |
| Repeated identical request | +83.0 ms | [+49.2, +97.3] ms | -15.7 ms | [-38.3, +2.7] ms |
| Small appended turn (~4 KiB) | +72.7 ms | [+42.1, +110.4] ms | -17.9 ms | [-31.2, -6.8] ms |
| Large appended result (~40 KiB) | +43.9 ms | [+32.9, +59.1] ms | -38.2 ms | [-60.9, -28.9] ms |
| Edit in middle (~4 KiB) | +40.9 ms | [+30.4, +80.3] ms | -36.8 ms | [-48.2, -21.7] ms |
| Session metadata changed | +67.3 ms | [+58.0, +78.0] ms | -2.6 ms | [-18.6, +7.4] ms |
| Relay cache lost; resynchronize | -176.3 ms | [-193.4, -158.6] ms | -258.4 ms | [-286.7, -240.4] ms |

These intervals describe repeated samples on this connection, not all networks or future conditions. Percentile/p95 estimates in the raw JSON have only 12 observations each. Cache loss requires two relay requests: the miss is rejected before upstream delivery, then the full body is resynchronized.

## Actual Claude Code calls

Both modes used Haiku, the existing Pro OAuth login, the same VPS, the same prompt, and an instruction to answer only OK. Per-invocation CLI settings selected the route. Each accepted call was observed by its selected proxy; an unobserved route fails validation. All measured calls used a warmed Anthropic prompt cache. First-token time includes Claude Code startup/auth, upload, Anthropic prefill, and generation.

| Mode | Calls | Upload body median | First token median | First token range |
|---|---:|---:|---:|---:|
| full | 6 | 291.51 KiB | 1.753 s | 1.517–2.236 s |
| delta | 6 | 8.64 KiB | 1.878 s | 1.574–2.155 s |

Actual Claude Code body-upload reduction: **97.04%**. Observed median first-token improvement: **-125.1 ms** (bootstrap 95% interval [-369.4, +198.4] ms). A negative value means the delta calls were slower in this sample. These are small samples of a variable inference service; do not attribute a noisy difference entirely to transport.

The first live pilot calibrated token counts but failed route verification because Claude's saved environment settings overrode the process environment. Its timings were excluded and retained only in `100k-live-calibration.json`. Later pilot and confirmation calls explicitly set the base URL through CLI settings and positively verified the route.

## Compression choice

The deployed transport uses binary chunk records with **Zstandard level 6**, checksums, and declared decompressed-size validation before allocation. Chunk data is not base64 encoded. The following cold-envelope codec measurements used the same calibrated source fixture and 32 repetitions each; they exclude chunk scanning common to all codecs.

| Codec/framing | Encoded bytes | Median encoding time |
|---|---:|---:|
| legacy-json-base64-deflate-3 | 119,285 | 6.253 ms |
| binary-zstd-1 | 75,523 | 0.790 ms |
| binary-zstd-3 | 70,250 | 0.765 ms |
| binary-zstd-6 | 63,441 | 2.936 ms |
| binary-zstd-9 | 62,050 | 4.172 ms |
| binary-brotli-4 | 67,195 | 2.759 ms |
| binary-brotli-5 | 62,662 | 4.658 ms |
| binary-brotli-6 | 61,506 | 5.357 ms |
| binary-brotli-9 | 59,721 | 8.337 ms |

Level 6 trades a small ratio difference against Brotli for lower encoding CPU. Level 9 saved little additional data for extra CPU. Raw comparison data includes unchanged and appended-request cases as well.

## Additional synthetic stress fixture

A separate 100,000-reference-token generated fixture (412,152 request bytes) was tested with the same 12-trial matrix. It is more repetitive/compressible than actual source and is not provider-token calibrated. It used the previous Deflate/base64 transport. Its measurements are secondary and are not substituted for the current Zstandard public-code results above.

## Interpretation and limits

The benefit is largest for repeated large contexts over constrained upload links. Delta scanning still walks the entire request locally, and compression alone can beat it on latency when the full compressed request already fits comfortably in the available bandwidth. New sessions can modify metadata and boundary chunks, so the live CLI sends more than the minimal unchanged-request manifest. First requests and cache loss send a full compressed chunk set.

Anthropic still receives and accounts for the full context; this does not reduce model input tokens or server inference time. Response-token traffic was deliberately small, so reported bandwidth savings concern uploads, not an entire session with large generated outputs. Existing production proxy configuration was preserved and the temporary sidecar was removed after testing.

Raw data: [public-code transport](100k-public-code.json), [live calls](100k-live.json), [synthetic stress](100k-synthetic.json).
