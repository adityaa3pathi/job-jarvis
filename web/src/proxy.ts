import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

// Proves to the API that a request came through here, so it may trust the forwarded
// client address. Read at runtime on the server; never sent to the browser.
const HEADER = "x-jobagent-proxy";

export function proxy(request: NextRequest) {
  const headers = new Headers(request.headers);
  headers.delete(HEADER); // a browser must never supply its own
  const secret = process.env.JOBAGENT_PROXY_SECRET;
  if (secret) headers.set(HEADER, secret);
  return NextResponse.next({ request: { headers } });
}

export const config = { matcher: "/api/:path*" };
