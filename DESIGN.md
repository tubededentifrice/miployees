---
version: alpha
name: crew.day
description: Warm, paper-textured house-management UI — Moss-primary, mobile-first, semantic-CSS, light/dark themed.
colors:
  paper: "#FAF7F2"
  paper-2: "#F3ECE0"
  paper-3: "#EBE1D0"
  card: "#FFFDF9"
  card-raised: "#FFFCF5"
  ink: "#1F1A14"
  ink-2: "#524A3E"
  ink-3: "#8C8274"
  ink-4: "#B5AD9F"
  line: "#E7E0D1"
  line-strong: "#D6CCB7"
  primary: "#3F6E3B"
  primary-2: "#2F5A2C"
  primary-3: "#254623"
  destructive: "#B04A27"
  warning: "#D9A441"
  info: "#4F7CA8"
typography:
  display-lg:
    fontFamily: Fraunces
    fontSize: 1.85rem
    fontWeight: 600
    lineHeight: 1.15
    letterSpacing: -0.012em
    fontVariation: '"opsz" 40'
  display-md:
    fontFamily: Fraunces
    fontSize: 1.65rem
    fontWeight: 600
    lineHeight: 1.1
    letterSpacing: -0.02em
    fontVariation: '"opsz" 72'
  display-sm:
    fontFamily: Fraunces
    fontSize: 1.22rem
    fontWeight: 600
    lineHeight: 1.25
    letterSpacing: -0.012em
  headline-md:
    fontFamily: Fraunces
    fontSize: 1.35rem
    fontWeight: 600
    lineHeight: 1.25
    letterSpacing: 0
  section-title:
    fontFamily: Fraunces
    fontSize: 14px
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: 0.09em
  body-lg:
    fontFamily: Inter Tight
    fontSize: 16px
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: 0
  body-md:
    fontFamily: Inter Tight
    fontSize: 15px
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: 0
  body-sm:
    fontFamily: Inter Tight
    fontSize: 13px
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: 0
  label-md:
    fontFamily: Inter Tight
    fontSize: 14px
    fontWeight: 500
    lineHeight: 1.3
    letterSpacing: 0
  label-sm:
    fontFamily: Inter Tight
    fontSize: 12px
    fontWeight: 500
    lineHeight: 1.4
    letterSpacing: 0
  mono-sm:
    fontFamily: JetBrains Mono
    fontSize: 11px
    fontWeight: 400
    lineHeight: 1.4
    letterSpacing: 0
spacing:
  0: 0
  1: 2
  2: 4
  3: 6
  4: 8
  5: 10
  6: 12
  7: 14
  8: 16
  9: 20
  10: 24
  11: 32
  12: 40
  13: 48
  14: 56
rounded:
  tag: 4px
  control: 6px
  card: 10px
  modal: 14px
  pill: 999px
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "#FFFFFF"
    typography: "{typography.label-md}"
    rounded: "{rounded.pill}"
    padding: "8px 14px"
    height: 40px
  button-ghost:
    backgroundColor: transparent
    textColor: "{colors.ink-2}"
    typography: "{typography.label-md}"
    rounded: "{rounded.pill}"
    padding: "8px 14px"
    height: 40px
  button-destructive:
    backgroundColor: "{colors.destructive}"
    textColor: "#FFFFFF"
    typography: "{typography.label-md}"
    rounded: "{rounded.pill}"
    padding: "8px 14px"
    height: 40px
  chip:
    backgroundColor: "{colors.paper-2}"
    textColor: "{colors.ink-2}"
    typography: "{typography.label-sm}"
    rounded: "{rounded.pill}"
    padding: "3px 10px"
  tag:
    backgroundColor: "{colors.paper-2}"
    textColor: "{colors.ink-2}"
    typography: "{typography.label-sm}"
    rounded: "{rounded.tag}"
    padding: "2px 8px"
  card:
    backgroundColor: "{colors.card}"
    textColor: "{colors.ink}"
    rounded: "{rounded.card}"
    padding: "16px"
  input:
    backgroundColor: "{colors.card}"
    textColor: "{colors.ink}"
    typography: "{typography.body-md}"
    rounded: "{rounded.control}"
    padding: "10px 12px"
    height: 44px
  checkbox:
    backgroundColor: transparent
    rounded: "{rounded.control}"
    size: 22px
  modal:
    backgroundColor: "{colors.card}"
    textColor: "{colors.ink}"
    rounded: "{rounded.modal}"
    padding: "24px"
---

# crew.day — DESIGN.md

Style guide for agents writing code under `mocks/web/`, `app/web/`,
and `site/web/`. Tokens above are normative; the prose below explains
*why* and *when*.

The living implementation lives in
[`mocks/web/src/styles/tokens.css`](mocks/web/src/styles/tokens.css)
and [`mocks/web/src/styles/globals.css`](mocks/web/src/styles/globals.css).

**On divergence between this file and the CSS, neither side wins
silently.** An agent who notices a token, type level, radius,
component property, or any other normative value disagreeing with
the implementation MUST stop and resolve it via `AskUserQuestion`
before continuing other work — present both values, surface the
likely cause (intentional change to one side without a paired
update; recent refactor; spec drift) and ask which is correct.
Once the user decides, the agent fixes the wrong side (either edit
this file's frontmatter / prose, or edit `tokens.css` /
`globals.css` / the relevant component) in the same turn so the
two stay aligned. Do not paper over the gap with a comment, do
not "make it match" by guessing, and do not defer the fix to a
later PR — divergence is a contract bug, treat it as one.

## Overview

crew.day is house-management software for short-term rental
operators, their workers, and their clients. The brand is
**warm, paper-textured, and quietly confident** — a small-business
ledger digitised, not a spreadsheet. Users are reaching for the
phone between guests, on the way to a property, mid-shift; the UI
has to read instantly under bright daylight and feel calm, not
alarming, when something needs attention.

Three audiences share the codebase:
- **Workers** on phones, mostly in motion. Glanceable cards,
  thumb-safe tap targets, offline-first PWA.
- **Owners and managers** at desks, switching properties.
  Dense list views, keyboard navigation, agent-first.
- **Clients** in a narrower portal. Read-mostly, no agent.

Across all three the same tokens, the same components, the same
shape language. A user who knows one surface should feel oriented
on the next.

## Colors

The palette is built on **warm neutrals** — `paper` is a creamy
off-white, never `#FFF`; `ink` is a brown-black, never `#000`.
Solids feel printed, not pixelated. Accent hues are loaded
semantically and used sparingly.

- **Paper** (`paper`, `paper-2`, `paper-3`) — page and surface
  washes. `paper` is the page; `paper-2` is the muted block
  background (chips, inline code, hover states); `paper-3` is the
  recessed wash (timeline tracks, scrubbed rails).
- **Card** (`card`, `card-raised`) — elevated content sits on
  these slightly brighter creams to read above the page.
- **Ink** (`ink`, `ink-2`, `ink-3`, `ink-4`) — the text ramp.
  `ink` for primary content; `ink-2` for secondary labels; `ink-3`
  for muted metadata; `ink-4` for disabled / strike-through.
- **Line** (`line`, `line-strong`) — hairlines. `line` between
  rows; `line-strong` for inputs and outlined buttons.
- **Moss** = `primary` — the only "go" colour. CTAs, active state,
  focus ring, progress fills, completed checkboxes. Moss is rare in
  dashboards, which is part of how the product feels distinct;
  resist using it as decoration.
- **Rust** = `destructive` — irreversible action, error, decline,
  past-due. Never as accent.
- **Sand** = `warning` — pending, awaiting approval, "needs
  attention". Diagonal hatching on bars where the state is
  in-flight rather than terminal.
- **Sky** = `info` — information, links, personal-task left tick.
  Lower visual weight than Moss on purpose.

**Soft variants** (`*-soft`) are 8-18% washes used for chip
backgrounds, banner fills, and tinted backgrounds where the solid
would shout.

**Light is primary; dark is first-class.** Both themes ship a
complete map under `[data-theme="light"]` and `[data-theme="dark"]`
on `<html>`/`<body>`. Never hard-code a hex outside `tokens.css` —
read tokens via `var(--paper)`, `var(--moss)`, etc. The token
disappears under dark mode if you bypass it.

**Accessibility floor:** every text/background pair in the palette
meets WCAG 2.2 AA contrast (4.5:1 for body, 3:1 for ≥18pt or bold).
Pairs to avoid: `ink-3` on `paper-2`; `moss` on `paper-2` for
small text (use `moss-2` or solid background instead).

## Typography

Three typefaces, loaded once.

- **Fraunces** — variable serif. Display, headings, section
  titles, modal titles, decorative italic accents (e.g. "today",
  empty-day "rest"). Use the optical-size axis: `"opsz" 40` on
  small headings, `"opsz" 72` on the largest display titles.
- **Inter Tight** — variable sans. All body, labels, buttons,
  inputs, navigation. Default `font-size: 15px` on `<body>`.
- **JetBrains Mono** — monospace. Code, timestamps, the sideways
  hour-rail label, dev-facing views (token strings, IDs). Never
  for body prose.

The `typography` tokens in the YAML above name the levels every
new screen should pick from. If a one-off needs a size between
two levels, prefer the closer level over inventing a new one;
file a PR if a missing level keeps recurring.

**Casing.** Sentence case for buttons, labels, titles. Uppercase
only on `section-title` (with the `0.09em` letterspacing) — the
small-caps effect that marks list dividers. Never `text-transform:
uppercase` on body copy.

**Numbers.** Use `font-feature-settings: "tnum"` for any column
of figures (payroll, time, money) so digits align.

## Layout

Mobile-first. Worker breakpoint stacks up; manager grid breaks
down. Always design at **360 px wide first**, then at 720 px
(tablet / desktop transition), then at 1080 px (manager 3-column
grid).

- **Spacing scale** (px): `0, 2, 4, 6, 8, 10, 12, 14, 16, 20, 24, 32, 40, 48, 56`.
  Padding inside cards: 12-20. Padding inside sections: 16-24
  on phone; 24-48 on desktop. Gap between siblings: 4-16.
  Anything outside the scale needs a justification in the PR.
- **Container widths.** Phone is edge-to-edge below 720 px (no
  centred letterbox). Desktop manager: three columns
  `232px / 1fr / 340px` for nav / main / agent.
- **Sticky regions** are page-header (top) and CTA bar (bottom on
  phone, inline on desktop). Use `position: sticky` with
  `env(safe-area-inset-*)` padding so notched iOS PWAs render
  under the notch, not behind it.
- **Safe areas.** Every full-bleed surface includes
  `padding-bottom: env(safe-area-inset-bottom)` for home-bar
  clearance, and `padding-top: env(safe-area-inset-top)` on the
  sticky page header.
- **Touch.** Click targets ≥ 44 × 44 CSS pixels on every
  interactive element. Icons inside smaller graphic boundaries
  get transparent padding to reach the floor.

## Elevation & Depth

Depth is **soft and warm**, not slab. Shadows tint moss and ink
together so cards lift without looking pasted on.

- **Page** (z=0) — `paper` background with the SVG noise grain
  overlay (`.grain` at ~3.5% opacity). Never a flat white page.
- **Card** (z=1) — `card` background, `--shadow`
  (`0 2px 4px rgba(63,110,59,0.06), 0 8px 24px rgba(31,26,20,0.06)`).
  This is the everyday block.
- **Raised** (z=2) — `card-raised` background, `--shadow-raised`
  (`0 6px 18px rgba(31,26,20,0.10)`). Toasts, popovers, the
  agent sidebar header.
- **Modal** (z=3) — same as raised + a `rgba(31,26,20,0.55)`
  backdrop with a 4 px blur. Native `<dialog>` only.
- **Tonal layers**, not stacked shadows. If two surfaces are at
  the same z, distinguish them with `paper-2` / `paper-3` washes,
  not by adding more shadow.

Dark mode uses heavier shadows (full black) because warm shadows
disappear on dark grounds — that mapping is already in `tokens.css`.

## Shapes

Corner radii are deliberately **mixed** — the radius tells you
what the thing is.

- **`tag` 4 px** — small inline labels, the preview-banner badge,
  the inline-code `<code>` bubble.
- **`control` 6 px** — checkboxes, focus rings, tight inputs.
- **`card` 10 px** — every card, panel, banner, dropzone,
  instruction block. The default radius if you're unsure.
- **`modal` 14 px** — `<dialog>` corners. One step softer than a
  card to feel like a separate plane.
- **`pill` 999 px** — buttons, chips, FABs, progress bars, the
  booking hint, segmented controls. Anything that should feel
  *tappable* is a pill.

Never invent a radius between these levels. If a one-off needs
something different (e.g. an asymmetric drawer), justify in the PR.

**No square corners** anywhere user-facing — `border-radius: 0`
is reserved for sub-elements that explicitly inherit (e.g. a
button inside a `.phone__dock` that fills the bar).

## Components

Every component below has a class in `mocks/web/src/styles/globals.css`.
Reuse before inventing; promote variants via BEM modifiers
(`task-card--overdue`), not by adding new classes.

### Buttons (`.btn`)

Pill-shaped, 40 px tall by default, `Inter Tight 14/500`.

- `.btn` — neutral, on `paper-2`. Use for tertiary actions.
- `.btn--moss` — primary action. **One per page** (the
  `PageHeader.trailing` slot enforces this).
- `.btn--rust` — destructive. Confirm via `<dialog>` first.
- `.btn--ghost` — outlined, `line-strong` border. Secondary action.
- `.btn--sm` (32 px) and `.btn--lg` (48 px) for density.
- `.btn--block` — full-width, ≥ 44 px tall. Pairs with size
  modifiers; use inside `.btn-group--stack` / `--split` for
  rows of equal CTAs.

Group buttons with `.btn-group` and a layout modifier
(`--end`, `--center`, `--between`, `--split`, `--stack`). Never
hand-roll `display: flex` for a row of buttons.

### Chips & tags (`.chip`)

Pill-shaped, 12 px text, used for status, filters, metadata.
Variants by hue (`--moss`, `--rust`, `--sand`, `--sky`, `--ghost`)
and density (`--sm`, `--lg`). For *static* labels (not tappable),
prefer the 4 px `tag` over a chip.

`.chip-radio` is the keyboard-accessible chip-as-radio for
single-select chip groups (e.g. category pickers).

### Cards (`.card`, `.task-card`, `.instruction-card`, etc.)

10 px radius, `card` background, `--shadow`. `padding: 14px`
default. Cards never carry borders *and* shadows — pick one
elevation strategy per card family.

When a card sits inside a `.panel`, it strips its own chrome
(see `.panel > .stack-row` rule) — the panel becomes the card.
Don't double-frame.

### Inputs (`.input`, `<textarea>`, `<select>`)

44 px tall, 6 px radius, `card` background, `line-strong` border,
`Inter Tight 15/400`. Focus state inherits the global
`:focus-visible` ring (2 px Moss outline, 2 px offset).

Labels are always `<label>` elements, never placeholder-only.
Required fields are marked with text, not colour alone.

### Checkboxes (`.checklist__box`)

22 × 22 px, 6 px radius, 1.5 px ink-3 border. On `--done` the
box fills moss with a white tick (Lucide `Check`) and animates a
brief scale-up (cubic-bezier ease-out-back).

### Tooltips & popovers

Use native `<dialog>` for popovers (one keyboard model, one
escape semantic). Tooltip-on-hover is reserved for icon-only
controls — every icon-only button must also carry an `aria-label`.

### Page header (`PageHeader`)

Three slots: `leading` (back / hamburger), `title + sub`,
`trailing` (≤ 1 primary action; rest in `⋯` overflow). Sticky on
phone with safe-area inset; non-sticky on desktop. **Every
authenticated route has one** unless the page itself establishes
identity (`/chat`, `/me`).

### Icons (Lucide only)

Every UI glyph is a [Lucide](https://lucide.dev) icon via
`lucide-react`. No emoji, no Heroicons, no inline SVG, no icon
fonts. Decorative icons get `aria-hidden="true"`; semantic ones
get an `aria-label` on the parent. Data fields that reference an
icon store the **PascalCase Lucide name** (e.g. `"Snowflake"`),
rendered through a whitelist (`components/AssetIcon.tsx`).

Typographic unicode (`✓`, `⊘`, `←`, `→`, `·`) is allowed for CSS
`::before`/`::after` content and directional affordances — they
inherit `currentColor`.

## Do's and Don'ts

### Do

- **Read tokens via CSS variables** — `var(--moss)`, not `#3F6E3B`.
- **Use semantic class names** — `task-card`, `shift-timeline`,
  `payroll-summary`. Name the *thing*, not the look.
- **Reuse before inventing.** Search globals.css with `rg` for an
  existing block before adding a new one. Promote variants via
  BEM modifiers (`--overdue`, `--pending`, `--done`).
- **Hit ≥ 44 × 44 px** on every interactive element, including
  icon-only buttons (transparent padding around the glyph).
- **Pair colour with text or icon.** Colour alone is never the
  state indicator (WCAG 2.2 AA, accessibility gate §14).
- **Respect `prefers-reduced-motion`.** Reset is in `reset.css`;
  custom transitions need the same media query.
- **Test in both themes.** Toggle `[data-theme="dark"]` on
  `<html>` before opening a PR. A token-clean change just works;
  a hard-coded hex breaks.
- **Use native `<dialog>`** for modals and popovers (keyboard +
  screen-reader free).
- **One primary action per page** (`.btn--moss` in
  `PageHeader.trailing`). Secondary actions live in the overflow
  menu or the page body.

### Don't

- **No utility / atomic classes.** No Tailwind, no `text-sm`, no
  `flex gap-4`. The repo is BEM + per-component CSS modules.
- **No inline `style=""`** for presentation. The only acceptable
  inline styles are dynamic geometry the engine has to compute
  (e.g. `top: ${minutes * pxPerMinute}px` for a timeline tag).
- **No presentational HTML attributes** — `bgcolor`, `align`,
  `<center>`, `<font>`. These don't theme.
- **No hard-coded colours, fonts, radii, or shadows** outside
  `tokens.css`. Add a token if a new value is genuinely needed.
- **No emoji as UI** — they don't theme, don't recolour, and
  carry per-platform glyph drift.
- **No Heroicons / Material Icons / FontAwesome.** Lucide only.
- **No drag-to-mutate** for high-blast-radius edits (rota
  rulesets, schedules). Drags are fine for per-row edits where
  one drag writes one row.
- **No `div` with `onClick`.** Buttons are `<button>`; links are
  `<a>`. Tab order matters.
- **No layout jumps > 100 ms.** Use `aria-busy` and skeletons,
  not late-arriving fills.
- **No third-party CSS frameworks** (Tailwind, Bulma, Bootstrap,
  daisyUI, shadcn). The hand-rolled system is the system.
- **No bouncing, parallax, gradient sweeps, or decorative motion.**
  Motion is reserved for *meaningful* state change (page enter,
  list add, tick-to-checkmark).

---

**Cross-references.** Frontend invariants: `docs/specs/14-web-frontend.md`.
Marketing-site design: `docs/specs-site/00-overview.md` (tokens flow
one-way app → site). Agent operating rules: `AGENTS.md`. Component
source of truth: `mocks/web/src/`.
