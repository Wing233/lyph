# Contributing

感谢你愿意改进 LYPH。

## 开发环境

- Python 3.13 + [uv](https://docs.astral.sh/uv/)
- Node.js 22+
- Rust 1.95（采集端）
- Docker Compose ≥ 2.24.4

```bash
git clone https://github.com/Wing233/lyph.git
cd lyph

# API
cd api
uv sync --frozen --extra test
uv run pytest

# Web
cd ../web
npm ci
npm run typecheck
npm run build

# Collectors
cd ..
cargo test --locked
```

## 本地配置

```bash
uv run --project api lyph init-config \
  --output .env \
  --origin http://127.0.0.1:8080 \
  --minio-endpoint localhost:9000
```

不要使用真实生产密码/Key；测试用临时值。

## 代码风格

- Python：4 空格，类型注解，异常信息面向用户
- TypeScript：严格模式，避免 `any`
- Rust：`cargo fmt` + `cargo clippy`
- 提交信息：`feat:` / `fix:` / `docs:` / `chore:` / `security:`

## 数据与隐私

- 不提交 `.env`、`data/`、`runtime/`、`backups/`、`public-apps/`
- 不提交真实访客 IP、聊天记录、截图、设备 token
- 测试 fixture 用 `example.test` / `example.com`
- 截图/日志先脱敏

## 新增内容/功能

- 公开内容必须走发布快照，不要直接暴露草稿
- 新 API 需要鉴权 + 输入校验 + 测试
- 涉及文件/模型/设备/网络时，在 PR 里写清威胁模型
- 数据库变更必须增量、可回滚

## PR

- 一个 PR 做一件事
- 说明“改了什么、为什么、怎么验证”
- CI 必须通过
- 不提交生成物/缓存

## 行为准则

见 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
