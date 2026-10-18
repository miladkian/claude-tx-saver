# Benchmarks

The checked-in results are protocol-baseline measurements from the previous
implementation. The Rust implementation keeps the same CDP3 framing, chunk
identity, and Zstandard level, so those numbers remain useful for bandwidth
comparison.

Build and run the Rust benchmark target with:

```sh
cargo bench
```

For a production comparison, use identical request fixtures and cache state,
then record full request bytes, transport bytes, p50/p95 latency, and time to the
first SSE event. Do not include credentials, private prompts, or captured user
contexts in committed results. See [`RUST.md`](RUST.md) for the comparison
protocol.
