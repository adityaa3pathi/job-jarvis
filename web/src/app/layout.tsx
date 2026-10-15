import type { Metadata } from "next";
import { Cinzel_Decorative, EB_Garamond } from "next/font/google";
import { Suspense, type ReactNode } from "react";
import { Nav } from "@/components/Nav";
import "./globals.css";

export const metadata: Metadata = {
  title: "Job Agent",
  description: "Find jobs, get coached on fit, build tailored resumes.",
  robots: { index: false, follow: false },
};

// Parchment faces; the plain theme never references these variables.
const display = Cinzel_Decorative({ weight: "700", subsets: ["latin"], display: "swap", variable: "--font-cinzel" });
const body = EB_Garamond({ subsets: ["latin"], display: "swap", variable: "--font-garamond" });

// Reads the theme cookie in the browser: the layout must not call cookies() under cacheComponents.
const THEME_SCRIPT =
  '(function(){try{var m=document.cookie.match(/(?:^|; )theme=(plain|parchment)(?:;|$)/);if(m)document.documentElement.dataset.theme=m[1]}catch(e){}})()';

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${display.variable} ${body.variable} h-full antialiased`} suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body className="flex min-h-full flex-col bg-surface text-ink">
        <Suspense>
          <Nav />
        </Suspense>
        <main className="mx-auto w-full max-w-5xl flex-1 px-4 py-6">{children}</main>
      </body>
    </html>
  );
}
