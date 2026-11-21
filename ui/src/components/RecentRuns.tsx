import { Badge, type BadgeVariant } from "@astryxdesign/core/Badge";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { formatCents, formatClock, formatDateTime } from "../format";
import type { RunSummary, Settlement } from "../types";
import { STATUS_TONE, StatusMark } from "./StatusMark";

export interface RecentRunsProps {
  runs: RunSummary[];
  /** The run on screen, marked in the list. */
  currentRunId?: string | null;
  /** Opens a run (its view and audit trail); omitted or disabled while a run is busy. */
  onOpen?: (runId: string) => void;
  disabled?: boolean;
}

/** Settlements worth a mark in the list: the money did not stay where the run left it. */
const SETTLEMENT_MARK: Partial<Record<Settlement, { label: string; variant: BadgeVariant }>> = {
  refunded: { label: "Refunded", variant: "warning" },
  disputed: { label: "Disputed", variant: "error" },
  failed: { label: "Payment failed", variant: "error" },
};

/**
 * Recent runs from GET /runs, newest first: status, start time, the redacted request and the
 * total. The request text comes from the server already redacted; nothing raw is shown here.
 */
export function RecentRuns({ runs, currentRunId, onOpen, disabled }: RecentRunsProps) {
  return (
    <section className="recent-runs" aria-labelledby="recent-runs-title" data-testid="recent-runs">
      <Heading level={2} id="recent-runs-title" className="recent-runs-title">
        Recent runs
      </Heading>
      {runs.length === 0 ? (
        <Text type="supporting" color="secondary" as="p">
          No runs yet.
        </Text>
      ) : (
        <ul className="recent-runs-list">
          {runs.map((r) => {
            const status = r.status.replace(/_/g, " ");
            const total =
              r.total_cents !== null ? formatCents(r.total_cents, r.currency ?? "CHF") : "–";
            const mark = r.settlement ? SETTLEMENT_MARK[r.settlement] : undefined;
            return (
              <li key={r.run_id}>
                <button
                  type="button"
                  className="recent-run"
                  data-current={r.run_id === currentRunId ? "true" : undefined}
                  aria-current={r.run_id === currentRunId ? "true" : undefined}
                  disabled={disabled || !onOpen}
                  onClick={() => onOpen?.(r.run_id)}
                  title={`${formatDateTime(r.created_at)} UTC · ${status}${mark ? ` · ${mark.label.toLowerCase()}` : ""}`}
                  data-testid="recent-run"
                >
                  <StatusMark tone={STATUS_TONE[r.status] ?? "neutral"} label={status} />
                  <Text type="code" size="sm" color="secondary" className="recent-run-time">
                    {formatClock(r.created_at)}
                  </Text>
                  <Text size="sm" className="recent-run-request">
                    {r.request_redacted || "(empty request)"}
                  </Text>
                  <span className="recent-run-end">
                    {mark && (
                      <Badge
                        variant={mark.variant}
                        label={mark.label}
                        data-testid="recent-run-settlement"
                        data-settlement={r.settlement}
                      />
                    )}
                    <Text size="sm" weight="medium" className="recent-run-total">
                      {total}
                    </Text>
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
