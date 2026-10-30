import { randomUUID } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";

const origin = "http://127.0.0.1:3011";

async function navigationSettings(page: Page) {
  await page.goto("/admin/settings");
  await page.getByRole("button", { name: "导航菜单", exact: true }).click();
  await expect(page.getByRole("heading", { name: "导航与小球菜单" })).toBeVisible();
}

test("导航菜单管理联动首页、排序、公开状态与小球入口，并清理临时菜单", async ({ page }) => {
  const id = `e2e-navigation-${randomUUID().replaceAll("-", "")}`;
  const label = "导航回归临时入口";
  const updatedLabel = "导航回归编辑入口";

  await page.goto("/admin/login");
  await page.getByLabel("站主密码").fill("e2e-owner-password");
  await page.getByRole("button", { name: "进入工作台" }).click();
  await expect(page).toHaveURL(/admin\/dashboard/);
  const session = await page.request.get("/api/v1/auth/session");
  expect(session.ok()).toBeTruthy();
  const headers = { Origin: origin, "X-CSRF-Token": (await session.json()).csrf_token };

  const publicItems = async () => {
    const response = await page.request.get("/api/v1/public/navigation");
    expect(response.ok()).toBeTruthy();
    return (await response.json()).items as { id: string; label: string; href: string }[];
  };

  try {
    await navigationSettings(page);
    await page.getByRole("button", { name: "添加菜单 +", exact: true }).click();
    await page.getByLabel("菜单名称", { exact: true }).fill(label);
    await page.getByLabel("访问地址", { exact: false }).fill("/blog");
    await page.getByLabel("标识（可选）", { exact: false }).fill(id);
    await page.getByLabel("排序（越小越靠前）", { exact: true }).fill("-90000");
    await page.getByLabel("生成小球", { exact: false }).uncheck();
    await page.getByRole("button", { name: "保存菜单", exact: true }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    const row = page.getByRole("row").filter({ hasText: id });
    await expect(row).toContainText(label);
    await expect(row).toContainText("直接显示");
    expect((await publicItems())[0].id).toBe(id);
    await page.screenshot({ path: "tests/results/screenshots/navigation-admin.png", fullPage: true });

    await page.goto("/");
    const directLink = page.locator(".home-nav").getByRole("link", { name: label, exact: true });
    await expect(directLink).toBeVisible();
    await expect(directLink).toHaveAttribute("href", "/blog");
    await expect(directLink).not.toHaveAttribute("data-bot-route", id);
    await page.screenshot({ path: "tests/results/screenshots/navigation-direct-home.png", fullPage: true });

    await navigationSettings(page);
    await page.getByRole("row").filter({ hasText: id }).getByRole("button", { name: "编辑", exact: true }).click();
    await page.getByLabel("菜单名称", { exact: true }).fill(updatedLabel);
    await page.getByLabel("访问地址", { exact: false }).fill("/documents");
    await page.getByLabel("排序（越小越靠前）", { exact: true }).fill("90000");
    await page.getByRole("button", { name: "保存菜单", exact: true }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.getByRole("row").filter({ hasText: id })).toContainText(updatedLabel);
    const sorted = await publicItems();
    expect(sorted.at(-1)).toMatchObject({ id, label: updatedLabel, href: "/documents" });
    await page.goto("/");
    await expect(page.locator(".home-nav").getByRole("link", { name: updatedLabel, exact: true })).toHaveAttribute("href", "/documents");

    await navigationSettings(page);
    await page.getByRole("row").filter({ hasText: id }).getByRole("button", { name: "编辑", exact: true }).click();
    await page.getByLabel("公开显示", { exact: false }).uncheck();
    await page.getByRole("button", { name: "保存菜单", exact: true }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    expect((await publicItems()).some((item) => item.id === id)).toBe(false);
    await page.goto("/");
    await expect(page.locator(".home-nav a").filter({ hasText: updatedLabel })).toHaveCount(0);

    await navigationSettings(page);
    await page.getByRole("row").filter({ hasText: id }).getByRole("button", { name: "编辑", exact: true }).click();
    await page.getByLabel("公开显示", { exact: false }).check();
    await page.getByLabel("生成小球", { exact: false }).check();
    await page.getByRole("button", { name: "保存菜单", exact: true }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    expect((await publicItems()).some((item) => item.id === id)).toBe(true);
    await page.goto("/");
    const botLink = page.locator(`.home-nav [data-bot-route="${id}"]`);
    await expect(botLink).toHaveCount(1);
    await expect(botLink).toBeHidden();
    await expect(page.locator("#bg")).toBeVisible();
    await page.screenshot({ path: "tests/results/screenshots/navigation-bot-home.png", fullPage: true });

    await navigationSettings(page);
    await page.getByRole("row").filter({ hasText: id }).getByRole("button", { name: "删除", exact: true }).click();
    await page.getByRole("row").filter({ hasText: id }).getByRole("button", { name: "确认删除", exact: true }).click();
    await expect(page.getByRole("row").filter({ hasText: id })).toHaveCount(0);
    expect((await publicItems()).some((item) => item.id === id)).toBe(false);
  } finally {
    // Only this run's nonce ID is removed; fixtures and default menus remain untouched.
    const cleanup = await page.request.delete(`/api/v1/admin/navigation/${id}`, { headers });
    expect([200, 404]).toContain(cleanup.status());
    expect((await publicItems()).some((item) => item.id === id)).toBe(false);
    expect((await page.request.post("/api/v1/auth/logout", { headers })).ok()).toBeTruthy();
  }
});
