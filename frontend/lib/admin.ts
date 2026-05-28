export type IngestStatusValue = "processing" | "completed" | "failed";

export type IngestStatus = {
  id: string;
  status: IngestStatusValue;
  chunk_count: number;
  title: string | null;
  error: string | null;
};

export type UploadProgress = (loaded: number, total: number) => void;

const INGEST_UPLOAD_URL = "/api/backend/ingest/upload";
const INGEST_SCRAPE_URL = "/api/backend/ingest/scrape";

// We use XMLHttpRequest (not fetch) because `xhr.upload.onprogress` is the only
// browser API that exposes byte-level upload progress. The route's response is
// a normal JSON IngestStatus, not SSE.
export function uploadFile(
  file: File,
  title: string | null,
  onProgress?: UploadProgress,
): Promise<IngestStatus> {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", file);
    if (title) form.append("title", title);

    const xhr = new XMLHttpRequest();
    xhr.open("POST", INGEST_UPLOAD_URL);

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

export async function scrapeUrl(url: string): Promise<IngestStatus> {
  const res = await fetch(INGEST_SCRAPE_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
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
