# OFM 管理侧车

此 Rust 服务供已部署 Our Free Model（OFM）的环境选用。它和 OFM 容器共享网络命名空间，连接容器内的 `127.0.0.1:18900`，并在内网端口 `18905` 接收 LYPH Nginx 和本机健康探针的请求。

## 部署

OFM 容器需已启动，并能被 LYPH Nginx 从私网访问。将 `OFM_DATA_DIR` 设为宿主机上包含 OFM `settings.json` 的目录。若容器名不同，通过 `OFM_CONTAINER_NAME` 指定名称。

```bash
export OFM_DATA_DIR=/path/to/ofm-data
docker compose -f compose.yaml -f compose.ofm.yaml up -d --build --no-deps ofm-admin-proxy
docker compose -f compose.yaml -f compose.ofm.yaml exec ofm-admin-proxy \
  wget -qO- http://127.0.0.1:18905/healthz
```

使用 TLS 部署时，将 `-f compose.tls.yaml` 加入同一组 Compose 文件。侧车按 TCP 对端地址判断访问权限，Compose 将白名单设为 `LYPH_PROXY_IP` 和 `127.0.0.1`。已有管理入口须先完成站主认证再反向代理到侧车。Compose 配置将该端口留在私网。

侧车从只读挂载加载 `managementKey`，缺失时回退到 `forwardKey`。它在服务端注入会话 Cookie，必要时在首次 `401` 后重新登录并重试一次。请求体上限为 32 MiB，响应按流转发。

## 验证

从本目录运行。测试使用模拟 OFM 和临时凭据，需要空闲的回环端口 `18900`，并依赖 Rust、Node 22、Python 3 和 `rust-anything` 1.4.0。

```bash
cargo build --locked --release --manifest-path rust/app/Cargo.toml
cargo test --locked --manifest-path rust/app/Cargo.toml
python rust/app-cases/verify.py replay
python rust/app-cases/verify.py replay --binary rust/app/target/release/lyph-ofm-admin-proxy
python rust/app-cases/verify.py diff --binary rust/app/target/release/lyph-ofm-admin-proxy
```

Windows 下可执行文件路径需加 `.exe`。原 Node 实现作为回放基准，侧车运行镜像由 Alpine 和 Rust 可执行文件组成。验收记录为 27 条 HTTP 回放和 44 项补充差分，覆盖会话刷新、流式响应、请求体边界和流水线报文。分块请求的后续报文、HTTP 升级及其他边界不确定的请求由 Hyper 处理。
