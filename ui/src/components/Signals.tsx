import { Text } from "@astryxdesign/core/Text";
import type { RunView, VerifyResult } from "../types";
import { StatusMark, type MarkTone } from "./StatusMark";

interface Lamp {
  id: string;
  label: string;
  value: string;
  dot: MarkTone | null;
  pulse?: boolean;
  tip: string;
}

/** The three controls the demo proves, each as one lamp that lights from real state. */
export function lamps(run: RunView | null, verify: VerifyResult | null, status: RunView["status"] | null = run?.status ?? null): Lamp[] {
  const gate: Lamp = {
    id: "gate",
    label: "Gate",
    value: "idle",
    dot: null,
    tip: "Policy gate and human approval: payment waits for a decision bound to the quote hash.",
  };
  if (status === "awaiting_approval") Object.assign(gate, { value: "waiting", dot: "accent", pulse: true });
  else if (status === "completed") Object.assign(gate, { value: "approved", dot: "success" });
  else if (status === "awaiting_payment") Object.assign(gate, { value: "approved · paying", dot: "accent", pulse: true });
  else if (status === "rejected") Object.assign(gate, { value: "rejected", dot: "warning" });
  else if (status === "refused") Object.assign(gate, { value: "refused", dot: "refused" });
  else if (status === "running") Object.assign(gate, { value: "working", dot: "accent", pulse: true });
  else if (status === "failed") Object.assign(gate, { value: "stopped", dot: "error" });

  const injections = run?.flags.filter((f) => f.startsWith("injection_flagged:")).length ?? 0;
  const guard: Lamp = {
    id: "guard",
    label: "Injection guard",
    value: injections > 0 ? `${injections} caught` : "quiet",
    dot: injections > 0 ? "warning" : run ? "success" : null,
    tip: "Demo: start the API with TRAVEL_SEARCH_POISONED=1.",
  };

  const chain: Lamp = {
    id: "chain",
    label: "Audit chain",
    value: verify ? (verify.ok ? "intact" : `broken at ${verify.first_bad_seq ?? "?"}`) : "unchecked",
    dot: verify ? (verify.ok ? "success" : "error") : null,
    tip: "Demo: edit one line of var/audit.jsonl, then Verify chain.",
  };
  return [gate, guard, chain];
}

const SEVERITY: Record<string, number> = { error: 5, refused: 5, warning: 4, accent: 3, success: 1, neutral: 0 };

/** The lamp a phone shows when there is room for one: the most severe lit one. */
export function primaryLamp(list: Lamp[]): string {
  let best = list[0];
  for (const l of list) if ((SEVERITY[l.dot ?? ""] ?? -1) > (SEVERITY[best.dot ?? ""] ?? -1)) best = l;
  return best.id;
}

export function Signals({
  run,
  verify,
  status,
}: {
  run: RunView | null;
  verify: VerifyResult | null;
  /** The run's live status (the stream knows it before the run's view arrives). */
  status?: RunView["status"] | null;
}) {
  const list = lamps(run, verify, status ?? run?.status ?? null);
  const primary = primaryLamp(list);
  return (
    <ul className="signals" aria-label="Controls">
      {list.map((l) => (
        <li
          key={l.id}
          className="signal"
          data-signal={l.id}
          data-lit={l.dot ?? undefined}
          data-primary={l.id === primary ? "true" : undefined}
          title={l.tip}
        >
          {l.dot ? <StatusMark tone={l.dot} label={l.value} pulse={l.pulse} /> : <span className="signal-off" aria-hidden="true" />}
          <Text type="supporting" color="secondary" className="signal-label">
            {l.label}
          </Text>
          <Text type="supporting" weight="semibold" data-testid={`signal-${l.id}`}>
            {l.value}
          </Text>
        </li>
      ))}
    </ul>
  );
}
