/**
 * Zero-dependency i18n (single fixed locale: zh-TW).
 *
 * Why not next-intl / next-i18next: there is exactly one locale and no
 * user locale switching — a provider/context/middleware stack is pure
 * overhead. A module-level const dictionary + a sync `t()` is usable
 * verbatim in both Server Components (most pages) and Client Components
 * (e.g. the sidebar) with zero runtime cost (tree-shaken by Next).
 *
 * Type-safety: `I18nKey` is the compile-time union of every dotted leaf
 * path in `dict`, so `t('status.title')` is checked and a typo fails the
 * build. Missing key → returns the key itself (visible, never throws) —
 * matching the project's defensive "degrade, don't crash" ethos.
 *
 * Interpolation: `{{name}}` placeholders, filled from `vars`.
 */
import { dict } from "./locale/zh-TW";

type Join<P extends string, K extends string> = P extends ""
  ? K
  : `${P}.${K}`;

type Leaves<T, P extends string = ""> = T extends string
  ? P
  : {
      [K in keyof T & string]: Leaves<T[K], Join<P, K>>;
    }[keyof T & string];

export type I18nKey = Leaves<typeof dict>;

export function t(
  key: I18nKey,
  vars?: Record<string, string | number>,
): string {
  const raw = (key as string)
    .split(".")
    .reduce<unknown>(
      (o, k) =>
        o == null ? o : (o as Record<string, unknown>)[k],
      dict,
    );
  let out = typeof raw === "string" ? raw : (key as string);
  if (vars) {
    for (const [k, v] of Object.entries(vars)) {
      out = out.split(`{{${k}}}`).join(String(v));
    }
  }
  return out;
}
