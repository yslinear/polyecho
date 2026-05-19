/**
 * Tiny isolation seam for the optional `undici` `ProxyAgent`.
 *
 * Why a separate module (NOT inlined into balance.ts):
 *  - balance.ts is a `"use server"` module → it may export ONLY async
 *    functions. Keeping the undici import here lets the test mock THIS
 *    module by its local path, so the test runner never has to resolve
 *    the bare `undici` specifier itself.
 *  - The Next.js server runtime bundles undici (Next polyfills `fetch`
 *    with it), so `import("undici")` resolves under `next build`.
 *  - Plain Node does not expose `undici` as a resolvable bare specifier;
 *    if the import ever fails (non-Next host, missing module) this fails
 *    SOFT — the caller simply makes a direct (un-proxied) request, which
 *    is acceptable for a strictly best-effort public read.
 *
 * Only http(s) proxies are honoured (matches the settings-side SSRF policy
 * in app/settings/actions.ts). Returns `null` whenever a proxy dispatcher
 * cannot or should not be built — never throws.
 */

/**
 * Build an undici `ProxyAgent` for `proxyUrl`, or `null` if it is not an
 * http(s) URL or undici is unavailable. Never throws.
 */
export async function makeProxyDispatcher(
  proxyUrl: string
): Promise<unknown | null> {
  if (!/^https?:\/\//i.test(proxyUrl)) return null;
  try {
    // The specifier is assembled at runtime so NO static bundler analysis
    // (Vite/Rollup in vitest, Turbopack/webpack in `next build`) folds it
    // to a literal it would try to pre-resolve/fail on. It stays a true
    // runtime dynamic import: Node's `fetch` is undici, so the Next.js
    // server runtime resolves "undici"; any other host fails the import
    // and we fall through to `null` (fail-soft — direct request instead).
    const spec = ["un", "dici"].join("");
    const dynImport = new Function(
      "s",
      "return import(s)"
    ) as (s: string) => Promise<unknown>;
    const mod = (await dynImport(spec)) as {
      ProxyAgent?: new (u: string) => unknown;
    };
    const ProxyAgent = mod.ProxyAgent;
    if (typeof ProxyAgent !== "function") return null;
    return new ProxyAgent(proxyUrl);
  } catch {
    return null;
  }
}
