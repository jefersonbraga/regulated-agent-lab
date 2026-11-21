import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useEventPlayer } from "./useEventPlayer";
import type { RunEvent } from "./types";

const started: RunEvent = { type: "run_started", run_id: "r" };
const planner: RunEvent = { type: "node_started", node: "planner" };
const status: RunEvent = { type: "status", status: "running" };

afterEach(() => vi.useRealTimers());

describe("useEventPlayer", () => {
  it("applies events at once with pace 0", async () => {
    const apply = vi.fn();
    const { result } = renderHook(() => useEventPlayer(apply, 0));
    result.current.push(started);
    result.current.push(planner);
    expect(apply.mock.calls.map((c) => c[0])).toEqual([started, planner]);
    await expect(result.current.drained()).resolves.toBeUndefined();
  });

  it("spaces events out in order and resolves drained() after the last", async () => {
    vi.useFakeTimers();
    const apply = vi.fn();
    const { result } = renderHook(() => useEventPlayer(apply, 100));
    let drained = false;
    act(() => {
      for (const e of [started, planner, status]) result.current.push(e);
    });
    void result.current.drained().then(() => (drained = true));
    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(apply).toHaveBeenCalledTimes(1);
    // run_started holds the stage for 0.4 × pace.
    await act(async () => vi.advanceTimersByTimeAsync(40));
    expect(apply).toHaveBeenCalledTimes(2);
    expect(drained).toBe(false);
    // node_started holds it for a full pace.
    await act(async () => vi.advanceTimersByTimeAsync(100));
    expect(apply.mock.calls.map((c) => c[0])).toEqual([started, planner, status]);
    expect(drained).toBe(true);
  });
});
