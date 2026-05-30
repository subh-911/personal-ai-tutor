"use client";

import { useState, type FormEvent } from "react";
import { toast } from "sonner";

import { pollIngestStatus, scrapeUrl, type IngestStage } from "@/lib/admin";
import { useBackendToken } from "@/lib/auth-token";

type ScrapeFormProps = {
  onIngested?: () => void;
};

const STAGE_TOAST: Record<IngestStage, string> = {
  queued: "Queued for embedding…",
  chunking: "Chunking text…",
  embedding: "Embedding chunks…",
  persisting: "Saving vectors…",
  completed: "Done",
  failed: "Failed",
};

export function ScrapeForm({ onIngested }: ScrapeFormProps = {}) {
  const [url, setUrl] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const getToken = useBackendToken();

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    const trimmed = url.trim();
    if (!trimmed) return;

    setSubmitting(true);
    const toastId = toast.loading(`Fetching ${trimmed}…`);
    try {
      const accepted = await scrapeUrl(trimmed, getToken);
      // Notify the parent immediately so DocumentsTable shows the queued row.
      onIngested?.();
      toast.loading(STAGE_TOAST.queued, { id: toastId });

      const final = await pollIngestStatus(
        accepted.id,
        (s) => {
          if (s.stage) toast.loading(STAGE_TOAST[s.stage], { id: toastId });
        },
        getToken,
      );

      if (final.status === "completed") {
        toast.success(
          `Scraped "${final.title ?? trimmed}" — ${final.chunk_count} chunk${
            final.chunk_count === 1 ? "" : "s"
          }`,
          { id: toastId },
        );
        setUrl("");
        onIngested?.();
      } else {
        toast.error(`Scrape failed: ${final.error ?? "worker reported failure"}`, { id: toastId });
        onIngested?.();
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      toast.error(`Scrape failed: ${message}`, { id: toastId });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section
      className="rounded-2xl border border-zinc-200 bg-white p-5 shadow-sm dark:border-zinc-800 dark:bg-zinc-900"
      data-testid="scrape-form"
    >
      <h2 className="mb-3 text-lg font-semibold">Scrape a URL</h2>
      <p className="mb-4 text-sm text-zinc-600 dark:text-zinc-400">
        Paste a public article URL — the server fetches the page, extracts the main text via BeautifulSoup,
        and queues chunk + embed work onto a background worker.
      </p>
      <form onSubmit={onSubmit} className="flex flex-col gap-3">
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">URL</span>
          <input
            type="url"
            required
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://example.com/system-design/whatsapp"
            disabled={submitting}
            className="w-full rounded-lg border border-zinc-300 bg-white px-3 py-2 text-sm outline-none focus:border-blue-500 disabled:opacity-60 dark:border-zinc-700 dark:bg-zinc-900"
            data-testid="scrape-url-input"
          />
        </label>
        <div>
          <button
            type="submit"
            disabled={submitting || !url.trim()}
            className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
            data-testid="scrape-submit"
          >
            {submitting ? "Scraping…" : "Scrape"}
          </button>
        </div>
      </form>
    </section>
  );
}
