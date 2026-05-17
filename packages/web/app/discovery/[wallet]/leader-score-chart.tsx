"use client";

/**
 * LeaderScoreChart — shadcn chart (Recharts) wrapper for score history.
 * Client island fed by server-fetched ScorePoint array.
 *
 * DESIGN: the line is tinted by net trend so the direction is legible at a
 * glance without reading the axis — rising history uses `--gain`, falling
 * uses `--loss`, flat/insufficient uses the calm `--primary` accent. Color is
 * reinforcement: the y-axis and tooltip carry the real numbers. Gridlines sit
 * at the low-contrast `--border` token; no gradients or shadows.
 */

import * as React from "react";
import { Line, LineChart, CartesianGrid, XAxis, YAxis } from "recharts";
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import { Skeleton } from "@/components/ui/skeleton";
import type { ScorePoint } from "./page";

interface LeaderScoreChartProps {
  data: ScorePoint[];
  /** Render a reserved-space skeleton instead of the chart (>300ms loads). */
  loading?: boolean;
}

/** Net direction across the first and last non-null score point. */
function trend(data: ScorePoint[]): "up" | "down" | "flat" {
  const pts = data
    .map((d) => d.total_score)
    .filter((v): v is number => v !== null && Number.isFinite(v));
  if (pts.length < 2) return "flat";
  const delta = pts[pts.length - 1] - pts[0];
  if (delta > 0) return "up";
  if (delta < 0) return "down";
  return "flat";
}

export function LeaderScoreChart({ data, loading }: LeaderScoreChartProps) {
  if (loading) {
    // Reserve the exact chart footprint so there is no layout shift.
    return <Skeleton className="h-[260px] w-full" />;
  }

  if (data.length === 0) {
    return (
      <p className="py-6 text-center text-sm text-muted-foreground">
        No score history recorded yet.
      </p>
    );
  }

  const dir = trend(data);
  const stroke =
    dir === "up"
      ? "hsl(var(--gain))"
      : dir === "down"
        ? "hsl(var(--loss))"
        : "hsl(var(--primary))";

  const chartConfig: ChartConfig = {
    total_score: {
      label: "Total Score",
      color: stroke,
    },
  };

  const trendLabel =
    dir === "up"
      ? "trending up"
      : dir === "down"
        ? "trending down"
        : "flat";

  return (
    <figure className="space-y-2">
      <figcaption className="sr-only">
        Total score history over time, {trendLabel}.
      </figcaption>
      <ChartContainer config={chartConfig} className="h-[260px] w-full">
        <LineChart
          data={data}
          margin={{ top: 8, right: 16, left: 0, bottom: 0 }}
        >
          <CartesianGrid
            strokeDasharray="3 3"
            vertical={false}
            stroke="hsl(var(--border))"
          />
          <XAxis
            dataKey="date"
            tick={{ fontSize: 11 }}
            tickLine={false}
            axisLine={false}
            tickFormatter={(v: string) => v.slice(5)} // MM-DD
          />
          <YAxis
            domain={[0, 100]}
            tick={{ fontSize: 11 }}
            width={32}
            tickLine={false}
            axisLine={false}
          />
          <ChartTooltip content={<ChartTooltipContent />} />
          <Line
            type="monotone"
            dataKey="total_score"
            stroke="var(--color-total_score)"
            strokeWidth={2}
            dot={false}
            connectNulls
            isAnimationActive={false}
          />
        </LineChart>
      </ChartContainer>
    </figure>
  );
}
