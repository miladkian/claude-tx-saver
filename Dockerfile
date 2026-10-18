FROM rust:latest AS build
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN cargo build --release

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/target/release/claude-tx-saver /usr/local/bin/claude-tx-saver
ENTRYPOINT ["/usr/local/bin/claude-tx-saver"]
CMD ["--help"]
