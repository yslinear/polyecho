/**
 * Shared (non-"use server") types for the /research promote-to-live flow.
 *
 * A Next.js "use server" module (app/research/actions.ts) may export ONLY
 * async functions — a `type`/`interface` export there fails the build. The
 * result envelope that BOTH the Server Action and its client island
 * (research-table.tsx) need therefore lives here, in a plain module.
 */

/** Result envelope returned by `promoteToLiveFollow`. */
export interface PromoteToLiveFollowResult {
  ok: boolean;
  message: string;
  /** The id of the inserted (PAUSED) follow_relations row, on success. */
  followRelationId?: string;
}
