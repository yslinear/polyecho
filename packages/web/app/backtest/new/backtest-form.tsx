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

import { t } from "@/lib/i18n";
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
import { createBacktestJob, type BacktestJobDefaults } from "../actions";

// ---------------------------------------------------------------------------
// Zod schema — mirrors PRD §5.4 BacktestParams
// ---------------------------------------------------------------------------
const schema = z
  .object({
    leader_address: z
      .string()
      .min(10, t("backtest.form.validation.walletInvalid"))
      .regex(/^0x[0-9a-fA-F]+$/, t("backtest.form.validation.walletPrefix")),
    start_date: z
      .string()
      .regex(/^\d{4}-\d{2}-\d{2}$/, t("backtest.form.validation.dateFormat")),
    end_date: z
      .string()
      .regex(/^\d{4}-\d{2}-\d{2}$/, t("backtest.form.validation.dateFormat")),
    initial_capital_usd: z.coerce
      .number()
      .min(10, t("backtest.form.validation.capitalMin"))
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
      message: t("backtest.form.validation.sizingRequired"),
      path: ["copy_ratio"],
    }
  )
  .refine((d) => d.start_date < d.end_date, {
    message: t("backtest.form.validation.dateOrder"),
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
  /**
   * Re-run pre-fill: the full set of form defaults reconstructed from an
   * existing job (the list's 「重新執行」 → `?from=<jobId>` path). Any
   * field left undefined falls back to the form's own baseline default.
   */
  initialValues?: BacktestJobDefaults;
}

export function BacktestForm({
  defaultLeader = "",
  initialValues,
}: BacktestFormProps) {
  const [submitting, setSubmitting] = useState(false);

  // Default dates: 90 days ago → today
  const today = new Date();
  const ninetyDaysAgo = new Date(today);
  ninetyDaysAgo.setDate(today.getDate() - 90);
  const toISO = (d: Date) => d.toISOString().slice(0, 10);

  // Re-run pre-fill: a value from `initialValues` (a source job) wins, else
  // the form's own baseline. `?? undefined` guards keep the resolver's
  // numeric coercion happy when a source field was absent.
  const iv = initialValues;

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    // Inline validation on blur (DESIGN.md "Forms"): the field is checked
    // when the user leaves it, and re-checked on every change once invalid.
    mode: "onBlur",
    reValidateMode: "onChange",
    defaultValues: {
      leader_address: iv?.leader_address ?? defaultLeader,
      start_date: iv?.start_date ?? toISO(ninetyDaysAgo),
      end_date: iv?.end_date ?? toISO(today),
      initial_capital_usd: iv?.initial_capital_usd ?? 1000,
      copy_ratio: iv?.copy_ratio ?? 0.001,
      fixed_size_usd: iv?.fixed_size_usd ?? "",
      max_position_usd: iv?.max_position_usd ?? 50,
      min_trade_size_usd: iv?.min_trade_size_usd ?? 1,
      daily_loss_limit_usd: iv?.daily_loss_limit_usd ?? 100,
      slippage_bps: iv?.slippage_bps ?? 150,
      execution_delay_seconds: iv?.execution_delay_seconds ?? 30,
      category_whitelist_raw: iv?.category_whitelist_raw ?? "",
      scenario: iv?.scenario ?? "triple",
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
      toast.success(t("backtest.form.toastQueued"));
    } catch (err) {
      // redirect() throws internally; re-throw to let Next.js handle it
      if ((err as Error).message?.includes("NEXT_REDIRECT")) {
        throw err;
      }
      toast.error(t("backtest.form.toastError"), {
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
          title={t("backtest.form.sections.leaderDates.title")}
          hint={t("backtest.form.sections.leaderDates.hint")}
        >
          <FormField
            control={form.control}
            name="leader_address"
            render={({ field }) => (
              <FormItem>
                <FormLabel>{t("backtest.form.fields.leaderAddress.label")}</FormLabel>
                <FormControl>
                  <Input
                    placeholder={t("backtest.form.fields.leaderAddress.placeholder")}
                    autoComplete="off"
                    spellCheck={false}
                    className="font-mono"
                    {...field}
                  />
                </FormControl>
                <FormDescription>
                  {t("backtest.form.fields.leaderAddress.help")}
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
                  <FormLabel>{t("backtest.form.fields.startDate.label")}</FormLabel>
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
                  <FormLabel>{t("backtest.form.fields.endDate.label")}</FormLabel>
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
          title={t("backtest.form.sections.capital.title")}
          hint={t("backtest.form.sections.capital.hint")}
        >
          <FormField
            control={form.control}
            name="initial_capital_usd"
            render={({ field }) => (
              <FormItem className="max-w-xs">
                <FormLabel>{t("backtest.form.fields.initialCapital.label")}</FormLabel>
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
                  <FormLabel>{t("backtest.form.fields.copyRatio.label")}</FormLabel>
                  <FormControl>
                    <Input
                      type="number"
                      step="0.0001"
                      min="0.0001"
                      max="1"
                      placeholder={t("backtest.form.fields.copyRatio.placeholder")}
                      className="tabular-nums"
                      {...field}
                      value={field.value ?? ""}
                    />
                  </FormControl>
                  <FormDescription>{t("backtest.form.fields.copyRatio.help")}</FormDescription>
                  <FormMessage />
                </FormItem>
              )}
            />
            <FormField
              control={form.control}
              name="fixed_size_usd"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>{t("backtest.form.fields.fixedSize.label")}</FormLabel>
                  <FormControl>
                    <Input
                      type="number"
                      step="0.5"
                      min="1"
                      placeholder={t("backtest.form.fields.fixedSize.placeholder")}
                      className="tabular-nums"
                      {...field}
                      value={field.value ?? ""}
                    />
                  </FormControl>
                  <FormDescription>{t("backtest.form.fields.fixedSize.help")}</FormDescription>
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
                  <FormLabel>{t("backtest.form.fields.maxPosition.label")}</FormLabel>
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
                  <FormLabel>{t("backtest.form.fields.minTradeSize.label")}</FormLabel>
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
                <FormLabel>{t("backtest.form.fields.dailyLossLimit.label")}</FormLabel>
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
                  {t("backtest.form.fields.dailyLossLimit.help")}
                </FormDescription>
                <FormMessage />
              </FormItem>
            )}
          />
        </Section>

        <Separator />

        {/* ---- Execution Params ---- */}
        <Section
          title={t("backtest.form.sections.execution.title")}
          hint={t("backtest.form.sections.execution.hint")}
        >
          <div className="grid grid-cols-2 gap-5">
            <FormField
              control={form.control}
              name="slippage_bps"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>{t("backtest.form.fields.slippage.label")}</FormLabel>
                  <FormControl>
                    <Input
                      type="number"
                      step="1"
                      min="0"
                      className="tabular-nums"
                      {...field}
                    />
                  </FormControl>
                  <FormDescription>{t("backtest.form.fields.slippage.help")}</FormDescription>
                  <FormMessage />
                </FormItem>
              )}
            />
            <FormField
              control={form.control}
              name="execution_delay_seconds"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>{t("backtest.form.fields.executionDelay.label")}</FormLabel>
                  <FormControl>
                    <Input
                      type="number"
                      step="1"
                      min="0"
                      className="tabular-nums"
                      {...field}
                    />
                  </FormControl>
                  <FormDescription>{t("backtest.form.fields.executionDelay.help")}</FormDescription>
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
                <FormLabel>{t("backtest.form.fields.categoryWhitelist.label")}</FormLabel>
                <FormControl>
                  <Input
                    placeholder={t("backtest.form.fields.categoryWhitelist.placeholder")}
                    {...field}
                  />
                </FormControl>
                <FormDescription>
                  {t("backtest.form.fields.categoryWhitelist.help")}
                </FormDescription>
                <FormMessage />
              </FormItem>
            )}
          />
        </Section>

        <Separator />

        {/* ---- Scenario ---- */}
        <Section
          title={t("backtest.form.sections.scenario.title")}
          hint={t("backtest.form.sections.scenario.hint")}
        >
          <FormField
            control={form.control}
            name="scenario"
            render={({ field }) => (
              <FormItem className="max-w-md">
                <FormLabel>{t("backtest.form.runMode.label")}</FormLabel>
                <Select
                  onValueChange={field.onChange}
                  defaultValue={field.value}
                >
                  <FormControl>
                    <SelectTrigger>
                      <SelectValue placeholder={t("backtest.form.runMode.placeholder")} />
                    </SelectTrigger>
                  </FormControl>
                  <SelectContent>
                    <SelectItem value="triple">
                      {t("backtest.form.runMode.triple")}
                    </SelectItem>
                    <SelectItem value="optimistic">
                      {t("backtest.form.runMode.optimistic")}
                    </SelectItem>
                    <SelectItem value="neutral">
                      {t("backtest.form.runMode.neutral")}
                    </SelectItem>
                    <SelectItem value="pessimistic">
                      {t("backtest.form.runMode.pessimistic")}
                    </SelectItem>
                  </SelectContent>
                </Select>
                <FormDescription>
                  {t("backtest.form.runMode.help")}
                </FormDescription>
                <FormMessage />
              </FormItem>
            )}
          />
        </Section>

        <div className="flex items-center justify-end gap-4 pt-2">
          <p className="text-xs text-muted-foreground">
            {t("backtest.form.submitHint")}
          </p>
          <Button type="submit" disabled={submitting}>
            {submitting ? t("backtest.form.submitting") : t("backtest.form.submit")}
          </Button>
        </div>
      </form>
    </Form>
  );
}
