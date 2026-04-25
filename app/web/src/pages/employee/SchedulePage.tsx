import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import PageHeader from "@/components/PageHeader";
import DeskPage from "@/components/DeskPage";
import { useRole } from "@/context/RoleContext";
import type { Me } from "@/types/api";
import { InfiniteScheduleBody } from "./schedule/InfiniteScheduleBody";
import { isoDate } from "./schedule/lib/dateHelpers";
import { useIsPhone } from "./schedule/lib/useIsPhone";

// §14 "Schedule view". Self-only calendar hub that replaces the old
// `/week` flat list, the `/me/schedule` alias, and the retired
// `/bookings` page. Phone and desktop both render a continuous
// agenda backed by a bidirectional infinite query (7-day pages): the
// worker lands on today, scrolls up to past weeks, scrolls down to
// load the next. Phone stacks days as cards; desktop stacks 7-column
// Mon..Sun grids, one per ISO week. Click a day anywhere to open the
// shared day drawer with rota, tasks, bookings (§09, amend/decline
// inline), plus the Request-leave / Request-override forms. A
// pending banner sits above the agenda whenever any booking in the
// loaded window is pending_approval or has a pending self-amend —
// so a stale approval can't fall off-screen. See spec §06 for the
// approval rules and §09 for the booking lifecycle.
//
// This file is the thin orchestrator: it picks Phone vs Desktop via
// `useIsPhone`, fetches `/me` once for the dialog invalidation scope,
// and hands off to `InfiniteScheduleBody` for the real work. The
// internals live next door under `./schedule/`.

export default function SchedulePage() {
  const { role } = useRole();
  const isPhone = useIsPhone();
  // Manager always renders inside `.desk__main` (own scroll
  // container); the phone agenda assumes the document scrolls. So we
  // only run the phone code path for non-manager workers viewing on
  // a narrow viewport.
  const phoneMode = isPhone && role !== "manager";
  const today = useMemo(() => new Date(), []);
  const todayIso = useMemo(() => isoDate(today), [today]);
  const [selectedIso, setSelectedIso] = useState<string | null>(null);
  const [leaveIso, setLeaveIso] = useState<string | null>(null);
  const [overrideIso, setOverrideIso] = useState<string | null>(null);
  const [proposeIso, setProposeIso] = useState<string | null>(null);

  // Fetched for invalidation scope on dialog submits — /me's leave
  // panel reads `/api/v1/employees/{empId}/leaves`, which is keyed
  // off the v0-era `employee_id`.
  const meQ = useQuery({
    queryKey: qk.me(),
    queryFn: () => fetchJson<Me>("/api/v1/me"),
  });
  const empId = meQ.data?.employee.id ?? null;

  const title = "Schedule";
  const sub = role === "manager"
    ? "Your rota, hours, and time off — request changes inline."
    : "Your week at a glance. Tap a day to see tasks or request time off.";

  const body: ReactNode = (
    <InfiniteScheduleBody
      variant={phoneMode ? "phone" : "desktop"}
      today={today}
      todayIso={todayIso}
      empId={empId}
      selectedIso={selectedIso}
      setSelectedIso={setSelectedIso}
      leaveIso={leaveIso}
      setLeaveIso={setLeaveIso}
      overrideIso={overrideIso}
      setOverrideIso={setOverrideIso}
      proposeIso={proposeIso}
      setProposeIso={setProposeIso}
    />
  );

  if (role === "manager") {
    return (
      <DeskPage title={title} sub={sub}>
        {body}
      </DeskPage>
    );
  }
  return (
    <>
      <PageHeader title={title} sub={sub} />
      <div className="page-stack">{body}</div>
    </>
  );
}
