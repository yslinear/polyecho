/**
 * zh-TW dictionary aggregate.
 *
 * One namespace per page-group module so the i18n swarm edits disjoint
 * files (zero merge conflict). This index is written ONCE and is NOT
 * touched by the per-group agents — they only edit their own module.
 */
import { alertsSettings } from "./alerts-settings";
import { backtest } from "./backtest";
import { common, sidebar } from "./common";
import { dashboard } from "./dashboard";
import { discovery } from "./discovery";
import { status } from "./status";
import { tables } from "./tables";

export const dict = {
  common,
  sidebar,
  dashboard,
  status,
  discovery,
  backtest,
  tables,
  alertsSettings,
} as const;
