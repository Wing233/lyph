import { expect, test } from "@playwright/test";

const legacyPages = [
  "projects", "project", "stack", "interests", "blog", "documents",
  "journal", "entry", "collection",
];

test("旧公开页面临时重定向至聊天首页，源码作为备用保留", async ({ request }) => {
  test.skip(process.env.LYPH_CHAT_FIRST_ENABLED !== "true", "聊天首页经原型审核并启用开关后验收新路由");
  for (const page of legacyPages) {
    for (const path of [`/${page}`, `/${page}/`, `/${page}.html`, `/prototype2/${page}.html`]) {
      const response = await request.get(`${path}?id=legacy-entry&date=2026-10-25`, { maxRedirects: 0 });
      expect(response.status(), path).toBe(307);
      const destination = new URL(response.headers().location, response.url());
      expect(destination.pathname, path).toBe("/");
      const kind = page === "project" ? "project" : page === "entry" ? "content" : null;
      expect(Object.fromEntries(destination.searchParams), path).toEqual(kind ? { entity: kind, id: "legacy-entry", date: "2026-10-25" } : {});
    }
  }
  for (const path of ["/companies", "/companies/", "/companies/index.html"]) {
    const response = await request.get(path, { maxRedirects: 0 });
    if (process.env.LYPH_VERIFY_STATIC_APPS === "true") {
      expect(response.status(), path).toBe(path === "/companies" ? 308 : 200);
      if (path === "/companies") expect(new URL(response.headers().location, response.url()).pathname).toBe("/companies/");
    } else {
      // A direct Next server has no static application mount; Nginx serves it in production.
      expect(response.status(), path).toBe(307);
      expect(new URL(response.headers().location, response.url()).pathname, path).toBe("/");
    }
  }
});

test("默认保留原有 HTML 阅读链接兼容", async ({ request }) => {
  test.skip(process.env.LYPH_CHAT_FIRST_ENABLED === "true", "新聊天路由启用时由307专项验收");
  for (const page of legacyPages) {
    for (const path of [`/${page}.html`, `/prototype2/${page}.html`]) {
      const response = await request.get(`${path}?id=legacy-entry&date=2026-10-25`, { maxRedirects: 0 });
      expect(response.status(), path).toBe(307);
      const destination = new URL(response.headers().location, response.url());
      expect(destination.pathname, path).toBe(`/${page}`);
      expect(Object.fromEntries(destination.searchParams), path).toEqual({ id: "legacy-entry", date: "2026-10-25" });
    }
  }
});

test("聊天路由不拦截后台、API和采集PWA", async ({ request }) => {
  for (const path of [
    "/", "/admin/login", "/collector", "/collector-sw.js",
    "/collector-icon.svg", "/manifest.webmanifest", "/api/v1/auth/session",
  ]) {
    const response = await request.get(path, { maxRedirects: 0 });
    expect(response.status(), path).toBe(200);
  }
  const admin = await request.get("/admin/settings", { maxRedirects: 0 });
  if (admin.headers().location) {
    expect(new URL(admin.headers().location, admin.url()).pathname).not.toBe("/");
  }
  const unknown = await request.get("/unknown-public-page", { maxRedirects: 0 });
  expect(unknown.status()).toBe(404);
});
