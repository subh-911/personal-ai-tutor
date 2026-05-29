"use client";

import { useCallback, useRef, useState, type DragEvent, type ChangeEvent } from "react";
import { toast } from "sonner";

import { uploadFile, type IngestStatus } from "@/lib/admin";
import { useBackendToken } from "@/lib/auth-token";

type UploadStatus = "pending" | "uploading" | "done" | "error";

type UploadItem = {
  id: string;
  file: File;
  status: UploadStatus;
  progress: number;
  result?: IngestStatus;
  error?: string;
};

const ACCEPTED_MIMES = new Set([
  "application/pdf",
  "text/plain",
  "text/markdown",
  "text/x-markdown",
]);
const ACCEPTED_EXTS = [".pdf", ".txt", ".md", ".markdown"];

function isAccepted(file: File): boolean {
  if (ACCEPTED_MIMES.has(file.type)) return true;
  const name = file.name.toLowerCase();
  return ACCEPTED_EXTS.some((ext) => name.endsWith(ext));
}

function makeId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function UploadZone() {
  const [items, setItems] = useState<UploadItem[]>([]);
  const [dragOver, setDragOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const getToken = useBackendToken();

  const startUpload = useCallback(async (file: File) => {
    const id = makeId();
    setItems((prev) => [
      ...prev,
      { id, file, status: "pending", progress: 0 },
    ]);

    const update = (patch: Partial<UploadItem>) =>
      setItems((prev) => prev.map((it) => (it.id === id ? { ...it, ...patch } : it)));

    update({ status: "uploading" });

    try {
      const result = await uploadFile(
        file,
        null,
        (loaded, total) => {
          update({ progress: total > 0 ? loaded / total : 0 });
        },
        getToken,
      );
      update({ status: "done", progress: 1, result });
      toast.success(
        `Ingested "${file.name}" — ${result.chunk_count} chunk${result.chunk_count === 1 ? "" : "s"}`,
      );
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      update({ status: "error", error: message });
      toast.error(`Upload failed for "${file.name}": ${message}`);
    }
  }, [getToken]);

  const handleFiles = useCallback(
    (files: FileList | File[]) => {
      const accepted: File[] = [];
      for (const file of Array.from(files)) {
        if (!isAccepted(file)) {
          toast.error(`"${file.name}" is not a supported type. Use PDF, TXT, or Markdown.`);
          continue;
        }
        accepted.push(file);
      }
      for (const f of accepted) startUpload(f);
    },
    [startUpload],
  );

  const onDragEnter = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragOver(true);
  };
  const onDragOver = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragOver(true);
  };
  const onDragLeave = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragOver(false);
  };
  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragOver(false);
    if (e.dataTransfer.files?.length) handleFiles(e.dataTransfer.files);
  };

  const onPick = (e: ChangeEvent<HTMLInputElement>) => {
    if (e.target.files?.length) handleFiles(e.target.files);
    e.target.value = ""; // allow re-uploading the same file
  };

  return (
    <section
      className="rounded-2xl border border-zinc-200 bg-white p-5 shadow-sm dark:border-zinc-800 dark:bg-zinc-900"
      data-testid="upload-zone"
    >
      <h2 className="mb-3 text-lg font-semibold">Upload files</h2>
      <p className="mb-4 text-sm text-zinc-600 dark:text-zinc-400">
        Drop one or more <code>.pdf</code>, <code>.txt</code>, or <code>.md</code> files below.
        The full ingestion pipeline (parse → chunk → embed → save) runs server-side.
      </p>

      <div
        onDragEnter={onDragEnter}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
        onClick={() => inputRef.current?.click()}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") inputRef.current?.click();
        }}
        className={
          "flex cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed p-8 text-center transition-colors " +
          (dragOver
            ? "border-blue-500 bg-blue-50 dark:bg-blue-950"
            : "border-zinc-300 hover:bg-zinc-50 dark:border-zinc-700 dark:hover:bg-zinc-900")
        }
        aria-label="Drop files here, or click to pick"
        data-testid="upload-dropzone"
      >
        <p className="text-sm font-medium">Drop files or click to browse</p>
        <p className="mt-1 text-xs text-zinc-500">PDF · TXT · Markdown · up to 25 MB each</p>
        <input
          ref={inputRef}
          type="file"
          multiple
          accept=".pdf,.txt,.md,.markdown,application/pdf,text/plain,text/markdown"
          className="hidden"
          onChange={onPick}
          aria-hidden
        />
      </div>

      {items.length > 0 && (
        <ul className="mt-4 flex flex-col gap-2" data-testid="upload-list">
          {items.map((it) => (
            <li
              key={it.id}
              className="rounded-lg border border-zinc-200 px-3 py-2 text-sm dark:border-zinc-800"
              data-testid="upload-item"
            >
              <div className="flex items-center justify-between gap-3">
                <span className="truncate font-medium" title={it.file.name}>
                  {it.file.name}
                </span>
                <span className="shrink-0 text-xs text-zinc-500">{formatBytes(it.file.size)}</span>
              </div>
              {it.status === "uploading" && (
                <div className="mt-2 h-1.5 w-full overflow-hidden rounded bg-zinc-200 dark:bg-zinc-700">
                  <div
                    className="h-full bg-blue-600 transition-[width]"
                    style={{ width: `${Math.round(it.progress * 100)}%` }}
                  />
                </div>
              )}
              {it.status === "done" && it.result && (
                <p className="mt-1 text-xs text-green-700 dark:text-green-400">
                  ✓ {it.result.chunk_count} chunk{it.result.chunk_count === 1 ? "" : "s"} ingested
                </p>
              )}
              {it.status === "error" && (
                <p className="mt-1 text-xs text-red-700 dark:text-red-400">✗ {it.error}</p>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
