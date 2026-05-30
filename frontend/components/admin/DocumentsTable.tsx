"use client";

import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";

import { deleteDocument, listDocuments, type Document, type IngestStage } from "@/lib/admin";
import { useBackendToken } from "@/lib/auth-token";

type Props = {
  refreshKey: number;
};

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function displayTitle(doc: Document): string {
  if (doc.title && doc.title.trim().length > 0) return doc.title;
  return doc.source_uri;
}

const STAGE_LABEL: Record<IngestStage, string> = {
  queued: "Queued",
  chunking: "Chunking…",
  embedding: "Embedding…",
  persisting: "Saving…",
  completed: "Ready",
  failed: "Failed",
};

function StatusBadge({ doc }: { doc: Document }) {
  if (doc.status === "completed") {
    return (
      <span
        className="rounded-full bg-green-100 px-2 py-0.5 text-xs font-medium text-green-700 dark:bg-green-950 dark:text-green-300"
        data-testid="documents-status"
        data-status="completed"
      >
        Ready
      </span>
    );
  }
  if (doc.status === "failed") {
    return (
      <span
        className="rounded-full bg-red-100 px-2 py-0.5 text-xs font-medium text-red-700 dark:bg-red-950 dark:text-red-300"
        data-testid="documents-status"
        data-status="failed"
      >
        Failed
      </span>
    );
  }
  // processing — surface the live stage label when we have one
  const label = doc.stage ? STAGE_LABEL[doc.stage] : "Processing…";
  return (
    <span
      className="rounded-full bg-blue-100 px-2 py-0.5 text-xs font-medium text-blue-700 dark:bg-blue-950 dark:text-blue-300"
      data-testid="documents-status"
      data-status="processing"
      data-stage={doc.stage ?? ""}
    >
      {label}
    </span>
  );
}

export function DocumentsTable({ refreshKey }: Props) {
  const [docs, setDocs] = useState<Document[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const getToken = useBackendToken();

  const fetchDocs = useCallback(async (): Promise<Document[]> => {
    setLoading(true);
    setError(null);
    try {
      const rows = await listDocuments(getToken);
      setDocs(rows);
      return rows;
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      return [];
    } finally {
      setLoading(false);
    }
  }, [getToken]);

  useEffect(() => {
    // Phase 10 — auto-poll while any row is in-flight. The fetch cycles every
    // 1.5s; once no row reports `status === "processing"` we stop. Effect
    // cleanup cancels any pending tick so route changes / unmounts don't leak.
    let cancelled = false;
    let timeoutId: ReturnType<typeof setTimeout> | null = null;

    const tick = async () => {
      if (cancelled) return;
      const rows = await fetchDocs();
      if (cancelled) return;
      if (rows.some((d) => d.status === "processing")) {
        timeoutId = setTimeout(() => void tick(), 1500);
      }
    };

    void tick();
    return () => {
      cancelled = true;
      if (timeoutId) clearTimeout(timeoutId);
    };
  }, [fetchDocs, refreshKey]);

  const confirmDelete = useCallback(
    (doc: Document) => {
      // Optimistic delete: drop the row, fire the request, restore on error.
      const performDelete = async () => {
        const snapshot = docs;
        setDocs((prev) => prev.filter((d) => d.id !== doc.id));
        try {
          await deleteDocument(doc.id, getToken);
          toast.success(`Deleted "${displayTitle(doc)}"`);
        } catch (err) {
          setDocs(snapshot);
          const message = err instanceof Error ? err.message : String(err);
          toast.error(`Delete failed: ${message}`);
        }
      };

      toast(`Delete "${displayTitle(doc)}"?`, {
        description: "This removes the document and all its chunks. Cannot be undone.",
        action: {
          label: "Delete",
          onClick: () => {
            void performDelete();
          },
        },
        cancel: {
          label: "Cancel",
          onClick: () => {
            // dismiss only
          },
        },
        duration: 10_000,
      });
    },
    [docs, getToken],
  );

  return (
    <section
      className="rounded-2xl border border-zinc-200 bg-white p-5 shadow-sm dark:border-zinc-800 dark:bg-zinc-900"
      data-testid="documents-table"
    >
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">Your knowledge base</h2>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            Documents you&apos;ve ingested. Delete any you no longer want the tutor to retrieve.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void fetchDocs()}
          disabled={loading}
          className="rounded-md border border-zinc-300 px-3 py-1.5 text-xs font-medium hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-900"
          data-testid="documents-refresh"
        >
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>

      {error && (
        <p
          className="mb-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300"
          data-testid="documents-error"
        >
          {error}
        </p>
      )}

      {!error && !loading && docs.length === 0 && (
        <p className="py-6 text-center text-sm text-zinc-500" data-testid="documents-empty">
          No documents yet. Upload a file or scrape a URL above to get started.
        </p>
      )}

      {docs.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm" data-testid="documents-list">
            <thead className="border-b border-zinc-200 text-xs uppercase tracking-wide text-zinc-500 dark:border-zinc-800">
              <tr>
                <th className="px-2 py-2 font-medium">Title</th>
                <th className="px-2 py-2 font-medium">Source</th>
                <th className="px-2 py-2 font-medium">Status</th>
                <th className="px-2 py-2 font-medium">Chunks</th>
                <th className="px-2 py-2 font-medium">Added</th>
                <th className="px-2 py-2 font-medium"></th>
              </tr>
            </thead>
            <tbody>
              {docs.map((doc) => (
                <tr
                  key={doc.id}
                  className="border-b border-zinc-100 last:border-b-0 dark:border-zinc-800"
                  data-testid="documents-row"
                  data-document-id={doc.id}
                >
                  <td className="max-w-xs truncate px-2 py-3 font-medium" title={displayTitle(doc)}>
                    {displayTitle(doc)}
                  </td>
                  <td className="px-2 py-3 text-xs uppercase tracking-wide text-zinc-500">
                    {doc.source_type}
                  </td>
                  <td className="px-2 py-3"><StatusBadge doc={doc} /></td>
                  <td className="px-2 py-3 tabular-nums">{doc.chunk_count}</td>
                  <td className="px-2 py-3 text-xs text-zinc-500">{formatDate(doc.created_at)}</td>
                  <td className="px-2 py-3 text-right">
                    <button
                      type="button"
                      onClick={() => confirmDelete(doc)}
                      className="rounded-md border border-red-200 px-2.5 py-1 text-xs font-medium text-red-700 hover:bg-red-50 dark:border-red-900 dark:text-red-300 dark:hover:bg-red-950"
                      data-testid="documents-delete"
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
