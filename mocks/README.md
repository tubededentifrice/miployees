# miployees — UI preview mocks

Disposable, hard-coded preview of the manager and employee UIs while
the real application hasn't been built. No DB, no auth, no real
business logic. The container runs as a non-root user (`miployees:10001`,
per `docs/specs/16`). Every mutation (tick a checklist item, approve an
expense, etc.) is an in-memory toggle that lives until restart.

The goal is to make `docs/specs/14-web-frontend.md` tangible — every
spec-listed route renders something, the design tokens match, the
vocabulary matches.

## Running

```bash
docker compose -f mocks/docker-compose.yml up -d --build
```

- Local: http://127.0.0.1:8100
- Public: https://dev.miployees.com (via Pangolin / Traefik with
  badger auth; same wiring as `../fj2`)

## Audience toggle

Top banner has **Employee · Manager** pills (sets a cookie and
redirects to that audience's home) and a **☾ / ☀** theme toggle
(light is primary, dark is manual per §14). There's also a link to
`/styleguide` for the component gallery.

## Route map (matches §14)

**Public / unauthenticated**

- `/login` — passkey sign-in
- `/recover` — enter a break-glass code (admin CLI kicks it off)
- `/enroll/<token>` — passkey enrollment landing page
- `/guest/<token>` — tokenized guest welcome page (wifi, access,
  check-out checklist from `guest_visible` items)

**Employee (mobile-first PWA)**

- `/today`, `/week`, `/task/<id>` (sticky CTA, conditional
  "Complete with photo", skip-with-reason modal, evidence note)
- `/issues/new`, `/expenses`, `/messages`, `/shifts`, `/me`

Bottom nav per §14: **Today · Week · Issues · Expenses · Me** plus a
capability-gated chat bubble (shown when `chat.assistant = true`).

**Manager (desktop-first)**

- `/dashboard` — stats, tasks, approvals, issues, leaves
- `/properties`, `/property/<id>`, `/property/<id>/closures`
- `/stays` — four-layer calendar (stays · turnover · closures · leave)
- `/employees`, `/employee/<id>`, `/employee/<id>/leaves`, `/leaves`
- `/templates`, `/schedules`, `/instructions`, `/instructions/<id>`,
  `/inventory`, `/pay`
- `/approvals`, `/expenses` (approvals queue — same URL as the
  employee, audience-dependent view per §14)
- `/audit`, `/webhooks`, `/llm`, `/settings`

**Dev / ops**

- `/styleguide`, `/healthz`, `/readyz`, `/metrics`

## Design tokens

Hand-written CSS keyed off semantic classes (per project CLAUDE.md —
no utility frameworks). Tokens follow §14:

- Palette: Paper `#FAF7F2` / Ink `#1F1A14` / Moss `#3F6E3B` / Rust
  `#B04A27` / Sand `#D9A441` / Sky `#4F7CA8` / Night `#F4EFE6` (dark).
- Typography: Fraunces (display, variable `opsz`), Inter Tight
  (body), JetBrains Mono (dev-facing).
- Grain texture at 3–6% on warm paper; moss-tinted shadows.
- Dark theme via `[data-theme="dark"]` lifted for AA contrast.
- `prefers-reduced-motion` respected; focus ring is a Moss 2px outline.

## Pangolin setup (one-time, if not already done)

The container joins the existing `traefik-proxy` network and
registers Traefik labels — same pattern as `../fj2`. For badger to
gate the domain, `dev.miployees.com` needs to exist as a **resource**
in the Pangolin dashboard (target `miployees-mocks:8000`). DNS is
already CNAMEd to this host.

## Files

- `app/main.py` — all FastAPI routes
- `app/mock_data.py` — the fake household (3 properties, 5 staff,
  7 tasks, 5 stays, ~45 other rows across instructions / inventory /
  leaves / payslips / audit / agents)
- `app/templates/` — Jinja2, three base layouts (`base`,
  `employee_base`, `manager_base`) sharing one design system
- `app/static/styles.css` — hand-written, ~900 lines, both themes
- `Dockerfile` — Python 3.12-slim, `USER miployees:miployees`
- `docker-compose.yml` — Traefik labels + `user: "10001:10001"`

## Removing

```bash
docker compose -f mocks/docker-compose.yml down
rm -rf mocks/
```
