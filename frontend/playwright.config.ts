import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: 1,
  reporter: process.env.CI ? "list" : "list",
  use: {
    baseURL: "http://localhost:3000",
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: [
    {
      // Phase 8 — DEV_AUTH_BYPASS=1 disables JWT verification so the suite
      // can drive auth-gated routes without minting real Clerk JWTs.
      command: "uv run uvicorn app.main:app --port 8000",
      cwd: "../backend",
      url: "http://localhost:8000/health",
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
      env: { DEV_AUTH_BYPASS: "1" },
    },
    {
      // Phase 8 — NEXT_PUBLIC_TEST_DISABLE_AUTH=1 makes middleware.ts a
      // pure passthrough, and `app/layout.tsx` + `app/page.tsx` skip
      // rendering Clerk components, so the hermetic test suite doesn't
      // need real Clerk credentials at all.
      command: "npm run dev -- --port 3000",
      cwd: ".",
      url: "http://localhost:3000",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      env: { NEXT_PUBLIC_TEST_DISABLE_AUTH: "1" },
    },
  ],
});
