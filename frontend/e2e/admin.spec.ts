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

  test("documents table renders the caller's documents from GET /documents", async ({ page }) => {
    await page.route("**/api/backend/documents", async (route) => {
      const body = JSON.stringify([
        {
          id: "22222222-2222-2222-2222-222222222222",
          source_type: "scrape",
          source_uri: "https://example.com/article-a",
          title: "Article A",
          status: "completed",
          chunk_count: 7,
          created_at: "2026-05-29T10:00:00Z",
        },
        {
          id: "33333333-3333-3333-3333-333333333333",
          source_type: "upload",
          source_uri: "notes.pdf",
          title: "Notes",
          status: "completed",
          chunk_count: 14,
          created_at: "2026-05-28T08:00:00Z",
        },
      ]);
      await route.fulfill({
        status: 200,
        headers: { "content-type": "application/json" },
        body,
      });
    });

    await page.goto("/admin");
    await expect(page.getByTestId("documents-table")).toBeVisible();
    const rows = page.getByTestId("documents-row");
    await expect(rows).toHaveCount(2);
    await expect(page.getByText("Article A")).toBeVisible();
    await expect(page.getByText("Notes")).toBeVisible();
  });

  test("deleting a row confirms via sonner and removes it on success", async ({ page }) => {
    await page.route("**/api/backend/documents", async (route) => {
      const body = JSON.stringify([
        {
          id: "44444444-4444-4444-4444-444444444444",
          source_type: "upload",
          source_uri: "notes.pdf",
          title: "Notes to delete",
          status: "completed",
          chunk_count: 3,
          created_at: "2026-05-29T10:00:00Z",
        },
      ]);
      await route.fulfill({
        status: 200,
        headers: { "content-type": "application/json" },
        body,
      });
    });
    await page.route(
      "**/api/backend/documents/44444444-4444-4444-4444-444444444444",
      async (route) => {
        await route.fulfill({ status: 204, body: "" });
      },
    );

    await page.goto("/admin");
    const row = page.getByTestId("documents-row");
    await expect(row).toHaveCount(1);

    await page.getByTestId("documents-delete").click();

    // sonner confirmation toast appears with a Delete action.
    const confirmButton = page.getByRole("button", { name: "Delete", exact: true }).last();
    await expect(confirmButton).toBeVisible({ timeout: 5_000 });
    await confirmButton.click();

    await expect(page.getByText(/deleted.*notes to delete/i)).toBeVisible({ timeout: 5_000 });
    await expect(page.getByTestId("documents-row")).toHaveCount(0);
  });
});
