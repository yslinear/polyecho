import type { NextConfig } from "next";
import path from "node:path";

const nextConfig: NextConfig = {
  // Required for Docker: infra/docker/Dockerfile.web copies .next/standalone.
  // In this pnpm monorepo the build runs from packages/web, so without an
  // explicit tracing root Next.js emits standalone/server.js at the bundle
  // root AND misses workspace-hoisted node_modules. Pinning the tracing root
  // to the repo root makes Next emit standalone/packages/web/server.js with a
  // correctly-traced node_modules — matching Dockerfile.web's COPY + CMD.
  output: "standalone",
  outputFileTracingRoot: path.join(__dirname, "../../"),
};

export default nextConfig;
