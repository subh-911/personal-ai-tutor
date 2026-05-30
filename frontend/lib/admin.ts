export type IngestStatusValue = "processing" | "completed" | "failed";

// Phase 10 — finer-grained pipeline stage written by the ARQ worker. Null on
// rows ingested before the column existed; surfaces as a label in the UI while
// `status === "processing"`.
export type IngestStage =
  | "queued"
  | "chunking"
  | "embedding"
  | "persisting"
  | "completed"
  | "failed";

export type IngestStatus = {
  id: string;
  status: IngestStatusValue;
  stage: IngestStage | null;
  chunk_count: number;
  title: string | null;
  error: string | null;
};

export type UploadProgress = (loaded: number, total: number) => void;
export type GetToken = () => Promise<string | null>;

const INGEST_UPLOAD_URL = "/api/backend/ingest/upload";
const INGEST_SCRAPE_URL = "/api/backend/ingest/scrape";
const DOCUMENTS_URL = "/api/backend/documents";

export type Document = {
  id: string;
  source_type: string;
  source_uri: string;
  title: string | null;
  status: IngestStatusValue;
  // Phase 10 — populated by the ARQ worker while `status === "processing"`.
  // Null on rows ingested before the column existed.
  stage: IngestStage | null;
  chunk_count: number;
  created_at: string;
};

// We use XMLHttpRequest (not fetch) because `xhr.upload.onprogress` is the only
// browser API that exposes byte-level upload progress. The route's response is
// a normal JSON IngestStatus, not SSE.
export async function uploadFile(
  file: File,
  title: string | null,
  onProgress?: UploadProgress,
  getToken?: GetToken,
): Promise<IngestStatus> {
  const token = getToken ? await getToken() : null;
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", file);
    if (title) form.append("title", title);

    const xhr = new XMLHttpRequest();
    xhr.open("POST", INGEST_UPLOAD_URL);
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);

    if (onProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress(e.loaded, e.total);
      };
    }

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText) as IngestStatus);
        } catch (err) {
          reject(new Error(`could not parse server response: ${String(err)}`));
        }
      } else {
        let detail = xhr.statusText;
        try {
          const body = JSON.parse(xhr.responseText);
          if (body?.detail) detail = body.detail;
        } catch {
          // ignore
        }
        reject(new Error(`upload failed (${xhr.status}): ${detail}`));
      }
    };
    xhr.onerror = () => reject(new Error("network error during upload"));
    xhr.onabort = () => reject(new Error("upload aborted"));

    xhr.send(form);
  });
}

export async function scrapeUrl(url: string, getToken?: GetToken): Promise<IngestStatus> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const token = getToken ? await getToken() : null;
  if (token) headers.Authorization = `Bearer ${token}`;

  const res = await fetch(INGEST_SCRAPE_URL, {
    method: "POST",
    headers,
    body: JSON.stringify({ url }),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body?.detail) detail = body.detail;
    } catch {
      // ignore
    }
    throw new Error(`scrape failed (${res.status}): ${detail}`);
  }
  return (await res.json()) as IngestStatus;
}

async function authHeaders(getToken?: GetToken): Promise<Record<string, string>> {
  const token = getToken ? await getToken() : null;
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function detailOrStatus(res: Response): Promise<string> {
  let detail = res.statusText;
  try {
    const body = await res.json();
    if (body?.detail) detail = body.detail;
  } catch {
    // ignore
  }
  return detail;
}

export async function listDocuments(getToken?: GetToken): Promise<Document[]> {
  const res = await fetch(DOCUMENTS_URL, { headers: await authHeaders(getToken) });
  if (!res.ok) {
    throw new Error(`list documents failed (${res.status}): ${await detailOrStatus(res)}`);
  }
  return (await res.json()) as Document[];
}

export async function deleteDocument(id: string, getToken?: GetToken): Promise<void> {
  const res = await fetch(`${DOCUMENTS_URL}/${id}`, {
    method: "DELETE",
    headers: await authHeaders(getToken),
  });
  if (!res.ok) {
    throw new Error(`delete document failed (${res.status}): ${await detailOrStatus(res)}`);
  }
}

const INGEST_STATUS_URL = "/api/backend/ingest";

/**
 * Phase 10 — poll `/ingest/{id}` until the worker reports a terminal status.
 *
 * Resolves with the final status (`completed` or `failed`). The optional
 * `onUpdate` callback fires on every fetched status — including the first one
 * — so the UI can drive a live "stage" label without waiting for the terminal
 * value. Rejects if the poll runs past `timeoutMs` (default 5 min) or hits a
 * non-200 response, with the last error preserved.
 */
export async function pollIngestStatus(
  id: string,
  onUpdate: (s: IngestStatus) => void,
  getToken?: GetToken,
  opts: { intervalMs?: number; timeoutMs?: number } = {},
): Promise<IngestStatus> {
  const intervalMs = opts.intervalMs ?? 1500;
  const timeoutMs = opts.timeoutMs ?? 5 * 60 * 1000;
  const deadline = Date.now() + timeoutMs;

  const fetchOnce = async (): Promise<IngestStatus> => {
    const res = await fetch(`${INGEST_STATUS_URL}/${id}`, {
      headers: await authHeaders(getToken),
    });
    if (!res.ok) {
      throw new Error(`status poll failed (${res.status}): ${await detailOrStatus(res)}`);
    }
    return (await res.json()) as IngestStatus;
  };

  while (Date.now() < deadline) {
    const status = await fetchOnce();
    onUpdate(status);
    if (status.status === "completed" || status.status === "failed") {
      return status;
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  throw new Error(`ingest ${id} did not reach a terminal status within ${timeoutMs / 1000}s`);
}
