"use client";

import { useCallback, useEffect, useState } from "react";

import {
  SESSION_STORAGE_KEY,
  type ForceRoute,
  type Message,
} from "@/lib/api";
import { useBackendToken } from "@/lib/auth-token";
import { streamChat } from "@/lib/sse";

import { ChatInput } from "./ChatInput";
import { MessageList } from "./MessageList";

function makeId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
}

export function Chat() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const getToken = useBackendToken();

  useEffect(() => {
    if (typeof window === "undefined") return;
    const stored = window.localStorage.getItem(SESSION_STORAGE_KEY);
    if (stored) setSessionId(stored);
  }, []);

  useEffect(() => {
    if (typeof window === "undefined" || !sessionId) return;
    window.localStorage.setItem(SESSION_STORAGE_KEY, sessionId);
  }, [sessionId]);

  const send = useCallback(
    async (text: string, forceRoute?: ForceRoute) => {
      setError(null);
      const userId = makeId();
      const assistantId = makeId();

      setMessages((prev) => [
        ...prev,
        { id: userId, role: "user", content: text },
        { id: assistantId, role: "assistant", content: "" },
      ]);
      setStreaming(true);

      try {
        for await (const event of streamChat(
          {
            message: text,
            session_id: sessionId ?? undefined,
            force_route: forceRoute,
          },
          undefined,
          getToken,
        )) {
          if (event.kind === "session") {
            setSessionId(event.sessionId);
          } else if (event.kind === "delta") {
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantId ? { ...m, content: m.content + event.text } : m,
              ),
            );
          }
        }
      } catch (err) {
        const detail = err instanceof Error ? err.message : String(err);
        setError(detail);
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId ? { ...m, content: `_Error: ${detail}_` } : m,
          ),
        );
      } finally {
        setStreaming(false);
      }
    },
    [sessionId, getToken],
  );

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center justify-between border-b border-zinc-200 px-4 py-3 dark:border-zinc-800">
        <h1 className="text-lg font-semibold">Personal AI Tutor</h1>
        {sessionId ? (
          <code
            data-testid="session-id"
            className="rounded bg-zinc-100 px-2 py-1 text-xs text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400"
            title="Session id (stored in localStorage)"
          >
            {sessionId.slice(0, 8)}…
          </code>
        ) : null}
      </header>
      <MessageList messages={messages} />
      {error ? (
        <div className="border-t border-red-300 bg-red-50 px-4 py-2 text-sm text-red-700 dark:border-red-800 dark:bg-red-950 dark:text-red-200">
          {error}
        </div>
      ) : null}
      <ChatInput disabled={streaming} onSend={send} />
    </div>
  );
}
