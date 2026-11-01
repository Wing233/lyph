import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests", testMatch: "chat-first.spec.ts", workers: 1, fullyParallel: false, timeout: 60000,
  expect: { timeout: 20000 },
  use: { baseURL: "http://127.0.0.1:3011", headless: true, trace: "retain-on-failure" },
  reporter: [["list"], ["json", { outputFile: "tests/results/chat-first.json" }]],
  webServer: {
    command: `"${process.execPath}" node_modules/next/dist/bin/next dev --hostname 127.0.0.1 --port 3011`,
    url: "http://127.0.0.1:3011", timeout: 180000, reuseExistingServer: false,
    env: { LYPH_CHAT_FIRST_ENABLED: "true", NEXT_TELEMETRY_DISABLED: "1", NODE_OPTIONS: "--max-old-space-size=2048" },
  },
});
