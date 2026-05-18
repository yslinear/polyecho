"use client";

/**
 * BacktestForm — shadcn form (react-hook-form + zod) for creating backtest jobs.
 * On valid submit: calls createBacktestJob server action, toasts, then navigates.
 */

import type { ReactNode } from "react";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
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
// Section — a grouped block of fields. Not a Card: DESIGN.md bans the
// identical-card-grid; groups are separated by hairlines + varied rhythm.
// ---------------------------------------------------------------------------
function Section({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <section className="space-y-5">
      <div className="space-y-1">
        <h2 className="text-sm font-semibold text-foreground">{title}</h2>
        {hint && (
          <p className="text-xs text-muted-foreground">{hint}</p>
        )}
      </div>
      {children}
    </section>
  );
}

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
    // Inline validation on blur (DESIGN.md "Forms"): the field is checked
    // when the user leaves it, and re-checked on every change once invalid.
    mode: "onBlur",
    reValidateMode: "onChange",
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
      toast.success("Backtest queued. Redirecting…");
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
      <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-10">
        {/* ---- Leader & Dates ---- */}
        <Section
          title="Leader & date range"
          hint="Which wallet to mirror, and the window to replay."
        >
          <FormField
            control={form.control}
            name="leader_address"
            render={({ field }) => (
              <FormItem>
                <FormLabel>Leader Address</FormLabel>
                <FormControl>
                  <Input
                    placeholder="0x…"
                    autoComplete="off"
                    spellCheck={false}
                    className="font-mono"
                    {...field}
                  />
                </FormControl>
                <FormDescription>
                  Wallet address of the leader to backtest
                </FormDescription>
                <FormMessage />
              </FormItem>
            )}
          />

          <div className="grid grid-cols-2 gap-5">
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
        </Section>

        <Separator />

        {/* ---- Capital & Sizing ---- */}
        <Section
          title="Capital & sizing"
          hint="Starting capital and how each copied trade is sized. Set a copy ratio or a fixed size, not both."
        >
          <FormField
            control={form.control}
            name="initial_capital_usd"
            render={({ field }) => (
              <FormItem className="max-w-xs">
                <FormLabel>Initial Capital (USD)</FormLabel>
                <FormControl>
                  <Input
                    type="number"
                    min="10"
                    step="10"
                    className="tabular-nums"
                    {...field}
                  />
                </FormControl>
                <FormMessage />
              </FormItem>
            )}
          />

          <div className="grid grid-cols-2 gap-5">
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
                      className="tabular-nums"
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
                      placeholder="Leave blank to use copy ratio"
                      className="tabular-nums"
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

          <div className="grid grid-cols-2 gap-5">
            <FormField
              control={form.control}
              name="max_position_usd"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Max Position (USD)</FormLabel>
                  <FormControl>
                    <Input
                      type="number"
                      step="1"
                      min="1"
                      className="tabular-nums"
                      {...field}
                    />
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
                    <Input
                      type="number"
                      step="0.01"
                      min="0.01"
                      className="tabular-nums"
                      {...field}
                    />
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
              <FormItem className="max-w-xs">
                <FormLabel>Daily Loss Limit (USD)</FormLabel>
                <FormControl>
                  <Input
                    type="number"
                    step="1"
                    min="1"
                    className="tabular-nums"
                    {...field}
                  />
                </FormControl>
                <FormDescription>
                  Stop copying once the day&rsquo;s loss exceeds this limit
                </FormDescription>
                <FormMessage />
              </FormItem>
            )}
          />
        </Section>

        <Separator />

        {/* ---- Execution Params ---- */}
        <Section
          title="Execution model"
          hint="Slippage and fill delay applied to every simulated trade."
        >
          <div className="grid grid-cols-2 gap-5">
            <FormField
              control={form.control}
              name="slippage_bps"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Slippage (bps)</FormLabel>
                  <FormControl>
                    <Input
                      type="number"
                      step="1"
                      min="0"
                      className="tabular-nums"
                      {...field}
                    />
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
                    <Input
                      type="number"
                      step="1"
                      min="0"
                      className="tabular-nums"
                      {...field}
                    />
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
                    placeholder="Politics, Sports, Economy"
                    {...field}
                  />
                </FormControl>
                <FormDescription>
                  Comma-separated. Leave blank to include all categories.
                </FormDescription>
                <FormMessage />
              </FormItem>
            )}
          />
        </Section>

        <Separator />

        {/* ---- Scenario ---- */}
        <Section
          title="Scenario"
          hint="Run a single execution model, or all three side by side."
        >
          <FormField
            control={form.control}
            name="scenario"
            render={({ field }) => (
              <FormItem className="max-w-md">
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
                      Triple (all 3 scenarios, recommended)
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
                  &ldquo;Triple&rdquo; runs all three at once so you can
                  compare best, base, and worst case (PRD §5.4).
                </FormDescription>
                <FormMessage />
              </FormItem>
            )}
          />
        </Section>

        <div className="flex items-center justify-end gap-4 pt-2">
          <p className="text-xs text-muted-foreground">
            Queues the job. A worker processes it shortly after.
          </p>
          <Button type="submit" disabled={submitting}>
            {submitting ? "Queueing…" : "Queue Backtest"}
          </Button>
        </div>
      </form>
    </Form>
  );
}
