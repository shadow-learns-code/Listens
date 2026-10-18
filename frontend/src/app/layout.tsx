import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import GlassPreference from "@/components/GlassPreference";
import InstallCapture from "@/components/InstallCapture";
import ServiceWorker from "@/components/ServiceWorker";
import WakingBanner from "@/components/WakingBanner";
import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Shadow Listens",
  description: "Your private music streaming server",
  applicationName: "Shadow Listens",
  manifest: "/manifest.webmanifest",
  // iPhone "Add to Home Screen": opens full screen with its own icon (Android reads the manifest instead)
  appleWebApp: { capable: true, title: "Shadow Listens", statusBarStyle: "black-translucent" },
  icons: {
    icon: [
      { url: "/favicon.svg", type: "image/svg+xml" },
      { url: "/icons/favicon-32.png", sizes: "32x32", type: "image/png" },
    ],
    apple: "/icons/apple-touch-icon.png",
  },
  formatDetection: { telephone: false },
};
// viewportFit "cover": use the whole screen on phones with a notch or gesture bar; the layout pads itself with env(safe-area-inset-*)
export const viewport: Viewport = { themeColor: "#0c0d11", width: "device-width", initialScale: 1, viewportFit: "cover" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable}`}>
      <body>
        <GlassPreference />
        <ServiceWorker />
        <InstallCapture />
        <WakingBanner />
        {children}
      </body>
    </html>
  );
}
