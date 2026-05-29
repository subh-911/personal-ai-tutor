"use client";

import Link from "next/link";
import { useCallback, useState } from "react";

import { DocumentsTable } from "@/components/admin/DocumentsTable";
import { ScrapeForm } from "@/components/admin/ScrapeForm";
import { UploadZone } from "@/components/admin/UploadZone";

export default function AdminPage() {
  const [refreshKey, setRefreshKey] = useState(0);
  const onIngested = useCallback(() => setRefreshKey((k) => k + 1), []);

  return (
    <main
      className="mx-auto flex min-h-screen max-w-5xl flex-col gap-6 p-6"
      data-testid="admin-page"
    >
      <header className="flex items-center justify-between border-b border-zinc-200 pb-4 dark:border-zinc-800">
        <div>
          <h1 className="text-2xl font-semibold">Admin · Ingestion</h1>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            Upload files or scrape URLs to feed the tutor&apos;s retrieval corpus.
          </p>
        </div>
        <Link
          href="/chat"
          className="rounded-md border border-zinc-300 px-3 py-2 text-sm font-medium hover:bg-zinc-50 dark:border-zinc-700 dark:hover:bg-zinc-900"
        >
          ← Back to chat
        </Link>
      </header>

      <div className="grid gap-6 md:grid-cols-2">
        <UploadZone onIngested={onIngested} />
        <ScrapeForm onIngested={onIngested} />
      </div>

      <DocumentsTable refreshKey={refreshKey} />
    </main>
  );
}
