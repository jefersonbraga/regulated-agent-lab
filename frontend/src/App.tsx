import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { LogOut, Moon, Sun, Wallet, Workflow } from "lucide-react";
import { AppShell } from "@astryxdesign/core/AppShell";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Heading } from "@astryxdesign/core/Heading";
import { Icon } from "@astryxdesign/core/Icon";
import { IconButton } from "@astryxdesign/core/IconButton";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { Text } from "@astryxdesign/core/Text";
import { TopNav } from "@astryxdesign/core/TopNav";
import {
  ApiError,
  cancelPayment,
  completeWalletSetup,
  getAudit,
  getMe,
  getMode,
  getQuote,
  getRun,
  getWallet,
  login,
  logout,
  reconcile,
  listRuns,
  refreshPayment,
  removeWalletCard,
  setWalletMandate,
  startWalletSetup,
  streamApproval,
  streamRun,
  verifyAudit,
  type ApiClient,
} from "./api";
import { AgentStage, NODE_SPECS, StageNotes } from "./components/AgentStage";
import { ApprovalCard } from "./components/ApprovalCard";
import { CommandBar } from "./components/CommandBar";
import { EvidenceChain, focusAuditBlock, isApproval, isFlagged, isProcessorEvent } from "./components/EvidenceChain";
import { FlagsBanner } from "./components/FlagsBanner";
import { LoginScreen } from "./components/LoginScreen";
import { PaymentCard } from "./components/PaymentCard";
import { OutcomeCard } from "./components/OutcomeCard";
import { RuleStrip } from "./components/RuleStrip";
import { WalletPanel } from "./components/WalletPanel";
import { Signals } from "./components/Signals";
import { RecentRuns } from "./components/RecentRuns";
import { STATUS_TONE, StatusMark } from "./components/StatusMark";
import { formatCents, formatCentsShort } from "./format";
import { eventsFromView, initialGraph, liveFlags, reduceGraph, shortTool, type GraphNodeId } from "./runGraph";
import type { AuditEntry, Me, Mode, PaymentModel, RunEvent, RunSummary, RunView, VerifyResult, WalletView } from "./types";
import { cardLabel, PROCESSOR, paymentStep } from "./processor";
import { useEventPlayer } from "./useEventPlayer";

/** Sent when the API runs without authentication; with it, the server records the signed-in user. */
const APPROVER = "demo-approver";

/** The live API (src/api.ts). */
export const liveClient: ApiClient = {
  streamRun,
  streamApproval,
  getQuote,
  getAudit,
  verifyAudit,
  getMode,
  listRuns,
  getRun,
  reconcile,
  refreshPayment,
  cancelPayment,
  getMe,
  login,
  logout,
  getWallet,
  startWalletSetup,
  completeWalletSetup,
  removeWalletCard,
  setWalletMandate,
};

/** Whose wallet the API uses when it runs without authentication (agents/booking.py). */
export const LOCAL_PAYER = "local-dev";

/** `?wallet=saved|canceled`: the processor sent the user back from its card form. */
export function parseWalletReturn(search: string): "saved" | "canceled" | null {
  const value = new URLSearchParams(search).get("wallet");
  return value === "saved" || value === "canceled" ? value : null;
}

/** What coming back from the card form did, in words. */
export function walletReturnNotice(back: "saved" | "canceled", view: WalletView | null): string {
  if (back === "canceled") return `You left ${PROCESSOR}'s card form without saving a card.`;
  if (view?.setup_state === "saved" && view.card) return `Card saved: ${cardLabel(view.card)}.`;
  if (view?.setup_state === "pending") return `${PROCESSOR} has not confirmed the card yet. Check again in a moment.`;
  return "No card was saved.";
}

/** "Visa ····4242 · limit CHF 200" under the wallet on the stage. */
export function walletNote(wallet: WalletView | null): string | null {
  if (!wallet) return null;
  const limit = `limit ${formatCentsShort(wallet.mandate_cents, wallet.currency)}`;
  return wallet.card ? `${cardLabel(wallet.card)} · ${limit}` : `no card saved · ${limit}`;
}

/** How many runs the history shows. */
export const RECENT_RUNS_LIMIT = 20;
/** A processor capture waits for the processor's webhook: re-read the run this often, a while. */
export const SETTLEMENT_POLL_MS = 4000;
export const SETTLEMENT_POLL_TRIES = 15;
/** A run waiting on the processor's payment page: re-read it this many times (webhook). */
export const PAYMENT_POLL_TRIES = 75;

/** `?run=<id>&checkout=success|cancel`: the processor sent the customer back to the console. */
export function parseCheckoutReturn(search: string): { runId: string; checkout: "success" | "cancel" } | null {
  const q = new URLSearchParams(search);
  const runId = q.get("run");
  const checkout = q.get("checkout");
  if (!runId || !/^[0-9a-f]{32}$/.test(runId)) return null;
  if (checkout !== "success" && checkout !== "cancel") return null;
  return { runId, checkout };
}

/** Readable error text: an Error's message without the "Error: " prefix. */
export function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** Why a second person decides: shown to the requester who waits and to the approver who decides. */
export const FOUR_EYES =
  "This is the four-eyes rule (segregation of duties): whoever asks the agent to spend money " +
  "cannot also authorize the spend, so a hijacked account or a manipulated agent cannot pay on its own.";

/**
 * Why `viewer` cannot decide a quote of a run requested by `requester`, or null when they can.
 * Mirrors the API's checks (approver role, REQUIRE_SEPARATE_APPROVER) so the card can explain
 * the rule before a click instead of showing the server's 403 after it.
 */
export function approverNote(viewer: Me | null, requester: string | null | undefined): string | null {
  if (!viewer || !viewer.auth_enabled) return null;
  if (viewer.role === "requester") {
    return (
      "Your account can request trips but cannot approve them. Sign in as an approver, for " +
      "example in a private window, to approve or reject this quote. It waits here until then."
    );
  }
  if (viewer.require_separate_approver && requester && requester === viewer.name) {
    return (
      "You requested this trip, so a second person must approve it. " +
      FOUR_EYES +
      " Sign in as the approver, for example in a private window: this quote waits for them, " +
      "and nothing is charged until they decide."
    );
  }
  return null;
}

/**
 * Why `viewer` cannot request trips (an approver-only account), or null when they can. Mirrors
 * the API's requester-role check so planning is locked with a reason instead of a 403 on send.
 */
export function requestNote(viewer: Me | null): string | null {
  if (!viewer || !viewer.auth_enabled || viewer.role !== "approver") return null;
  return "This account approves trips and cannot request them. Sign in as a requester to plan one.";
}

/**
 * The runs in `runs` whose quote `viewer` may decide now: waiting for approval and allowed by
 * the same rules as approverNote. An approver did not start these runs, so the page lists them
 * in the Human gate instead of leaving them to be found in the run history.
 */
export function awaitingFor(viewer: Me | null, runs: RunSummary[]): RunSummary[] {
  if (!viewer) return [];
  return runs.filter((r) => r.status === "awaiting_approval" && approverNote(viewer, r.requester) === null);
}

/** How often the run history refreshes while the page is idle, so new requests show up. */
const RECENT_REFRESH_MS = 15000;

/** Stream error types the server sends, in words a reader can act on. */
const KNOWN_FAILURES: Record<string, string> = {
  McpTransportError: "Lost connection to the travel tools",
};

/**
 * Why a run failed, as a sentence; the raw code stays in brackets for support. `tool` is the
 * call that was in flight when the stream broke, when the stage saw one.
 */
export function describeFailure(raw: string, tool?: string | null): string {
  const code = raw.trim();
  const known = KNOWN_FAILURES[code];
  if (known) return `${known}${tool ? ` during ${tool}` : ""} (${code}).`;
  return /[.!?]$/.test(raw) ? raw : `${raw}.`;
}

/**
 * Header badges for what is real in a run: "simulated" when nothing is, otherwise the real parts
 * ("live timetable", "test payments"). Fares and seat holds are always simulated.
 */
export function modeBadges(mode: Mode | null): { label: string; variant: "neutral" | "info" }[] {
  const badges: { label: string; variant: "neutral" | "info" }[] = [];
  if (mode?.timetable === "live") badges.push({ label: "live timetable", variant: "info" });
  if (mode?.payments === "stripe_test") badges.push({ label: "test payments", variant: "info" });
  return badges.length > 0 ? badges : [{ label: "simulated", variant: "neutral" }];
}

/** Shown in the locked composer while a quote waits for a human decision. */
export const DECIDE_FIRST = "Approve or reject the quote first";

/** Default spacing of live events on the stage, in ms (see useEventPlayer). */
export const DEFAULT_PACE_MS = 170;

export type ThemeMode = "light" | "dark";

/** The run will not change any more, so its audit trail is complete. */
export function isTerminal(status: RunView["status"]): boolean {
  return status !== "running" && status !== "awaiting_approval" && status !== "awaiting_payment";
}

/**
 * Moves focus to a card heading (tabIndex -1) and scrolls the card into view when it is off
 * screen, so the decision, not the composer, holds focus.
 */
function reveal(heading: HTMLElement | null) {
  if (!heading) return;
  heading.focus({ preventScroll: true });
  const { top, bottom } = heading.getBoundingClientRect();
  if (top < 0 || bottom > window.innerHeight) {
    const card = heading.closest<HTMLElement>("[role='region']") ?? heading;
    card.scrollIntoView?.({ behavior: "smooth", block: "center" });
  }
}

/** True while `el` is at least partly on screen; false without IntersectionObserver support. */
function useOnScreen(el: HTMLElement | null): boolean {
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    setVisible(false);
    if (!el || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver(([e]) => setVisible(e.isIntersecting));
    io.observe(el);
    return () => io.disconnect();
  }, [el]);
  return visible;
}

/** Phones and tablets: pinned while a quote waits and the approval card is off screen. */
function PendingApprovalBar({ total, onReview }: { total: string; onReview: () => void }) {
  return (
    <div className="approval-bar" role="region" aria-label="Pending approval">
      <StatusDot variant="accent" label="Pending" isPulsing />
      <Text weight="semibold">Approval needed · {total}</Text>
      <Button label="Review" size="sm" variant="primary" onClick={onReview} />
    </div>
  );
}

export interface AppProps {
  /** Spacing of live events on the stage; 0 applies each event as it arrives. */
  paceMs?: number;
  themeMode?: ThemeMode;
  onThemeModeChange?: (mode: ThemeMode) => void;
  /** Backend; the live API unless a recorded demo is replayed (src/demo.ts). */
  client?: ApiClient;
  /** Shown in the header when the client is a recorded demo. */
  demoLabel?: string;
  /** Sends the browser to the processor's hosted card form (tests replace it). */
  navigate?: (url: string) => void;
  /** Quiet time (ms) after which a running run says it is still working (tests shorten it). */
  quietNoticeMs?: number;
  /** How often (ms) a quote another person must decide is re-read (tests shorten it). */
  decisionPollMs?: number;
}

/**
 * While the quote on screen waits for someone else (segregation of duties), the page re-reads
 * the run this often, so the requester sees the decision without reloading. Plain polling of
 * GET /runs/{id}: the decision is rare and a few seconds late is fine.
 */
export const DECISION_POLL_MS = 4000;

/**
 * Default quiet time before the "still working" notice. Tool steps send events within a second
 * or two; a longer silence is the language model thinking (on-prem it can take up to its
 * timeout, about a minute, plus one retry), and without a word the page looks frozen.
 */
export const QUIET_NOTICE_MS = 8000;

function goTo(url: string) {
  window.location.assign(url);
}

type AuthState = { kind: "none" } | { kind: "checking" } | { kind: "signed_out" } | { kind: "signed_in"; me: Me };

/**
 * Sessions first: a client with getMe (the live API) shows the sign-in screen until
 * GET /auth/me answers; a recorded demo has none and opens the console directly. An API that
 * cannot be reached opens the console too, so its errors are shown where they always were.
 */
export default function App(props: AppProps) {
  const client = props.client ?? liveClient;
  const [auth, setAuth] = useState<AuthState>(() => (client.getMe ? { kind: "checking" } : { kind: "none" }));
  useEffect(() => {
    if (!client.getMe) return;
    let live = true;
    client
      .getMe()
      .then((me) => live && setAuth({ kind: "signed_in", me }))
      .catch((e: unknown) => {
        if (live) setAuth(e instanceof ApiError && e.status === 401 ? { kind: "signed_out" } : { kind: "none" });
      });
    return () => {
      live = false;
    };
  }, [client]);
  const signedOut = useCallback(() => setAuth({ kind: "signed_out" }), []);

  if (auth.kind === "checking") {
    return (
      <main className="login-screen" aria-busy="true">
        <Text color="secondary">Checking your session…</Text>
      </main>
    );
  }
  if (auth.kind === "signed_out" && client.login) {
    const signIn = client.login;
    return (
      <LoginScreen
        onLogin={async (name, password) => {
          setAuth({ kind: "signed_in", me: await signIn(name, password) });
        }}
      />
    );
  }
  const me = auth.kind === "signed_in" && auth.me.auth_enabled ? auth.me : null;
  async function signOut() {
    try {
      await client.logout?.();
    } finally {
      setAuth({ kind: "signed_out" });
    }
  }
  return (
    <Console
      key={me?.name ?? "anonymous"}
      {...props}
      client={client}
      user={me}
      onSignOut={me ? () => void signOut() : undefined}
      onUnauthorized={client.getMe ? signedOut : undefined}
    />
  );
}

interface ConsoleProps extends AppProps {
  client: ApiClient;
  /** The signed-in user; null when the API runs without authentication or for a demo. */
  user: Me | null;
  onSignOut?: () => void;
  /** A call answered 401 (the session ended): back to the sign-in screen. */
  onUnauthorized?: () => void;
}

function Console({
  paceMs = DEFAULT_PACE_MS,
  themeMode = "dark",
  onThemeModeChange,
  client,
  demoLabel,
  user,
  onSignOut,
  onUnauthorized,
  navigate = goTo,
  quietNoticeMs = QUIET_NOTICE_MS,
  decisionPollMs = DECISION_POLL_MS,
}: ConsoleProps) {
  const [graph, dispatch] = useReducer(reduceGraph, undefined, initialGraph);
  const [run, setRun] = useState<RunView | null>(null);
  // The run on screen, for timers that outlive a render.
  const runRef = useRef<RunView | null>(null);
  runRef.current = run;
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [verify, setVerify] = useState<VerifyResult | null>(null);
  const [verifiedAt, setVerifiedAt] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Why the current run failed mid-stream; shown in the outcome card, not as a page alert.
  const [failure, setFailure] = useState<string | null>(null);
  const [request, setRequest] = useState<string | null>(null);
  const [selected, setSelected] = useState<GraphNodeId | null>(null);
  const [mode, setMode] = useState<Mode | null>(null);
  const [recent, setRecent] = useState<RunSummary[]>([]);
  // The customer came back from the processor's payment page (?checkout=success|cancel).
  const [returned, setReturned] = useState<"success" | "cancel" | null>(null);
  // The signed-in user's wallet (saved card, limit), when the wallet model is allowed.
  const [wallet, setWallet] = useState<WalletView | null>(null);
  const [walletNotice, setWalletNotice] = useState<string | null>(null);
  const walletHeading = useRef<HTMLHeadingElement>(null);

  /** An API error on screen, unless the session ended: then the sign-in screen takes over. */
  const report = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401 && onUnauthorized) onUnauthorized();
      else setError(errorText(e));
    },
    [onUnauthorized],
  );

  // Which parts of a run are real. Unknown (demo client, API down) reads as simulated.
  useEffect(() => {
    let live = true;
    client
      .getMode?.()
      .then((m) => live && setMode(m))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [client]);

  // The wallet panel: only when this backend allows the agent to pay from a wallet.
  const walletAllowed = mode?.payments === "stripe_test" && (mode.payment_models ?? []).includes("wallet");
  useEffect(() => {
    if (!walletAllowed || !client.getWallet) return;
    let live = true;
    client
      .getWallet()
      .then((w) => live && setWallet(w))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [client, walletAllowed]);

  // Back from the processor's card form: the wallet reads the setup back (the URL proves nothing).
  useEffect(() => {
    const back = parseWalletReturn(window.location.search);
    if (!back) return;
    try {
      window.history.replaceState(null, "", window.location.pathname);
    } catch {
      /* the URL keeps its query; harmless */
    }
    if (back === "canceled" || !client.completeWalletSetup) {
      setWalletNotice(walletReturnNotice(back, null));
      return;
    }
    client
      .completeWalletSetup()
      .then((view) => {
        setWallet(view);
        setWalletNotice(walletReturnNotice(back, view));
      })
      .catch((e: unknown) => report(e));
    // Once per page load, like the checkout return.
  }, []);

  async function handleWalletSave() {
    if (!client.startWalletSetup) return;
    setError(null);
    try {
      const { setup_url } = await client.startWalletSetup();
      navigate(setup_url);
    } catch (e) {
      report(e);
    }
  }

  async function handleWalletRemove() {
    if (!client.removeWalletCard) return;
    setError(null);
    try {
      setWallet(await client.removeWalletCard());
      setWalletNotice("Card removed from your wallet.");
    } catch (e) {
      report(e);
    }
  }

  async function handleWalletLimit(limitCents: number) {
    if (!client.setWalletMandate) return;
    setError(null);
    try {
      const view = await client.setWalletMandate(limitCents);
      setWallet(view);
      setWalletNotice(`Limit set to ${formatCentsShort(view.mandate_cents, view.currency)} per booking.`);
    } catch (e) {
      report(e);
    }
  }

  const approvalHeading = useRef<HTMLHeadingElement>(null);
  const paymentHeading = useRef<HTMLHeadingElement>(null);
  const outcomeHeading = useRef<HTMLHeadingElement>(null);
  const [approvalCard, setApprovalCard] = useState<HTMLDivElement | null>(null);
  const approvalOnScreen = useOnScreen(approvalCard);

  // The run the stream last announced, so a failed stream can still load its audit trail.
  const runIdRef = useRef<string | null>(null);
  // When the last run event arrived, and whether the run has been quiet for too long.
  const lastEventAt = useRef(0);
  const [quiet, setQuiet] = useState(false);
  // Runs this page created (run_started only comes from POST /runs/stream): their requester is
  // the signed-in user, before the run history lists them.
  const startedHere = useRef(new Set<string>());
  const apply = useCallback((e: RunEvent) => {
    lastEventAt.current = Date.now();
    setQuiet(false);
    dispatch(e);
    if (e.type === "run_started") {
      runIdRef.current = e.run_id;
      startedHere.current.add(e.run_id);
    }
    if (e.type === "run_view") {
      const { type: _type, ...view } = e;
      setRun(view);
    }
  }, []);
  const player = useEventPlayer(apply, paceMs);

  const refreshAudit = useCallback(async (runId: string) => {
    try {
      setEntries(await client.getAudit(runId));
    } catch (e) {
      report(e);
    }
  }, [client, report]);

  // Run history: best effort, a failure leaves the last list (or none) on screen.
  const refreshRecent = useCallback(async () => {
    if (!client.listRuns) return;
    try {
      setRecent(await client.listRuns(RECENT_RUNS_LIMIT));
    } catch {
      /* the history is secondary: never an alert */
    }
  }, [client]);

  useEffect(() => {
    void refreshRecent();
  }, [refreshRecent]);

  // A signed-in user (an approver waiting for requests, in particular) sees new runs without
  // reloading; never while a stream is open.
  useEffect(() => {
    if (!user || busy) return;
    const id = setInterval(() => void refreshRecent(), RECENT_REFRESH_MS);
    return () => clearInterval(id);
  }, [user, busy, refreshRecent]);
  const waiting = awaitingFor(user, recent);

  // The approver is usually in another window: the tab title carries the count of quotes that
  // wait for them, visible from any tab. Restored when nothing waits or on unmount.
  const baseTitle = useRef(document.title);
  useEffect(() => {
    const base = baseTitle.current;
    const flag = `(${waiting.length}) Approval needed`;
    document.title = waiting.length > 0 ? (base ? `${flag} · ${base}` : flag) : base;
    return () => {
      document.title = base;
    };
  }, [waiting.length]);

  // A corner notice for quotes that started waiting for this approver, once per trip and page.
  // It never opens a quote by itself (the human chooses), and it waits while a stream runs or
  // another quote is open, so it does not cut into a decision.
  const announced = useRef(new Set<string>());
  const [notice, setNotice] = useState<RunSummary[]>([]);
  const waitingIds = waiting.map((r) => r.run_id).join(",");

  const runVerify = useCallback(async () => {
    try {
      setVerify(await client.verifyAudit());
      setVerifiedAt(Date.now());
    } catch (e) {
      report(e);
    }
  }, [client, report]);

  function lock(on: boolean) {
    busyRef.current = on;
    setBusy(on);
  }

  // While a stream is open, a long gap between events gets a "still working" notice; the next
  // event (or the end of the stream) clears it.
  useEffect(() => {
    if (!busy) {
      setQuiet(false);
      return;
    }
    lastEventAt.current = Date.now();
    const id = setInterval(
      () => {
        if (Date.now() - lastEventAt.current >= quietNoticeMs) setQuiet(true);
      },
      Math.max(10, Math.min(1000, quietNoticeMs / 2)),
    );
    return () => clearInterval(id);
  }, [busy, quietNoticeMs]);

  async function handleSend(text: string) {
    if (busyRef.current) return;
    lock(true);
    setError(null);
    setFailure(null);
    setRequest(text);
    setVerify(null);
    setSelected(null);
    setEntries([]);
    setRun(null);
    runIdRef.current = null;
    try {
      const view = await client.streamRun(text, player.push);
      await player.drained();
      await refreshAudit(view.run_id);
      // A run's first load is checked right away, so the badge never sits on "not verified".
      await runVerify();
    } catch (e) {
      await player.drained();
      // A run that started and then failed ends in its outcome card; anything else is an API error.
      if (runIdRef.current) setFailure(errorText(e));
      else report(e);
      // What the run did before it failed is still on the record.
      if (runIdRef.current) {
        await refreshAudit(runIdRef.current);
        await runVerify();
      }
    } finally {
      lock(false);
      void refreshRecent();
    }
  }

  /** Asks the processor about this run's capture now (reconciliation), instead of waiting for a webhook. */
  async function handleCheckProcessor(runId: string) {
    if (busyRef.current || !client.reconcile || !client.getRun) return;
    lock(true);
    setError(null);
    try {
      await client.reconcile();
      const view = await client.getRun(runId);
      setRun((cur) => (cur?.run_id === view.run_id ? view : cur));
      await refreshAudit(view.run_id);
      await runVerify();
      void refreshRecent();
    } catch (e) {
      report(e);
    } finally {
      lock(false);
    }
  }

  /** Reopens a run from the history: its view on the stage, its audit trail, a fresh verify. */
  async function handleOpenRun(runId: string): Promise<RunView | null> {
    if (busyRef.current || !client.getRun) return null;
    lock(true);
    setError(null);
    try {
      const view = await client.getRun(runId);
      setFailure(null);
      setSelected(null);
      setVerify(null);
      setEntries([]);
      setRequest(recent.find((r) => r.run_id === runId)?.request_redacted ?? null);
      // The same events a fallback replay builds: the stage, status and run view follow.
      for (const e of eventsFromView(view)) apply(e);
      await refreshAudit(view.run_id);
      await runVerify();
      return view;
    } catch (e) {
      report(e);
      return null;
    } finally {
      lock(false);
    }
  }

  /**
   * Hosted Checkout: asks the processor now (refresh) or closes the payment page (cancel). The
   * stage replays only the steps after `current`, like a resumed approval.
   */
  async function handlePayment(action: (runId: string) => Promise<RunView>, current: RunView) {
    if (busyRef.current) return;
    lock(true);
    setError(null);
    try {
      const view = await action(current.run_id);
      for (const e of eventsFromView(view, current)) apply(e);
      await refreshAudit(view.run_id);
      if (isTerminal(view.status)) await runVerify();
    } catch (e) {
      report(e);
    } finally {
      lock(false);
      void refreshRecent();
    }
  }

  // Back from the processor's page: reopen that run and, after a payment, ask the processor.
  // The query is removed first, so a reload (or StrictMode's second effect) does not repeat it.
  useEffect(() => {
    const back = parseCheckoutReturn(window.location.search);
    if (!back || !client.getRun) return;
    try {
      window.history.replaceState(null, "", window.location.pathname);
    } catch {
      /* the URL keeps its query; harmless */
    }
    setReturned(back.checkout);
    void (async () => {
      const view = await handleOpenRun(back.runId);
      if (view?.status === "awaiting_payment" && back.checkout === "success" && client.refreshPayment) {
        await handlePayment(client.refreshPayment, view);
      }
    })();
    // Once per page load, by design (the handlers read the first render's state, which is fine).
  }, []);

  async function handleDecision(approved: boolean, quoteHash: string, fareIds: string[], paymentModel?: PaymentModel) {
    if (!run || busyRef.current) return;
    lock(true);
    setError(null);
    try {
      // The hash is the server's quote for exactly these fares: approval binds to the selection,
      // and to the payment model the approver chose.
      const view = await client.streamApproval(
        run.run_id,
        {
          approved,
          approver: user?.name ?? APPROVER,
          quote_hash: quoteHash,
          fare_ids: fareIds,
          ...(paymentModel ? { payment_model: paymentModel } : {}),
        },
        player.push,
        run,
      );
      await player.drained();
      await refreshAudit(view.run_id);
      // The decision and payment blocks are now in the chain: check it again.
      if (isTerminal(view.status)) await runVerify();
    } catch (e) {
      await player.drained();
      report(e);
    } finally {
      lock(false);
      void refreshRecent();
    }
  }

  // A run waiting on the processor's payment page: pick up the webhook's completion when it lands.
  const payingRunId = run?.status === "awaiting_payment" ? run.run_id : null;
  useEffect(() => {
    const getRun = client.getRun;
    if (!payingRunId || !getRun) return;
    let live = true;
    let tries = 0;
    const timer = setInterval(() => {
      if (busyRef.current) return;
      tries += 1;
      if (tries >= PAYMENT_POLL_TRIES) clearInterval(timer);
      getRun(payingRunId)
        .then((view) => {
          if (!live || view.status === "awaiting_payment") return;
          clearInterval(timer);
          const cur = runRef.current;
          if (cur?.run_id !== view.run_id) return;
          for (const e of eventsFromView(view, cur)) apply(e);
          void refreshAudit(view.run_id);
          void runVerify();
          void refreshRecent();
        })
        .catch(() => undefined);
    }, SETTLEMENT_POLL_MS);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [payingRunId, client, apply, refreshAudit, refreshRecent, runVerify]);

  // A charged run whose processor has not reported yet: pick up its webhook when it lands.
  const pollRunId = run?.status === "completed" && run.settlement === "pending" ? run.run_id : null;
  useEffect(() => {
    const getRun = client.getRun;
    if (!pollRunId || !getRun) return;
    let live = true;
    let tries = 0;
    const timer = setInterval(() => {
      if (busyRef.current) return;
      tries += 1;
      if (tries >= SETTLEMENT_POLL_TRIES) clearInterval(timer);
      getRun(pollRunId)
        .then((view) => {
          if (!live || view.settlement === "pending") return;
          clearInterval(timer);
          setRun((cur) => (cur?.run_id === view.run_id ? view : cur));
          void refreshAudit(view.run_id);
          void refreshRecent();
        })
        .catch(() => undefined);
    }, SETTLEMENT_POLL_MS);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [pollRunId, client, refreshAudit, refreshRecent]);

  const pendingQuote = run?.status === "awaiting_approval" ? (run.interrupt?.quote ?? run.quote) : null;
  // Why the signed-in user cannot decide the quote on screen (someone else must), if so.
  const decisionNote =
    pendingQuote && run
      ? approverNote(
          user,
          recent.find((r) => r.run_id === run.run_id)?.requester ??
            (startedHere.current.has(run.run_id) ? user?.name : null),
        )
      : null;

  const quoteOpen = pendingQuote !== null;
  useEffect(() => {
    if (busy || quoteOpen) return;
    const fresh = waiting.filter((r) => !announced.current.has(r.run_id));
    if (fresh.length === 0) return;
    for (const r of fresh) announced.current.add(r.run_id);
    setNotice(fresh);
    // Keyed on the ids (waitingIds), not on the array, which is new on every render.
  }, [waitingIds, busy, quoteOpen]);
  // Trips decided meanwhile (elsewhere, or opened from the gate) leave the notice.
  const noticeRuns = notice.filter((r) => waiting.some((w) => w.run_id === r.run_id));
  const showNotice = noticeRuns.length > 0 && !busy && !quoteOpen;

  // That quote is decided in another session: re-read the run until its status moves on, then
  // replay the steps after it (approval, booking, payment) like any resumed run.
  const waitingRunId = decisionNote && run ? run.run_id : null;
  useEffect(() => {
    if (!waitingRunId || !client.getRun) return;
    const getRun = client.getRun;
    let stopped = false;
    const id = setInterval(async () => {
      const current = runRef.current;
      if (stopped || busyRef.current || !current || current.run_id !== waitingRunId) return;
      try {
        const view = await getRun(waitingRunId);
        if (stopped || view.status === current.status) return;
        for (const e of eventsFromView(view, current)) apply(e);
        await refreshAudit(view.run_id);
        if (isTerminal(view.status)) await runVerify();
        void refreshRecent();
      } catch {
        /* a missed poll is retried on the next tick; never an alert */
      }
    }, decisionPollMs);
    return () => {
      stopped = true;
      clearInterval(id);
    };
  }, [waitingRunId, client, decisionPollMs, apply, refreshAudit, runVerify, refreshRecent]);
  // A stream that failed before its final view still ends the run on screen.
  const failedRun: RunView | null =
    !run && !busy && graph.runId && graph.error
      ? { run_id: graph.runId, status: "failed", quote: null, interrupt: null, trajectory: [], payment: null, flags: [], settlement: null, checkout: null }
      : null;
  const pendingPayment = run?.status === "awaiting_payment" ? (run.checkout ?? null) : null;
  const ended = run && isTerminal(run.status) ? run : failedRun;
  // Flags as the stream shows them, until the run's own view lists them.
  const flags = run && run.run_id === graph.runId ? run.flags : liveFlags(graph);
  const approvalEntry = entries.find(isApproval);
  const denialEntry = entries.find((e) => e.event === "policy_denied");
  const clarificationEntry = entries.find((e) => e.event === "needs_clarification");
  const checkoutEnd = entries.find((e) => e.event === "checkout_ended");
  const flaggedEntries = entries.filter(isFlagged);
  const settlementEntry = [...entries].reverse().find(isProcessorEvent);
  const denyReason =
    graph.denyReason ?? (typeof denialEntry?.data["reason"] === "string" ? denialEntry.data["reason"] : null);
  const flagsPhase = ended ? (ended.status === "completed" ? "completed" : "ended") : "live";
  // A refusal is explained in full by its outcome card: the page banner keeps only other flags.
  const bannerFlags = ended?.status === "refused" ? flags.filter((f) => !f.startsWith("policy_denied:")) : flags;
  const decision = pendingQuote ? "pending" : pendingPayment ? "paying" : ended ? "done" : "none";

  // When a quote first arrives (or the run ends), the decision card takes focus.
  const focusKey = run ? `${run.run_id}:${decision}` : "";
  useEffect(() => {
    if (decision === "pending") reveal(approvalHeading.current);
    else if (decision === "paying") reveal(paymentHeading.current);
    else if (decision === "done") reveal(outcomeHeading.current);
  }, [focusKey, decision]);

  // Between run_started and the first status event the run is simply running.
  const status = graph.error && !busy ? "failed" : (graph.status ?? (graph.runId ? "running" : null));
  const live = busy && graph.current ? graph.current : null;
  const selectedNode = selected ? graph.nodes[selected] : null;

  const statusText = status ? status.replace(/_/g, " ") : "No run yet";
  const nextMode: ThemeMode = themeMode === "dark" ? "light" : "dark";

  return (
    <AppShell
      variant="wash"
      contentPadding={0}
      height="auto"
      topNav={
        <TopNav
          label="Main"
          heading={
            <span className="brand">
              <span className="brand-mark" aria-hidden="true">
                <Icon icon={Workflow} size="sm" color="inherit" />
              </span>
              <Heading level={1} className="brand-title">
                Agent console
              </Heading>
              {modeBadges(mode).map((b) => (
                <Badge key={b.label} variant={b.variant} label={b.label} data-testid="mode-badge" />
              ))}
              {demoLabel && <Badge variant="info" label={demoLabel} data-testid="demo-badge" />}
            </span>
          }
          endContent={
            <span className="nav-end">
              <Signals run={run} verify={verify} status={status} />
              {wallet && (
                <IconButton
                  label="Payment method"
                  variant="ghost"
                  size="sm"
                  icon={<Icon icon={Wallet} size="sm" color="inherit" />}
                  onClick={() => reveal(walletHeading.current)}
                  data-testid="nav-wallet"
                />
              )}
              {user && (
                <span className="nav-user" data-testid="nav-user">
                  <Text size="sm" color="secondary">
                    Signed in as <strong>{user.name}</strong>
                  </Text>
                  {onSignOut && (
                    <IconButton
                      label="Sign out"
                      variant="ghost"
                      size="sm"
                      icon={<Icon icon={LogOut} size="sm" color="inherit" />}
                      onClick={onSignOut}
                    />
                  )}
                </span>
              )}
              <IconButton
                label={`Switch to ${nextMode} mode`}
                variant="ghost"
                size="sm"
                icon={<Icon icon={themeMode === "dark" ? Sun : Moon} size="sm" color="inherit" />}
                onClick={() => onThemeModeChange?.(nextMode)}
              />
            </span>
          }
        />
      }
    >
      <div
        className="console"
        data-gate={pendingQuote ? "open" : undefined}
        data-decision={decision}
        data-mode={themeMode}
      >
        {error && (
          <Banner
            status="error"
            role="alert"
            title="API error"
            description={error}
            isDismissable
            onDismiss={() => setError(null)}
            className="console-error"
          />
        )}
        <div className="area-command">
          <CommandBar
            onSend={(t) => void handleSend(t)}
            busy={busy}
            lockedHint={pendingQuote ? DECIDE_FIRST : (requestNote(user) ?? undefined)}
          />
        </div>

        <section className="area-stage" aria-label="Run">
          <span className="sr-only" role="status" aria-live="polite">
            {status ? `Run ${statusText}${pendingQuote ? `: approve ${formatCents(pendingQuote.total_cents, pendingQuote.currency)}?` : ""}` : ""}
          </span>
          {request && (
            <p className="request-quote" title={request}>
              <Text type="supporting" color="secondary" data-testid="run-request">
                “{request}”
              </Text>
            </p>
          )}
          <div className="stage-caption">
            {status ? (
              <StatusMark tone={STATUS_TONE[status] ?? "accent"} label={statusText} pulse={status === "running" || busy} />
            ) : (
              <span className="signal-off" aria-hidden="true" />
            )}
            <Text weight="medium" data-testid="run-status">
              {statusText}
            </Text>
            {graph.runId && (
              <Text type="code" color="secondary" size="sm">
                {graph.runId.slice(0, 8)}
              </Text>
            )}
            {live && (
              <Text type="supporting" color="secondary" className="stage-live" data-testid="stage-live">
                {NODE_SPECS[live].label}
                {graph.lastTool ? ` · ${shortTool(graph.lastTool)}` : ""}
              </Text>
            )}
          </div>
          {quiet && busy && !pendingQuote && (
            <Banner
              status="info"
              data-testid="run-quiet"
              className="quiet-banner"
              title="Still working"
              description={
                `${live ? NODE_SPECS[live].label : "The run"} is waiting for the language model, which can take ` +
                "up to two minutes. The run is not stuck: it continues on its own, and if the model does " +
                "not answer in time it stops safely, and nothing is booked or charged."
              }
            />
          )}
          {/* The approval card carries its own banner while a quote waits. */}
          {bannerFlags.length > 0 && !pendingQuote && (
            <FlagsBanner flags={bannerFlags} phase={flagsPhase} reason={denyReason} />
          )}
          <AgentStage
            graph={graph}
            selected={selected}
            onSelect={setSelected}
            processor={mode?.payments === "stripe_test" ? `${PROCESSOR} · test mode` : null}
            paymentStep={mode?.payments === "stripe_test" ? paymentStep(mode.payments_flow, run?.payment_model) : null}
            showWallet={walletAllowed}
            walletNote={walletNote(wallet)}
            paymentModel={run?.payment_model ?? null}
          />
          <StageNotes graph={graph} />
          <RuleStrip />
          {selectedNode && (
            <Text type="supporting" color="secondary" as="p" className="stage-inspect" data-testid="stage-inspect">
              {NODE_SPECS[selectedNode.id].label} · {selectedNode.visits} visit{selectedNode.visits === 1 ? "" : "s"}
              {selectedNode.tools.map(
                (t) =>
                  ` · ${shortTool(t.tool)} ×${t.calls}${t.flagged ? ` (${t.flagged} flagged)` : ""}${
                    t.denied ? ` (${t.denied} denied)` : ""
                  }`,
              )}
            </Text>
          )}
        </section>

        <aside className="area-gate" aria-label="Decision">
          {pendingQuote && run ? (
            <ApprovalCard
              key={run.run_id}
              quote={pendingQuote}
              offer={run.interrupt?.offer ?? null}
              payments={mode?.payments}
              paymentsFlow={mode?.payments_flow}
              paymentOptions={run.payment_options ?? null}
              viewer={user?.name ?? LOCAL_PAYER}
              onQuote={(fareIds) => client.getQuote(run.run_id, fareIds)}
              approveBefore={run.interrupt ? run.interrupt.approve_before : undefined}
              flags={run.flags}
              flaggedEntries={flaggedEntries}
              onShowFlagged={flaggedEntries.length > 0 ? () => focusAuditBlock(flaggedEntries[0].seq) : undefined}
              request={request}
              onRequestNew={request && !busy && !requestNote(user) ? () => void handleSend(request) : undefined}
              onDecision={handleDecision}
              approverNote={decisionNote}
              headingRef={approvalHeading}
              cardRef={setApprovalCard}
            />
          ) : pendingPayment && run ? (
            <PaymentCard
              key={run.run_id}
              checkout={pendingPayment}
              returned={returned}
              busy={busy}
              fallback={run.payment_fallback ?? null}
              limit={wallet ? formatCentsShort(wallet.mandate_cents, wallet.currency) : null}
              onCheck={client.refreshPayment ? () => void handlePayment(client.refreshPayment!, run) : undefined}
              onCancel={client.cancelPayment ? () => void handlePayment(client.cancelPayment!, run) : undefined}
              headingRef={paymentHeading}
            />
          ) : ended ? (
            <OutcomeCard
              run={ended}
              approval={approvalEntry}
              onViewApproval={approvalEntry ? () => focusAuditBlock(approvalEntry.seq) : undefined}
              denial={denialEntry}
              onViewDenial={denialEntry ? () => focusAuditBlock(denialEntry.seq) : undefined}
              clarification={clarificationEntry}
              failure={failure ? describeFailure(failure, graph.failedTool) : null}
              auditCount={entries.length}
              settlementEntry={settlementEntry}
              checkoutEnd={checkoutEnd}
              onCheckProcessor={
                client.reconcile && client.getRun && ended.settlement === "pending" && !busy
                  ? () => void handleCheckProcessor(ended.run_id)
                  : undefined
              }
              onRetry={
                (ended.status === "failed" || ended.status === "refused") && request && !busy && !requestNote(user)
                  ? () => void handleSend(request)
                  : undefined
              }
              headingRef={outcomeHeading}
            />
          ) : (
            <Card padding={4} className="gate-card gate-idle" role="region" aria-label="Human gate">
              <div className="gate-card-body">
                <div className="gate-head">
                  <StatusDot variant={busy ? "accent" : "neutral"} label={busy ? "Working" : "Idle"} isPulsing={busy} />
                  <Heading level={2}>Human gate</Heading>
                </div>
                <Text color="secondary" as="p">
                  {busy
                    ? "Agents at work…"
                    : waiting.length > 0
                      ? `${waiting.length} ${waiting.length === 1 ? "trip is" : "trips are"} waiting for your approval.`
                      : "Nothing to approve."}
                </Text>
                {!busy && waiting.length > 0 && user?.require_separate_approver && (
                  <Text type="supporting" color="secondary" as="p" data-testid="gate-why">
                    Someone else requested these trips; you are the second person who decides. {FOUR_EYES}
                  </Text>
                )}
                {!busy && waiting.length > 0 && client.getRun && (
                  <ul className="gate-waiting" data-testid="gate-waiting">
                    {waiting.slice(0, 5).map((r) => (
                      <li key={r.run_id}>
                        <Button
                          label={`Review ${r.total_cents != null ? formatCents(r.total_cents, r.currency ?? "CHF") : "quote"}`}
                          variant="secondary"
                          size="sm"
                          onClick={() => void handleOpenRun(r.run_id)}
                        />
                        <Text type="supporting" color="secondary" size="sm" className="gate-waiting-line">
                          {r.requester ?? "someone"} · {r.request_redacted}
                        </Text>
                      </li>
                    ))}
                  </ul>
                )}
                <Text type="supporting" color="secondary" as="p">
                  {busy
                    ? "The quote lands here for your decision. Nothing is charged until you approve it."
                    : "Plan a trip or type a request. The agents propose a quote; payment waits here for a human decision bound to the quote hash."}
                </Text>
              </div>
            </Card>
          )}
        </aside>

        <div className="area-evidence">
          <EvidenceChain
            entries={entries}
            verify={verify}
            verifiedAt={verifiedAt}
            filter={selected}
            onClearFilter={() => setSelected(null)}
            onVerify={runVerify}
            recording={busy && entries.length === 0}
          />
          {client.listRuns && (
            <RecentRuns
              runs={recent}
              currentRunId={run?.run_id ?? graph.runId}
              onOpen={client.getRun ? (id) => void handleOpenRun(id) : undefined}
              disabled={busy}
            />
          )}
          {wallet && (
            <WalletPanel
              wallet={wallet}
              notice={walletNotice}
              busy={busy}
              onSave={handleWalletSave}
              onRemove={handleWalletRemove}
              onSetLimit={handleWalletLimit}
              headingRef={walletHeading}
            />
          )}
        </div>

        {pendingQuote && !approvalOnScreen && (
          <PendingApprovalBar
            total={formatCents(pendingQuote.total_cents, pendingQuote.currency)}
            onReview={() => reveal(approvalHeading.current)}
          />
        )}
        {showNotice && (
          <div className="approval-notice" role="status" aria-live="polite">
            <Banner
              status="info"
              data-testid="approval-notice"
              title={
                noticeRuns.length === 1
                  ? `${noticeRuns[0].requester ?? "Someone"} requested a trip`
                  : `${noticeRuns.length} trips wait for your approval`
              }
              description={
                noticeRuns.length === 1
                  ? `${noticeRuns[0].request_redacted}${
                      noticeRuns[0].total_cents != null
                        ? ` · ${formatCents(noticeRuns[0].total_cents, noticeRuns[0].currency ?? "CHF")}`
                        : ""
                    }. It waits for your decision.`
                  : "They are listed in the Human gate."
              }
              endContent={
                <span className="approval-notice-actions">
                  {client.getRun && (
                    <Button
                      label="Review"
                      variant="primary"
                      size="sm"
                      onClick={() => {
                        setNotice([]);
                        void handleOpenRun(noticeRuns[0].run_id);
                      }}
                    />
                  )}
                  <Button label="Later" variant="secondary" size="sm" onClick={() => setNotice([])} />
                </span>
              }
            />
          </div>
        )}
      </div>
    </AppShell>
  );
}
