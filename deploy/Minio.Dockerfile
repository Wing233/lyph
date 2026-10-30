FROM golang:1.26-alpine@sha256:8ac98ca534ac3f51e1f420a1dd2c15e74c75cfa0f23f3ad27eb5d7236c349a0c AS builder
RUN apk add --no-cache git ca-certificates
ARG MINIO_COMMIT=7aac2a2c5b7c882e68c1ce017d8256be2feea27f
WORKDIR /src
RUN git init && git remote add origin https://github.com/minio/minio.git && git fetch --depth=1 origin ${MINIO_COMMIT} && git checkout --detach FETCH_HEAD && test "$(git rev-parse HEAD)" = "${MINIO_COMMIT}"
RUN CGO_ENABLED=0 go build -trimpath -o /minio .
FROM alpine:3.22@sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8
RUN apk add --no-cache ca-certificates curl && addgroup -g 10001 minio && adduser -D -u 10001 -G minio minio && mkdir /data && chown minio:minio /data
COPY --from=builder /minio /usr/local/bin/minio
COPY --from=builder /src/LICENSE /usr/share/licenses/minio/LICENSE
USER 10001:10001
VOLUME /data
EXPOSE 9000 9001
ENTRYPOINT ["minio"]
CMD ["server", "/data", "--console-address", ":9001"]
