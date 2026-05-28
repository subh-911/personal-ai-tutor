// Chat UI smoke tests. We mock the backend SSE stream so these tests verify
// frontend behaviour only (streaming consumption, message rendering, action
// buttons, session-id round-trip) — independent of whether the live LLM
// (Gemini in phase 5+) is reachable or quota-available.

import { expect, test, type Route } from "@playwright/test";

const SESSION_ID = "11111111-1111-1111-1111-111111111111";

function sseBytes(chunks: string[]): Buffer {
  const parts = chunks.map((c) => `data: ${JSON.stringify({ delta: c })}\n\n`);
  parts.push("data: [DONE]\n\n");
  return Buffer.from(parts.join(""), "utf-8");
}

async function fulfilSse(route: Route, chunks: string[]): Promise<void> {
  await route.fulfill({
    status: 200,
    headers: {
      "content-type": "text/event-stream",
      "cache-control": "no-cache",
      "x-session-id": SESSION_ID,
    },
    body: sseBytes(chunks),
  });
}

test.describe("chat page", () => {
  test.beforeEach(async ({ context }) => {
    await context.clearCookies();
    await context.addInitScript(() => {
      try {
        window.localStorage.clear();
      } catch {
        /* noop */
      }
    });
  });

  test("sends a message and visually receives a streamed response", async ({ page }) => {
    await page.route("**/api/backend/chat", (route) =>
      fulfilSse(route, ["The ", "CAP ", "theorem ", "states… "]),
    );

    await page.goto("/chat");
    await page.getByLabel("Message").fill("Explain CAP theorem");
    await page.getByRole("button", { name: "Send" }).click();

    await expect(page.getByTestId("user-message").last()).toHaveText("Explain CAP theorem");
    await expect(page.getByTestId("assistant-message").last()).toContainText(
      "The CAP theorem states",
      { timeout: 15_000 },
    );
    await expect(page.getByTestId("session-id")).toBeVisible();
  });

  test("'Test my knowledge' button forces a quiz response", async ({ page }) => {
    await page.route("**/api/backend/chat", async (route) => {
      const post = route.request().postDataJSON();
      expect(post.force_route).toBe("quiz");
      await fulfilSse(route, [
        "Question: ",
        "Which best describes Raft? ",
        "A) Option A ",
        "B) Option B ",
        "C) Option C ",
        "D) Option D",
      ]);
    });

    await page.goto("/chat");
    await page.getByLabel("Message").fill("Raft consensus");
    await page.getByRole("button", { name: "Test my knowledge" }).click();

    const assistantMessage = page.getByTestId("assistant-message").last();
    await expect(assistantMessage).toContainText("Question:", { timeout: 15_000 });
    await expect(assistantMessage).toContainText("A)");
    await expect(assistantMessage).toContainText("D)");
  });

  test("'Give me an example' button forces a tutor response", async ({ page }) => {
    await page.route("**/api/backend/chat", async (route) => {
      const post = route.request().postDataJSON();
      expect(post.force_route).toBe("tutor");
      await fulfilSse(route, ["Sure — here is an example of "]);
    });

    await page.goto("/chat");
    await page.getByRole("button", { name: "Give me an example" }).click();

    await expect(page.getByTestId("user-message").last()).toContainText("Give me an example");
    await expect(page.getByTestId("assistant-message").last()).toContainText(
      "here is an example",
      { timeout: 15_000 },
    );
  });
});
