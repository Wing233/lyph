import { defineConfig } from "@playwright/test";

// Presentation verification against a running, already-built web container.
// API calls are isolated by chat-first.spec.ts; this config starts no services.
export default defineConfig({
  testDir: "./tests",
  testMatch: "chat-first.spec.ts",
  workers: 1,
  fullyParallel: false,
  timeout: 60000,
  expect: { timeout: 20000 },
  use: {
    baseURL: process.env.LYPH_VERIFY_URL || "http://127.0.0.1:3011",
    viewport: { width: 1440, height: 1000 },
    headless: true,
    trace: "retain-on-failure",
    launchOptions: { executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH },
  },
  reporter: [
    ["list"],
    ["json", { outputFile: "tests/results/chat-ship.json" }],
  ],
});
