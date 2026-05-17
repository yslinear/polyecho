"use client";

/**
 * LeaderScoreChart — shadcn chart (Recharts) wrapper for score history.
 * Client island fed by server-fetched ScorePoint array.
 */

import { Line, LineChart, CartesianGrid, XAxis, YAxis } from "recharts";
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import type { ScorePoint } from "./page";

const chartConfig: ChartConfig = {
  total_score: {
    label: "Total Score",
    color: "hsl(var(--primary))",
  },
};

interface LeaderScoreChartProps {
  data: ScorePoint[];
}

export function LeaderScoreChart({ data }: LeaderScoreChartProps) {
  if (data.length === 0) {
    return (
      <p className="text-sm text-muted-foreground py-6 text-center">
        No score history recorded yet.
      </p>
    );
  }

  return (
    <ChartContainer config={chartConfig} className="h-[260px] w-full">
      <LineChart data={data} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" vertical={false} />
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
        />
      </LineChart>
    </ChartContainer>
  );
}
