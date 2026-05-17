"use client";

/**
 * BacktestForm — shadcn form (react-hook-form + zod) for creating backtest jobs.
 * On valid submit: calls createBacktestJob server action, toasts, then navigates.
 */

import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Form,
  FormControl,
  FormDescription,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { createBacktestJob } from "../actions";

// ---------------------------------------------------------------------------
// Zod schema — mirrors PRD §5.4 BacktestParams
// ---------------------------------------------------------------------------
const schema = z
  .object({
    leader_address: z
      .string()
      .min(10, "Enter a valid wallet address")
      .regex(/^0x[0-9a-fA-F]+$/, "Must start with 0x"),
    start_date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/, "YYYY-MM-DD"),
    end_date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/, "YYYY-MM-DD"),
    initial_capital_usd: z.coerce
      .number()
      .min(10, "Minimum $10")
      .max(1_000_000),
    copy_ratio: z.coerce
      .number()
      .min(0.0001)
      .max(1)
      .optional()
      .or(z.literal("")),
    fixed_size_usd: z.coerce
      .number()
      .min(1)
      .optional()
      .or(z.literal("")),
    max_position_usd: z.coerce.number().min(1),
    min_trade_size_usd: z.coerce.number().min(0.01),
    daily_loss_limit_usd: z.coerce.number().min(1),
    slippage_bps: z.coerce.number().int().min(0).max(10000),
    execution_delay_seconds: z.coerce.number().int().min(0).max(3600),
    category_whitelist_raw: z.string().default(""),
    scenario: z.enum(["optimistic", "neutral", "pessimistic", "triple"]),
  })
  .refine(
    (d) => {
      const hasCopyRatio = d.copy_ratio !== "" && d.copy_ratio !== undefined;
      const hasFixed = d.fixed_size_usd !== "" && d.fixed_size_usd !== undefined;
      return hasCopyRatio || hasFixed;
    },
    {
      message: "Provide either copy_ratio or fixed_size_usd",
      path: ["copy_ratio"],
    }
  )
  .refine((d) => d.start_date < d.end_date, {
    message: "start_date must be before end_date",
    path: ["end_date"],
  });

type FormValues = z.infer<typeof schema>;

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------
interface BacktestFormProps {
  defaultLeader?: string;
}

export function BacktestForm({ defaultLeader = "" }: BacktestFormProps) {
  const [submitting, setSubmitting] = useState(false);

  // Default dates: 90 days ago → today
  const today = new Date();
  const ninetyDaysAgo = new Date(today);
  ninetyDaysAgo.setDate(today.getDate() - 90);
  const toISO = (d: Date) => d.toISOString().slice(0, 10);

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      leader_address: defaultLeader,
      start_date: toISO(ninetyDaysAgo),
      end_date: toISO(today),
      initial_capital_usd: 1000,
      copy_ratio: 0.001,
      fixed_size_usd: "",
      max_position_usd: 50,
      min_trade_size_usd: 1,
      daily_loss_limit_usd: 100,
      slippage_bps: 150,
      execution_delay_seconds: 30,
      category_whitelist_raw: "",
      scenario: "triple",
    },
  });

  async function onSubmit(values: FormValues) {
    setSubmitting(true);
    try {
      const category_whitelist = values.category_whitelist_raw
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);

      await createBacktestJob({
        leader_address: values.leader_address,
        start_date: values.start_date,
        end_date: values.end_date,
        initial_capital_usd: values.initial_capital_usd,
        copy_ratio:
          values.copy_ratio !== "" && values.copy_ratio !== undefined
            ? Number(values.copy_ratio)
            : undefined,
        fixed_size_usd:
          values.fixed_size_usd !== "" && values.fixed_size_usd !== undefined
            ? Number(values.fixed_size_usd)
            : undefined,
        max_position_usd: values.max_position_usd,
        min_trade_size_usd: values.min_trade_size_usd,
        daily_loss_limit_usd: values.daily_loss_limit_usd,
        slippage_bps: values.slippage_bps,
        execution_delay_seconds: values.execution_delay_seconds,
        category_whitelist,
        scenario: values.scenario,
      });
      toast.success("Backtest queued — redirecting…");
    } catch (err) {
      // redirect() throws internally; re-throw to let Next.js handle it
      if ((err as Error).message?.includes("NEXT_REDIRECT")) {
        throw err;
      }
      toast.error("Failed to queue backtest", {
        description: String(err),
      });
      setSubmitting(false);
    }
  }

  return (
    <Form {...form}>
      <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-6">
        {/* ---- Leader & Dates ---- */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">Leader & Date Range</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <FormField
              control={form.control}
              name="leader_address"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Leader Address</FormLabel>
                  <FormControl>
                    <Input placeholder="0x…" {...field} />
                  </FormControl>
                  <FormDescription>
                    Wallet address of the leader to backtest
                  </FormDescription>
                  <FormMessage />
                </FormItem>
              )}
            />

            <div className="grid grid-cols-2 gap-4">
              <FormField
                control={form.control}
                name="start_date"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Start Date</FormLabel>
                    <FormControl>
                      <Input type="date" {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="end_date"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>End Date</FormLabel>
                    <FormControl>
                      <Input type="date" {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>
          </CardContent>
        </Card>

        {/* ---- Capital & Sizing ---- */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">Capital & Sizing</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <FormField
              control={form.control}
              name="initial_capital_usd"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Initial Capital (USD)</FormLabel>
                  <FormControl>
                    <Input type="number" min="10" step="10" {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            <Separator />
            <p className="text-xs text-muted-foreground">
              Provide either copy_ratio <em>or</em> fixed_size_usd (mutually
              exclusive).
            </p>

            <div className="grid grid-cols-2 gap-4">
              <FormField
                control={form.control}
                name="copy_ratio"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Copy Ratio</FormLabel>
                    <FormControl>
                      <Input
                        type="number"
                        step="0.0001"
                        min="0.0001"
                        max="1"
                        placeholder="0.001"
                        {...field}
                        value={field.value ?? ""}
                      />
                    </FormControl>
                    <FormDescription>e.g. 0.001 = 0.1%</FormDescription>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="fixed_size_usd"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Fixed Size (USD)</FormLabel>
                    <FormControl>
                      <Input
                        type="number"
                        step="0.5"
                        min="1"
                        placeholder="—"
                        {...field}
                        value={field.value ?? ""}
                      />
                    </FormControl>
                    <FormDescription>Fixed dollar amount per trade</FormDescription>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>

            <div className="grid grid-cols-2 gap-4">
              <FormField
                control={form.control}
                name="max_position_usd"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Max Position (USD)</FormLabel>
                    <FormControl>
                      <Input type="number" step="1" min="1" {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="min_trade_size_usd"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Min Trade Size (USD)</FormLabel>
                    <FormControl>
                      <Input type="number" step="0.01" min="0.01" {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>

            <FormField
              control={form.control}
              name="daily_loss_limit_usd"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Daily Loss Limit (USD)</FormLabel>
                  <FormControl>
                    <Input type="number" step="1" min="1" {...field} />
                  </FormControl>
                  <FormDescription>
                    Stop copying when daily PnL drops below –limit
                  </FormDescription>
                  <FormMessage />
                </FormItem>
              )}
            />
          </CardContent>
        </Card>

        {/* ---- Execution Params ---- */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">Execution Parameters</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <FormField
                control={form.control}
                name="slippage_bps"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Slippage (bps)</FormLabel>
                    <FormControl>
                      <Input type="number" step="1" min="0" {...field} />
                    </FormControl>
                    <FormDescription>150 bps = 1.5%</FormDescription>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="execution_delay_seconds"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Execution Delay (s)</FormLabel>
                    <FormControl>
                      <Input type="number" step="1" min="0" {...field} />
                    </FormControl>
                    <FormDescription>Simulated fill delay</FormDescription>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>

            <FormField
              control={form.control}
              name="category_whitelist_raw"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Category Whitelist</FormLabel>
                  <FormControl>
                    <Input
                      placeholder="Politics, Sports, Economy (comma-separated, blank = all)"
                      {...field}
                    />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
          </CardContent>
        </Card>

        {/* ---- Scenario ---- */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">Scenario</CardTitle>
          </CardHeader>
          <CardContent>
            <FormField
              control={form.control}
              name="scenario"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Run Mode</FormLabel>
                  <Select
                    onValueChange={field.onChange}
                    defaultValue={field.value}
                  >
                    <FormControl>
                      <SelectTrigger>
                        <SelectValue placeholder="Select scenario" />
                      </SelectTrigger>
                    </FormControl>
                    <SelectContent>
                      <SelectItem value="triple">
                        Triple (all 3 scenarios — recommended)
                      </SelectItem>
                      <SelectItem value="optimistic">
                        Optimistic (50 bps slippage, 10 s delay)
                      </SelectItem>
                      <SelectItem value="neutral">
                        Neutral (150 bps, 30 s)
                      </SelectItem>
                      <SelectItem value="pessimistic">
                        Pessimistic (300 bps, 60 s)
                      </SelectItem>
                    </SelectContent>
                  </Select>
                  <FormDescription>
                    &ldquo;Triple&rdquo; runs all three simultaneously for
                    comparison (PRD §5.4)
                  </FormDescription>
                  <FormMessage />
                </FormItem>
              )}
            />
          </CardContent>
        </Card>

        <Button type="submit" disabled={submitting} className="w-full">
          {submitting ? "Submitting…" : "Queue Backtest"}
        </Button>
      </form>
    </Form>
  );
}
