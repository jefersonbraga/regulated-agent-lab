/**
 * Readable copy for run-level flags such as "injection_flagged:travel_search.search_trains".
 * The API sends one flag per occurrence; the UI shows each distinct flag once with a count.
 */

export interface FlagSummary {
  /** The raw flag string from the API, e.g. "injection_flagged:travel_search.search_trains". */
  flag: string;
  kind: string;
  /** Tool ("server.tool") or graph node the flag names; empty if none. */
  target: string;
  count: number;
  /** One readable sentence. */
  text: string;
}

function sentence(kind: string, target: string, count: number): string {
  const times = count > 1 ? ` (${count}×)` : "";
  switch (kind) {
    case "injection_flagged":
      // The banner title already says "Possible prompt injection detected": name the source only.
      return `Source: ${target}, ${count} result${count === 1 ? "" : "s"}.`;
    case "policy_denied":
      return `The tool policy denied ${target}${times}.`;
    case "node_error":
      return `The ${target} step failed${times}.`;
    case "llm_unavailable":
      return `The ${target} step got no answer from the language model (unreachable or timed out)${times}.`;
    case "quote_mismatch":
      return `The approval did not match a selection from the offer and its quote hash${times}.`;
    case "audit_failed":
      return `An audit write failed for ${target}${times}.`;
    default:
      return `${kind.replace(/_/g, " ")}${target ? ` (${target})` : ""}${times}.`;
  }
}

/** Distinct flags in first-seen order, with how often each was raised. */
export function summarizeFlags(flags: string[]): FlagSummary[] {
  const counts = new Map<string, number>();
  for (const flag of flags) counts.set(flag, (counts.get(flag) ?? 0) + 1);
  return [...counts].map(([flag, count]) => {
    const i = flag.indexOf(":");
    const kind = i < 0 ? flag : flag.slice(0, i);
    const target = i < 0 ? "" : flag.slice(i + 1);
    return { flag, kind, target, count, text: sentence(kind, target, count) };
  });
}

/** What the injection guard did with flagged tool output (controls/injection_guard.py). */
export const INJECTION_GUARD_NOTE =
  "The guard replaced the untrusted text in the tool result; the quote was built from structured fields only (ids, times, prices).";

export function hasInjectionFlag(flags: string[]): boolean {
  return flags.some((f) => f.startsWith("injection_flagged:"));
}

/**
 * Short count of a run's flags, e.g. "2 tool results flagged" or "3 flags raised". The page
 * banner is the one place with the full explanation; other surfaces use this and point to it.
 */
export function flagCount(flags: string[]): string {
  const n = flags.length;
  if (n === 0) return "";
  if (flags.every((f) => f.startsWith("injection_flagged:"))) {
    return `${n} tool result${n === 1 ? "" : "s"} flagged`;
  }
  return `${n} flag${n === 1 ? "" : "s"} raised`;
}

/** Where a run is when its flags are shown; it picks the banner's tense. */
export type FlagsPhase = "live" | "review" | "completed" | "ended";

export interface FlagsCopy {
  /** A policy refusal is an error; flagged tool output is a warning. */
  status: "warning" | "error";
  title: string;
  /** Visible explanation lines (never only in a tooltip, so touch users can read them). */
  lines: string[];
}

/**
 * Banner copy for a run's flags. A policy denial names the refused tool and the reason; flagged
 * tool output says what the guard did and, once the run is over, what the human did about it.
 */
export function flagsCopy(flags: string[], phase: FlagsPhase, reason?: string | null): FlagsCopy {
  const summary = summarizeFlags(flags);
  const denied = summary.filter((f) => f.kind === "policy_denied");
  const injections = summary.filter((f) => f.kind === "injection_flagged");
  const injectionCount = injections.reduce((n, f) => n + f.count, 0);
  const injectionLines = injections.length > 0 ? [...injections.map((f) => f.text), INJECTION_GUARD_NOTE] : [];
  if (denied.length > 0) {
    const lines = denied.map(
      (f) => `The policy gate refused ${f.target}${reason ? `: ${reason}` : ""}${f.count > 1 ? ` (${f.count}×)` : ""}.`,
    );
    if (denied.some((f) => f.target === "payments.charge")) lines.push("No charge was made.");
    return {
      status: "error",
      title: `Blocked by policy: ${denied.map((f) => f.target).join(", ")}`,
      lines: [...lines, ...injectionLines],
    };
  }
  const unavailable = summary.filter((f) => f.kind === "llm_unavailable");
  if (unavailable.length > 0) {
    // The model endpoint never answered: say so plainly, not as a generic step failure.
    const steps = new Set(unavailable.map((f) => f.target));
    const other = summary.filter((f) => !(f.kind === "node_error" && steps.has(f.target))).filter((f) => f.kind !== "llm_unavailable");
    return {
      status: "error",
      title: "Language model unavailable",
      lines: [...unavailable.map((f) => f.text), "Nothing was booked or charged.", ...other.map((f) => f.text)],
    };
  }
  if (injections.length > 0) {
    const n = `${injectionCount} tool result${injectionCount === 1 ? "" : "s"}`;
    const title =
      phase === "completed"
        ? `${n} flagged and neutralised · reviewed by a human before payment`
        : phase === "ended"
          ? `${n} flagged and neutralised`
          : `Possible prompt injection · ${flagCount(flags)}`;
    const other = summary.filter((f) => f.kind !== "injection_flagged").map((f) => f.text);
    return { status: "warning", title, lines: [...injectionLines, ...other] };
  }
  return { status: "warning", title: `Run flagged · ${flagCount(flags)}`, lines: summary.map((f) => f.text) };
}
