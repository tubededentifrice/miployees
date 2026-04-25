import { useEffect, useMemo, useRef, useState } from "react";
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { useCloseOnEscape } from "@/lib/useCloseOnEscape";
import DeskPage from "@/components/DeskPage";
import AutoGrowTextarea from "@/components/AutoGrowTextarea";
import { Chip, Loading } from "@/components/common";
import type {
  InventoryItem,
  InventoryMovement,
  InventoryMovementReason,
  Property,
} from "@/types/api";

// §08 — the reason vocabulary used by both the adjust drawer and
// the stocktake sheet. Kept narrow and intentional: each entry is a
// real-world story ("theft" ≠ "loss") so reports stay meaningful.
const ADJUST_REASONS: { value: InventoryMovementReason; label: string }[] = [
  { value: "audit_correction", label: "Audit correction (no cause)" },
  { value: "theft", label: "Theft" },
  { value: "loss", label: "Loss (unknown)" },
  { value: "found", label: "Found" },
  { value: "waste", label: "Waste / damaged" },
  { value: "returned_to_vendor", label: "Returned to vendor" },
  { value: "restock", label: "Restock (off-channel purchase)" },
];

const REASON_LABEL: Record<InventoryMovementReason, string> = {
  restock: "Restock",
  consume: "Consumed by task",
  produce: "Produced by task",
  waste: "Waste",
  theft: "Theft",
  loss: "Loss",
  found: "Found",
  returned_to_vendor: "Returned",
  transfer_in: "Transfer in",
  transfer_out: "Transfer out",
  audit_correction: "Audit correction",
  adjust: "Adjust",
};

// Each reason maps to a timeline dot variant. The ledger aesthetic
// stays quiet — moss for gains, rust for losses, ink for neutrals.
const REASON_TONE: Record<
  InventoryMovementReason,
  "gain" | "loss" | "neutral"
> = {
  restock: "gain",
  produce: "gain",
  found: "gain",
  transfer_in: "gain",
  consume: "loss",
  waste: "loss",
  theft: "loss",
  loss: "loss",
  returned_to_vendor: "loss",
  transfer_out: "loss",
  audit_correction: "neutral",
  adjust: "neutral",
};

// Format decimal qty with up to 3 decimals, trailing zeros trimmed.
// `2` stays `2`, `0.300` becomes `0.3`. Shared across the drawer,
// templates page, and task detail panel.
function fmtQty(n: number): string {
  if (!Number.isFinite(n)) return String(n);
  const s = n.toFixed(3);
  return s.replace(/\.?0+$/, "");
}

// Human-ish relative timestamp. "just now" / "3h ago" / "Apr 12".
function fmtWhen(iso: string): string {
  const d = new Date(iso);
  const now = Date.now();
  const diffMin = Math.round((now - d.getTime()) / 60_000);
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.round(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.round(diffHr / 24);
  if (diffDay < 7) return `${diffDay}d ago`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

interface MovementsPage {
  items: InventoryMovement[];
  next_cursor: string | null;
}

export default function InventoryPage() {
  const qc = useQueryClient();
  const invQ = useQuery({
    queryKey: qk.inventory(),
    queryFn: () => fetchJson<InventoryItem[]>("/api/v1/inventory"),
  });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });

  const [openItemId, setOpenItemId] = useState<string | null>(null);
  const [stocktakePid, setStocktakePid] = useState<string | null>(null);
  const stocktakeRef = useRef<HTMLDialogElement>(null);

  const sub =
    "Per-property stock. Items at or below par trigger a procurement task. Click a row to see full history and adjust.";
  const actions = <button className="btn btn--moss">+ New item</button>;
  const overflow = [{ label: "Export CSV", onSelect: () => undefined }];

  if (invQ.isPending || propsQ.isPending) {
    return (
      <DeskPage title="Inventory" sub={sub} actions={actions} overflow={overflow}>
        <Loading />
      </DeskPage>
    );
  }
  if (!invQ.data || !propsQ.data) {
    return (
      <DeskPage title="Inventory" sub={sub} actions={actions} overflow={overflow}>
        Failed to load.
      </DeskPage>
    );
  }

  const propsById = new Map(propsQ.data.map((p) => [p.id, p]));
  const order: string[] = [];
  const byProp = new Map<string, InventoryItem[]>();
  for (const item of invQ.data) {
    if (!byProp.has(item.property_id)) {
      byProp.set(item.property_id, []);
      order.push(item.property_id);
    }
    byProp.get(item.property_id)!.push(item);
  }

  const openItem = openItemId
    ? invQ.data.find((i) => i.id === openItemId) ?? null
    : null;

  function startStocktake(pid: string) {
    setStocktakePid(pid);
    stocktakeRef.current?.showModal();
    qc.invalidateQueries({ queryKey: qk.inventory() });
  }

  return (
    <DeskPage title="Inventory" sub={sub} actions={actions} overflow={overflow}>
      {order.map((pid) => {
        const p = propsById.get(pid);
        const items = byProp.get(pid) ?? [];
        return (
          <div key={pid} className="panel">
            <header className="panel__head">
              <h2>
                {p && <Chip tone={p.color} size="sm">{p.name}</Chip>} Inventory
              </h2>
              <div className="inv-panel__actions">
                <span className="muted mono">{items.length} items</span>
                <button
                  className="btn btn--ghost btn--sm"
                  onClick={() => startStocktake(pid)}
                >
                  Start stocktake
                </button>
              </div>
            </header>
            <table className="table inv-table">
              <thead>
                <tr>
                  <th>Item</th>
                  <th>SKU</th>
                  <th>Area</th>
                  <th className="num-col">On hand</th>
                  <th className="num-col">Par</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => {
                  const empty = item.on_hand <= 0;
                  const low = item.on_hand < item.par;
                  const active = openItemId === item.id;
                  const rowCls = [
                    "inv-row",
                    empty ? "row--critical" : low ? "row--warn" : "",
                    active ? "inv-row--active" : "",
                  ]
                    .filter(Boolean)
                    .join(" ");
                  return (
                    <tr
                      key={item.id}
                      className={rowCls}
                      onClick={() => setOpenItemId(item.id)}
                    >
                      <td><strong>{item.name}</strong></td>
                      <td className="mono muted">{item.sku}</td>
                      <td>{item.area}</td>
                      <td className="mono num-col">
                        <strong>{fmtQty(item.on_hand)}</strong>{" "}
                        <span className="unit">{item.unit}</span>
                      </td>
                      <td className="mono muted num-col">{fmtQty(item.par)}</td>
                      <td>
                        {empty ? (
                          <Chip tone="rust" size="sm">out of stock</Chip>
                        ) : low ? (
                          <Chip tone="sand" size="sm">below par</Chip>
                        ) : (
                          <Chip tone="moss" size="sm">ok</Chip>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        );
      })}

      {openItem && (
        <InventoryDrawer
          item={openItem}
          onClose={() => setOpenItemId(null)}
        />
      )}

      <dialog ref={stocktakeRef} className="modal modal--sheet">
        {stocktakePid && (
          <StocktakeSheet
            propertyId={stocktakePid}
            propertyName={propsById.get(stocktakePid)?.name ?? ""}
            items={byProp.get(stocktakePid) ?? []}
            onClose={() => {
              stocktakeRef.current?.close();
              setStocktakePid(null);
            }}
          />
        )}
      </dialog>
    </DeskPage>
  );
}

function InventoryDrawer({ item, onClose }: { item: InventoryItem; onClose: () => void }) {
  const qc = useQueryClient();
  useCloseOnEscape(onClose);

  // Infinite-scrolling ledger. 8 per page keeps the first screen
  // tight on laptops; IntersectionObserver fetches more when the
  // sentinel enters the drawer's own scroll viewport.
  const movementsQ = useInfiniteQuery<MovementsPage, Error>({
    queryKey: qk.inventoryMovements(item.id),
    initialPageParam: null,
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({ limit: "8" });
      if (typeof pageParam === "string") params.set("before", pageParam);
      return fetchJson<MovementsPage>(
        `/api/v1/inventory/${item.id}/movements?${params.toString()}`,
      );
    },
    getNextPageParam: (last) => last.next_cursor,
  });

  const allMovements = useMemo(
    () => movementsQ.data?.pages.flatMap((p) => p.items) ?? [],
    [movementsQ.data],
  );

  // Drawer-scoped scroll root for the IntersectionObserver. We
  // observe the sentinel relative to the aside's own scroll viewport
  // so the trigger fires even when the drawer's inner overflow (not
  // the window) is what's scrolling.
  const drawerRef = useRef<HTMLElement | null>(null);
  const sentinelRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const sentinel = sentinelRef.current;
    const root = drawerRef.current;
    if (!sentinel || !root) return;
    const io = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (
            entry.isIntersecting &&
            movementsQ.hasNextPage &&
            !movementsQ.isFetchingNextPage
          ) {
            void movementsQ.fetchNextPage();
          }
        }
      },
      { root, rootMargin: "140px" },
    );
    io.observe(sentinel);
    return () => io.disconnect();
  }, [movementsQ]);

  const [observed, setObserved] = useState<string>(String(item.on_hand));
  const [reason, setReason] = useState<InventoryMovementReason>(
    "audit_correction",
  );
  const [note, setNote] = useState("");
  const [err, setErr] = useState<string | null>(null);

  const adjust = useMutation({
    mutationFn: (body: {
      observed_on_hand: number;
      reason: InventoryMovementReason;
      note: string;
    }) =>
      fetchJson<unknown>(`/api/v1/inventory/${item.id}/adjust`, {
        method: "POST",
        body,
      }),
    onSuccess: () => {
      setErr(null);
      setNote("");
      qc.invalidateQueries({ queryKey: qk.inventory() });
      qc.invalidateQueries({ queryKey: qk.inventoryMovements(item.id) });
    },
    onError: (e: Error) => setErr(e.message || "Adjust failed"),
  });

  const observedNum = Number.parseFloat(observed);
  const delta = Number.isFinite(observedNum)
    ? Number((observedNum - item.on_hand).toFixed(4))
    : null;

  const coverage = item.par > 0 ? Math.min(1, item.on_hand / item.par) : 0;
  const statusLabel =
    item.on_hand <= 0 ? "out of stock" : item.on_hand < item.par ? "below par" : "in stock";
  const statusTone: "rust" | "sand" | "moss" =
    item.on_hand <= 0 ? "rust" : item.on_hand < item.par ? "sand" : "moss";

  return (
    <>
      <div
        className="inv-drawer__scrim"
        onClick={onClose}
        aria-hidden="true"
      />
      <aside
        ref={drawerRef}
        className="inv-drawer"
        role="dialog"
        aria-label={`Inventory ledger — ${item.name}`}
      >
        <div className="inv-drawer__ribbon" aria-hidden="true" />
        <header className="inv-drawer__head">
          <div className="inv-drawer__head-top">
            <span className="inv-drawer__eyebrow">{item.sku}</span>
            <button
              type="button"
              className="inv-drawer__close"
              onClick={onClose}
              aria-label="Close (Esc)"
            >
              ×
            </button>
          </div>
          <h3 className="inv-drawer__title">{item.name}</h3>
          <div className="inv-drawer__meta">
            <span>{item.area}</span>
            <span className="inv-drawer__meta-sep" aria-hidden="true">·</span>
            <Chip tone={statusTone} size="sm">{statusLabel}</Chip>
          </div>
        </header>

        <section className="inv-hero">
          <div className="inv-hero__stat">
            <span className="inv-hero__label">On hand</span>
            <span className="inv-hero__num">{fmtQty(item.on_hand)}</span>
            <span className="inv-hero__unit">{item.unit}</span>
          </div>
          <div className="inv-hero__divider" aria-hidden="true" />
          <div className="inv-hero__stat inv-hero__stat--muted">
            <span className="inv-hero__label">Par</span>
            <span className="inv-hero__num">{fmtQty(item.par)}</span>
            <span className="inv-hero__unit">{item.unit}</span>
          </div>
          <div
            className="inv-hero__gauge"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={item.par || 1}
            aria-valuenow={item.on_hand}
            aria-label="Stock vs par"
          >
            <div
              className={`inv-hero__gauge-fill inv-hero__gauge-fill--${statusTone}`}
              style={{ width: `${coverage * 100}%` }}
            />
          </div>
        </section>

        <section className="inv-drawer__adjust">
          <h4 className="inv-drawer__sect">Record an event</h4>
          <form
            className="inv-adjust"
            onSubmit={(e) => {
              e.preventDefault();
              if (delta === null || delta === 0) {
                setErr("Observed must differ from current on-hand.");
                return;
              }
              adjust.mutate({
                observed_on_hand: observedNum,
                reason,
                note,
              });
            }}
          >
            <div className="inv-adjust__grid">
              <label className="field inv-adjust__field">
                <span>Observed count</span>
                <div className="inv-adjust__input-row">
                  <input
                    className="inv-adjust__input mono"
                    type="number"
                    step="0.01"
                    min="0"
                    value={observed}
                    onChange={(e) => setObserved(e.target.value)}
                    required
                  />
                  <span className="inv-adjust__unit">{item.unit}</span>
                </div>
              </label>
              <label className="field inv-adjust__field">
                <span>Reason</span>
                <select
                  className="inv-adjust__select"
                  value={reason}
                  onChange={(e) =>
                    setReason(e.target.value as InventoryMovementReason)
                  }
                >
                  {ADJUST_REASONS.map((r) => (
                    <option key={r.value} value={r.value}>
                      {r.label}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <label className="field">
              <span>Note (optional)</span>
              <AutoGrowTextarea
                className="inv-adjust__note"
                placeholder="e.g. Found in garage, soaked in rain."
                value={note}
                onChange={(e) => setNote(e.target.value)}
              />
            </label>
            <div className="inv-adjust__footer">
              <div
                className={`inv-delta inv-delta--${
                  delta === null || delta === 0
                    ? "neutral"
                    : delta > 0
                      ? "gain"
                      : "loss"
                }`}
                aria-live="polite"
              >
                {delta === null || delta === 0 ? (
                  <>
                    <span className="inv-delta__sign">—</span>
                    <span className="inv-delta__body">no change yet</span>
                  </>
                ) : (
                  <>
                    <span className="inv-delta__sign">
                      {delta > 0 ? "+" : "−"}
                    </span>
                    <span className="inv-delta__num mono">
                      {fmtQty(Math.abs(delta))}
                    </span>
                    <span className="inv-delta__unit">{item.unit}</span>
                  </>
                )}
              </div>
              <button
                className="btn btn--moss"
                type="submit"
                disabled={adjust.isPending || delta === null || delta === 0}
              >
                Record adjustment
              </button>
            </div>
            {err && <p className="form-error">{err}</p>}
          </form>
        </section>

        <section className="inv-drawer__history">
          <header className="inv-drawer__history-head">
            <h4 className="inv-drawer__sect">Ledger</h4>
            {!movementsQ.isPending && (
              <span className="inv-drawer__history-count muted mono">
                {allMovements.length} entries
              </span>
            )}
          </header>
          {movementsQ.isPending ? (
            <Loading />
          ) : movementsQ.isError ? (
            <p className="form-error">Failed to load history.</p>
          ) : allMovements.length === 0 ? (
            <p className="muted inv-history__empty">
              No movements yet. The first restock or task completion shows up here.
            </p>
          ) : (
            <ol className="inv-history">
              {allMovements.map((m, idx) => {
                const tone = REASON_TONE[m.reason];
                return (
                  <li
                    key={m.id}
                    className={`inv-history__row inv-history__row--${tone}`}
                    style={{ animationDelay: `${Math.min(idx * 28, 320)}ms` }}
                  >
                    <div className="inv-history__dot" aria-hidden="true" />
                    <div className="inv-history__body">
                      <div className="inv-history__top">
                        <span className="inv-history__reason">
                          {REASON_LABEL[m.reason]}
                        </span>
                        <time
                          className="inv-history__when mono"
                          dateTime={m.occurred_at}
                        >
                          {fmtWhen(m.occurred_at)}
                        </time>
                      </div>
                      <div
                        className={`inv-history__delta mono inv-history__delta--${tone}`}
                      >
                        {m.delta > 0 ? "+" : "−"}
                        {fmtQty(Math.abs(m.delta))} {item.unit}
                      </div>
                      <div className="inv-history__foot">
                        <span className="muted">{m.actor_id}</span>
                        {m.source_task_id && (
                          <span className="inv-history__src">
                            ↳ task {m.source_task_id}
                          </span>
                        )}
                        {m.source_stocktake_id && (
                          <span className="inv-history__src">
                            ↳ stocktake
                          </span>
                        )}
                        {m.note && (
                          <span className="inv-history__note">{m.note}</span>
                        )}
                      </div>
                    </div>
                  </li>
                );
              })}
            </ol>
          )}
          <div ref={sentinelRef} className="inv-history__sentinel" aria-hidden="true" />
          {movementsQ.isFetchingNextPage && (
            <p className="inv-history__loading muted">
              <span className="inv-history__loading-dots" aria-hidden="true">
                <i /><i /><i />
              </span>
              loading older entries
            </p>
          )}
          {!movementsQ.hasNextPage && allMovements.length > 0 && !movementsQ.isFetchingNextPage && (
            <p className="inv-history__end muted">· end of ledger ·</p>
          )}
        </section>
      </aside>
    </>
  );
}

interface StocktakeLine {
  item_id: string;
  observed: string;
  reason: InventoryMovementReason;
  note: string;
}

function StocktakeSheet({
  propertyId,
  propertyName,
  items,
  onClose,
}: {
  propertyId: string;
  propertyName: string;
  items: InventoryItem[];
  onClose: () => void;
}) {
  const qc = useQueryClient();
  // Native <dialog> handles Escape via the browser's close event;
  // the hook stays no-op here but keeps the pattern consistent.

  const [lines, setLines] = useState<Record<string, StocktakeLine>>(() =>
    Object.fromEntries(
      items.map((i) => [
        i.id,
        {
          item_id: i.id,
          observed: String(i.on_hand),
          reason: "audit_correction" as InventoryMovementReason,
          note: "",
        },
      ]),
    ),
  );
  const [err, setErr] = useState<string | null>(null);

  const open = useMutation({
    mutationFn: () =>
      fetchJson<{ id: string }>(
        `/api/v1/properties/${propertyId}/stocktakes`,
        { method: "POST", body: {} },
      ),
  });
  const commit = useMutation({
    mutationFn: ({
      sid,
      payload,
    }: {
      sid: string;
      payload: unknown;
    }) =>
      fetchJson<unknown>(`/api/v1/stocktakes/${sid}/commit`, {
        method: "POST",
        body: payload,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.inventory() });
      onClose();
    },
  });

  const dirty = useMemo(
    () =>
      items.filter((i) => {
        const l = lines[i.id];
        if (!l) return false;
        const n = Number.parseFloat(l.observed);
        return Number.isFinite(n) && Math.abs(n - i.on_hand) > 1e-9;
      }),
    [items, lines],
  );

  async function submit() {
    setErr(null);
    try {
      const session = await open.mutateAsync();
      const payload = {
        lines: dirty.flatMap((i) => {
          // ``dirty`` is already filtered to items whose ``lines[i.id]``
          // is defined; re-narrow here for the type-checker since
          // ``lines`` is a Record (open index signature).
          const l = lines[i.id];
          if (!l) return [];
          return [
            {
              item_id: i.id,
              observed_on_hand: Number.parseFloat(l.observed),
              reason: l.reason,
              note: l.note,
            },
          ];
        }),
      };
      await commit.mutateAsync({ sid: session.id, payload });
    } catch (e) {
      setErr((e as Error).message || "Stocktake failed");
    }
  }

  return (
    <form
      className="modal__body stocktake"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <h3 className="modal__title">Stocktake — {propertyName}</h3>
      <p className="modal__sub">
        Walk the property, enter observed counts, pick a reason for any
        drift, and commit. A single audit row ties the whole session
        together.
      </p>

      <ul className="stocktake__list">
        {items.map((i) => {
          const l = lines[i.id]!;
          const observedNum = Number.parseFloat(l.observed);
          const delta = Number.isFinite(observedNum)
            ? Number((observedNum - i.on_hand).toFixed(4))
            : null;
          return (
            <li key={i.id} className="stocktake__row">
              <div className="stocktake__item">
                <strong>{i.name}</strong>
                <span className="muted mono">{i.sku}</span>
                <span className="muted">
                  on hand {fmtQty(i.on_hand)} {i.unit}
                </span>
              </div>
              <input
                className="input--inline mono stocktake__observed"
                type="number"
                step="0.01"
                min="0"
                value={l.observed}
                onChange={(e) =>
                  setLines((prev) => ({
                    ...prev,
                    [i.id]: { ...prev[i.id]!, observed: e.target.value },
                  }))
                }
              />
              <select
                className="input--inline"
                value={l.reason}
                onChange={(e) =>
                  setLines((prev) => ({
                    ...prev,
                    [i.id]: {
                      ...prev[i.id]!,
                      reason: e.target.value as InventoryMovementReason,
                    },
                  }))
                }
                disabled={delta === null || delta === 0}
              >
                {ADJUST_REASONS.map((r) => (
                  <option key={r.value} value={r.value}>{r.label}</option>
                ))}
              </select>
              <div className="stocktake__delta mono">
                {delta === null || delta === 0 ? (
                  <span className="muted">—</span>
                ) : (
                  <span className={delta > 0 ? "delta-pos" : "delta-neg"}>
                    {delta > 0 ? "+" : ""}
                    {fmtQty(delta)}
                  </span>
                )}
              </div>
            </li>
          );
        })}
      </ul>

      {err && <p className="form-error">{err}</p>}

      <div className="modal__actions">
        <button type="button" className="btn btn--ghost" onClick={onClose}>
          Cancel
        </button>
        <button
          type="submit"
          className="btn btn--moss"
          disabled={dirty.length === 0 || open.isPending || commit.isPending}
        >
          {dirty.length === 0
            ? "No changes to commit"
            : `Commit ${dirty.length} change${dirty.length === 1 ? "" : "s"}`}
        </button>
      </div>
    </form>
  );
}
