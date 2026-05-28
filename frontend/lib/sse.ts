import { CHAT_ENDPOINT, type ChatPayload } from "./api";

export type StreamEvent =
  | { kind: "session"; sessionId: string }
  | { kind: "delta"; text: string }
  | { kind: "done" };

export async function* streamChat(
  payload: ChatPayload,
  signal?: AbortSignal,
): AsyncGenerator<StreamEvent, void, void> {
  const res = await fetch(CHAT_ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal,
  });
  if (!res.ok) {
    throw new Error(`chat request failed: ${res.status} ${res.statusText}`);
  }

  const sessionId = res.headers.get("X-Session-Id");
  if (sessionId) yield { kind: "session", sessionId };

  if (!res.body) {
    yield { kind: "done" };
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let sep: number;
      while ((sep = buffer.indexOf("\n\n")) >= 0) {
        const event = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        if (!event.startsWith("data: ")) continue;

        const data = event.slice("data: ".length);
        if (data === "[DONE]") {
          yield { kind: "done" };
          return;
        }
        try {
          const parsed = JSON.parse(data) as { delta?: string };
          if (typeof parsed.delta === "string") {
            yield { kind: "delta", text: parsed.delta };
          }
        } catch {
          // ignore malformed event; the next read may complete the JSON
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}
