import { describe, expect, it } from "vitest";
import { flagCount, flagsCopy, summarizeFlags } from "./flags";

describe("summarizeFlags", () => {
  it("dedupes with a count and readable text", () => {
    const [s, ...rest] = summarizeFlags([
      "injection_flagged:travel_search.search_trains",
      "injection_flagged:travel_search.search_trains",
    ]);
    expect(rest).toHaveLength(0);
    expect(s).toMatchObject({ kind: "injection_flagged", target: "travel_search.search_trains", count: 2 });
    // The banner title names the risk; the line only names the source and how many results.
    expect(s.text).toBe("Source: travel_search.search_trains, 2 results.");
  });

  it("keeps unknown kinds readable", () => {
    expect(summarizeFlags(["weird_thing"])[0].text).toBe("weird thing.");
  });
});

describe("flagCount", () => {
  it("counts flagged tool results", () => {
    const inj = "injection_flagged:travel_search.search_trains";
    expect(flagCount([inj, inj])).toBe("2 tool results flagged");
    expect(flagCount([inj])).toBe("1 tool result flagged");
  });
  it("counts other flags generically", () => {
    expect(flagCount(["policy_denied:payments.charge", "injection_flagged:a.b"])).toBe("2 flags raised");
    expect(flagCount([])).toBe("");
  });
});

describe("flagsCopy", () => {
  const inj = "injection_flagged:travel_search.search_trains";
  it("names a policy refusal and its reason as an error", () => {
    const c = flagsCopy(["policy_denied:payments.charge"], "ended", "tool requires human approval");
    expect(c.status).toBe("error");
    expect(c.title).toBe("Blocked by policy: payments.charge");
    expect(c.lines).toEqual([
      "The policy gate refused payments.charge: tool requires human approval.",
      "No charge was made.",
    ]);
  });
  it("words injection flags by phase and keeps the guard note visible", () => {
    expect(flagsCopy([inj, inj], "review").title).toBe("Possible prompt injection · 2 tool results flagged");
    expect(flagsCopy([inj, inj], "completed").title).toBe(
      "2 tool results flagged and neutralised · reviewed by a human before payment",
    );
    expect(flagsCopy([inj], "ended").title).toBe("1 tool result flagged and neutralised");
    expect(flagsCopy([inj], "live").lines[1]).toMatch(/structured fields only/);
  });
});

describe("flagsCopy for an unreachable model", () => {
  it("names the cause, drops the duplicate step failure and says nothing was charged", () => {
    const copy = flagsCopy(["llm_unavailable:supervisor", "node_error:supervisor"], "ended");
    expect(copy.status).toBe("error");
    expect(copy.title).toBe("Language model unavailable");
    expect(copy.lines).toEqual([
      "The supervisor step got no answer from the language model (unreachable or timed out).",
      "Nothing was booked or charged.",
    ]);
  });
});
