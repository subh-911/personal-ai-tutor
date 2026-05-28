"use client";

import { useState, type FormEvent, type KeyboardEvent } from "react";

import type { ForceRoute } from "@/lib/api";

type Props = {
  disabled: boolean;
  onSend: (message: string, forceRoute?: ForceRoute) => void;
};

export function ChatInput({ disabled, onSend }: Props) {
  const [value, setValue] = useState("");

  const send = (forceRoute?: ForceRoute) => {
    const trimmed = value.trim();
    let composed: string;
    if (forceRoute === "tutor") {
      composed = trimmed ? `Give me an example of: ${trimmed}` : "Give me an example.";
    } else if (forceRoute === "quiz") {
      composed = trimmed ? `Test my knowledge on: ${trimmed}` : "Test my knowledge.";
    } else {
      if (!trimmed) return;
      composed = trimmed;
    }
    onSend(composed, forceRoute);
    setValue("");
  };

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    send();
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  return (
    <form
      onSubmit={onSubmit}
      className="flex flex-col gap-2 border-t border-zinc-200 bg-white p-3 dark:border-zinc-800 dark:bg-zinc-950"
    >
      <textarea
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={onKeyDown}
        rows={2}
        placeholder="Ask a question…"
        disabled={disabled}
        aria-label="Message"
        className="w-full resize-none rounded-lg border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-900 outline-none focus:border-blue-500 disabled:opacity-60 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100"
      />
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="submit"
          disabled={disabled || !value.trim()}
          className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
        >
          Send
        </button>
        <button
          type="button"
          onClick={() => send("tutor")}
          disabled={disabled}
          className="rounded-md border border-zinc-300 px-3 py-2 text-sm font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-900"
        >
          Give me an example
        </button>
        <button
          type="button"
          onClick={() => send("quiz")}
          disabled={disabled}
          className="rounded-md border border-zinc-300 px-3 py-2 text-sm font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-900"
        >
          Test my knowledge
        </button>
      </div>
    </form>
  );
}
