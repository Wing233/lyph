FROM rust:1.100.0-alpine3.24 AS build
WORKDIR /app
RUN apk add --no-cache musl-dev
COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/app/target \
    cargo build --locked --release \
    && install -m 0755 target/release/lyph-ofm-admin-proxy /usr/local/bin/

FROM alpine:3.24
COPY --from=build /usr/local/bin/lyph-ofm-admin-proxy /usr/local/bin/
ENTRYPOINT ["/usr/local/bin/lyph-ofm-admin-proxy"]
