"use client";

/**
 * BacktestPnlChart — shadcn chart (Recharts) for PnL curves.
 * Renders 1–3 scenario lines depending on what result contains.
 */

import {
  Line,
  LineChart,
  CartesianGrid,
  ReferenceLine,
  XAxis,
  YAxis,
} from "recharts";
import {
  ChartContainer,
  ChartLegend,
  ChartLegendContent,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import { t } from "@/lib/i18n";

interface DayPoint {
  date: string;
  pnl_usd: number;
}

interface ScenarioResult {
  label: "optimistic" | "neutral" | "pessimistic";
  slippage_bps: number;
  delay_seconds: number;
  pnl_curve: DayPoint[];
}

interface BacktestPnlChartProps {
  scenarios: ScenarioResult[];
}

// Scenario lines map to the design tokens (DESIGN.md "Charts"):
// optimistic = best case (--gain), pessimistic = worst case (--loss),
// neutral = the calm accent (--primary). Never raw hex / category neon.
const COLORS: Record<string, string> = {
  optimistic: "hsl(var(--gain))",
  neutral: "hsl(var(--primary))",
  pessimistic: "hsl(var(--loss))",
};

/** Calm honest empty state — one muted sentence, no axes box. */
function PnlEmpty() {
  return (
    <div className="flex h-[280px] w-full items-center justify-center rounded-md border border-dashed border-border">
      <p className="text-sm text-muted-foreground">
        {t("backtest.result.pnlEmpty")}
      </p>
    </div>
  );
}

export function BacktestPnlChart({ scenarios }: BacktestPnlChartProps) {
  if (!scenarios.length) {
    return <PnlEmpty />;
  }

  // Build a merged date-keyed dataset so all scenarios align on XAxis
  const dateSet = new Set<string>();
  for (const s of scenarios) {
    for (const p of s.pnl_curve) {
      dateSet.add(p.date);
    }
  }
  const dates = Array.from(dateSet).sort();

  const mergedData = dates.map((date) => {
    const row: Record<string, string | number> = { date };
    for (const s of scenarios) {
      const point = s.pnl_curve.find((p) => p.date === date);
      row[s.label] = point?.pnl_usd ?? 0;
    }
    return row;
  });

  // P1.2: 3 scenarios can exist while EVERY `pnl_curve` is empty (adapted
  // legacy rows). `!scenarios.length` above passes, but Recharts would
  // then render a bare axes box with no curve. Surface the honest empty
  // state instead of an empty chart.
  if (mergedData.length === 0) {
    return <PnlEmpty />;
  }

  const chartConfig: ChartConfig = Object.fromEntries(
    scenarios.map((s) => [
      s.label,
      {
        label: `${s.label.charAt(0).toUpperCase()}${s.label.slice(1)} (${s.slippage_bps} bps / ${s.delay_seconds} s)`,
        color: COLORS[s.label] ?? "hsl(var(--primary))",
      },
    ])
  );

  return (
    <ChartContainer config={chartConfig} className="h-[280px] w-full">
      <LineChart
        data={mergedData}
        margin={{ top: 8, right: 16, left: 0, bottom: 0 }}
      >
        <CartesianGrid
          strokeDasharray="3 3"
          vertical={false}
          className="stroke-border"
          strokeOpacity={0.5}
        />
        <XAxis
          dataKey="date"
          tick={{ fontSize: 11 }}
          tickLine={false}
          axisLine={false}
          tickFormatter={(v: string) => v.slice(5)}
        />
        <YAxis
          tick={{ fontSize: 11 }}
          width={52}
          tickLine={false}
          axisLine={false}
          className="tabular-nums"
          tickFormatter={(v: number) => `$${v.toFixed(0)}`}
        />
        <ReferenceLine
          y={0}
          className="stroke-border"
          strokeOpacity={0.8}
          label={{
            value: "$0",
            position: "insideLeft",
            fontSize: 11,
            className: "fill-muted-foreground tabular-nums",
          }}
        />
        <ChartTooltip content={<ChartTooltipContent />} />
        <ChartLegend content={<ChartLegendContent />} />
        {scenarios.map((s) => (
          <Line
            key={s.label}
            type="monotone"
            dataKey={s.label}
            stroke={`var(--color-${s.label})`}
            strokeWidth={2}
            dot={false}
            connectNulls
          />
        ))}
      </LineChart>
    </ChartContainer>
  );
}
