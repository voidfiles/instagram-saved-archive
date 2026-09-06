import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 2 : 3,
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:4321",
    browserName: "chromium",
    serviceWorkers: "block",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    hasTouch: true,
  },
  projects: [
    { name: "mobile", use: { viewport: { width: 375, height: 812 } } },
    { name: "tablet", use: { viewport: { width: 768, height: 1024 } } },
    { name: "desktop", use: { viewport: { width: 1440, height: 1000 } } },
  ],
  webServer: {
    command: "npm run preview -- --host 127.0.0.1 --port 4321",
    // Astro 7 otherwise detaches automatically when it detects an agent.
    env: { ASTRO_PREVIEW_BACKGROUND: "1" },
    url: "http://127.0.0.1:4321",
    reuseExistingServer: false,
  },
});
