# Architecture review — synelia-cloud-backend

Date: 2026-09-08. Consultant-level review, based on docs (`GUIDE-MODULE.md`, `PLAN-DIRECTEUR*.md`, `ADR/`), the schema, `depot.py`, `rls.py`, `travaux/moteur.py` header, `litellm/config.yaml`, and the two compose files — targeted reads, not an exhaustive audit.
Reviewer: Fable 5.1 (design/architecture consultant pass).

Context I'm judging against: a real but small multi-tenant cloud console, one OpenStack lab reachable from one host (dev01), 41 modules serving a ~544-operation contract, mostly built by one developer with agents. Not a hyperscale target.

## Decisions

1. **One polymorphic `ressources` table (type + org_id + JSON `donnees`) behind `Depot[T]`, instead of ~120 per-type tables (ADR 0001).**
   This is the decision that made the contract shippable: a new resource type is a string and a Pydantic class, zero migrations. The cost is real and already visible: filtering/sorting/pagination happen in Python after loading every row of that type for the org (`lister` → `tous` → `filtrer_trier_paginer`), so every list is O(rows-per-org). The ADR's own ~10^4 ceiling is honest and fine for this scale. Right call for now; the escape hatch (extract one type to a real table without touching routes) is credible because routes never see SQL. I'd ask: is anything already approaching that ceiling (audit is separate, but `smtp_messages`-style high-volume types would be)?

2. **Contract-first: Pydantic models generated from the frontend's `openapi.json`, never hand-edited; the route function name and status code come from the contract index.**
   The best structural decision in the repo. It gives a coverage counter (`contrat_diff`) and makes "did we build what the UI expects" a mechanical question. Cost: the backend cannot evolve a shape without going through the frontend generator (§13 explicitly refuses to), which is correct discipline but a bottleneck when the backend discovers the contract is wrong (e.g. lists returned as bare arrays that the web client now special-cases). Keep it.

3. **RLS as belt-and-braces behind the application `org_id` filter.**
   Intent is right, but as wired I don't think it's doing anything in production. The policy allows `org_id IS NULL OR app.org_id = ''`, so an unset context passes everything; and `ENABLE ROW LEVEL SECURITY` without `FORCE` doesn't apply to the table owner, and the app connects as `synelia`, the owner of the database. Unless there is a separate application role I didn't find, the real isolation is the `Depot._requete` filter alone. Follow-up: either add `FORCE ROW LEVEL SECURITY` + a non-owner app role, or rewrite the docstring so nobody relies on the belt.

4. **Jobs: one `202`/`TravailProvisioning` façade with two engines — `MoteurLocal` (in-process, default) and `MoteurTemporal` (optional extra). dev01 runs Local with no worker.**
   Pragmatic, and the "same façade" means Temporal can be switched on later without touching modules. But the consequence documented in GUIDE-MODULE is severe: executors run on the API's asyncio loop, and one un-wrapped SDK call froze the API for every tenant for 10+ minutes. The `asyncio.to_thread` rule is now a convention enforced by review, not by structure. For this scale I'd still not deploy Temporal, but I would run a second `synelia worker`-style process with the Local engine so provisioning can never block request handling. That's a small change with a large blast-radius reduction.

5. **`Simule`/`Reel` pair for every upstream, real activated only by an env var.**
   This is what let the whole contract exist before the lab was reachable, and it's why Vercel previews work (ADR 0002). Its failure mode is also the repo's most frequent bug class, by its own admission: an executor that only touches `depot.*` and reports `done` — indistinguishable from success. The "reconcile on read" pattern (`statut_serveur() == "absente"`) is the right mitigation. I'd want a single integration test per provisioning type that asserts the upstream object exists afterward; the guide asks for it but nothing enforces it.

6. **Schema created by `create_all` at startup; Alembic named as authoritative in production, but no migrations directory exists in the repo.**
   For a JSON-document store this is nearly free — the polymorphic table rarely changes — but identity/travaux/audit are real relational tables and the first column change on Postgres will be a hand operation. Low urgency, but the docstring currently promises something that isn't there.

7. **Native identity (argon2, TOTP, rotating refresh sessions, API keys) instead of Keycloak.**
   The contract's `connexion → mfa` shape genuinely fits poorly with a direct-grant IdP, and the schema is small and readable. Right for one team; the cost is owning SSO federation and the downstream OIDC provider yourselves (the plan's Phase 1 "Keycloak fallback at 4 weeks" checkpoint is a good hedge — was it re-evaluated?).

8. **LiteLLM as a config-only gateway in front of a single OpenRouter key; no LiteLLM database; no GPUs, by decision.**
   Exactly the right size: routing and quotas live in `ia_agents` where the tenant model already is, and there's nothing stateful to back up. The config file honestly records that 8 of 9 listed models are currently blocked by an account guardrail the key can't fix — that's an ops dependency outside the codebase worth escalating, not an architecture issue.

9. **Audit table append-only with a hash chain, in the same Postgres and same role as everything else.**
   The chain is only as trustworthy as the writer, and the writer owns the table. Fine as tamper-evidence for an honest operator; don't describe it as tamper-proof to an auditor without moving the chain head (or exports) off-box.

## Overall

The backend's shape is coherent and unusually well documented for its size; the "hard invariants" section of GUIDE-MODULE is the most valuable file in the repo because it records paid-for lessons. The two things I'd fix before anything else are cheap: make RLS actually apply (or stop claiming it), and take job execution out of the API process.
