import type { NextConfig } from "next";
import { PHASE_PRODUCTION_BUILD } from "next/constants";

// The browser only talks to this app; /api is proxied so the login cookie stays first-party.
// A build without API_URL would ship rewrites to localhost, so it fails here instead.
export default function config(phase: string): NextConfig {
  const apiUrl = process.env.API_URL;
  if (!apiUrl && phase === PHASE_PRODUCTION_BUILD) {
    throw new Error("API_URL is not set: point it at the API (e.g. the Render URL) and build again.");
  }
  return {
    cacheComponents: true,
    partialPrefetching: true,
    turbopack: {
      rules: {
        "*.css": {
          loaders: ["@tailwindcss/turbopack"],
          as: "*.css",
        },
      },
    },
    async rewrites() {
      const base = apiUrl ?? "http://127.0.0.1:8000";
      return [{ source: "/api/:path*", destination: `${base}/api/:path*` }];
    },
  };
}
