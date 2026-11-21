import type { ComponentType, SVGProps } from "react";
import { ShieldCheck, Sparkles, UserCheck } from "lucide-react";
import { Icon } from "@astryxdesign/core/Icon";
import { Text } from "@astryxdesign/core/Text";

type Glyph = ComponentType<SVGProps<SVGSVGElement>>;

/** The design rule, as three short claims under the stage (see CLAUDE.md, docs/architecture.md). */
export const RULES: { id: string; icon: Glyph; title: string; line: string }[] = [
  { id: "propose", icon: Sparkles, title: "LLM proposes", line: "typed intent, trip and rationale only" },
  { id: "dispose", icon: ShieldCheck, title: "Code disposes", line: "every tool call passes the policy gate" },
  { id: "human", icon: UserCheck, title: "Human approves", line: "payment is bound to the quote hash" },
];

export function RuleStrip() {
  return (
    <ul className="rules" aria-label="LLM proposes, code disposes">
      {RULES.map((r) => (
        <li key={r.id} className="rule" data-rule={r.id}>
          <span className="rule-icon" aria-hidden="true">
            <Icon icon={r.icon} size="xsm" color="inherit" />
          </span>
          <Text type="supporting" weight="semibold">
            {r.title}
          </Text>
          <Text type="supporting" color="secondary" className="rule-line">
            {r.line}
          </Text>
        </li>
      ))}
    </ul>
  );
}
