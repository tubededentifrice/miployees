// Infinite-agenda scroll plumbing for `/schedule` (§14 "Schedule view").
//
// Owns the bidirectional `useInfiniteQuery`, the page-merge into a
// flat `cells[]`, the IntersectionObserver sentinels at top + bottom,
// the prepend scroll preservation, the today re-anchor settle window,
// the sticky monthbar's topmost-cell observer, and the scroll-to-today
// handler. The body component just consumes the returned bag of
// values + handlers — keeping the JSX focused on layout.

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import type { UseInfiniteQueryResult, InfiniteData } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import type { MySchedulePayload } from "@/types/api";
import type { DayCell } from "./buildCells";
import { buildCells, mergeSchedulePages } from "./buildCells";
import {
  addDays,
  isoDate,
  parseIsoDate,
  startOfIsoWeek,
} from "./dateHelpers";

// Walk up from `start` until we hit an element with `overflow-y` of
// `auto`, `scroll`, or `overlay`. Returns that element, or `null`
// meaning "the document itself scrolls, use `window`". Caught at
// mount once; the ancestor chain doesn't change within a page.
function findScrollRoot(start: HTMLElement): HTMLElement | null {
  let node: HTMLElement | null = start.parentElement;
  while (node) {
    const oy = getComputedStyle(node).overflowY;
    if (oy === "auto" || oy === "scroll" || oy === "overlay") return node;
    node = node.parentElement;
  }
  return null;
}

export interface UseInfiniteAgenda {
  q: UseInfiniteQueryResult<InfiniteData<MySchedulePayload>, Error>;
  merged: MySchedulePayload | null;
  cells: DayCell[];
  containerRef: (node: HTMLDivElement | null) => void;
  topSentinelRef: (node: HTMLDivElement | null) => void;
  bottomSentinelRef: (node: HTMLDivElement | null) => void;
  monthLabel: string;
  todayInView: boolean;
  scrollToToday: () => void;
}

export function useInfiniteAgenda(
  today: Date,
  todayIso: string,
): UseInfiniteAgenda {
  const initialMondayIso = useMemo(
    () => isoDate(startOfIsoWeek(today)),
    [today],
  );

  const q = useInfiniteQuery({
    // Single key for the whole infinite stream so React Query keeps
    // accumulated pages across re-renders. Mutations elsewhere
    // invalidate `["my-schedule", ...]` by prefix and pick this one
    // up too.
    queryKey: ["my-schedule", "infinite", initialMondayIso] as const,
    initialPageParam: initialMondayIso,
    queryFn: ({ pageParam }) => {
      const fromIso = pageParam;
      const toIso = isoDate(addDays(parseIsoDate(pageParam), 6));
      return fetchJson<MySchedulePayload>(
        `/api/v1/me/schedule?from_=${fromIso}&to=${toIso}`,
      );
    },
    getNextPageParam: (_last, _all, lastParam) =>
      isoDate(addDays(parseIsoDate(lastParam), 7)),
    getPreviousPageParam: (_first, _all, firstParam) =>
      isoDate(addDays(parseIsoDate(firstParam), -7)),
  });

  const merged = useMemo(
    () => (q.data ? mergeSchedulePages(q.data.pages) : null),
    [q.data],
  );

  const firstParam = (q.data?.pageParams[0] as string | undefined) ?? initialMondayIso;
  const totalDays = (q.data?.pageParams.length ?? 1) * 7;

  const cells = useMemo(() => {
    if (!merged) return [];
    return buildCells(parseIsoDate(firstParam), totalDays, merged);
  }, [merged, firstParam, totalDays]);

  // ── Scroll plumbing ────────────────────────────────────────────────

  const innerContainerRef = useRef<HTMLDivElement | null>(null);
  const innerTopSentinelRef = useRef<HTMLDivElement | null>(null);
  const innerBottomSentinelRef = useRef<HTMLDivElement | null>(null);

  // `null` ⇒ the document is the scroll container (phone only, where
  // `.phone__body { display: contents }` defers overflow to `<html>`).
  // An HTMLElement here means an ancestor with its own overflow owns
  // scroll: `.phone__body` at desktop width for worker /schedule, or
  // `.desk__main` for manager /schedule. Every observer, height read,
  // and scroll-by has to target that root rather than `window`.
  //
  // Captured via a callback ref so the detection fires the moment the
  // container mounts — not in a `useLayoutEffect([])`, which runs once
  // after the *first* render. The first render currently commits
  // `<Loading />` (see wrapper below), so a mount-only effect would
  // fire with `containerRef.current === null` and never re-run once
  // the real container appears on the next render. Detection via
  // callback ref fires after the ref is actually assigned.
  const [scrollRoot, setScrollRoot] = useState<HTMLElement | null>(null);
  const setContainerEl = useCallback((node: HTMLDivElement | null) => {
    innerContainerRef.current = node;
    if (node) setScrollRoot(findScrollRoot(node));
  }, []);
  const setTopSentinelEl = useCallback((node: HTMLDivElement | null) => {
    innerTopSentinelRef.current = node;
  }, []);
  const setBottomSentinelEl = useCallback((node: HTMLDivElement | null) => {
    innerBottomSentinelRef.current = node;
  }, []);

  // `null` root = use `window` / document. Any non-null root is an
  // element whose own overflow owns scroll.
  const getScrollHeight = useCallback(
    () => scrollRoot?.scrollHeight ?? document.documentElement.scrollHeight,
    [scrollRoot],
  );
  const scrollByDelta = useCallback((delta: number) => {
    const target: Element | Window = scrollRoot ?? window;
    target.scrollBy({ top: delta, behavior: "instant" as ScrollBehavior });
  }, [scrollRoot]);

  // Preserve scroll position when prepending. Captured BEFORE
  // `fetchPreviousPage` runs and consumed once the new first page
  // appears in `q.data.pages`.
  const heightBeforePrependRef = useRef<number | null>(null);
  const prevFirstParamRef = useRef<string | null>(null);

  // The initial paint loads today's week, but the bottom (and top)
  // sentinels then fire concurrently and pull in 1-3 adjacent weeks.
  // Each prepend shifts the document, and a single
  // `scrollIntoView({block:"start"})` only positions today *once* —
  // by the time the prefetches settle today has drifted ~half a
  // screen down. So we keep re-anchoring today to the top until
  // either (a) all the auto-prefetches have settled or (b) the
  // worker has scrolled today out of view themselves.
  const settledRef = useRef(false);

  // Bottom sentinel — extend the future when the worker thumbs down.
  // `root` is the scrollRoot element or `null` (document).
  useEffect(() => {
    const node = innerBottomSentinelRef.current;
    if (!node) return;
    const obs = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (
            e.isIntersecting
            && q.hasNextPage
            && !q.isFetchingNextPage
            && !q.isFetching
          ) {
            q.fetchNextPage();
          }
        }
      },
      { root: scrollRoot, rootMargin: "600px 0px 600px 0px" },
    );
    obs.observe(node);
    return () => obs.disconnect();
  }, [
    scrollRoot,
    q.hasNextPage,
    q.isFetchingNextPage,
    q.isFetching,
    q.fetchNextPage,
  ]);

  // Top sentinel — extend the past, capturing scroll height so we
  // can compensate after the prepend.
  useEffect(() => {
    const node = innerTopSentinelRef.current;
    if (!node) return;
    const obs = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (
            e.isIntersecting
            && q.hasPreviousPage
            && !q.isFetchingPreviousPage
            && !q.isFetching
          ) {
            heightBeforePrependRef.current = getScrollHeight();
            q.fetchPreviousPage();
          }
        }
      },
      { root: scrollRoot, rootMargin: "600px 0px 600px 0px" },
    );
    obs.observe(node);
    return () => obs.disconnect();
  }, [
    scrollRoot,
    getScrollHeight,
    q.hasPreviousPage,
    q.isFetchingPreviousPage,
    q.isFetching,
    q.fetchPreviousPage,
  ]);

  // After a prepend lands and we are *past* the initial settle, keep
  // the worker's visual position by compensating for the scroll
  // root's growth. During settle the re-anchor below takes priority
  // instead — running both isn't harmful but the re-anchor is what
  // actually pins today, so we skip the scrollBy work then.
  useLayoutEffect(() => {
    if (!q.data) return;
    const first = q.data.pageParams[0] as string;
    if (
      settledRef.current
      && prevFirstParamRef.current !== null
      && prevFirstParamRef.current !== first
      && heightBeforePrependRef.current !== null
    ) {
      const delta = getScrollHeight() - heightBeforePrependRef.current;
      if (delta > 0) scrollByDelta(delta);
    }
    if (
      prevFirstParamRef.current !== null
      && prevFirstParamRef.current !== first
    ) {
      heightBeforePrependRef.current = null;
    }
    prevFirstParamRef.current = first;
  }, [q.data, getScrollHeight, scrollByDelta]);

  // Re-anchor today on every cells change while we are still in the
  // initial settle window. Bails out as soon as the worker scrolls
  // today materially out of view — they are now driving.
  useLayoutEffect(() => {
    if (settledRef.current) return;
    if (cells.length === 0) return;
    const node = (innerContainerRef.current ?? document).querySelector(
      `[data-schedule-iso="${todayIso}"]`,
    ) as HTMLElement | null;
    if (!node) return;
    const rect = node.getBoundingClientRect();
    const drift = rect.top;
    // If today has drifted off-screen by more than ~one viewport in
    // either direction, the worker is actively reading another week.
    // Stop fighting them. Use the scroll root's client height on
    // manager / worker desktop (where `.desk__main` / `.phone__body`
    // is smaller than the window) so the threshold tracks the pane
    // the user actually sees, not the outer window.
    const paneHeight = scrollRoot?.clientHeight ?? window.innerHeight;
    if (drift > paneHeight * 1.5 || rect.bottom < -paneHeight * 0.5) {
      settledRef.current = true;
      return;
    }
    node.scrollIntoView({ block: "start", behavior: "instant" as ScrollBehavior });
  }, [cells, todayIso, scrollRoot]);

  // End the settle window 200ms after all initial fetches have
  // calmed down. Past that point auto-anchoring stops and the
  // prepend scroll-preserver above takes over.
  useEffect(() => {
    if (settledRef.current) return;
    if (cells.length === 0) return;
    const stillFetching =
      q.isFetching || q.isFetchingPreviousPage || q.isFetchingNextPage;
    if (stillFetching) return;
    const t = window.setTimeout(() => {
      settledRef.current = true;
    }, 200);
    return () => window.clearTimeout(t);
  }, [
    cells.length,
    q.isFetching,
    q.isFetchingPreviousPage,
    q.isFetchingNextPage,
  ]);

  // ── Sticky month label + Today FAB ─────────────────────────────────

  const [topVisibleIso, setTopVisibleIso] = useState<string>(todayIso);
  const [todayInView, setTodayInView] = useState<boolean>(true);

  // One observer per cell row — the topmost intersecting cell drives
  // the monthbar label, and the today cell drives the FAB visibility.
  useEffect(() => {
    if (cells.length === 0) return;
    const root = innerContainerRef.current;
    if (!root) return;
    const nodes = Array.from(
      root.querySelectorAll<HTMLElement>("[data-schedule-iso]"),
    );
    if (nodes.length === 0) return;

    const intersecting = new Set<string>();
    const obs = new IntersectionObserver(
      (entries) => {
        let nextTodayInView: boolean | null = null;
        for (const e of entries) {
          const iso = (e.target as HTMLElement).dataset.scheduleIso;
          if (!iso) continue;
          if (e.isIntersecting) intersecting.add(iso);
          else intersecting.delete(iso);
          if (iso === todayIso) nextTodayInView = e.isIntersecting;
        }
        if (intersecting.size > 0) {
          let earliest: string | null = null;
          for (const iso of intersecting) {
            if (earliest === null || iso < earliest) earliest = iso;
          }
          if (earliest) setTopVisibleIso(earliest);
        }
        if (nextTodayInView !== null) setTodayInView(nextTodayInView);
      },
      // Crop to the area between the sticky monthbar and the bottom
      // of the viewport. ≈64px is the monthbar height; adjust here
      // if the bar grows. `root` is the scrollRoot (or null = document),
      // which matters for manager /schedule where the viewport is
      // `.desk__main` rather than the window.
      { root: scrollRoot, rootMargin: "-64px 0px -40% 0px", threshold: [0, 1] },
    );
    nodes.forEach((n) => obs.observe(n));
    return () => obs.disconnect();
  }, [scrollRoot, cells, todayIso]);

  const monthLabel = useMemo(() => {
    const d = parseIsoDate(topVisibleIso);
    return d.toLocaleDateString("en-GB", { month: "long", year: "numeric" });
  }, [topVisibleIso]);

  const scrollToToday = useCallback(() => {
    const node = (innerContainerRef.current ?? document).querySelector(
      `[data-schedule-iso="${todayIso}"]`,
    ) as HTMLElement | null;
    if (!node) return;
    // The worker explicitly tapped Today — they want a deliberate,
    // smoothly-animated jump back. Mark settled so the auto-anchor
    // doesn't snap them somewhere else mid-scroll.
    settledRef.current = true;
    node.scrollIntoView({ block: "start", behavior: "smooth" });
  }, [todayIso]);

  return {
    q,
    merged,
    cells,
    containerRef: setContainerEl,
    topSentinelRef: setTopSentinelEl,
    bottomSentinelRef: setBottomSentinelEl,
    monthLabel,
    todayInView,
    scrollToToday,
  };
}
