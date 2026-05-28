// Admin ingestion UI smoke test. Mocks /api/backend/ingest/scrape so the test
// doesn't depend on a real external URL being reachable — the goal is to verify
// the UI flow (form → submit → toast), not the backend's scraping correctness.

import { expect, test } from "@playwright/test";

test.describe("admin page", () => {
  test("scraping an HLD article URL shows a success toast", async ({ page }) => {
    await page.route("**/api/backend/ingest/scrape", async (route) => {
      const body = JSON.stringify({
        id: "11111111-1111-1111-1111-111111111111",
        status: "completed",
        chunk_count: 12,
        title: "WhatsApp High-Level System Design",
        error: null,
      });
      await route.fulfill({
        status: 200,
        headers: { "content-type": "application/json" },
        body,
      });
    });

    await page.goto("/admin");
    await expect(page.getByTestId("admin-page")).toBeVisible();
    await expect(page.getByTestId("upload-zone")).toBeVisible();
    await expect(page.getByTestId("scrape-form")).toBeVisible();

    await page
      .getByTestId("scrape-url-input")
      .fill("https://example.com/system-design/whatsapp-architecture");
    await page.getByTestId("scrape-submit").click();

    const successToast = page.getByText(
      /scraped.*whatsapp high-level system design.*12 chunks/i,
    );
    await expect(successToast).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId("scrape-url-input")).toHaveValue("");
  });

  test("scraping shows an error toast when the backend returns 4xx", async ({ page }) => {
    await page.route("**/api/backend/ingest/scrape", async (route) => {
      await route.fulfill({
        status: 502,
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ detail: "failed to fetch URL" }),
      });
    });

    await page.goto("/admin");
    await page.getByTestId("scrape-url-input").fill("https://nonexistent.example/article");
    await page.getByTestId("scrape-submit").click();

    await expect(
      page.getByText(/scrape failed.*failed to fetch url/i),
    ).toBeVisible({ timeout: 10_000 });
  });
});
