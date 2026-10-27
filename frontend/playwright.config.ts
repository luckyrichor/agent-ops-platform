import { defineConfig } from "@playwright/test";
import path from "node:path";
const output =
  process.env.P5_ARTIFACTS || path.resolve("../.local/p5-playwright");
export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  workers: 1,
  timeout: 60000,
  expect: { timeout: 12000 },
  outputDir: path.join(output, "test-results"),
  reporter: [
    ["list"],
    ["json", { outputFile: path.join(output, "playwright.json") }],
  ],
  use: {
    baseURL: process.env.P5_BASE_URL || "http://127.0.0.1:5173",
    browserName: "chromium",
    launchOptions: { executablePath: process.env.P5_BROWSER_PATH },
    viewport: { width: 1440, height: 1000 },
    locale: "zh-CN",
    trace: "off",
    video: "off",
    screenshot: "off",
  },
  projects: [{ name: "chromium" }],
});
