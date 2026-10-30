# 正式前端回归记录

验证日期：2026-10-07，Asia/Shanghai。此记录对应正式 `web/`，原型目录保持不变。

## 最终结果

| 检查 | 结果 |
| --- | --- |
| `npm run test:e2e` | 9 passed，0 failed / skipped / flaky；97.05秒，含生产服务启动 |
| 正式 `next build` | 通过；由 E2E 服务启动流程执行 |
| `npm run typecheck` | 通过，最终回归后复核 |
| `npm run lint` | 通过 |
| `npm audit` | 0 vulnerabilities |
| 五宽截图 | 75张：九个后台页面与六个官网/PWA页面，各390、768、1024、1440、1920px |

完整报告在 `tests/results/e2e.json`，截图在 `tests/results/screenshots`。最终JSON报告保留为验证证据，截图、失败调试产物和trace被Git忽略，可用下列命令重新生成：

```powershell
npm run test:e2e
npm run typecheck
npm run lint
npm audit
```

回归配置使用一名浏览器 worker、独立端口 `3011` / `8011`。它先构建 Next，再运行生产模式 `next start`；构建和运行均设置 `API_BASE_URL=http://127.0.0.1:8011`。此命令会覆盖本地 `.next`，常规正式运行前需按部署 API 地址重新构建。Next 对 `output: standalone` 提示应直接运行 standalone server；本记录验证生产浏览器行为，Docker 的 standalone 启动与版本控制由部署专项另行验证。

## 已覆盖行为

1. 未登录后台拒绝、错误主密码、真实登录和退出。
2. Markdown自动保存、发布快照与草稿隔离、立即私密过滤、脚本与危险链接清理。
3. 保存失败保留输入、重试、恢复历史只更新草稿。
4. 每批12条/每组48条、下一组与返回URL、旧日期链接、收藏及撤销清空。
5. 真实HTTP SSE、成功后首次免费失效、共享次数只结算一次、密码错误时保留问题。
6. PWA离线速记、离线刷新、恢复网络后的确认上传，以及设备凭证无法读取owner数据。
7. 九模块与官网/PWA五宽无水平溢出、减少动态效果、后台抽屉关闭后焦点恢复、首页菜单初始隐藏与Canvas卸载。
8. 后台设置联动首页介绍/标题、附件发布前拒绝及发布后访问、引用对象禁止删除、模型停用状态。
9. SSE完成事件损坏后查询已结算答案；验证密码后续发原问题。

抽查了390px首页、390px内容后台、390px采集台、768px日记、1024px模型后台、1440px总览截图，暖纸/墨色/朱橙排版保持，页面内容与控件可见。

## 回归中处理的问题

Service Worker保留v2在线优先策略和旧缓存清理。开发HMR的无hash运行时会发生离线安装等待与页面未水合，最终验收使用正式构建资源；生产模式PWA与连续五宽导航均通过，没有删除离线刷新断言。模型停用后同时存在成功提示与加载提示，测试改为定位具体成功提示，并继续检查持久停用状态。

开发模式失败记录保存在忽略目录 `tests/results/e2e-dev-before.json`，首轮生产模式定位器失败记录在 `tests/results/e2e-production-before.json`。最终报告是上述9项全部通过的生产模式运行。

## 真实服务与fixture边界

身份、CSRF、SQLite、内容版本、权限、额度、设备API、SSE终态与结算均经过真实FastAPI；数据库与文件放在独立临时目录。`tests/api_fixture.py`明确将对象客户端替换为本地文件fixture，将模型SDK的HTTP传输替换为供应商fixture。生产启动不加载这些fixture，也不导入回归口令或样例内容。

本结果不代表真实MinIO、外部模型供应商、iOS真机、Windows/Wayland采集端或阿里云2核2G VPS已验收。容量门槛仍由部署验收控制，浏览器回归未绕过AI预览或发布门槛。

## 2026-10-26 导航专项

新增`navigation.spec.ts`：真实后台表单新增/编辑/排序、首页直接入口、隐藏后的公开API过滤、改为菜单球后初始隐藏、删除及finally清理，1 passed/0 failed/0 skipped/0 flaky，7.85秒。最终使用本轮成功构建的Linux API/Web镜像与独立临时SQLite，沿用Playwright1.63.0；不使用生产数据或凭证，不注入吞噬事件。三张截图在忽略目录`tests/results/screenshots/navigation-*.png`，报告`tests/results/navigation-e2e.json`。

初次按原启动配置重复Windows Next构建遇到本机内存不足，报告保留为`navigation-windows-build-before.json`；正式Linux构建已通过，最终直接启动该产物验证。首轮专项因帮助文字属于表单可访问名称而定位失败，修正测试的label匹配后通过，未修改产品表单或放宽业务断言。原9项历史报告`e2e.json`已保留原件；本轮没有声称重跑全部9项。TypeScript与ESLint通过。
