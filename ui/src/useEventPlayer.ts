import { useCallback, useEffect, useLayoutEffect, useRef } from "react";
import type { RunEvent } from "./types";

/**
 * How long each event holds the stage before the next one, as a share of the pace. The server
 * sends a mock run in a few milliseconds; spacing the real events out keeps each step legible.
 * Nothing is invented: every frame is a server event, in server order.
 */
function weight(e: RunEvent): number {
  switch (e.type) {
    case "node_started":
      return e.node === "supervisor" ? 0.5 : 1;
    case "node_finished":
      return e.node === "supervisor" ? 0.25 : 0.6;
    case "tool_call":
      return 0.55;
    case "tool_result":
      return 0.35;
    case "policy_denied":
    case "tool_error":
      return 1;
    default:
      return 0.4;
  }
}

/**
 * Plays run events into `apply` one at a time, `paceMs` apart (0: immediately). `drained()`
 * resolves once everything pushed so far has been applied.
 */
export function useEventPlayer(apply: (e: RunEvent) => void, paceMs: number) {
  const applyRef = useRef(apply);
  useLayoutEffect(() => {
    applyRef.current = apply;
  });
  const queue = useRef<RunEvent[]>([]);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const waiters = useRef<(() => void)[]>([]);

  const step = useCallback(() => {
    timer.current = null;
    const e = queue.current.shift();
    if (e) applyRef.current(e);
    if (queue.current.length > 0) {
      timer.current = setTimeout(step, e ? weight(e) * paceMs : 0);
    } else {
      for (const w of waiters.current.splice(0)) w();
    }
  }, [paceMs]);

  const push = useCallback(
    (e: RunEvent) => {
      if (paceMs <= 0) {
        applyRef.current(e);
        return;
      }
      queue.current.push(e);
      if (timer.current === null) timer.current = setTimeout(step, 0);
    },
    [paceMs, step],
  );

  const drained = useCallback(
    () =>
      queue.current.length === 0 && timer.current === null
        ? Promise.resolve()
        : new Promise<void>((resolve) => waiters.current.push(resolve)),
    [],
  );

  useEffect(
    () => () => {
      if (timer.current !== null) clearTimeout(timer.current);
    },
    [],
  );

  return { push, drained };
}
