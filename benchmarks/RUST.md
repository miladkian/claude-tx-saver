# Rust benchmark notes

The Rust binary uses the same CDP3 envelope, SHA-256 chunk identities, and
Zstandard level 6 as the protocol baseline. This makes measurements directly
comparable with the existing reports in `benchmarks/results/`.

Build an optimized binary with `cargo build --release`, then run the existing
workload generator against a local client and relay. Record request body bytes,
wire bytes, p50/p95 latency, and first SSE event latency. A valid comparison
must use the same workload seed and cache state for both implementations.

The deployed server was smoke-tested against the live HTTPS listener with an
authenticated CDP3 request; the upstream response status and opaque body were
preserved inside the framed response.
