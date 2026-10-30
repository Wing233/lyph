import { test, expect, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";

// Deterministic presentation checks. Production model/MCP acceptance is performed
// separately; these fixtures never run from the application or production API.
async function fixture(page: Page, options: { authorized?: boolean; interrupted?: boolean; viewConflict?: boolean; staleHistory?: boolean; freeModel?: boolean; delayed?: boolean; failed?: boolean } = {}) {
  const requests: Record<string, unknown>[] = [], actions: Record<string, unknown>[] = [], reads: string[] = [], viewReads: string[] = [], statusReads: string[] = [];
  let conflictRemaining = !!options.viewConflict;
  let revision = 1, city = "广州";
  const blocks = () => [{ type: "table", title: "企业查询结果", view_id: "fixture-view", revision, source: "公开企业快照", updated_at: "2026-10-01", query_state: { city, page: 1, limit: 8 }, pagination: { page: 1, limit: 8, total: 1, has_more: false }, controls: [{ key: "city", type: "text", label: "城市", value: city }], actions: [{ action: "filter", label: "应用筛选" }, { action: "export", label: "导出 CSV" }, { action: "select_entity", label: "查看企业" }], columns: [{ key: "name", label: "企业" }, { key: "city", label: "城市" }, { key: "active_jobs", label: "岗位数" }], rows: [{ id: "fixture-company", name: "测试企业", city, active_jobs: 12 }] }, { type: "chart", title: "岗位比较", view_id: "fixture-view", revision, kind: "bar", rows: [{ label: "测试企业", value: 12 }], unit: "个", source: "公开企业快照", updated_at: "2026-10-01" }];
  const answer = () => ({ status: "completed", answer: "已根据公开快照整理企业，请查看来源和更新日期。", message: "筛选企业", blocks: blocks(), suggestions: ["只看深圳", "比较岗位"], citations: [], cache: { type: requests.length > 1 ? "exact" : "miss", generated_at: "2026-10-01T00:00:00Z" } });
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url()), path = url.pathname;
    const respond = (value: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(value) });
    if (path === "/api/v1/chat/access") return respond({ free_remaining: options.authorized === false ? 0 : 1, authorized: options.authorized !== false, mode: "count", remaining: 10, ...(options.freeModel ? { model_mode: "public-free-test" } : {}) });
    if (path === "/api/v1/auth/session") return respond({ csrf_token: "fixture-csrf" });
    if (path === "/api/v1/public/navigation") return respond({ items: [{ id: "openilink", label: "OpenILink", href: "https://openilink.example.com/dashboard/overview" }, { id: "companies", label: "企业发现", href: "https://example.org/companies" }] });
    if (path === "/api/v1/public/events") return respond({ accepted: true });
    if (path === "/api/v1/chat/authorize") return respond({ detail: "临时密码无效" }, 403);
    if (path === "/api/v1/chat/entities") { reads.push(url.search); return respond({ blocks: [{ type: "content", title: "分享的项目", markdown: "这篇内容可以直接阅读，无需模型。", source: "公开项目", updated_at: "2026-10-01" }] }); }
    if (path === "/api/v1/chat/requests" && route.request().method() === "POST") {
      requests.push(route.request().postDataJSON());
      if (options.delayed) await new Promise((resolve) => setTimeout(resolve, 1500));
      if (options.failed) return respond({ detail: "模型暂时不可用" }, 503);
      const events = options.interrupted ? `event: delta\ndata: ${JSON.stringify({ text: "正在读取" })}\n\n` : `event: done\ndata: ${JSON.stringify(answer())}\n\n`;
      return route.fulfill({ status: 200, contentType: "text/event-stream", body: events });
    }
    if (path.startsWith("/api/v1/chat/requests/")) { statusReads.push(path); return respond(options.staleHistory ? { status: "stale", answer: "", blocks: [], citations: [], suggestions: [], error: "资料权限或版本变化，请重新查询" } : options.failed ? { status: "failed" } : answer()); }
    if (path === "/api/v1/chat/views/fixture-view" && route.request().method() === "GET") { viewReads.push(path); return respond({ blocks: blocks(), revision }); }
    if (path === "/api/v1/chat/views/fixture-view/actions") {
      const body = route.request().postDataJSON(); actions.push(body);
      expect(body.revision).toBe(revision);
      if (conflictRemaining) { conflictRemaining = false; city = "珠海"; revision += 1; return respond({ detail: "视图已更新" }, 409); }
      if (body.action === "export") return respond({ download: { filename: "fixture.csv", columns: [{ key: "city", label: "城市" }, { key: "jobs", label: "岗位数" }], rows: [{ city, jobs: 12 }] } });
      if (body.action === "select_entity") return respond({ blocks: [{ type: "content", title: "测试企业详情", markdown: "公开授权详情。", source: "企业快照", updated_at: "2026-10-01" }] });
      city = body.value.city; revision += 1; return respond({ blocks: blocks(), revision });
    }
    return respond({ detail: `Unexpected fixture request: ${path}` }, 404);
  });
  return { requests, actions, reads, viewReads, statusReads };
}

test("发送后立即清空输入框并显示等待状态，完成时保留新草稿", async ({ page }) => {
  const state = await fixture(page, { delayed: true, freeModel: true });
  await page.goto("/");
  const input = page.getByRole("textbox", { name: "与个人 Agent 对话" });
  await input.fill("筛选企业");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect.poll(() => state.requests.length).toBe(1);
  await expect(input).toHaveValue("");
  await expect(page.locator(".chat-progress[role=status]")).toContainText("正在理解问题");
  await input.fill("接下来只看深圳");
  await expect(page.getByRole("table").first()).toBeVisible();
  await expect(page.locator(".chat-progress[role=status]")).toHaveCount(0);
  await expect(input).toHaveValue("接下来只看深圳");
});

test("请求失败后恢复问题并提供重试", async ({ page }) => {
  await fixture(page, { failed: true });
  await page.goto("/");
  const input = page.getByRole("textbox", { name: "与个人 Agent 对话" });
  await input.fill("筛选企业");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "模型暂时不可用" })).toBeVisible();
  await expect(input).toHaveValue("筛选企业");
  await expect(page.getByRole("button", { name: "重试" })).toBeVisible();
  await expect(page.locator(".chat-progress[role=status]")).toHaveCount(0);
});

test("聊天首页连续筛选、图表切换、详情与CSV均不调用模型", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "想了解什么，直接问。" })).toBeVisible();
  await expect(page.getByRole("note")).toHaveCount(0);
  await expect(page.getByRole("link", { name: "OpenILink" })).toHaveAttribute("href", "https://openilink.example.com/dashboard/overview");
  await page.getByRole("textbox", { name: "与个人 Agent 对话" }).fill("筛选企业");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.getByRole("table").first()).toContainText("广州");
  await page.getByLabel("城市", { exact: true }).fill("深圳");
  await page.getByRole("button", { name: "应用筛选" }).click();
  await expect(page.getByRole("table").first()).toContainText("深圳");
  await page.getByRole("button", { name: "数据表", exact: true }).click();
  await expect(page.getByRole("table").nth(1)).toContainText("12");
  const downloadPending = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出 CSV" }).click();
  const download = await downloadPending;
  expect(await readFile((await download.path())!, "utf8")).toContain('"深圳","12"');
  await page.getByRole("button", { name: "查看企业" }).click();
  await expect(page.getByRole("dialog", { name: "内容阅读" })).toContainText("测试企业详情");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("table").first()).toContainText("深圳");
  await expect(page.getByRole("button", { name: "只看深圳" })).toBeVisible();
  expect(state.requests).toHaveLength(1); // Suggestions are not prefetched.
  expect(state.actions.map((action) => action.action)).toEqual(["filter", "export", "select_entity"]);
  await page.getByRole("button", { name: "重新查询", exact: true }).click();
  await expect.poll(() => state.requests.length).toBe(2);
  expect(state.requests[1].cache_mode).toBe("refresh");
  await page.setViewportSize({ width: 360, height: 800 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole("button", { name: "展开侧栏" }).click();
  await expect(page.getByRole("link", { name: "OpenILink" })).toBeVisible();
});

test("授权失败保留中文草稿，输入法确认键不提前提交", async ({ page }) => {
  const state = await fixture(page, { authorized: false });
  await page.goto("/");
  const input = page.getByRole("textbox", { name: "与个人 Agent 对话" });
  await input.fill("只看深圳的企业");
  await input.dispatchEvent("keydown", { key: "Enter", code: "Enter", keyCode: 229, isComposing: true });
  expect(state.requests).toHaveLength(0);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await input.press("Enter");
  await expect(page.getByRole("dialog")).toContainText("继续聊，输入临时密码");
  await expect(page.getByLabel("临时密码", { exact: true })).toBeFocused();
  await page.getByLabel("临时密码", { exact: true }).fill("invalid-fixture-password");
  await page.getByRole("button", { name: "验证并继续" }).click();
  await expect(page.getByRole("dialog")).toContainText("临时密码无效");
  await page.keyboard.press("Escape");
  await expect(input).toHaveValue("只看深圳的企业");
  expect(state.requests).toHaveLength(0);
});

test("断流恢复复用原请求，旧分享链接直接阅读", async ({ page }) => {
  const state = await fixture(page, { interrupted: true });
  await page.goto("/");
  await page.getByRole("textbox", { name: "与个人 Agent 对话" }).fill("筛选企业");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.getByRole("table").first()).toBeVisible();
  expect(state.requests).toHaveLength(1);
  await page.goto("/project?id=fixture-project");
  await expect(page).toHaveURL(/\?entity=project&id=fixture-project/);
  await expect(page.getByRole("dialog", { name: "内容阅读" })).toContainText("分享的项目");
  expect(state.reads.at(-1)).toContain("kind=project");
  expect(state.requests).toHaveLength(1);
});

test("视图冲突加载最新选择，不自动重放旧筛选，保存回答恢复最新视图", async ({ page }) => {
  const state = await fixture(page, { viewConflict: true });
  await page.goto("/");
  await page.getByRole("textbox", { name: "与个人 Agent 对话" }).fill("筛选企业");
  await page.getByRole("button", { name: "发送消息" }).click();
  await page.getByLabel("城市", { exact: true }).fill("深圳");
  await page.getByRole("button", { name: "应用筛选" }).click();
  await expect(page.getByRole("table").first()).toContainText("珠海");
  await expect(page.getByRole("alert").filter({ hasText: "已加载最新结果，请重试操作" })).toBeVisible();
  expect(state.actions).toHaveLength(1);
  expect(state.viewReads).toHaveLength(1);
  await page.getByRole("button", { name: /的对话/ }).click();
  await expect.poll(() => state.viewReads.length).toBe(2);
  await expect(page.getByRole("table").first()).toContainText("珠海");
  expect(state.requests).toHaveLength(1);
});

test("已保存回答来源失效时清除组件与引用，结束恢复且允许重新查询", async ({ page }) => {
  const state = await fixture(page, { staleHistory: true });
  await page.goto("/");
  await page.getByRole("textbox", { name: "与个人 Agent 对话" }).fill("筛选企业");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.getByRole("table").first()).toBeVisible();
  await page.getByRole("button", { name: /的对话/ }).click();
  await expect(page.getByRole("table")).toHaveCount(0);
  await expect(page.getByRole("alert").filter({ hasText: "资料权限或版本变化，请重新查询" })).toBeVisible();
  await expect(page.getByRole("button", { name: "停止回答" })).toHaveCount(0);
  expect(state.statusReads).toHaveLength(1);
  expect(state.requests).toHaveLength(1);
});

test("免费试运行说明可见，推荐问题点击后才提交", async ({ page }) => {
  const state = await fixture(page, { freeModel: true });
  await page.goto("/");
  await expect(page.getByRole("note")).toContainText("智能分析仅限公开企业数据");
  await expect(page.getByRole("note")).toContainText("请只输入可公开的问题");
  await expect(page.getByRole("note")).toContainText("繁忙时可能排队");
  await expect(page.getByRole("button").filter({ hasText: "有哪些文章" })).toBeVisible();
  expect(state.requests).toHaveLength(0);
  await page.getByRole("button").filter({ hasText: "有哪些项目并介绍技术栈" }).click();
  await expect.poll(() => state.requests.length).toBe(1);
  expect(state.requests[0].message).toBe("有哪些项目并介绍技术栈");
  await expect(page.getByRole("table").first()).toBeVisible();
  await page.setViewportSize({ width: 360, height: 800 });
  await expect(page.getByRole("note")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(state.requests).toHaveLength(1);
});
