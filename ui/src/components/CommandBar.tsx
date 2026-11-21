import { useState } from "react";
import { ChatComposer } from "@astryxdesign/core/Chat";
import { Icon } from "@astryxdesign/core/Icon";
import { Text } from "@astryxdesign/core/Text";
import { Lock, TrainFront } from "lucide-react";
import { STATIONS } from "../format";
import { TripPlanner } from "./TripPlanner";

export const PLACEHOLDER = "Book a train, e.g. Basel to Geneva on 5 December 2026, 2 adults…";

/** What the agents can book, said before the first request rather than after a failed one. */
export const SCOPE =
  `Train trips only, between ${Object.values(STATIONS).join(", ").replace(/, ([^,]*)$/, " and $1")}: ` +
  "one way or return, 1 to 9 passengers.";

export interface CommandBarProps {
  onSend: (text: string) => void;
  busy: boolean;
  /** Why the composer is locked (a decision is pending); shown as a helper line under it. */
  lockedHint?: string;
  /** Today's local date (YYYY-MM-DD) for the planner's defaults; injectable for tests. */
  today?: string;
}

/**
 * The request end of the story: a guided trip planner (map, dates, passengers) that writes the
 * request sentence, and the free-text command line it shares the send path with.
 */
export function CommandBar({ onSend, busy, lockedHint, today }: CommandBarProps) {
  const [draft, setDraft] = useState("");
  const locked = busy || Boolean(lockedHint);

  function submit(text: string) {
    const t = text.trim();
    if (!t || locked) return;
    setDraft("");
    onSend(t);
  }

  return (
    <div className="command" role="region" aria-label="Request">
      <TripPlanner onSend={submit} onEdit={setDraft} isDisabled={locked} today={today} />
      <ChatComposer
        value={draft}
        onChange={setDraft}
        onSubmit={submit}
        isDisabled={locked}
        elevation="none"
        placeholder={PLACEHOLDER}
      />
      {!lockedHint && (
        <p className="command-locked" data-testid="composer-scope">
          <Icon icon={TrainFront} size="xsm" color="secondary" />
          <Text type="supporting" color="secondary">
            {SCOPE}
          </Text>
        </p>
      )}
      {lockedHint && (
        <p className="command-locked" data-testid="composer-locked">
          <Icon icon={Lock} size="xsm" color="secondary" />
          <Text type="supporting" color="secondary">
            {lockedHint}
          </Text>
        </p>
      )}
    </div>
  );
}
