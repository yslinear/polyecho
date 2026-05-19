import { dirname } from "path";
import { fileURLToPath } from "url";
import { FlatCompat } from "@eslint/eslintrc";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const compat = new FlatCompat({
  baseDirectory: __dirname,
});

/**
 * i18n regression guard (i18n hardening punch-list, P2-4).
 *
 * Every user-facing string in this app is single-locale zh-TW and MUST go
 * through `t()` from `@/lib/i18n` (rendered as `{t("…")}`, a
 * JSXExpressionContainer — NOT JSXText). A literal run of 3+ Latin letters
 * sitting directly in JSX as a *text node* is therefore a hardcoded English
 * string that bypassed the dictionary — exactly the residual-English class
 * this punch-list removed.
 *
 * The esquery selector matches the raw `JSXText` node value: pure
 * whitespace / newlines / punctuation / HTML entities between elements have
 * no 3-letter Latin run and never trip. Proper-noun brand text (PolyEcho,
 * PE) is also routed through `t()` in this codebase, so the existing tree
 * passes clean. If a future genuine brand literal must live inline, add a
 * scoped `// eslint-disable-next-line no-restricted-syntax -- <reason>`.
 *
 * Scope: our app/ + components/ TSX. Excluded: __tests__ (assert real
 * rendered copy), and components/ui/** (vendored shadcn primitives — their
 * sr-only "Close"/"Toggle Sidebar" labels are upstream, not app copy; we
 * keep them verbatim to stay diff-clean against shadcn updates).
 */
const noHardcodedJsxEnglish = {
  selector: "JSXText[value=/[A-Za-z]{3,}/]",
  message:
    "Hardcoded user-facing English in JSX. All copy is zh-TW via t() from " +
    "@/lib/i18n — add a key to lib/locale/zh-TW and render {t(\"…\")}. " +
    "(If this is an unavoidable inline brand literal, disable this rule on " +
    "the line with a clear reason.)",
};

const eslintConfig = [
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  {
    files: ["app/**/*.tsx", "components/**/*.tsx"],
    ignores: [
      "**/__tests__/**",
      "**/*.test.tsx",
      "components/ui/**",
    ],
    rules: {
      "no-restricted-syntax": ["error", noHardcodedJsxEnglish],
    },
  },
];

export default eslintConfig;
