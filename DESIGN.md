# DESIGN.md — PolyEcho

Register: **product** (design serves the instrument). Color strategy:
**Restrained** — tinted neutrals + one calm accent; PnL semantics are the only
other saturated color, always reinforced by sign/arrow/figure (never
color-only). Theme: **light-primary with a first-class dark mode**. Scene:
*a solo trader reading copy-trade PnL on a 27" Mac in a daylit home office;
glancing at alerts on iPhone at night.* Daylight forces light-default; dark
is real, not an afterthought. Explicitly NOT crypto-neon-on-black, NOT
fintech navy/gold (both are category reflexes — banned).

Implementation: shadcn/ui CSS variables in `app/globals.css`
(`--token: H S% L%`, consumed via Tailwind `hsl(var(--token))`). Keep the
existing var mechanism + Tailwind 3.4 config; **retune the values** to the
palette below. Never `#000`/`#fff`; every neutral is tinted ~215° (cool
slate-azure, S 12–35%).

## Color tokens (HSL channels — light / dark)

| Token | Light | Dark | Use |
|---|---|---|---|
| `--background` | `215 30% 98%` | `222 38% 8%` | app bg (tinted, not white/black) |
| `--foreground` | `222 30% 13%` | `215 22% 90%` | body text |
| `--card` | `215 36% 99%` | `222 32% 11%` | raised surface (sparingly) |
| `--muted` | `215 24% 95%` | `222 24% 16%` | subtle fill |
| `--muted-foreground` | `215 12% 42%` | `215 14% 60%` | secondary text (≥4.5:1) |
| `--border` | `215 20% 88%` | `215 16% 22%` | hairlines (full borders only) |
| `--primary` | `205 80% 41%` | `200 74% 58%` | THE accent, ≤10% surface |
| `--primary-foreground` | `210 40% 99%` | `222 40% 10%` | on-accent |
| `--accent` | `205 50% 93%` | `205 40% 20%` | hover/active wash |
| `--ring` | `205 80% 45%` | `200 74% 58%` | focus ring (visible, 2px) |
| `--gain` | `158 62% 36%` | `158 52% 52%` | positive PnL |
| `--loss` | `8 68% 49%` | `8 70% 62%` | negative PnL |

Add `--gain`/`--loss` as new vars + Tailwind colors `gain`/`loss`. PnL is
ALWAYS `sign + arrow + tabular figure`, color is reinforcement only
(color-blind safe). Status/strategy/risk use Badge variants (text + icon),
not raw color.

## Typography

Inter (already loaded). Scale (rem): 0.75 meta · 0.8125 small · 0.875 body ·
1 · 1.25 · 1.5 · 2. Weights: 400 body, 500 labels/nav, 600 section, 700 page
title. Line-height 1.5 body, 1.2 headings. **`font-variant-numeric:
tabular-nums` on every metric, table numeric cell, price, score, countdown.**
Body measure ≤ 72ch.

## Layout & rhythm

4/8 spacing base; **vary** section gaps (12 / 20 / 32) — no uniform padding.
No global max-width wrapper; pages own their width. Cards are the lazy answer:
use a surface only when it's the right affordance; **never nested cards**;
**no identical card grid**. `/` is an information-led overview with ONE clear
focal point (today realized/unrealized PnL + open risk) then supporting data
in a varied grid — **the hero-metric template is banned**. Sidebar app-shell
already exists; keep it, retune to tokens, ensure active state + collapse.

## Motion

150–220ms, `cubic-bezier(0.22,1,0.36,1)` (ease-out, no bounce). Animate only
opacity/transform. Honor `prefers-reduced-motion`. Skeletons (not spinners)
for >300ms loads; reserve space (no layout shift).

## Components

shadcn primitives only; consistent across pages. Tables: sticky header,
right-aligned tabular numerics, row hover (`bg-muted/50`), sortable headers
with `aria-sort`, real empty state (one calm sentence + the actual next
action, e.g. "No candidates yet. Run discovery."). Forms: visible labels,
inline validation on blur, error below field, primary action singular.
Charts (Recharts via shadcn `chart`): use `--gain/--loss/--primary`,
gridlines at `--border`, no heavy gradients/shadows, empty + loading states.

## Absolute bans (enforced in review)

No side-stripe accent borders. No gradient text. No decorative
glassmorphism. No hero-metric template. No identical card grids. No
modal-first. No em dashes in UI copy. If tempted, restructure instead.

## Accessibility

Contrast ≥4.5:1 (text) / ≥3:1 (large/UI glyph) in BOTH themes. Visible 2px
focus ring (`--ring`). Icon-only buttons get `aria-label`. Keyboard order =
visual order. Color never the sole signal.
