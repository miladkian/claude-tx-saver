# Contributing

This is a Rust-only project. Install Rust stable, then run:

```sh
cargo fmt --check
cargo test --all-targets
cargo build --release
docker build -t claude-tx-saver:test .
```

The relay preserves opaque request and response bodies, unknown endpoints and
headers, upstream status codes, duplicate headers, and protocol v3 framing.
Transport changes must be coordinated between client and server. Never replay a
request whose upstream delivery is uncertain.

Live benchmarks are opt-in and must not include credentials or captured user
contexts. Use the protocol notes in `benchmarks/RUST.md` for reproducible
measurements.
