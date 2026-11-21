/** Format integer cents as e.g. "CHF 112.00" without floating-point arithmetic. */
export function formatCents(cents: number, currency = "CHF"): string {
  const sign = cents < 0 ? "-" : "";
  const abs = Math.abs(Math.trunc(cents));
  const units = Math.floor(abs / 100);
  const rest = String(abs % 100).padStart(2, "0");
  return `${sign}${currency} ${units}.${rest}`;
}

/** "CHF 200" for whole francs, "CHF 200.50" otherwise: limits read better without ".00". */
export function formatCentsShort(cents: number, currency = "CHF"): string {
  return Math.trunc(cents) % 100 === 0 ? `${currency} ${Math.trunc(cents) / 100}` : formatCents(cents, currency);
}

/**
 * An amount typed in francs ("200", "200.5", "200,50") as integer cents, parsed from the digits
 * (never through a float). Null for anything else.
 */
export function parseCents(text: string): number | null {
  const m = /^\s*(\d{1,7})(?:[.,](\d{1,2}))?\s*$/.exec(text);
  if (!m) return null;
  return Number(m[1]) * 100 + Number((m[2] ?? "0").padEnd(2, "0"));
}

/**
 * Fare class from the fare id suffix: "DR-IC-801-2nd" -> "2nd", and the saver fare
 * "DR-IC-801-2nd-saver" -> "2nd" too (the fare type is a separate field).
 */
export function fareClass(fareId: string): string {
  const id = fareId.endsWith("-saver") ? fareId.slice(0, -"-saver".length) : fareId;
  const i = id.lastIndexOf("-");
  return i >= 0 ? id.slice(i + 1) : id;
}

/** "2026-11-22T07:02:00" -> "2026-11-22 07:02". Kept string-based to avoid timezone shifts. */
export function formatDateTime(iso: string): string {
  const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(iso);
  return m ? `${m[1]} ${m[2]}` : iso;
}

/** Hashes on screen are 8 characters; the full value lives in a tooltip. */
export function shortHash(hash: string): string {
  return hash.slice(0, 8);
}

/**
 * Station codes from a train id, simulated ("DR-ZUE-BER-20261201-0702") or from the live
 * timetable ("LV-ZUE-BER-20261201-0732-IC1") -> ["ZUE", "BER"]; null for ids in another shape.
 */
export function stationCodes(trainId: string): [string, string] | null {
  const m = /^[A-Z]+-([A-Z]{3})-([A-Z]{3})-/.exec(trainId);
  return m ? [m[1], m[2]] : null;
}

/** Demo Rail station codes (see mcp_servers/travel_search). */
export const STATIONS: Record<string, string> = {
  ZUE: "Zurich HB",
  BER: "Bern",
  GVA: "Geneva",
  BSL: "Basel",
};

/** "ZUE" -> "Zurich HB"; unknown codes stay as they are. */
export function stationName(code: string): string {
  return STATIONS[code] ?? code;
}

/** Fare class for reading: "2nd" -> "2nd class". */
export function fareClassLabel(fareId: string): string {
  const c = fareClass(fareId);
  return /^\d(st|nd|rd|th)$/.test(c) ? `${c} class` : c;
}

/**
 * Approval deadline, e.g. "2026-11-21T10:15:30+00:00" -> "2026-11-21 10:15 UTC".
 * String-based like formatDateTime; the offset stays explicit so the deadline is never misread.
 */
export function formatDeadline(iso: string): string {
  const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:\d{2})$/.exec(iso);
  if (!m) return iso;
  const [, day, time, offset] = m;
  const zone = offset === "Z" || offset === "+00:00" ? "UTC" : `(UTC${offset})`;
  return `${day} ${time} ${zone}`;
}

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2026-12-01T07:02:00" -> "Tue 1 Dec 2026". The weekday is computed in UTC, so no zone shift. */
export function formatDay(iso: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!m) return iso;
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
  const weekday = WEEKDAYS[new Date(Date.UTC(y, mo - 1, d)).getUTCDay()];
  return `${weekday} ${d} ${MONTHS[mo - 1]} ${y}`;
}

/** "2026-12-01T07:02:00" -> "07:02". */
export function formatTime(iso: string): string {
  const m = /T(\d{2}:\d{2})/.exec(iso);
  return m ? m[1] : iso;
}

/** Epoch ms of an ISO instant, tolerating microseconds ("…:30.123456+00:00"); NaN if unreadable. */
export function parseInstant(iso: string): number {
  return Date.parse(iso.replace(/(\.\d{3})\d+/, "$1"));
}

/** Countdown to a deadline, e.g. "expires in 9 min", "expires in under a minute", "expired". */
export function formatCountdown(deadlineIso: string, nowMs: number): string {
  const at = parseInstant(deadlineIso);
  if (Number.isNaN(at)) return "";
  const left = at - nowMs;
  if (left <= 0) return "expired";
  const minutes = Math.floor(left / 60_000);
  if (minutes < 1) return "expires in under a minute";
  if (minutes < 60) return `expires in ${minutes} min`;
  return `expires in ${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

/** "2026-11-21T13:47:12.5+00:00" -> "13:47 UTC" (string-based, like formatDeadline). */
export function formatClock(iso: string): string {
  const m = /T(\d{2}:\d{2}).*?(Z|[+-]\d{2}:\d{2})?$/.exec(iso);
  if (!m) return iso;
  const zone = !m[2] || m[2] === "Z" || m[2] === "+00:00" ? "UTC" : `UTC${m[2]}`;
  return `${m[1]} ${zone}`;
}

/**
 * The approval deadline as a reader says it: "14:42 UTC today" when it falls on today's date
 * in its own zone, otherwise the full "2026-11-22 14:42 UTC". String-based like formatDeadline.
 */
export function formatApproveBy(iso: string, nowMs: number): string {
  const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?(Z|([+-])(\d{2}):(\d{2}))$/.exec(iso);
  if (!m) return iso;
  const [, day, time, offset, sign, oh, om] = m;
  const shiftMs = sign ? (sign === "-" ? -1 : 1) * (Number(oh) * 60 + Number(om)) * 60_000 : 0;
  const today = new Date(nowMs + shiftMs).toISOString().slice(0, 10);
  if (day !== today) return formatDeadline(iso);
  const zone = offset === "Z" || offset === "+00:00" ? "UTC" : `(UTC${offset})`;
  return `${time} ${zone} today`;
}

/** Minutes between two ISO wall-clock datetimes of the same zone (string-based, no zone shift). */
function wallMinutes(iso: string): number | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso);
  if (!m) return null;
  const day = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])) / 60_000;
  return day + Number(m[4]) * 60 + Number(m[5]);
}

/** Minutes from departure to arrival; null when either time is unreadable or arrival is first. */
export function durationMinutes(departIso: string, arriveIso: string): number | null {
  const a = wallMinutes(departIso);
  const b = wallMinutes(arriveIso);
  return a === null || b === null || b < a ? null : b - a;
}

/** Travel time, e.g. "56 min" or "2 h 45 min"; empty when either time is unreadable. */
export function formatDuration(departIso: string, arriveIso: string): string {
  const minutes = durationMinutes(departIso, arriveIso);
  if (minutes === null) return "";
  if (minutes < 60) return `${minutes} min`;
  return minutes % 60 === 0 ? `${minutes / 60} h` : `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

/** "07:00:00" -> "07:00". */
export function hhmm(time: string): string {
  return time.slice(0, 5);
}

/** Named windows the supervisor maps words to (agents/state.py); same bounds, same name. */
const NAMED_WINDOWS: [string, string, string][] = [
  ["Morning", "06:00", "12:00"],
  ["Afternoon", "12:00", "18:00"],
  ["Evening", "18:00", "23:59"],
];

/**
 * The departure window in words: "Morning 06:00–12:00", "From 17:00", "Until 14:00",
 * "09:00–12:00"; a defaulted window says no time was given.
 */
export function windowLine(window: { earliest: string | null; latest: string | null }, defaulted: boolean): string {
  const from = window.earliest ? hhmm(window.earliest) : null;
  const to = window.latest ? hhmm(window.latest) : null;
  if (defaulted) return `No time given: showing trains from ${from ?? "the first departure"}`;
  if (from && to) {
    const named = NAMED_WINDOWS.find(([, a, b]) => a === from && b === to);
    return named ? `${named[0]} ${from}–${to}` : `${from}–${to}`;
  }
  if (from) return `From ${from}`;
  if (to) return `Until ${to}`;
  return "Any time";
}
