"use client";

/**
 * DataChart — client-side Recharts wrapper for score history.
 * Receives pre-fetched data from a Server Component parent.
 */

import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";

export interface ScorePoint {
  date: string; // ISO YYYY-MM-DD
  total_score: number | null;
}

interface DataChartProps {
  data: ScorePoint[];
}

export function DataChart({ data }: DataChartProps) {
  if (data.length === 0) {
    return (
      <p className="text-sm text-muted-foreground py-6 text-center">
        No score history recorded yet.
      </p>
    );
  }

  return (
    <ResponsiveContainer width="100%" height={240}>
      <LineChart data={data} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" className="stroke-border" />
        <XAxis
          dataKey="date"
          tick={{ fontSize: 11 }}
          tickFormatter={(v: string) => v.slice(5)} // show MM-DD
        />
        <YAxis domain={[0, 100]} tick={{ fontSize: 11 }} width={32} />
        <Tooltip
          labelFormatter={(l: string) => `Date: ${l}`}
          formatter={(v: number) => [v.toFixed(1), "Total Score"]}
        />
        <Line
          type="monotone"
          dataKey="total_score"
          stroke="#6366f1"
          strokeWidth={2}
          dot={false}
          connectNulls
        />
      </LineChart>
    </ResponsiveContainer>
  );
}
