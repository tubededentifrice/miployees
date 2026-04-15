# 07 — Instructions (standing SOPs and knowledge base)

An **instruction** is a standing piece of content that a manager wants
staff (and agents) to reference when performing work: SOPs, house
rules, safety notes, "how we do it here" guides, brand guidelines, pet
quirks, local supplier preferences.

The user requirement: *instructions exist at global, property, and
room/area scope, and can be attached to tasks.*

## Properties of the system

- **Scope-aware**: three scopes — `global` (whole household),
  `property`, `area`.
- **Attachable**: can be linked to a task template, a schedule, a
  specific task, or a role. Links are the *only* connection between an
  instruction and the work that uses it; instructions are not nested
  inside templates.
- **Versioned**: every edit creates an immutable revision. Tasks record
  the revision in effect at completion time; the audit trail is exact
  even if instructions are later updated.
- **Retractable**: an instruction can be archived; existing links stay
  but new tasks do not auto-pick it up.
- **Indexable**: content is markdown; images allowed; searchable via
  the unified full-text search (§02).
- **LLM-fed**: instructions are injected into agent prompts when
  relevant, with scoping rules described below.
- **Rendered in context**: on the employee task screen, all
  applicable instructions are collapsed under a single "Instructions"
  panel, ordered by specificity (area > property > global).

## Data model

### `instruction`

| field              | type    | notes                                 |
|--------------------|---------|---------------------------------------|
| id                 | ULID PK |                                       |
| household_id       | ULID FK |                                       |
| scope              | enum    | `global | property | area`            |
| property_id        | ULID FK?| required iff scope != global          |
| area_id            | ULID FK?| required iff scope == area            |
| title              | text    | short, human-readable                 |
| tags               | text[]  | `safety`, `pets`, `food`, ...         |
| current_revision_id| ULID FK |                                       |
| status             | enum    | `active | archived`                   |
| deleted_at         | tstz?   |                                       |

Constraints:

- `scope = global` → `property_id` and `area_id` NULL.
- `scope = property` → `property_id` set, `area_id` NULL.
- `scope = area` → `area_id` set; `property_id` mirrored for query
  speed and consistency-checked.

### `instruction_revision`

Immutable.

| field             | type    | notes                                 |
|-------------------|---------|---------------------------------------|
| id                | ULID PK |                                       |
| instruction_id    | ULID FK |                                       |
| version           | int     | monotonic per instruction             |
| body_md           | text    | markdown                              |
| summary_md        | text?   | short version for tight UIs           |
| attachment_file_ids | ULID[]| images/PDFs                           |
| author_manager_id | ULID FK |                                       |
| created_at        | tstz    |                                       |
| change_note       | text?   |                                       |

### `instruction_link`

Explicit many-to-many between instructions and the things they apply
to. Plus one implicit link type: **scope-based automatic inclusion**
(see "Resolution" below).

| field             | type    | notes                                      |
|-------------------|---------|--------------------------------------------|
| id                | ULID PK |                                            |
| instruction_id    | ULID FK |                                            |
| target_kind       | enum    | `task_template | schedule | task | role`   |
| target_id         | ULID    | polymorphic, resolved in application       |
| added_by          | ULID    | manager or agent                           |
| added_at          | tstz    |                                            |

## Resolution: which instructions apply to a given task?

For a task with `property_id = P` and `area_id = A`, the set of
applicable instructions is the **union** of:

1. All `global` instructions (`status = active`) — universal.
2. All `property` instructions where `property_id = P`.
3. All `area` instructions where `area_id = A` (and therefore
   `property_id = P`).
4. Any `instruction_link` row targeting this task directly.
5. Any `instruction_link` targeting the task's `template_id`.
6. Any `instruction_link` targeting the task's `schedule_id`.
7. Any `instruction_link` targeting the task's `expected_role_id`.

Order in the UI: more specific first (area > property > global), then
linked (template/schedule/role/task explicit links) after, each with a
badge showing why it applies ("House-wide", "Villa Sud", "Pool",
"Linked to this task template", etc.).

Duplicates (same instruction reached by two routes) are shown once with
the highest-specificity label.

## Editing semantics

- Every save creates a new `instruction_revision` and points
  `current_revision_id` at it.
- Tasks do **not** own the instruction list as a source of truth;
  `instruction_link` is canonical. `task.linked_instruction_ids` is a
  denormalized cache of `instruction_link` rows where
  `target_kind = task AND target_id = task.id`, refreshed on every
  link insert/delete in the same transaction. Readers must not write
  to it directly.
- Tasks capture the **revision in effect at task creation** in the
  audit log, but the cached `linked_instruction_ids` array stores
  instruction ids, not revision ids — see below.
- Evidence of which **revision** was surfaced at completion time lives
  in the audit log (`instruction.render` action with
  `instruction_id + revision_id`).

### Why link to instruction, not revision, on tasks

Staff and agents expect "the latest safety note" — pinning a task to a
revision that has since been corrected would defeat the purpose. The
audit trail (revision id seen at task render) gives enough forensic
clarity without freezing tasks on stale content.

If an instruction is **retroactively updated** with a critical change,
the manager can click "Mark as critical" on the new revision, which
triggers an email digest entry: "Instruction 'Pool chemical handling'
was updated after the last task completion; review".

## Authoring UI (manager)

- Rich-text markdown editor with live preview; supports images
  (uploaded via the same file pipeline as evidence), headings, lists,
  callouts (`> ⚠️ Warning:` renders as a warning block).
- Scope picker at the top: **Global / Property / Area**, with a
  live-filtered property and area selector.
- Tag chips (free-form; auto-complete from existing tags).
- "Link to..." picker: task templates, schedules, roles, specific
  tasks.
- Preview shows exactly how it will render on the employee PWA.

## Reader UI (employee PWA)

On a task screen, an **"Instructions"** accordion shows:

- Area-scoped instructions (most relevant first)
- Property-scoped instructions
- Global instructions
- Explicit links (badge: "Linked to this task")

Each entry is collapsible; the first one is expanded by default.
Images inline. Markdown rendered to HTML server-side; no client-side
markdown compilation.

## LLM use

§11 details how instructions participate in the assistant. Short
version: when the assistant is invoked in a task context, the in-scope
instructions are injected into the system prompt as a labeled
knowledge block. Instruction bodies are never sent upstream unless
the household's `llm.send_instructions` setting is on (default:
**on**, as they are manager-authored and rarely sensitive). Global
instructions are injected into every assistant call even without a
task context.

## Search

- Free-text across title, tags, body.
- Scope filter (global/property/area).
- "Applies to <this task>" quick filter in the task view.

## Bulk operations

- Archive (soft, reversible).
- Rescope (rare): e.g., promote a property-scoped instruction to
  global. A rescope is just a new revision with a metadata marker;
  the audit log records the transition.

## Examples

- **Global**: "All employees must wear closed-toed shoes in service
  areas."
- **Property** (Villa Sud): "Keys are under the terracotta pot to the
  left of the front door. Return them there before leaving."
- **Area** (Villa Sud → Pool): "Pool chemicals are stored in the shed
  to the left. Never mix chlorine and pH-down. If you see cloudy
  water, call the manager before adding anything."
- **Role** (nanny): "Never post pictures of the children on social
  media."
- **Template** (turnover cleaning): "Change the duvet covers even if
  they look clean. Guests often sleep on top of them."
