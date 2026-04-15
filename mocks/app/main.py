"""miployees — UI preview mocks.

Presentational only. Mutations are in-memory. A `role` cookie picks
employee vs manager; `/switch/<role>` toggles. `theme` cookie picks
light vs dark; `/theme/toggle` flips.
"""

from __future__ import annotations

from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import mock_data as md


BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="miployees mocks", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


ROLE_COOKIE = "miployees_role"
THEME_COOKIE = "miployees_theme"
VALID_ROLES = {"employee", "manager"}
VALID_THEMES = {"light", "dark"}


def current_role(request: Request) -> str:
    r = request.cookies.get(ROLE_COOKIE)
    return r if r in VALID_ROLES else "employee"


def current_theme(request: Request) -> str:
    t = request.cookies.get(THEME_COOKIE)
    return t if t in VALID_THEMES else "light"


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    employee = md.employee_by_id(md.DEFAULT_EMPLOYEE_ID)
    ctx.setdefault("role", current_role(request))
    ctx.setdefault("theme", current_theme(request))
    ctx.setdefault("employee", employee)
    ctx.setdefault("manager_name", md.DEFAULT_MANAGER_NAME)
    ctx.setdefault("properties", md.PROPERTIES)
    ctx.setdefault("today", md.TODAY)
    ctx.setdefault("now", md.NOW)
    ctx.setdefault("property_by_id", md.property_by_id)
    ctx.setdefault("employee_by_id", md.employee_by_id)
    return templates.TemplateResponse(request, name, ctx)


# ── Health / ops ──────────────────────────────────────────────────────

@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/readyz")
def readyz() -> dict:
    return {"ok": True, "checks": {"db": "ok", "redis": "ok", "llm": "ok"}}


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    # Prom-style stub so the endpoint looks right in a preview.
    return (
        "# HELP miployees_tasks_completed_total Total tasks completed\n"
        "# TYPE miployees_tasks_completed_total counter\n"
        "miployees_tasks_completed_total{property=\"Villa Sud\"} 1\n"
        "miployees_tasks_pending{property=\"Villa Sud\"} 4\n"
        "miployees_shift_active 1\n"
    )


# ── Root / role / theme switch ────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    role = current_role(request)
    return RedirectResponse("/today" if role == "employee" else "/dashboard", status_code=303)


@app.get("/switch/{role}")
def switch_role(role: str):
    if role not in VALID_ROLES:
        return RedirectResponse("/", status_code=303)
    target = "/today" if role == "employee" else "/dashboard"
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(ROLE_COOKIE, role, max_age=60 * 60 * 24 * 30, samesite="lax")
    return resp


@app.post("/theme/toggle")
@app.get("/theme/toggle")
def theme_toggle(request: Request):
    new_theme = "dark" if current_theme(request) == "light" else "light"
    resp = RedirectResponse(request.headers.get("referer") or "/", status_code=303)
    resp.set_cookie(THEME_COOKIE, new_theme, max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp


# ══════════════════════════════════════════════════════════════════════
# Employee views
# ══════════════════════════════════════════════════════════════════════

@app.get("/today", response_class=HTMLResponse)
def today(request: Request):
    employee = md.employee_by_id(md.DEFAULT_EMPLOYEE_ID)
    tasks = sorted(md.tasks_for_employee(employee.id), key=lambda t: t.scheduled_start)
    today_tasks = [t for t in tasks if t.scheduled_start.date() == md.TODAY]
    now_task = next((t for t in today_tasks if t.status in {"pending", "in_progress"}), None)
    upcoming = [t for t in today_tasks if t is not now_task and t.status in {"pending", "in_progress"}]
    completed = [t for t in today_tasks if t.status == "completed"]
    return render(request, "employee/today.html",
                  now_task=now_task, upcoming=upcoming, completed=completed)


@app.get("/week", response_class=HTMLResponse)
def week(request: Request):
    employee = md.employee_by_id(md.DEFAULT_EMPLOYEE_ID)
    tasks = sorted(md.tasks_for_employee(employee.id), key=lambda t: t.scheduled_start)
    return render(request, "employee/week.html", tasks=tasks)


@app.get("/task/{tid}", response_class=HTMLResponse)
def task_detail(tid: str, request: Request):
    task = md.task_by_id(tid)
    if task is None:
        return RedirectResponse("/today", status_code=303)
    return render(request, "employee/task_detail.html",
                  task=task,
                  property=md.property_by_id(task.property_id),
                  instructions=md.instructions_for_task(task))


@app.post("/task/{tid}/check/{idx}", response_class=HTMLResponse)
def task_check(tid: str, idx: int, request: Request):
    task = md.task_by_id(tid)
    if task is not None and 0 <= idx < len(task.checklist):
        task.checklist[idx]["done"] = not task.checklist[idx]["done"]
    return render(request, "partials/checklist_item.html",
                  task=task, item=task.checklist[idx], idx=idx)


@app.post("/task/{tid}/complete")
def task_complete(tid: str):
    task = md.task_by_id(tid)
    if task is not None:
        task.status = "completed"
    return RedirectResponse(f"/task/{tid}", status_code=303)


@app.post("/task/{tid}/skip")
def task_skip(tid: str, reason: str = Form("")):
    task = md.task_by_id(tid)
    if task is not None:
        task.status = "skipped"
    return RedirectResponse(f"/task/{tid}", status_code=303)


@app.get("/shifts", response_class=HTMLResponse)
def shifts(request: Request):
    history = [
        {"date": md.TODAY,                             "in": "08:12", "out": "— (active)", "hours": "2h 00m"},
        {"date": md.TODAY.replace(day=14),             "in": "08:02", "out": "16:38",      "hours": "8h 36m"},
        {"date": md.TODAY.replace(day=13),             "in": "08:05", "out": "13:12",      "hours": "5h 07m"},
        {"date": md.TODAY.replace(day=11),             "in": "09:30", "out": "14:48",      "hours": "5h 18m"},
    ]
    return render(request, "employee/shifts.html", history=history)


@app.post("/shifts/toggle")
def shifts_toggle():
    employee = md.employee_by_id(md.DEFAULT_EMPLOYEE_ID)
    employee.clocked_in_at = None if employee.clocked_in_at else md.NOW
    return RedirectResponse("/today", status_code=303)


@app.get("/expenses", response_class=HTMLResponse)
def expenses(request: Request):
    # §14: same URL, audience-dependent view. Employees see their own
    # submission form + list; managers see the approvals queue.
    if current_role(request) == "manager":
        return render(request, "manager/expenses.html", all_expenses=md.EXPENSES)
    employee = md.employee_by_id(md.DEFAULT_EMPLOYEE_ID)
    return render(request, "employee/expenses.html",
                  expenses=md.expenses_for_employee(employee.id))


@app.post("/expenses", response_class=HTMLResponse)
def expenses_create(request: Request,
                    merchant: str = Form(...), amount: str = Form(...), note: str = Form("")):
    try:
        cents = int(round(float(amount) * 100))
    except ValueError:
        cents = 0
    md.EXPENSES.insert(0, md.Expense(
        id=f"x-{len(md.EXPENSES) + 1}", employee_id=md.DEFAULT_EMPLOYEE_ID,
        amount_cents=cents, currency="EUR",
        merchant=merchant or "Unknown", submitted_at=datetime.now(),
        status="pending", note=note, ocr_confidence=None,
    ))
    return RedirectResponse("/expenses", status_code=303)


@app.get("/issues/new", response_class=HTMLResponse)
def issue_new(request: Request):
    return render(request, "employee/issue_new.html")


@app.post("/issues/new", response_class=HTMLResponse)
def issue_new_post(request: Request,
                   title: str = Form(...), severity: str = Form("medium"),
                   category: str = Form("other"), property_id: str = Form(...),
                   area: str = Form(""), body: str = Form("")):
    md.ISSUES.insert(0, md.Issue(
        id=f"iss-{len(md.ISSUES)+1}", reported_by=md.DEFAULT_EMPLOYEE_ID,
        property_id=property_id, area=area or "—",
        severity=severity, category=category, title=title, body=body,
        reported_at=datetime.now(), status="open",
    ))
    return RedirectResponse("/me", status_code=303)


@app.get("/me", response_class=HTMLResponse)
def me(request: Request):
    employee = md.employee_by_id(md.DEFAULT_EMPLOYEE_ID)
    my_issues = [i for i in md.ISSUES if i.reported_by == employee.id]
    return render(request, "employee/me.html",
                  my_issues=my_issues,
                  my_leaves=md.leaves_for_employee(employee.id))


# Messaging per §14: employee side is task comments, not a standalone
# inbox. Kept as a convenience page linking back to those threads.
@app.get("/messages", response_class=HTMLResponse)
def messages(request: Request):
    threads = [
        {"kind": "manager", "with": "Élodie Bernard",             "last": "Can you take the 14:00 airport run?",    "unread": True,  "time": "09:42", "link": "/task/t-4"},
        {"kind": "task",    "with": "Kitchen deep clean",          "last": "Noted — will start after linen change.", "unread": False, "time": "Yesterday", "link": "/task/t-3"},
        {"kind": "assistant","with": "miployees assistant",         "last": "Fresh sheets are on shelf 2 of cupboard A — the lavender-scented ones.", "unread": False, "time": "08:32", "link": "/task/t-2"},
    ]
    return render(request, "employee/messages.html", threads=threads)


# ══════════════════════════════════════════════════════════════════════
# Manager views
# ══════════════════════════════════════════════════════════════════════

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    on_shift = [e for e in md.EMPLOYEES if e.clocked_in_at]
    today_tasks = [t for t in md.TASKS if t.scheduled_start.date() == md.TODAY]
    by_status = {
        "completed":   [t for t in today_tasks if t.status == "completed"],
        "in_progress": [t for t in today_tasks if t.status == "in_progress"],
        "pending":     [t for t in today_tasks if t.status == "pending"],
    }
    open_issues = [i for i in md.ISSUES if i.status != "resolved"]
    return render(request, "manager/dashboard.html",
                  on_shift=on_shift, by_status=by_status,
                  pending_approvals=md.APPROVALS,
                  pending_expenses=[x for x in md.EXPENSES if x.status == "pending"],
                  pending_leaves=[lv for lv in md.LEAVES if lv.approved_at is None],
                  open_issues=open_issues,
                  stays_today=[s for s in md.STAYS if s.check_in <= md.TODAY <= s.check_out])


@app.get("/properties", response_class=HTMLResponse)
def properties_list(request: Request):
    return render(request, "manager/properties.html",
                  stays_for_property=md.stays_for_property,
                  closures_for_property=md.closures_for_property)


@app.get("/property/{pid}", response_class=HTMLResponse)
def property_detail(pid: str, request: Request):
    prop = md.property_by_id(pid)
    prop_tasks = [t for t in md.TASKS if t.property_id == pid]
    return render(request, "manager/property_detail.html",
                  property=prop, property_tasks=prop_tasks,
                  stays=md.stays_for_property(pid),
                  inventory=md.inventory_for_property(pid),
                  instructions=[i for i in md.INSTRUCTIONS if i.property_id == pid or i.scope == "global"])


@app.get("/property/{pid}/closures", response_class=HTMLResponse)
def property_closures(pid: str, request: Request):
    prop = md.property_by_id(pid)
    return render(request, "manager/property_closures.html",
                  property=prop,
                  closures=md.closures_for_property(pid),
                  stays=md.stays_for_property(pid))


@app.get("/employees", response_class=HTMLResponse)
def employees_list(request: Request):
    return render(request, "manager/employees.html", employees_list=md.EMPLOYEES)


@app.get("/employee/{eid}", response_class=HTMLResponse)
def employee_detail(eid: str, request: Request):
    emp = md.employee_by_id(eid)
    return render(request, "manager/employee_detail.html",
                  subject=emp,
                  subject_tasks=md.tasks_for_employee(eid),
                  subject_expenses=md.expenses_for_employee(eid),
                  subject_leaves=md.leaves_for_employee(eid),
                  subject_payslips=md.payslips_for_employee(eid))


@app.get("/employee/{eid}/leaves", response_class=HTMLResponse)
def employee_leaves(eid: str, request: Request):
    emp = md.employee_by_id(eid)
    return render(request, "manager/employee_leaves.html",
                  subject=emp, leaves=md.leaves_for_employee(eid))


@app.post("/leaves/{lid}/{decision}")
def leaves_decide(lid: str, decision: str):
    for lv in md.LEAVES:
        if lv.id == lid:
            if decision == "approve":
                lv.approved_at = datetime.now()
            elif decision == "reject":
                md.LEAVES.remove(lv)
            break
    return RedirectResponse("/leaves", status_code=303)


@app.get("/leaves", response_class=HTMLResponse)
def leaves_inbox(request: Request):
    pending = [lv for lv in md.LEAVES if lv.approved_at is None]
    approved = [lv for lv in md.LEAVES if lv.approved_at is not None]
    return render(request, "manager/leaves.html", pending=pending, approved=approved)


@app.get("/stays", response_class=HTMLResponse)
def stays_view(request: Request):
    return render(request, "manager/stays.html",
                  stays=sorted(md.STAYS, key=lambda s: s.check_in),
                  closures=md.CLOSURES,
                  leaves=[lv for lv in md.LEAVES if lv.approved_at is not None])


@app.get("/approvals", response_class=HTMLResponse)
def approvals(request: Request):
    return render(request, "manager/approvals.html", approvals=md.APPROVALS)


@app.post("/approvals/{aid}/{decision}")
def approvals_decide(aid: str, decision: str):
    md.APPROVALS[:] = [a for a in md.APPROVALS if a.id != aid]
    return RedirectResponse("/approvals", status_code=303)


@app.post("/expenses/{xid}/{decision}")
def expenses_decide(xid: str, decision: str):
    for x in md.EXPENSES:
        if x.id == xid:
            x.status = {"approve": "approved", "reject": "rejected", "reimburse": "reimbursed"}.get(decision, x.status)
    return RedirectResponse("/expenses", status_code=303)


@app.get("/templates", response_class=HTMLResponse)
def templates_list(request: Request):
    return render(request, "manager/templates.html", templates_list=md.TEMPLATES)


@app.get("/schedules", response_class=HTMLResponse)
def schedules_list(request: Request):
    by_id = {t.id: t for t in md.TEMPLATES}
    return render(request, "manager/schedules.html",
                  schedules=md.SCHEDULES, templates_by_id=by_id)


@app.get("/instructions", response_class=HTMLResponse)
def instructions_list(request: Request):
    return render(request, "manager/instructions.html", instructions=md.INSTRUCTIONS)


@app.get("/instructions/{iid}", response_class=HTMLResponse)
def instruction_detail(iid: str, request: Request):
    instr = next((i for i in md.INSTRUCTIONS if i.id == iid), None)
    if instr is None:
        return RedirectResponse("/instructions", status_code=303)
    return render(request, "manager/instruction_detail.html", instruction=instr)


@app.get("/inventory", response_class=HTMLResponse)
def inventory(request: Request):
    return render(request, "manager/inventory.html", inventory=md.INVENTORY)


@app.get("/pay", response_class=HTMLResponse)
def pay(request: Request):
    current = [p for p in md.PAYSLIPS if p.period_starts.month == 4]
    previous = [p for p in md.PAYSLIPS if p.period_starts.month == 3]
    return render(request, "manager/pay.html", current=current, previous=previous)


@app.get("/audit", response_class=HTMLResponse)
def audit(request: Request):
    return render(request, "manager/audit.html", entries=md.AUDIT)


@app.get("/webhooks", response_class=HTMLResponse)
def webhooks(request: Request):
    return render(request, "manager/webhooks.html", webhooks=md.WEBHOOKS)


@app.get("/llm", response_class=HTMLResponse)
def llm(request: Request):
    total_spent = sum(a.spent_24h_usd for a in md.LLM_ASSIGNMENTS)
    total_budget = sum(a.daily_budget_usd for a in md.LLM_ASSIGNMENTS)
    total_calls = sum(a.calls_24h for a in md.LLM_ASSIGNMENTS)
    return render(request, "manager/llm.html",
                  assignments=md.LLM_ASSIGNMENTS, calls=md.LLM_CALLS,
                  total_spent=total_spent, total_budget=total_budget, total_calls=total_calls)


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    return render(request, "manager/settings.html", settings=md.HOUSEHOLD_SETTINGS)


@app.get("/styleguide", response_class=HTMLResponse)
def styleguide(request: Request):
    return render(request, "styleguide.html")


# ══════════════════════════════════════════════════════════════════════
# Public / unauthenticated
# ══════════════════════════════════════════════════════════════════════

@app.get("/login", response_class=HTMLResponse)
def login(request: Request):
    return render(request, "public/login.html")


@app.get("/recover", response_class=HTMLResponse)
def recover(request: Request):
    return render(request, "public/recover.html")


@app.get("/enroll/{token}", response_class=HTMLResponse)
def enroll(token: str, request: Request):
    # token is opaque in the mock — decorative only
    return render(request, "public/enroll.html", token=token)


@app.get("/guest/{token}", response_class=HTMLResponse)
def guest(token: str, request: Request):
    stay = md.stay_by_id(md.GUEST_STAY_ID)
    turnover_task = next((t for t in md.TASKS if t.turnover_bundle_id == "tb-apt-3b-18"), None)
    guest_checklist = [c for c in (turnover_task.checklist if turnover_task else []) if c.get("guest_visible")]
    return render(request, "public/guest.html",
                  stay=stay, property=md.property_by_id(stay.property_id),
                  guest_checklist=guest_checklist, token=token)
