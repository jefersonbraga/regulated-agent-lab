import { describe, expect, it } from "vitest";
import {
  fareClass,
  formatApproveBy,
  formatClock,
  formatCountdown,
  formatDay,
  formatDeadline,
  formatDuration,
  formatTime,
  windowLine,
} from "./format";

describe("formatDeadline", () => {
  it("labels UTC instants as UTC", () => {
    expect(formatDeadline("2026-11-21T10:15:30.123456+00:00")).toBe("2026-11-21 10:15 UTC");
    expect(formatDeadline("2026-11-21T10:15:30Z")).toBe("2026-11-21 10:15 UTC");
  });

  it("keeps any other offset explicit", () => {
    expect(formatDeadline("2026-11-21T11:15:00+01:00")).toBe("2026-11-21 11:15 (UTC+01:00)");
  });

  it("returns unknown input unchanged", () => {
    expect(formatDeadline("soon")).toBe("soon");
  });
});

describe("formatDay and formatTime", () => {
  it("show the day once and a compact time", () => {
    expect(formatDay("2026-12-01T07:02:00")).toBe("Tue 1 Dec 2026");
    expect(formatTime("2026-12-01T07:02:00")).toBe("07:02");
  });
});

describe("formatCountdown", () => {
  const deadline = "2026-11-21T10:15:30.123456+00:00";
  const at = Date.UTC(2026, 10, 21, 10, 15, 30);
  it("counts down in minutes", () => {
    expect(formatCountdown(deadline, at - 9 * 60_000 - 5_000)).toBe("expires in 9 min");
    expect(formatCountdown(deadline, at - 30_000)).toBe("expires in under a minute");
    expect(formatCountdown(deadline, at - 90 * 60_000)).toBe("expires in 1 h 30 min");
  });
  it("says expired at or after the deadline", () => {
    expect(formatCountdown(deadline, at + 1_000)).toBe("expired");
  });
  it("is empty for an unreadable deadline", () => {
    expect(formatCountdown("soon", at)).toBe("");
  });
});

describe("formatClock", () => {
  it("shows hours and minutes with the zone", () => {
    expect(formatClock("2026-11-21T13:47:12.5+00:00")).toBe("13:47 UTC");
  });
});

describe("formatApproveBy", () => {
  const now = Date.parse("2026-11-21T09:00:00Z");

  it("says today for a deadline later on the same date", () => {
    expect(formatApproveBy("2026-11-21T10:15:30.123456+00:00", now)).toBe("10:15 UTC today");
  });

  it("keeps the date for a deadline on another day", () => {
    expect(formatApproveBy("2026-11-22T00:05:00+00:00", now)).toBe("2026-11-22 00:05 UTC");
  });

  it("reads today in the deadline's own zone", () => {
    // 11:00 UTC on 21 Nov is already 01:00 on 22 Nov in UTC+14.
    const late = Date.parse("2026-11-21T11:00:00Z");
    expect(formatApproveBy("2026-11-22T02:30:00+14:00", late)).toBe("02:30 (UTC+14:00) today");
    expect(formatApproveBy("2026-11-21T23:30:00+00:00", late)).toBe("23:30 UTC today");
  });

  it("returns unknown input unchanged", () => {
    expect(formatApproveBy("soon", now)).toBe("soon");
  });
});

describe("offer formatting", () => {
  it("reads the class of a saver fare id and the travel time", () => {
    expect(fareClass("DR-ZUE-BER-20261201-0902-2nd-saver")).toBe("2nd");
    expect(fareClass("DR-ZUE-BER-20261201-0902-1st")).toBe("1st");
    expect(formatDuration("2026-12-01T09:02:00", "2026-12-01T09:58:00")).toBe("56 min");
    expect(formatDuration("2026-12-01T09:02:00", "2026-12-01T11:47:00")).toBe("2 h 45 min");
    expect(formatDuration("2026-12-01T23:30:00", "2026-12-02T00:30:00")).toBe("1 h");
  });

  it("words a departure window", () => {
    expect(windowLine({ earliest: "07:00:00", latest: null }, true)).toBe("No time given: showing trains from 07:00");
    expect(windowLine({ earliest: "06:00:00", latest: "12:00:00" }, false)).toBe("Morning 06:00–12:00");
    expect(windowLine({ earliest: "12:00:00", latest: "18:00:00" }, false)).toBe("Afternoon 12:00–18:00");
    expect(windowLine({ earliest: "18:00:00", latest: "23:59:00" }, false)).toBe("Evening 18:00–23:59");
    expect(windowLine({ earliest: "17:00:00", latest: null }, false)).toBe("From 17:00");
    expect(windowLine({ earliest: null, latest: "14:00:00" }, false)).toBe("Until 14:00");
    expect(windowLine({ earliest: "09:00:00", latest: "12:00:00" }, false)).toBe("09:00–12:00");
  });
});
