import { describe, expect, it } from "vitest";
import { addDays, composeRequest, initialPlanner, longDate, pickStation, plannerProblem, type PlannerState } from "./planner";
import { LINKS, NETWORK_STATIONS, formatMinutes, suggestionLabel, SUGGESTED_TRIPS, travelMinutes } from "./network";

const base = (patch: Partial<PlannerState> = {}): PlannerState => ({
  ...initialPlanner("2026-11-21"),
  origin: "Basel",
  destination: "Geneva",
  departDate: "2026-12-05",
  returnDate: "2026-12-06",
  ...patch,
});

describe("composeRequest", () => {
  it("writes a return trip with a part of day per leg", () => {
    expect(composeRequest(base({ passengers: 2 }))).toBe(
      "Train from Basel to Geneva on 5 December 2026 in the morning, back on 6 December 2026 in the evening, 2 adults.",
    );
  });

  it("writes a one way trip, and leaves out a leg time of any", () => {
    expect(composeRequest(base({ tripType: "one-way", outbound: { part: "any", after: "09:00" } }))).toBe(
      "One way from Basel to Geneva on 5 December 2026, 1 adult.",
    );
  });

  it("uses the words the supervisor maps to windows: afternoon and after HH:MM", () => {
    const s = base({
      origin: "Zurich HB",
      destination: "Bern",
      outbound: { part: "afternoon", after: "09:00" },
      back: { part: "after", after: "17:30" },
      passengers: 9,
    });
    expect(composeRequest(s)).toBe(
      "Train from Zurich HB to Bern on 5 December 2026 in the afternoon, back on 6 December 2026 after 17:30, 9 adults.",
    );
    expect(composeRequest(base({ tripType: "one-way", outbound: { part: "after", after: "07:15" } }))).toBe(
      "One way from Basel to Geneva on 5 December 2026 after 07:15, 1 adult.",
    );
  });

  it("ignores the return leg's time on a one way trip, even when it is unreadable", () => {
    const s = base({ tripType: "one-way", back: { part: "after", after: "late" } });
    expect(composeRequest(s)).toBe("One way from Basel to Geneva on 5 December 2026 in the morning, 1 adult.");
  });

  it("composes nothing until the request is complete, and says what is missing", () => {
    const cases: [Partial<PlannerState>, string][] = [
      [{ origin: null, destination: null }, "Pick where you leave from and where you go."],
      [{ destination: null }, "Pick where you go."],
      [{ origin: "Geneva" }, "Pick two different stations."],
      [{ returnDate: "2026-12-04" }, "The return date is before the departure date."],
      [{ outbound: { part: "after", after: "25:00" } }, "Give the departure time as HH:MM."],
      [{ back: { part: "after", after: "" } }, "Give the return time as HH:MM."],
      [{ passengers: 0 }, "Passengers: 1 to 9."],
      [{ passengers: 10 }, "Passengers: 1 to 9."],
      [{ departDate: "" }, "Pick a departure date."],
    ];
    for (const [patch, problem] of cases) {
      expect(plannerProblem(base(patch))).toBe(problem);
      expect(composeRequest(base(patch))).toBeNull();
    }
    expect(plannerProblem(base({ departDate: "2026-11-20", returnDate: "2026-11-22" }), "2026-11-21")).toBe(
      "The departure date is in the past.",
    );
  });
});

describe("planner defaults and dates", () => {
  it("starts a week ahead, back the next day, one adult, no stations", () => {
    const s = initialPlanner("2026-12-30");
    expect([s.departDate, s.returnDate, s.passengers, s.origin, s.destination]).toEqual([
      "2027-01-06",
      "2027-01-07",
      1,
      null,
      null,
    ]);
    expect(addDays("2026-02-28", 1)).toBe("2026-03-01");
    expect(longDate("2026-12-05")).toBe("5 December 2026");
  });
});

describe("pickStation", () => {
  it("sets the origin, then the destination; the origin again clears; a third pick starts over", () => {
    let sel = pickStation({ origin: null, destination: null }, "Basel");
    expect(sel).toEqual({ origin: "Basel", destination: null });
    expect(pickStation(sel, "Basel")).toEqual({ origin: null, destination: null });
    sel = pickStation(sel, "Geneva");
    expect(sel).toEqual({ origin: "Basel", destination: "Geneva" });
    expect(pickStation(sel, "Bern")).toEqual({ origin: "Bern", destination: null });
  });
});

describe("network", () => {
  it("links every pair of the four stations once, with readable times", () => {
    const names = NETWORK_STATIONS.map((s) => s.name);
    expect(LINKS).toHaveLength(6);
    for (const a of names) for (const b of names) if (a !== b) expect(travelMinutes(a, b)).not.toBeNull();
    expect(travelMinutes("Geneva", "Zurich HB")).toBe(165);
    expect(formatMinutes(165)).toBe("2 h 45");
    expect(formatMinutes(56)).toBe("56 min");
    expect(formatMinutes(120)).toBe("2 h");
    expect(suggestionLabel(SUGGESTED_TRIPS[0])).toBe("Zurich HB → Geneva · across the country, ~2 h 45");
  });
});
