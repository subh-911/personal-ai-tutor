"use client";

import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";

import type { Role } from "@/lib/api";

type Props = {
  role: Role;
  content: string;
};

export function Message({ role, content }: Props) {
  const isUser = role === "user";
  return (
    <div
      data-testid={isUser ? "user-message" : "assistant-message"}
      className={
        isUser
          ? "self-end max-w-[80%] rounded-2xl bg-blue-600 px-4 py-2 text-white"
          : "self-start max-w-[85%] rounded-2xl bg-zinc-100 px-4 py-3 text-zinc-900 dark:bg-zinc-800 dark:text-zinc-100"
      }
    >
      {isUser ? (
        <p className="whitespace-pre-wrap">{content}</p>
      ) : (
        <div className="prose-chat prose prose-sm dark:prose-invert max-w-none">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            rehypePlugins={[rehypeHighlight]}
          >
            {content || " "}
          </ReactMarkdown>
        </div>
      )}
    </div>
  );
}
