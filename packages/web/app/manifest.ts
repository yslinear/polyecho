/**
 * PWA manifest — Next 15 App Router idiomatic `app/manifest.ts`.
 *
 * This REPLACES the old Phase-5-stub public/manifest.json (deleted) so there
 * is exactly one source of truth and exactly one <link rel="manifest"> (Next
 * injects it automatically from this route; the hard-coded link in
 * app/layout.tsx was removed).
 *
 * Colors are the DESIGN.md tokens converted to sRGB (the design system uses
 * the HSL `--token` mechanism; PWA manifest needs concrete colors):
 *   background_color = --background light  hsl(215 30% 98%) -> #f6f8fb
 *   theme_color      = --primary           hsl(205 80% 41%) -> #1481bd
 *
 * iOS Safari 16.4+ only delivers Web Push when the app is installed to the
 * Home Screen, so `display: standalone` is required for the push feature to
 * work at all on iPhone (the /settings install hint explains this to users).
 */

import type { MetadataRoute } from "next";

export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "PolyEcho",
    short_name: "PolyEcho",
    description: "個人 Polymarket 跟單監控與警示",
    start_url: "/",
    scope: "/",
    display: "standalone",
    background_color: "#f6f8fb",
    theme_color: "#1481bd",
    icons: [
      {
        src: "/icons/icon-192.png",
        sizes: "192x192",
        type: "image/png",
        purpose: "any",
      },
      {
        src: "/icons/icon-512.png",
        sizes: "512x512",
        type: "image/png",
        purpose: "any",
      },
      {
        src: "/icons/icon-512.png",
        sizes: "512x512",
        type: "image/png",
        purpose: "maskable",
      },
    ],
  };
}
