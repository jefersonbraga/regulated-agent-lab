import { eventsFromView } from "./runGraph";
import { readSse } from "./sse";
import type {
  ApprovalRequest,
  AuditEntry,
  Me,
  Mode,
  Quote,
  RunEvent,
  RunSummary,
  RunView,
  VerifyResult,
  WalletView,
} from "./types";

export const API_URL: string = (import.meta.env.VITE_API_URL ?? "http://localhost:8000").replace(
  /\/$/,
  "",
);

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

/** Every call sends the session cookie (HttpOnly, SameSite=Strict, set by POST /auth/login). */
const CREDENTIALS: RequestCredentials = "include";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    credentials: CREDENTIALS,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiError(res.status, `${res.status} ${res.statusText} ${text}`.trim());
  }
  return (await res.json()) as T;
}

/** GET /auth/me: the signed-in user; ApiError 401 when there is no valid session. */
export function getMe(): Promise<Me> {
  return request<Me>("/auth/me");
}

/** POST /auth/login: sets the session cookie. 401 wrong name or password, 429 too many tries. */
export function login(name: string, password: string): Promise<Me> {
  return request<Me>("/auth/login", { method: "POST", body: JSON.stringify({ name, password }) });
}

/** POST /auth/logout: clears the session cookie (idempotent). */
export async function logout(): Promise<void> {
  await request<unknown>("/auth/logout", { method: "POST" });
}

/**
 * POST /runs/{run_id}/payment/refresh: asks the processor about the run's hosted Checkout now,
 * so the run completes without waiting for (or without any) webhook.
 */
export function refreshPayment(runId: string): Promise<RunView> {
  return request<RunView>(`/runs/${encodeURIComponent(runId)}/payment/refresh`, { method: "POST" });
}

/** POST /runs/{run_id}/payment/cancel: closes the payment page; the run fails unless already paid. */
export function cancelPayment(runId: string): Promise<RunView> {
  return request<RunView>(`/runs/${encodeURIComponent(runId)}/payment/cancel`, { method: "POST" });
}

/** GET /wallet: the signed-in user's saved card (brand, last four, expiry) and spending limit. */
export function getWallet(): Promise<WalletView> {
  return request<WalletView>("/wallet");
}

/** POST /wallet/setup: the processor's hosted card form (mode=setup); the browser goes there. */
export function startWalletSetup(): Promise<{ setup_url: string }> {
  return request<{ setup_url: string }>("/wallet/setup", { method: "POST" });
}

/** POST /wallet/setup/complete: back from the processor (?wallet=saved), the wallet reads it back. */
export function completeWalletSetup(): Promise<WalletView> {
  return request<WalletView>("/wallet/setup/complete", { method: "POST" });
}

/** POST /wallet/card/remove: forgets the card here and detaches it at the processor. */
export function removeWalletCard(): Promise<WalletView> {
  return request<WalletView>("/wallet/card/remove", { method: "POST" });
}

/** POST /wallet/mandate: the spending limit per booking, integer cents (audited when changed). */
export function setWalletMandate(limitCents: number): Promise<WalletView> {
  return request<WalletView>("/wallet/mandate", {
    method: "POST",
    body: JSON.stringify({ limit_cents: limitCents }),
  });
}

export function getMode(): Promise<Mode> {
  return request<Mode>("/mode");
}

export function createRun(message: string): Promise<RunView> {
  return request<RunView>("/runs", { method: "POST", body: JSON.stringify({ message }) });
}

/** POST /reconcile: reads every test-mode capture back from the processor and updates settlements. */
export function reconcile(): Promise<unknown> {
  return request<unknown>("/reconcile", { method: "POST" });
}

export function getRun(runId: string): Promise<RunView> {
  return request<RunView>(`/runs/${encodeURIComponent(runId)}`);
}

/** GET /runs: recent runs, newest first, with the request redacted by the server. */
export function listRuns(limit = 20): Promise<RunSummary[]> {
  return request<RunSummary[]>(`/runs?limit=${encodeURIComponent(String(limit))}`);
}

export function submitApproval(runId: string, body: ApprovalRequest): Promise<RunView> {
  return request<RunView>(`/runs/${encodeURIComponent(runId)}/approval`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/**
 * The server's quote (total and hash) for one fare per leg of a paused run's offer. Pure: nothing
 * is held or charged. 404 unknown run, 409 not awaiting approval, 422 selection not in the offer.
 */
export function getQuote(runId: string, fareIds: string[]): Promise<Quote> {
  return request<Quote>(`/runs/${encodeURIComponent(runId)}/quote`, {
    method: "POST",
    body: JSON.stringify({ fare_ids: fareIds }),
  });
}

export function getAudit(runId?: string): Promise<AuditEntry[]> {
  const q = runId ? `?run_id=${encodeURIComponent(runId)}` : "";
  return request<AuditEntry[]>(`/audit${q}`);
}

export function verifyAudit(): Promise<VerifyResult> {
  return request<VerifyResult>("/audit/verify");
}

// ---------------------------------------------------------------------------------------------
// Live run events: POST /runs/stream and POST /runs/{run_id}/approval/stream (server-sent events
// over fetch, since EventSource cannot POST). Each call resolves with the final RunView and hands
// every event to `onEvent` as it arrives. When streaming is unavailable it falls back to the
// plain endpoints and replays events rebuilt from the RunView, so the caller sees one story.
// ---------------------------------------------------------------------------------------------

export type OnRunEvent = (event: RunEvent) => void;

/** Thrown when the stream itself cannot be used; the caller then falls back. */
class StreamUnavailable extends Error {}

const RECOVER_TRIES = 20;
const RECOVER_DELAY_MS = 250;

function parseEvent(data: string): RunEvent | null {
  try {
    const value = JSON.parse(data) as unknown;
    return value && typeof value === "object" && "type" in value ? (value as RunEvent) : null;
  } catch {
    return null;
  }
}

/** A run whose stream broke after it started: read its view until it stops running. */
async function recover(runId: string): Promise<RunView> {
  let view = await getRun(runId);
  for (let i = 0; i < RECOVER_TRIES && view.status === "running"; i++) {
    await new Promise((r) => setTimeout(r, RECOVER_DELAY_MS));
    view = await getRun(runId);
  }
  return view;
}

/**
 * Streams one request. `fallbackStatuses`: HTTP statuses that mean "no stream endpoint here" (so
 * the plain endpoint is tried); any other non-2xx is a real answer and is thrown as ApiError.
 */
async function stream(
  path: string,
  body: unknown,
  onEvent: OnRunEvent,
  fallbackStatuses: number[],
): Promise<RunView> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, {
      method: "POST",
      credentials: CREDENTIALS,
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    throw new StreamUnavailable(String(e));
  }
  if (!res.ok) {
    if (res.status >= 500 || fallbackStatuses.includes(res.status)) throw new StreamUnavailable(String(res.status));
    const text = await res.text().catch(() => "");
    throw new ApiError(res.status, `${res.status} ${res.statusText} ${text}`.trim());
  }
  const source: ReadableStream<Uint8Array> =
    res.body ?? new Blob([await res.text()]).stream();
  let runId: string | null = null;
  let final: RunView | null = null;
  try {
    for await (const data of readSse(source)) {
      const event = parseEvent(data);
      if (!event) continue;
      if (event.type === "run_started") runId = event.run_id;
      if (event.type === "error") {
        onEvent(event);
        const status = event.status_code ?? 500;
        throw new ApiError(status, event.detail ?? event.error_type ?? "run failed");
      }
      onEvent(event);
      if (event.type === "run_view") {
        const { type: _type, ...view } = event;
        final = view;
      }
    }
  } catch (e) {
    if (e instanceof ApiError) throw e;
    // The request reached the server: never resend it (that could start a second run).
    if (!runId) throw new ApiError(0, `stream failed: ${String(e)}`);
  }
  if (final) return final;
  if (!runId) throw new ApiError(0, "stream ended before the run started");
  // The connection dropped mid-run; the server keeps going, so read the outcome instead.
  const view = await recover(runId);
  for (const e of eventsFromView(view).slice(1)) onEvent(e);
  return view;
}

function replay(view: RunView, previous: RunView | null, onEvent: OnRunEvent): RunView {
  for (const e of eventsFromView(view, previous)) onEvent(e);
  return view;
}

/** Starts a run and streams its events up to the approval pause (or the end). */
export async function streamRun(message: string, onEvent: OnRunEvent): Promise<RunView> {
  try {
    return await stream("/runs/stream", { message }, onEvent, [404, 405]);
  } catch (e) {
    if (!(e instanceof StreamUnavailable)) throw e;
    return replay(await createRun(message), null, onEvent);
  }
}

/**
 * Sends the human decision and streams the resumed run. 404 (unknown run) and 409 (not awaiting
 * approval, or a resume already running) are real answers and are never retried.
 * `previous`: the paused view, so a fallback replays only the steps after it.
 */
export async function streamApproval(
  runId: string,
  body: ApprovalRequest,
  onEvent: OnRunEvent,
  previous: RunView | null = null,
): Promise<RunView> {
  try {
    return await stream(`/runs/${encodeURIComponent(runId)}/approval/stream`, body, onEvent, [405]);
  } catch (e) {
    if (!(e instanceof StreamUnavailable)) throw e;
    return replay(await submitApproval(runId, body), previous, onEvent);
  }
}

/** What the console needs from a backend; the live API by default, a recorded demo in dev. */
export interface ApiClient {
  streamRun: typeof streamRun;
  streamApproval: typeof streamApproval;
  /** POST /runs/{run_id}/quote: the exact total and hash for a selection, before approving. */
  getQuote: typeof getQuote;
  getAudit: typeof getAudit;
  verifyAudit: typeof verifyAudit;
  /** Optional: recorded demos have no backend mode and show "simulated". */
  getMode?: typeof getMode;
  /** Optional: GET /runs (run history); a client without it shows no recent runs. */
  listRuns?: typeof listRuns;
  /** Optional: GET /runs/{run_id}, to reopen a run from the history. */
  getRun?: typeof getRun;
  /** Optional: POST /reconcile, to confirm a capture with the processor without webhooks. */
  reconcile?: typeof reconcile;
  /** Optional: hosted Checkout, ask the processor now / close the payment page. */
  refreshPayment?: typeof refreshPayment;
  cancelPayment?: typeof cancelPayment;
  /** Optional: the agent-platform wallet (saved card, spending limit). */
  getWallet?: typeof getWallet;
  startWalletSetup?: typeof startWalletSetup;
  completeWalletSetup?: typeof completeWalletSetup;
  removeWalletCard?: typeof removeWalletCard;
  setWalletMandate?: typeof setWalletMandate;
  /** Optional: sessions. A client without getMe (recorded demos) needs no login. */
  getMe?: typeof getMe;
  login?: typeof login;
  logout?: typeof logout;
}
