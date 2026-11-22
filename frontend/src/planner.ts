/**
 * Trip planner state and the request sentence it composes. The planner never calls a tool: it
 * writes the same kind of English sentence a person would type, and that goes through the normal
 * send path (the LLM extracts a typed trip request, code does the rest).
 */
import type { StationName } from "./network";

export type TripType = "one-way" | "return";
export type PartOfDay = "any" | "morning" | "afternoon" | "evening" | "after";

export interface LegTime {
  part: PartOfDay;
  /** "HH:MM", used when part is "after". */
  after: string;
}

export interface PlannerState {
  origin: StationName | null;
  destination: StationName | null;
  tripType: TripType;
  /** ISO dates, "YYYY-MM-DD". */
  departDate: string;
  returnDate: string;
  outbound: LegTime;
  back: LegTime;
  passengers: number;
}

export const MAX_PASSENGERS = 9;

/** The part-of-day choices, in the words the supervisor maps to windows (agents/graph.py). */
export const PARTS_OF_DAY: { value: PartOfDay; label: string }[] = [
  { value: "any", label: "Any time" },
  { value: "morning", label: "Morning (06:00–12:00)" },
  { value: "afternoon", label: "Afternoon (12:00–18:00)" },
  { value: "evening", label: "Evening (18:00–23:59)" },
  { value: "after", label: "After a time…" },
];

/** Adds `days` to an ISO date, in UTC so no zone can shift the day. */
export function addDays(iso: string, days: number): string {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + days)).toISOString().slice(0, 10);
}

/** Local "YYYY-MM-DD" of a moment. */
export function isoDay(at: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${at.getFullYear()}-${p(at.getMonth() + 1)}-${p(at.getDate())}`;
}

/** A week from today out, back the day after; morning out, evening back; one adult. */
export function initialPlanner(today: string): PlannerState {
  const departDate = addDays(today, 7);
  return {
    origin: null,
    destination: null,
    tripType: "return",
    departDate,
    returnDate: addDays(departDate, 1),
    outbound: { part: "morning", after: "09:00" },
    back: { part: "evening", after: "17:00" },
    passengers: 1,
  };
}

/**
 * Map click: the first station is the origin, the second the destination. Clicking the origin
 * again clears it; once both are set, a click starts a new route from that station.
 */
export function pickStation(
  sel: Pick<PlannerState, "origin" | "destination">,
  name: StationName,
): Pick<PlannerState, "origin" | "destination"> {
  if (sel.origin === null) return { origin: name, destination: null };
  if (sel.destination === null) {
    return name === sel.origin ? { origin: null, destination: null } : { origin: sel.origin, destination: name };
  }
  return { origin: name, destination: null };
}

const MONTHS = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];

/** "2026-12-05" -> "5 December 2026". */
export function longDate(iso: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  if (!m) return iso;
  return `${Number(m[3])} ${MONTHS[Number(m[2]) - 1]} ${m[1]}`;
}

const CLOCK = /^([01]\d|2[0-3]):[0-5]\d$/;
const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;

/** " in the morning", " after 17:00", or "" for any time. */
function timePhrase(t: LegTime): string {
  switch (t.part) {
    case "any":
      return "";
    case "after":
      return ` after ${t.after}`;
    default:
      return ` in the ${t.part}`;
  }
}

/** Why the planner cannot send yet, or null when the request is complete. */
export function plannerProblem(s: PlannerState, today?: string): string | null {
  if (!s.origin && !s.destination) return "Pick where you leave from and where you go.";
  if (!s.origin) return "Pick where you leave from.";
  if (!s.destination) return "Pick where you go.";
  if (s.origin === s.destination) return "Pick two different stations.";
  if (!ISO_DAY.test(s.departDate)) return "Pick a departure date.";
  if (today && s.departDate < today) return "The departure date is in the past.";
  if (s.tripType === "return") {
    if (!ISO_DAY.test(s.returnDate)) return "Pick a return date.";
    if (s.returnDate < s.departDate) return "The return date is before the departure date.";
  }
  if (s.outbound.part === "after" && !CLOCK.test(s.outbound.after)) return "Give the departure time as HH:MM.";
  if (s.tripType === "return" && s.back.part === "after" && !CLOCK.test(s.back.after))
    return "Give the return time as HH:MM.";
  if (!Number.isInteger(s.passengers) || s.passengers < 1 || s.passengers > MAX_PASSENGERS)
    return `Passengers: 1 to ${MAX_PASSENGERS}.`;
  return null;
}

/**
 * The request in plain English, e.g. "Train from Basel to Geneva on 5 December 2026 in the
 * morning, back on 6 December 2026 in the evening, 2 adults." Null while the form is incomplete.
 */
export function composeRequest(s: PlannerState): string | null {
  if (plannerProblem(s) !== null) return null;
  const people = `${s.passengers} adult${s.passengers === 1 ? "" : "s"}`;
  const out = `from ${s.origin} to ${s.destination} on ${longDate(s.departDate)}${timePhrase(s.outbound)}`;
  if (s.tripType === "one-way") return `One way ${out}, ${people}.`;
  return `Train ${out}, back on ${longDate(s.returnDate)}${timePhrase(s.back)}, ${people}.`;
}
