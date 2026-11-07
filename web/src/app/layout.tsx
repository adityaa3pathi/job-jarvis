import type { Metadata, Viewport } from "next";
import { Manrope } from "next/font/google";
import { Suspense, type ReactNode } from "react";
import { Nav } from "@/components/Nav";
import "./globals.css";

export const metadata: Metadata = {
  title: "Job Agent",
  description: "Find jobs, get coached on fit, build tailored resumes.",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f5f6f8" },
    { media: "(prefers-color-scheme: dark)", color: "#0d1014" },
  ],
};

const sans = Manrope({ subsets: ["latin"], display: "swap", variable: "--font-manrope" });

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${sans.variable} h-full antialiased`}>
      <body className="flex min-h-full flex-col bg-bg text-ink">
        <Suspense>
          <Nav />
        </Suspense>
        <main className="mx-auto w-full max-w-[1360px] flex-1 px-4 pt-5 pb-16 sm:px-6">{children}</main>
      </body>
    </html>
  );
}
