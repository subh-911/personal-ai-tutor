"use client";

import { useAuth } from "@clerk/nextjs";
import { useCallback } from "react";

/**
 * Phase 8 — returns a callable that resolves to the current Clerk session JWT.
 *
 * Components hand the callable to `streamChat` / `uploadFile` / `scrapeUrl`,
 * which invoke it immediately before issuing each request so the token is
 * always fresh (Clerk JWTs are short-lived; refresh is automatic).
 *
 * In test-bypass mode (NEXT_PUBLIC_TEST_DISABLE_AUTH=1) `<ClerkProvider>` is
 * not mounted, so `useAuth()` would throw. We swap in a no-op hook at module
 * load time — switching implementations based on a build-time constant keeps
 * the rules-of-hooks invariant for any individual component.
 */
const skipClerk = process.env.NEXT_PUBLIC_TEST_DISABLE_AUTH === "1";

const noopGetToken = async (): Promise<string | null> => null;

function useClerkBackendToken(): () => Promise<string | null> {
  const { getToken, isSignedIn } = useAuth();
  return useCallback(async () => {
    if (!isSignedIn) return null;
    return await getToken();
  }, [getToken, isSignedIn]);
}

function useNoopBackendToken(): () => Promise<string | null> {
  return useCallback(noopGetToken, []);
}

export const useBackendToken = skipClerk
  ? useNoopBackendToken
  : useClerkBackendToken;
