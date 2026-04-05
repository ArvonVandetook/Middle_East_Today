import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata = {
  title: "Middle East Today",
  description:
    "A daily brief on the Middle East, surfacing the most important developments and analysis from around the region.",
  openGraph: {
    title: "Middle East Today",
    description:
      "A daily intelligence brief on the Middle East, surfacing the most important developments and analysis from around the region.",
    url: "https://middle-east-today.vercel.app",
    siteName: "Middle East Today",
    type: "website",
  },
};
export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}
      >
        {children}
      </body>
    </html>
  );
}
