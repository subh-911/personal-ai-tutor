import Link from "next/link";

export default function Home() {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-6 p-12 text-center">
      <h1 className="text-4xl font-semibold tracking-tight">Personal AI Tutor</h1>
      <p className="max-w-prose text-zinc-600 dark:text-zinc-400">
        Ask questions, get explanations, and quiz yourself on the material you&apos;ve ingested.
      </p>
      <div className="flex flex-wrap items-center justify-center gap-3">
        <Link
          href="/chat"
          className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
        >
          Open chat →
        </Link>
        <Link
          href="/admin"
          className="rounded-md border border-zinc-300 px-4 py-2 text-sm font-medium hover:bg-zinc-50 dark:border-zinc-700 dark:hover:bg-zinc-900"
        >
          Admin · ingestion
        </Link>
        <a
          className="rounded-md border border-zinc-300 px-4 py-2 text-sm font-medium hover:bg-zinc-50 dark:border-zinc-700 dark:hover:bg-zinc-900"
          href="http://localhost:8000/docs"
          target="_blank"
          rel="noreferrer"
        >
          API docs
        </a>
      </div>
    </main>
  );
}
