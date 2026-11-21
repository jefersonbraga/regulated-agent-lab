import type { ReactNode } from "react";
import { ShieldCheck } from "lucide-react";
import { Banner } from "@astryxdesign/core/Banner";
import { Icon } from "@astryxdesign/core/Icon";
import { Text } from "@astryxdesign/core/Text";
import { flagsCopy, type FlagsPhase } from "../flags";

export interface FlagsBannerProps {
  flags: string[];
  /** Live run, approval review, completed, or otherwise ended: sets the tense of the title. */
  phase?: FlagsPhase;
  /** Why the policy gate refused a call, when known (from the stream or the audit entry). */
  reason?: string | null;
  endContent?: ReactNode;
}

/**
 * A run's flags with their explanation in view. A policy refusal is an error banner naming the
 * tool and reason; flagged tool output is a warning that says what the guard did.
 */
export function FlagsBanner({ flags, phase = "live", reason, endContent }: FlagsBannerProps) {
  if (flags.length === 0) return null;
  const copy = flagsCopy(flags, phase, reason);
  // Reviewed and paid: a neutral record row, so a successful booking does not read as a warning.
  if (phase === "completed" && copy.status === "warning") {
    return (
      <div className="flags-note" data-testid="run-flags" data-tone="note" role="note">
        <span className="flags-note-icon" aria-hidden="true">
          <Icon icon={ShieldCheck} size="sm" color="inherit" />
        </span>
        <span className="flags-note-body">
          <Text weight="medium">{copy.title}</Text>
          {copy.lines.map((l) => (
            <Text key={l} type="supporting" color="secondary">
              {l}
            </Text>
          ))}
        </span>
        {endContent}
      </div>
    );
  }
  return (
    <Banner
      status={copy.status}
      data-testid="run-flags"
      data-tone={copy.status}
      className="flags-banner"
      title={copy.title}
      description={
        copy.lines.length > 0 ? (
          <span className="flag-lines">
            {copy.lines.map((l) => (
              <span key={l}>{l}</span>
            ))}
          </span>
        ) : undefined
      }
      endContent={endContent}
    />
  );
}
