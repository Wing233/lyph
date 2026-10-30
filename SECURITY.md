# Security Policy

## Supported Versions

| Version | Supported |
|---|---|
| 0.1.x | ✅ |

## Reporting a Vulnerability

请不要在公开 Issue 里贴密钥、Cookie、访客数据、服务器地址或可复现的攻击细节。

优先使用 GitHub 私密漏洞报告：

<https://github.com/Wing233/lyph/security/advisories>

如果无法使用 Security Advisories，请通过 GitHub 资料中的公开邮箱联系维护者，并在标题写 `[SECURITY]`。

## 响应

- 48 小时内确认
- 7 天内初步判断
- 修复后在 Security Advisory / Release Notes 致谢

## 范围

属于安全范围：

- 认证/授权绕过、CSRF、session 固定
- 路径穿越、任意文件读写、对象存储越权
- SQL 注入、命令注入、SSRF
- 模型密钥/设备 token/密码泄露
- 内容发布快照越权（草稿/私密内容泄露）
- Docker socket/UI 预览导致的宿主机逃逸
- 拒绝服务（可稳定复现）

通常不属于：

- 需要先拿到服务器 root/容器权限
- 部署者把 8080/API 直接暴露公网且未加 TLS/访问控制
- 部署者自己泄露 `.env`、`data/`、`backups/`
- 第三方模型/服务自身问题
- 模型幻觉、生成内容不准确

## 部署者检查

- [ ] `.env` 权限 600，未提交
- [ ] `LYPH_SECRET_KEY`、MinIO key、模型 key 都是随机值
- [ ] 生产 `LYPH_SECURE_COOKIES=true`
- [ ] `LYPH_BIND_ADDRESS=127.0.0.1`，外层 TLS 代理
- [ ] 不启用 `compose.ui.yaml`，除非你理解 Docker socket 风险
- [ ] 定期备份 `data/`、`runtime/`、MinIO
- [ ] 定期轮换模型 key、设备 token
- [ ] 不在公开仓库放真实访客数据/截图/日志
