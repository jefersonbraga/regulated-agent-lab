import { useRef, useState, type FormEvent } from "react";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Fingerprint, Link2, ShieldCheck, Sparkles, UserCheck, Workflow } from "lucide-react";
import { ApiError } from "../api";
import heroUrl from "../assets/login-hero.jpg";

/** The thesis, in three steps, as the evaluator meets it before signing in. */
const PILLARS = [
  { icon: Sparkles, title: "LLM proposes", line: "Typed intent and trip only. The model never calls a tool." },
  { icon: ShieldCheck, title: "Code disposes", line: "Search, pricing and every tool call pass a policy gate." },
  { icon: UserCheck, title: "Human approves", line: "Payment is bound to the exact quote hash you saw." },
] as const;

/** What is real in this demo; the console header repeats it per run. */
const REAL = ["On-prem or cloud LLM", "Live timetable", "Stripe test mode", "Hash-chained audit"] as const;

export interface LoginScreenProps {
  /** Resolves once signed in; rejects with ApiError 401 (wrong credentials) or 429 (wait). */
  onLogin: (name: string, password: string) => Promise<void>;
}

/** Why sign-in failed, in words; never echoes the password. */
export function loginError(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 401) return "Wrong name or password.";
    if (e.status === 429) return "Too many attempts. Wait a minute, then try again.";
    if (e.status === 403) return "This page's address is not allowed to sign in to the API.";
    if (e.status === 0) return "The API cannot be reached.";
  }
  return "Sign-in failed. Try again in a moment.";
}

/** Shown when GET /auth/me answers 401: the console needs a demo account. */
export function LoginScreen({ onLogin }: LoginScreenProps) {
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // A second Enter before React re-renders must not send a second login.
  const busyRef = useRef(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busyRef.current || !name || !password) return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    try {
      await onLogin(name.trim(), password);
    } catch (err) {
      setError(loginError(err));
      setPassword("");
      setBusy(false);
      busyRef.current = false;
    }
  }

  return (
    <main className="login-screen">
      <section className="login-hero" aria-label="About this demo">
        <img className="login-hero-image" src={heroUrl} alt="" />
        <div className="login-hero-shade" aria-hidden="true" />
        <div className="login-hero-content">
          <p className="login-brand">
            <span className="login-brand-mark" aria-hidden="true">
              <Workflow size="1em" />
            </span>
            regulated-agent-lab
          </p>
          <h1 className="login-headline">Agents that can spend money need more than a good prompt.</h1>
          <p className="login-sub">
            A supervised multi-agent booking console with regulated-grade controls: human approval before payment,
            least privilege per agent and an audit trail you can verify.
          </p>
          <ul className="login-pillars">
            {PILLARS.map(({ icon: PillarIcon, title, line }) => (
              <li key={title}>
                <span className="login-pillar-icon" aria-hidden="true">
                  <PillarIcon size="1.1em" />
                </span>
                <span>
                  <strong>{title}</strong>
                  <span className="login-pillar-line">{line}</span>
                </span>
              </li>
            ))}
          </ul>
          <ul className="login-real" aria-label="Real in this demo">
            {REAL.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        </div>
      </section>

      <section className="login-panel">
        <Card padding={5} className="login-card" role="region" aria-label="Sign in">
          <form className="login-form" onSubmit={(e) => void submit(e)} noValidate>
            <span className="login-lock" aria-hidden="true">
              <Fingerprint size="1.25em" />
            </span>
            <Heading level={2}>Sign in to the agent console</Heading>
            <Text color="secondary" as="p">
              Accounts are issued per evaluator. Every request and approval is recorded under your name in the
              audit chain.
            </Text>
            {error && <Banner status="error" title={error} role="alert" />}
            <TextInput
              label="Name"
              value={name}
              onChange={setName}
              autoComplete="username"
              htmlName="username"
              isRequired
              hasAutoFocus
            />
            <TextInput
              label="Password"
              type="password"
              value={password}
              onChange={setPassword}
              autoComplete="current-password"
              htmlName="password"
              isRequired
            />
            <Button type="submit" label="Sign in" variant="primary" isLoading={busy} isDisabled={!name || !password} />
            <dl className="login-roles" aria-label="Roles">
              <div>
                <dt>Requester</dt>
                <dd>plans trips and gets quotes</dd>
              </div>
              <div>
                <dt>Approver</dt>
                <dd>approves payments, can be required to be someone else</dd>
              </div>
            </dl>
            <p className="login-note">
              <Link2 size="1em" aria-hidden="true" /> Test mode only: Stripe test cards, no real money moves.
            </p>
          </form>
        </Card>
      </section>
    </main>
  );
}
