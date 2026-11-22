/**
 * The Demo Rail network for the trip planner: 4 stations at approximate relative positions and
 * the typical direct travel time of each of the 6 links.
 *
 * LINKS mirrors services/travel_search/data.py DURATIONS_MIN (backend). It is a static table rather than
 * an API call so the planner works before any backend answers (and in demo mode), and the API
 * gains no endpoint; tests/test_ui_network_sync.py fails if the two ever disagree.
 */

export type StationName = "Zurich HB" | "Bern" | "Geneva" | "Basel";

export interface NetworkStation {
  name: StationName;
  code: string;
  /** One neutral line of geography for people who do not know the country. */
  blurb: string;
  /** Position on the schematic map, in viewBox units (0..320 x 0..260). */
  x: number;
  y: number;
  /** Which side of the dot the name sits on. */
  labelSide: "left" | "right";
}

/** Approximate relative positions: Basel north-west, Zurich HB north-east, Bern centre, Geneva south-west. */
export const NETWORK_STATIONS: NetworkStation[] = [
  { name: "Basel", code: "BSL", blurb: "on the Rhine, by France and Germany", x: 150, y: 30, labelSide: "left" },
  { name: "Zurich HB", code: "ZUE", blurb: "largest city and main rail hub", x: 262, y: 70, labelSide: "left" },
  { name: "Bern", code: "BER", blurb: "the capital, in the centre", x: 150, y: 128, labelSide: "right" },
  { name: "Geneva", code: "GVA", blurb: "French-speaking, on Lake Geneva", x: 36, y: 232, labelSide: "right" },
];

/** [station, station, typical direct travel time in minutes]; unordered pairs. Keep in sync with data.py. */
export const LINKS: [StationName, StationName, number][] = [
  ["Zurich HB", "Bern", 56],
  ["Zurich HB", "Geneva", 165],
  ["Zurich HB", "Basel", 53],
  ["Bern", "Geneva", 102],
  ["Bern", "Basel", 55],
  ["Geneva", "Basel", 162],
];

export function station(name: StationName): NetworkStation {
  return NETWORK_STATIONS.find((s) => s.name === name)!;
}

/** Typical minutes between two stations, or null for the same station / an unknown pair. */
export function travelMinutes(a: StationName, b: StationName): number | null {
  const link = LINKS.find(([x, y]) => (x === a && y === b) || (x === b && y === a));
  return link ? link[2] : null;
}

/** 56 -> "56 min", 165 -> "2 h 45", 120 -> "2 h". */
export function formatMinutes(minutes: number): string {
  if (minutes < 60) return `${minutes} min`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m === 0 ? `${h} h` : `${h} h ${String(m).padStart(2, "0")}`;
}

export interface SuggestedTrip {
  id: string;
  origin: StationName;
  destination: StationName;
  /** Why someone would take it, in a few neutral words. */
  why: string;
}

export const SUGGESTED_TRIPS: SuggestedTrip[] = [
  { id: "zue-gva", origin: "Zurich HB", destination: "Geneva", why: "across the country" },
  { id: "bsl-ber", origin: "Basel", destination: "Bern", why: "from the Rhine to the capital" },
  { id: "ber-gva", origin: "Bern", destination: "Geneva", why: "from the capital to the lake" },
  { id: "zue-bsl", origin: "Zurich HB", destination: "Basel", why: "a short hop to the Rhine" },
];

/** "Zurich HB → Geneva · across the country, ~2 h 45". */
export function suggestionLabel(t: SuggestedTrip): string {
  const minutes = travelMinutes(t.origin, t.destination);
  return `${t.origin} → ${t.destination} · ${t.why}${minutes ? `, ~${formatMinutes(minutes)}` : ""}`;
}
