import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";
import { NextResponse } from "next/server";

// Phase 8 — protect the chat + admin pages and the rewrite proxy to FastAPI.
const isProtectedRoute = createRouteMatcher([
  "/chat(.*)",
  "/admin(.*)",
  "/api/backend/(.*)",
]);

// Test-mode bypass: in Playwright we skip Clerk entirely. The Clerk client SDK
// needs a real publishable key pointing at a reachable Clerk Frontend API to
// initialize, which the hermetic test environment doesn't have. The bypass
// flag matches what `app/layout.tsx` and `app/page.tsx` use to skip rendering
// Clerk components — they're consistent across the stack.
const testBypass = process.env.NEXT_PUBLIC_TEST_DISABLE_AUTH === "1";

export default testBypass
  ? function passthrough() {
      return NextResponse.next();
    }
  : clerkMiddleware(async (auth, req) => {
      if (isProtectedRoute(req)) {
        await auth.protect();
      }
    });

export const config = {
  matcher: [
    // Skip Next.js internals and all static files, unless found in search params.
    "/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)",
    // Always run for the rewrite proxy
    "/api/backend/(.*)",
  ],
};
