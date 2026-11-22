/**
 * Dev-only demo mode: `?demo=<scenario>` replays recorded run events (src/fixtures/events.ts)
 * through the same client interface as the live API, so every screen state can be reached
 * without a backend, deterministically. Never active in a production build unless
 * VITE_DEMO=1 is set at build time.
 *
 *   ?demo=flagged   poisoned search result: injection flags, review step, then pay
 *   ?demo=clean     the same trip with clean tool output
 *   ?demo=denied    the policy gate refuses an unapproved payments.charge
 *   ?demo=error     the server fails mid-run
 *   &hold=<n>       stop the create stream after n events (a run frozen mid-flight)
 *   &pace=<ms>      stage pacing (0: apply every event at once)
 *   &tamper=<seq>   Verify chain reports the chain broken at that seq (tamper evidence)
 */
import { ApiError, type ApiClient, type OnRunEvent } from "./api";
import * as fx from "./fixtures/events";
import type { ApprovalRequest, AuditEntry, Quote, RunEvent, RunView, VerifyResult } from "./types";

export const SCENARIOS = ["flagged", "clean", "denied", "error"] as const;
export type Scenario = (typeof SCENARIOS)[number];

export interface DemoOptions {
  scenario: Scenario;
  /** Events of the create stream to send before it stalls; undefined sends them all. */
  hold?: number;
  /** Stage pacing override for useEventPlayer. */
  paceMs?: number;
  /** Seq that Verify chain reports as the first bad entry, as if var/audit.jsonl was edited. */
  tamper?: number;
}

export function demoEnabled(): boolean {
  return import.meta.env.DEV || import.meta.env.VITE_DEMO === "1";
}

/** Reads demo options from a query string; null when the demo is off or not asked for. */
export function parseDemo(search: string, enabled = demoEnabled()): DemoOptions | null {
  if (!enabled) return null;
  const q = new URLSearchParams(search);
  const scenario = q.get("demo");
  if (!scenario) return null;
  if (!(SCENARIOS as readonly string[]).includes(scenario)) return null;
  const num = (k: string) => {
    const v = q.get(k);
    if (v === null || !/^\d+$/.test(v)) return undefined;
    return Number(v);
  };
  return { scenario: scenario as Scenario, hold: num("hold"), paceMs: num("pace"), tamper: num("tamper") };
}

interface Script {
  create: RunEvent[];
  approve: RunEvent[];
  reject: RunEvent[];
  audit: { afterCreate: AuditEntry[]; approved: AuditEntry[]; rejected: AuditEntry[] };
}

function script(s: Scenario): Script {
  switch (s) {
    case "flagged":
      return {
        create: fx.createEvents,
        approve: fx.approveEvents,
        reject: fx.rejectEvents,
        audit: {
          afterCreate: fx.audit.poisoned.awaiting,
          approved: fx.audit.poisoned.completed,
          rejected: fx.audit.poisoned.rejected,
        },
      };
    case "clean":
      return {
        create: fx.cleanCreateEvents,
        approve: fx.cleanApproveEvents,
        reject: fx.cleanRejectEvents,
        audit: { afterCreate: fx.audit.clean.awaiting, approved: fx.audit.clean.completed, rejected: fx.audit.clean.rejected },
      };
    case "denied":
      return {
        create: fx.deniedCreateEvents,
        approve: [],
        reject: [],
        audit: { afterCreate: fx.audit.denied, approved: fx.audit.denied, rejected: fx.audit.denied },
      };
    case "error":
      return {
        create: fx.errorCreateEvents,
        approve: [],
        reject: [],
        audit: { afterCreate: fx.audit.error, approved: fx.audit.error, rejected: fx.audit.error },
      };
  }
}

/** Sends events in order; resolves with the final view, or throws like the live client. */
function play(events: RunEvent[], onEvent: OnRunEvent): RunView {
  let final: RunView | null = null;
  for (const e of events) {
    onEvent(e);
    if (e.type === "error") throw new ApiError(e.status_code ?? 500, e.detail ?? e.error_type ?? "run failed");
    if (e.type === "run_view") {
      const { type: _type, ...view } = e;
      final = view;
    }
  }
  if (!final) throw new ApiError(0, "stream ended before the run finished");
  return final;
}

/** The demo offer is valid this long from the moment the run is sent (server: 15 min). */
export const DEMO_HOLD_MS = 15 * 60_000;

/** "…T16:35:00+00:00": the demo deadline relative to now, on a whole minute. */
export function demoDeadline(nowMs: number): string {
  const at = Math.floor((nowMs + DEMO_HOLD_MS) / 60_000) * 60_000;
  return new Date(at).toISOString().replace(/\.\d{3}Z$/, "+00:00");
}

/** The recorded events with their fixed far-future deadline moved to `deadline`. */
function withDeadline(events: RunEvent[], deadline: string): RunEvent[] {
  return events.map((e) => {
    if (e.type !== "run_view" || !e.interrupt) return e;
    const offer = { ...e.interrupt.offer, expires_at: deadline };
    return { ...e, interrupt: { ...e.interrupt, offer, approve_before: deadline } };
  });
}

/** The recorded approval entry, rewritten for the decision actually sent. */
function withDecision(entries: AuditEntry[], body: ApprovalRequest): AuditEntry[] {
  return entries.map((e) =>
    e.event === "approval_decision"
      ? {
          ...e,
          data: {
            ...e.data,
            approved: body.approved,
            quote_hash: body.quote_hash,
            fare_ids: body.fare_ids ?? fx.RECOMMENDED_FARE_IDS,
          },
        }
      : e,
  );
}

/** A completed stream whose quote and charge are the selection the approver chose. */
function forSelection(events: RunEvent[], selected: Quote): RunEvent[] {
  return events.map((e) => {
    if (e.type !== "run_view") return e;
    const { type: _type, ...view } = e;
    return { type: "run_view", ...fx.completedFor(selected, view) };
  });
}

/** A client with the live client's shape that answers from the recorded scenario. */
export function demoClient({ scenario, hold, tamper }: DemoOptions): ApiClient {
  const sc = script(scenario);
  let audit: AuditEntry[] = [];
  return {
    async getQuote(_runId: string, fareIds: string[]) {
      const q = fx.quoteFor(fareIds);
      if (!q) throw new ApiError(422, "422 Unprocessable Entity selection not in the offer");
      return q;
    },
    async streamRun(_message: string, onEvent: OnRunEvent) {
      audit = [];
      const create = withDeadline(sc.create, demoDeadline(Date.now()));
      if (hold !== undefined && hold < create.length) {
        for (const e of create.slice(0, hold)) onEvent(e);
        // A run frozen mid-flight: the stream never ends.
        return new Promise<RunView>(() => {});
      }
      try {
        return play(create, onEvent);
      } finally {
        audit = sc.audit.afterCreate;
      }
    },
    async streamApproval(_runId: string, body: ApprovalRequest, onEvent: OnRunEvent) {
      // Like the server: an approval is bound to the quote recomputed for the selection.
      const selected = fx.quoteFor(body.fare_ids ?? fx.RECOMMENDED_FARE_IDS);
      const approved = body.approved && selected !== null && selected.quote_hash === body.quote_hash;
      const view = play(approved && selected ? forSelection(sc.approve, selected) : sc.reject, onEvent);
      audit = withDecision(approved ? sc.audit.approved : sc.audit.rejected, { ...body, approved });
      return view;
    },
    async getAudit() {
      return audit;
    },
    // A recorded demo has no run history: the list stays empty.
    async listRuns() {
      return [];
    },
    async verifyAudit(): Promise<VerifyResult> {
      if (tamper !== undefined && audit.some((e) => e.seq === tamper)) {
        return { ok: false, entries: audit.length, first_bad_seq: tamper, reason: "hash mismatch" };
      }
      return { ok: true, entries: audit.length, first_bad_seq: null, reason: null };
    },
  };
}
