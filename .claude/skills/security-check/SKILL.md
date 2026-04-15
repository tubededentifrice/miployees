---
name: security-check
description: Red-team audit of a feature, spec, or code area. Investigates vulnerabilities, permission gaps, and PII / privacy leaks; suggests concrete fixes.
---

# Security-Check Skill

Act as a **red-team penetration tester**. Thoroughly investigate the
current feature or area — code or spec — for security problems,
permission gaps, and data-protection issues.

> For miployees, security overlaps heavily with **privacy**. The threat
> model is a small household deployment holding highly-personal data:
> staff identities, pay, schedules, photos, LLM conversations. The
> attacker of most concern is usually not a stranger on the public
> internet — it's a disgruntled ex-employee, a shared device, or an
> upstream model provider ingesting data it shouldn't.

## Scope

Analyse the area the user specifies for:

1. **Authentication & authorisation**
   - Missing or weak permission checks.
   - Privilege escalation paths (owner ↔ manager ↔ staff ↔ guest).
   - Passkey / WebAuthn edge cases (lost device, second credential,
     attestation trust).
   - Magic-link bootstrap flow — link reuse, expiry, enumeration.
   - API-token scopes — are they enforced everywhere, including the
     CLI?
   - IDOR (Insecure Direct Object Reference) — can user A read
     `/tasks/<uuid>` for user B's property?

2. **Input validation & injection**
   - SQL injection — any raw SQL, any `f"SELECT ... {x}"`? All queries
     parameterised?
   - Command injection in subprocess / shell calls.
   - Path traversal in evidence uploads, icons, attachments.
   - SSRF in iCal fetch, webhook targets, model provider calls.
   - XSS in HTMX fragments / Jinja templates — any `|safe`, any
     `Markup(...)` on user input?

3. **Data protection & privacy**
   - PII exposure in logs, responses, or error messages.
   - PII routed to upstream LLMs without the opt-in redaction seam
     (see [`docs/specs/11-llm-and-agents.md`](../../../docs/specs/11-llm-and-agents.md)).
   - Missing rate limiting on sensitive endpoints (login, magic-link
     send, webhook receive).
   - Insecure secret storage (`.env` in repo, plaintext API tokens in
     DB).
   - Missing or too-lax CORS.
   - Unencrypted backups.

4. **OWASP top 10 / defence-in-depth**
   - Broken access control.
   - Cryptographic failures — weak hashes on tokens, insecure random.
   - Security misconfiguration — DEBUG left on, verbose tracebacks in
     prod.
   - Vulnerable / outdated components — `/fix-osv-finding` hits.
   - SSRF.

5. **FastAPI / Python specifics**
   - Missing `Depends(...)` on an auth-required route.
   - Pydantic model accepts more fields than intended
     (`extra="allow"`).
   - Async route doing blocking I/O (holds the event loop, enables
     DoS by slow clients).
   - `Response.media_type` set to `text/html` on an endpoint that
     interpolates user input.

6. **Deployment & operations** (see
   [`docs/specs/16-deployment-operations.md`](../../../docs/specs/16-deployment-operations.md))
   - Anything bound to the public interface (217.182.203.57 or
     0.0.0.0 in dev/prod). **Must** be `127.0.0.1` or `tailscale0`.
   - Secrets in environment variables exported to worker subprocesses
     unintentionally.
   - Backup locations world-readable.

## Investigation process

1. **Identify the target.** What feature, spec section, or module is
   being audited? State it back.
2. **Map the attack surface.** All entry points — HTTP routes, CLI
   commands, background jobs, webhook receivers, iCal polls.
3. **Trace data flow.** Follow user input from request → dependency →
   service → DB → response (or → LLM → response). Mark every boundary.
4. **Check permission boundaries.** Verify each action checks the
   appropriate scope / role.
5. **Test assumptions.** What if the client bypasses client-side
   validation? What if the JWT is tampered? What if the passkey is
   registered on a second device first?
6. **Review related code.** Middleware, dependencies, shared
   utilities — issues compound across layers.

## Output format

For each finding:

```
**[SEVERITY] Issue title**
- Location: `path/to/file.py:line_number` (or `docs/specs/XX.md §Y`)
- Description: What the vulnerability is.
- Attack scenario: How an attacker would exploit it. Concrete, not
  hand-wavy.
- Fix: Concrete code change (or spec edit) to remediate.
```

Severity levels:

- **CRITICAL**: immediate exploitation risk, data-breach potential.
- **HIGH**: significant security impact, fix promptly.
- **MEDIUM**: defence-in-depth issue, should be addressed.
- **LOW**: minor issue or hardening opportunity.
- **INFO**: observation or best-practice suggestion.

### Summary

- Total issues by severity.
- Top items requiring immediate attention.
- Recommended remediation order (often: auth first, injection second,
  privacy third, ops last).

## Example checks

```python
# BAD: missing permission check
@router.delete("/tasks/{task_id}")
async def delete_task(task_id: UUID, session: AsyncSession = Depends(get_db)):
    task = await session.get(Task, task_id)
    await session.delete(task)  # Anyone can delete any task!

# GOOD: auth + ownership
@router.delete("/tasks/{task_id}")
async def delete_task(
    task_id: UUID,
    actor: Actor = Depends(require_scope("tasks:write")),
    session: AsyncSession = Depends(get_db),
):
    task = await session.get(Task, task_id)
    if task is None or task.property_id not in actor.property_ids:
        raise HTTPException(404)
    await session.delete(task)
```

```python
# BAD: SQL injection
rows = await session.execute(text(f"SELECT * FROM users WHERE name = '{name}'"))

# GOOD: parameterised
rows = await session.execute(
    text("SELECT * FROM users WHERE name = :name"),
    {"name": name},
)
```

```python
# BAD: PII to upstream LLM
resp = await client.messages.create(
    model="anthropic/claude-opus-4-6",
    messages=[{"role": "user", "content": f"Summarise: {raw_employee_note}"}],
)

# GOOD: through redaction seam
resp = await llm.summarise(raw_employee_note, redact=True)
```

```html
<!-- BAD: XSS -->
<div>{{ user_input|safe }}</div>

<!-- GOOD: auto-escaped (default) -->
<div>{{ user_input }}</div>
```

## Reference

Consult these specs for context:

- [`docs/specs/03-auth-and-tokens.md`](../../../docs/specs/03-auth-and-tokens.md)
  — auth implementation.
- [`docs/specs/11-llm-and-agents.md`](../../../docs/specs/11-llm-and-agents.md)
  — model routing, redaction, audit.
- [`docs/specs/15-security-privacy.md`](../../../docs/specs/15-security-privacy.md)
  — threat model, GDPR, secrets.
- [`docs/specs/16-deployment-operations.md`](../../../docs/specs/16-deployment-operations.md)
  — interface binding, backups, observability.

## After the audit

1. Report all findings with severity.
2. Provide concrete fixes with code (or spec) examples.
3. Highlight anything that should block deployment.
4. Create Beads tasks for non-blocking hardening work:

```bash
bd create "sec(<area>): <title>" --body "…" --labels "area:security,priority:<n>"
```

Suggest additional hardening if appropriate (rate limits, audit log
coverage, dependency version bumps via `/bump-deps`).
