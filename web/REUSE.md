# 复用检查报告：正式官网、私人编辑台与移动 PWA

## 能力需求（L0 拆解）

- 保留现有暖纸视觉、菜单背景、独立内容页与收藏兼容，同时将正文、权限、额度和发布接入服务端。
- 使用成熟组件处理对话框焦点、Markdown 安全渲染、浏览器持久队列和真实浏览器回归。
- 保持 Next.js 15、React 19、Tailwind v4，不引入另一套 CMS、运行时或常驻前端服务。

## 候选清单（最多 5 个，按星数与相关度排序）

| # | 名称 | 链接 | 星数 | 最近维护 | License | 匹配度 |
|---|---|---|---|---|---|---|
|1|Next.js / React|[官方仓库](https://github.com/vercel/next.js)|动态，见仓库|固定注册表实际提供的15.5.27，包含2026年9月修补|MIT|高|
|2|Playwright|[官方仓库](https://github.com/microsoft/playwright)|动态，见仓库|固定1.63.0|Apache-2.0|高|
|3|Radix / shadcn 组件约定|[官方仓库](https://github.com/radix-ui/primitives)|动态，见仓库|固定Dialog 1.1.23|MIT|高|
|4|react-markdown + remark-gfm + rehype-sanitize|[官方仓库](https://github.com/remarkjs/react-markdown)|动态，见仓库|固定10.1.0 / 4.0.1 / 6.0.0|MIT|高|
|5|idb|[官方仓库](https://github.com/jakearchibald/idb)|动态，见仓库|固定8.0.3|ISC|高|

项目内部能力优先：原型 CSS、Playfair 字体、Canvas吞噬/覆盖/相机与菜单v3规则、收藏键和列表浏览规则均适配复用。字体许可同时保存在 `public/playfair-OFL.txt` 并随正式镜像分发，原型目录保持完整。正式工程没有整体 iframe 原型，也没有将原型业务状态作为权限。

## 深度裁决

### Next.js / React / Tailwind

直接组装已选框架。App Router 提供服务端正文与独立页面，交互部件按需加载。Next15补丁版本从npm注册表确认；锁文件固定传递依赖。Next传递PostCSS存在已公开的source-map文件读取漏洞，使用`overrides`固定8.5.28，并用类型检查和生产构建验证兼容。[官方安全通告](https://github.com/advisories/GHSA-fxqj-rqcc-2cmp)

### Radix 与 Markdown

Radix负责对话框键盘、焦点围栏、关闭后恢复；本项目按shadcn组件约定组装按钮/对话框，用原型设计变量覆盖视觉。无需fork上游。React Markdown禁用原始HTML，不装`rehype-raw`，渲染树再次清理；文本不通过`dangerouslySetInnerHTML`。

### IndexedDB / idb

使用idb组装队列和凭证持久化，避免重复实现Promise封装。应用层仅实现事件ID、确认状态、租约与设备隔离。浏览器缓存与设备权限分离；Service Worker只缓存采集台壳和构建资源，禁止缓存任何API/后台/私有媒体。静态资源在线优先、离线回退，避免开发无hash运行时或换版资源被旧缓存覆盖。选择小范围原生 Service Worker，而非加入完整 Workbox 构建插件，因为首版仅要求一个静态壳和前台续传，不需要后台同步框架。

### 测试与静态检查

Playwright访问真实FastAPI与独立SQLite；SDK fixture只用于测试供应商/对象接口，不存在于生产启动路径。ESLint 10.12 + typescript-eslint 8.71 + React Hooks 7.1.1运行真正的源码规则检查，兼容本工程TS5.9/React19。没有引入存在未修复braces传递漏洞的Next ESLint预设；保留官方成熟语言及Hook规则，无自研lint引擎。

## 裁决：组装积木，只写项目业务适配

框架、焦点管理、安全渲染、持久数据库封装和浏览器驱动均有成熟库；复用内部视觉与行为更贴合既定原型。无需fork，避免引入外部CMS与另一套产品设计。

## 动手清单

- [x] 保留原型视觉变量和本地字体，适配Canvas init/dispose。
- [x] 固定依赖和package-lock，真实接口取代演示存储。
- [x] 将权限和额度留在服务端，将PWA队列留在本机。
- [x] 类型检查、源码lint与正式构建；相关浏览器报告单独保存。

当前依赖检查以`npm audit`结果为准；许可证和维护证据来自上述官方仓库及固定包的元数据，不用星数替代功能或安全验证。
