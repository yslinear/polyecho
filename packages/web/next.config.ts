import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Required for Docker: infra/Dockerfile copies .next/standalone + public
  // Run with: node .next/standalone/server.js (not `next start`)
  output: "standalone",
};

export default nextConfig;
