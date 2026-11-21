import type { ComponentType, SVGProps } from "react";
import { Ban, CircleCheck, CircleX, TriangleAlert } from "lucide-react";
import { Icon } from "@astryxdesign/core/Icon";
import { StatusDot } from "@astryxdesign/core/StatusDot";

type Glyph = ComponentType<SVGProps<SVGSVGElement>>;

export type MarkTone = "success" | "warning" | "error" | "refused" | "accent" | "neutral";

/** Tone of each run status (api RunView.status). */
export const STATUS_TONE: Record<string, MarkTone> = {
  running: "accent",
  awaiting_approval: "accent",
  awaiting_payment: "accent",
  completed: "success",
  rejected: "warning",
  needs_clarification: "warning",
  answered: "success",
  refused: "refused",
  failed: "error",
};

const GLYPH: Record<"success" | "warning" | "error" | "refused", Glyph> = {
  success: CircleCheck,
  warning: TriangleAlert,
  error: CircleX,
  refused: Ban,
};

/**
 * A status as glyph plus colour, never colour alone. The theme's dark status dots are pastel
 * (success, error and warning read near-white), so terminal states use a shaped icon painted
 * with the app's own status tokens (index.css --app-*-strong). Live states keep the dot.
 */
export function StatusMark({ tone, label, pulse }: { tone: MarkTone; label: string; pulse?: boolean }) {
  if (tone === "accent" || tone === "neutral") return <StatusDot variant={tone} label={label} isPulsing={pulse} />;
  return (
    <span className="status-mark" data-tone={tone} role="img" aria-label={label}>
      <Icon icon={GLYPH[tone]} size="sm" color="inherit" />
    </span>
  );
}
