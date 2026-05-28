"use client";

import { useEffect, useRef } from "react";

import type { Message as Msg } from "@/lib/api";

import { Message } from "./Message";

type Props = {
  messages: Msg[];
};

export function MessageList({ messages }: Props) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  return (
    <div
      ref={ref}
      data-testid="message-list"
      className="flex-1 overflow-y-auto px-2 py-4"
    >
      <div className="mx-auto flex max-w-3xl flex-col gap-3">
        {messages.length === 0 ? (
          <p className="text-center text-zinc-500">
            Ask a question, or use the action buttons to force a tutor explanation or a knowledge check.
          </p>
        ) : (
          messages.map((m) => <Message key={m.id} role={m.role} content={m.content} />)
        )}
      </div>
    </div>
  );
}
