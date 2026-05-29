import { ClerkProvider } from "@clerk/nextjs";
import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { Toaster } from "sonner";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Personal AI Tutor",
  description: "Streaming, RAG-backed AI tutor with retrieval-grounded answers and quizzes.",
};

// In test-bypass mode (Playwright) we skip ClerkProvider — its client SDK needs
// a reachable Clerk Frontend API at boot, which the hermetic test environment
// doesn't have. middleware.ts + page.tsx use the same flag to stay consistent.
const skipClerk = process.env.NEXT_PUBLIC_TEST_DISABLE_AUTH === "1";

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  const body = (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col">
        {children}
        <Toaster richColors position="top-right" />
      </body>
    </html>
  );

  return skipClerk ? body : <ClerkProvider>{body}</ClerkProvider>;
}
