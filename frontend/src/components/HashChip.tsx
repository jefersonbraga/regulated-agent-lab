import { useState } from "react";
import { Icon } from "@astryxdesign/core/Icon";
import { Text } from "@astryxdesign/core/Text";
import { Tooltip } from "@astryxdesign/core/Tooltip";
import { shortHash } from "../format";

/** A hash as 8 characters; the full value in a tooltip, copied on click. */
export function HashChip({ hash, label = "hash" }: { hash: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard?.writeText(hash);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }
  return (
    <Tooltip content={<span className="hash-full">{copied ? "Copied" : hash}</span>}>
      <button
        type="button"
        className="hash-chip"
        data-testid="hash-chip"
        data-copied={copied ? "true" : undefined}
        aria-label={`Copy ${label} ${hash}`}
        onClick={() => void copy()}
        onMouseLeave={() => setCopied(false)}
        onBlur={() => setCopied(false)}
      >
        <Text type="code">{shortHash(hash)}</Text>
        <Icon icon={copied ? "check" : "copy"} size="xsm" color="secondary" />
      </button>
    </Tooltip>
  );
}
