// Verify the running image. No config transpilation or middleware stubs: changing
// the container's runtime flag must change these observable HTTP responses.
const assert = require("node:assert/strict");

const baseURL = process.env.LYPH_VERIFY_URL || "http://127.0.0.1:3011";
const mode = process.argv[2] || process.env.LYPH_CHAT_FIRST_ENABLED || "false";
assert.ok(["enabled", "disabled", "true", "false"].includes(mode), "Use enabled/disabled or LYPH_CHAT_FIRST_ENABLED=true/false");
const enabled = mode === "enabled" || mode === "true";
// The deployed Nginx gateway serves the independent app; direct Next has no app mount.
const staticApps = process.env.LYPH_VERIFY_STATIC_APPS === "true";
const pages = ["projects", "project", "stack", "interests", "blog", "documents", "journal", "entry", "collection"];
let checks = 0;

async function get(path) {
  const response = await fetch(new URL(path, baseURL), { redirect: "manual", signal: AbortSignal.timeout(15000) });
  if (path !== "/") await response.body?.cancel();
  return response;
}
function destination(response, path) {
  const location = response.headers.get("location");
  assert.ok(location, `${path}: missing Location header`);
  return new URL(location, response.url);
}
function expectQuery(url, expected, label) {
  assert.deepEqual(Object.fromEntries(url.searchParams), expected, `${label}: query parameters`);
}

(async () => {
  const home = await get("/");
  assert.equal(home.status, 200, "Homepage must be healthy");
  assert.equal(/class="chat-shell"/.test(await home.text()), enabled, "Homepage must follow the runtime flag");
  checks += 1;
  for (const page of pages) {
    const paths = enabled ? [`/${page}`, `/${page}/`, `/${page}.html`, `/prototype2/${page}.html`] : [`/${page}.html`, `/prototype2/${page}.html`];
    for (const path of paths) {
      const response = await get(`${path}?id=legacy-entry&date=2026-10-25&extra=discard`);
      assert.equal(response.status, 307, `${path}: temporary, reversible redirect`);
      const url = destination(response, path);
      assert.equal(url.pathname, enabled ? "/" : `/${page}`, path);
      const kind = page === "project" ? "project" : page === "entry" ? "content" : null;
      expectQuery(url, enabled ? kind ? { entity: kind, id: "legacy-entry", date: "2026-10-25" } : {} : { id: "legacy-entry", date: "2026-10-25", extra: "discard" }, path);
      checks += 1;
    }
    if (!enabled) {
      const response = await get(`/${page}?id=legacy-entry`);
      if (response.headers.has("location")) assert.notEqual(destination(response, page).pathname, "/", `${page}: old page must remain available`);
      checks += 1;
    }
  }
  if (enabled) {
    for (const [path, expected] of [
      ["/project?id=shared-project", { entity: "project", id: "shared-project" }],
      ["/entry?id=shared-content", { entity: "content", id: "shared-content" }],
      ["/entry?date=2026-10-25", { entity: "content", date: "2026-10-25" }],
    ]) {
      const response = await get(path);
      assert.equal(response.status, 307, path);
      const url = destination(response, path);
      assert.equal(url.pathname, "/", path); expectQuery(url, expected, path); checks += 1;
    }
  }
  if (staticApps) {
    const bare = await get("/companies");
    assert.equal(bare.status, 308); assert.equal(destination(bare, "/companies").pathname, "/companies/"); checks += 1;
    for (const path of ["/companies/", "/companies/index.html"]) {
      const response = await get(path);
      assert.equal(response.status, 200, path);
      assert.match(response.headers.get("content-type") || "", /text\/html/i, path); checks += 1;
    }
    const post = await fetch(new URL("/companies/api/run-incremental", baseURL), { method: "POST", redirect: "manual" });
    assert.equal(post.status, 403); checks += 1;
    const hidden = await get("/companies/.env");
    assert.equal(hidden.status, 403); checks += 1;
  } else if (enabled) {
    for (const path of ["/companies", "/companies/", "/companies/index.html"]) {
      const response = await get(path);
      assert.equal(response.status, 307, path); assert.equal(destination(response, path).pathname, "/", path); checks += 1;
    }
  }
  for (const path of ["/admin/login", "/admin/settings", "/collector", "/collector-sw.js", "/api/v1/auth/session"]) {
    const response = await get(path);
    if (response.headers.has("location")) assert.notEqual(destination(response, path).pathname, "/", `${path}: protected surface must not redirect to chat`);
    checks += 1;
  }
  console.log(`PASS: ${checks} HTTP routing checks against ${baseURL}; runtime chat flag ${enabled ? "enabled" : "disabled"}; share id/date preserved.`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
