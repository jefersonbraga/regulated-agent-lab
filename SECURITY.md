# Security policy

regulated-agent-lab is a lab: a multi-agent train-booking demo for the fictional operator
Demo Rail, with simulated data, a simulated payments institution and an optional card processor
in **test mode only**. It never moves real money and holds no real card numbers.

## Supported versions

Only the latest commit on `main` is supported. There are no released versions or backports.

## Reporting a vulnerability

Please report privately, not in a public issue or pull request:

1. Open the repository on GitHub, go to **Security → Advisories → Report a vulnerability**
   (GitHub's private vulnerability reporting).
2. Describe the issue, the affected component (`backend/`, `frontend/`, `infra/`), the steps to
   reproduce, and the impact you expect.

You can expect an acknowledgement within a few days. Fixes land on `main`, and the advisory is
published once a fix is available. Please do not test against deployments you do not own.

## Scope

In scope: the code and configuration in this repository, in particular the controls it
demonstrates:

- the gated tool client (policy, approval, injection guard, redaction, audit log);
- the approval flow and the binding of a charge to the approved quote;
- scoped agent tokens and single-use wallet tokens across the institutions;
- API authentication, sessions, the Origin check, rate limits and security headers;
- webhook signature verification and reconciliation;
- the container images and the production compose and Caddy setup in `infra/`.

Out of scope:

- anything that requires live payment keys: the lab accepts test-mode keys only (`sk_test_`,
  `rk_test_`), and a live-mode event is rejected by design;
- findings in third-party services (the card processor, the timetable API, LLM providers) or in
  dependencies without an exploit path through this repository (report those upstream);
- denial of service by volume, social engineering, and issues of deployments not built from
  this repository;
- the absence of production features listed under "Limitations and next steps" in the README.

Never include real secrets, real card numbers or personal data in a report.
