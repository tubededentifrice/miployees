import { useEffect, useState } from "react";
import { fetchJson, getActiveWorkspaceSlug, resolveApiPath } from "@/lib/api";

const DB_NAME = "crewday-offline-queue";
const DB_VERSION = 1;
const STORE_NAME = "pending-mutations";
const SYNC_TAG = "crewday-offline-queue";
const BASE_BACKOFF_MS = 1_000;
const MAX_BACKOFF_MS = 5 * 60_000;

export type QueuedMutationMethod = "POST" | "PUT" | "PATCH" | "DELETE";

export interface QueuedMutation {
  id: string;
  storageKey: string;
  workspaceSlug: string | null;
  kind: string;
  method: QueuedMutationMethod;
  path: string;
  body: unknown;
  idempotencyKey: string;
  createdAt: number;
  attempts: number;
  nextAttemptAt: number;
}

export interface EnqueueMutationInput {
  kind: string;
  method: QueuedMutationMethod;
  path: string;
  body?: unknown;
  id?: string;
  idempotencyKey?: string;
  workspaceSlug?: string | null;
  createdAt?: number;
}

export interface DrainOfflineQueueResult {
  attempted: number;
  succeeded: number;
  failed: number;
}

interface DrainOfflineQueueOptions {
  now?: () => number;
  scheduleRetry?: boolean;
}

type CountSubscriber = (count: number) => void;
type ReplaySubscriber = (entry: QueuedMutation) => void;
type SyncRegistration = ServiceWorkerRegistration & {
  sync?: { register: (tag: string) => Promise<void> };
};
type StoredReplayController = { stop: () => void };

const REPLAY_CONTROLLER_KEY = "__crewdayOfflineQueueReplay";

let dbPromise: Promise<IDBDatabase> | null = null;
let drainPromise: Promise<DrainOfflineQueueResult> | null = null;
let retryTimer: ReturnType<typeof window.setTimeout> | null = null;
const pendingCounts = new Map<string, number>();
const countSubscribers = new Set<{ workspaceSlug: string | null; listener: CountSubscriber }>();
const replaySubscribers = new Set<ReplaySubscriber>();

export function isBrowserOnline(): boolean {
  return typeof navigator === "undefined" || navigator.onLine !== false;
}

export async function enqueueMutation(input: EnqueueMutationInput): Promise<QueuedMutation> {
  const createdAt = input.createdAt ?? Date.now();
  const id = input.id ?? makeQueueId();
  const path = resolveApiPath(input.path);
  const workspaceSlug = input.workspaceSlug ?? workspaceSlugFromPath(path) ?? getActiveWorkspaceSlug();
  const storageKey = makeStorageKey(workspaceSlug, id);
  const entry: QueuedMutation = {
    id,
    storageKey,
    workspaceSlug,
    kind: input.kind,
    method: input.method,
    path,
    body: input.body ?? null,
    idempotencyKey: input.idempotencyKey ?? makeIdempotencyKey(storageKey),
    createdAt,
    attempts: 0,
    nextAttemptAt: createdAt,
  };

  await putEntry(entry);
  await refreshPendingCount(workspaceSlug);
  requestBackgroundSync();
  return entry;
}

export async function pendingMutationCount(
  workspaceSlug: string | null = getActiveWorkspaceSlug(),
): Promise<number> {
  return (await listEntries({ workspaceSlug })).length;
}

export function subscribePendingMutationCount(
  listener: CountSubscriber,
  workspaceSlug: string | null = getActiveWorkspaceSlug(),
): () => void {
  const subscriber = { workspaceSlug, listener };
  countSubscribers.add(subscriber);
  listener(getPendingMutationCountSnapshot(workspaceSlug));
  void refreshPendingCount(workspaceSlug);
  return () => {
    countSubscribers.delete(subscriber);
  };
}

export function subscribeOfflineQueueReplay(listener: ReplaySubscriber): () => void {
  replaySubscribers.add(listener);
  return () => {
    replaySubscribers.delete(listener);
  };
}

export function getPendingMutationCountSnapshot(
  workspaceSlug: string | null = getActiveWorkspaceSlug(),
): number {
  return pendingCounts.get(workspaceKey(workspaceSlug)) ?? 0;
}

export function usePendingMutationCount(): number {
  const workspaceSlug = getActiveWorkspaceSlug();
  const [count, setCount] = useState(() => getPendingMutationCountSnapshot(workspaceSlug));

  useEffect(() => subscribePendingMutationCount(setCount, workspaceSlug), [workspaceSlug]);

  return count;
}

export function startOfflineQueueReplay(): () => void {
  const existing = replayController();
  if (existing) return existing.stop;

  void drainOfflineQueue();

  const onOnline = (): void => {
    void drainOfflineQueue();
  };
  const onVisible = (): void => {
    if (document.visibilityState === "visible") void drainOfflineQueue();
  };
  const onMessage = (event: MessageEvent): void => {
    if (event.data === SYNC_TAG || event.data?.type === "CREWDAY_DRAIN_OFFLINE_QUEUE") {
      void drainOfflineQueue();
    }
  };

  window.addEventListener("online", onOnline);
  document.addEventListener("visibilitychange", onVisible);
  navigator.serviceWorker?.addEventListener("message", onMessage);

  const stop = (): void => {
    window.removeEventListener("online", onOnline);
    document.removeEventListener("visibilitychange", onVisible);
    navigator.serviceWorker?.removeEventListener("message", onMessage);
    if (retryTimer) window.clearTimeout(retryTimer);
    retryTimer = null;
    if (replayController()?.stop === stop) setReplayController(null);
  };

  setReplayController({ stop });
  return stop;
}

export function drainOfflineQueue(
  options: DrainOfflineQueueOptions = {},
): Promise<DrainOfflineQueueResult> {
  if (drainPromise) return drainPromise;
  drainPromise = drainOfflineQueueOnce(options).finally(() => {
    drainPromise = null;
  });
  return drainPromise;
}

export async function __listQueuedMutationsForTests(): Promise<QueuedMutation[]> {
  return listEntries();
}

export async function __clearOfflineQueueForTests(): Promise<void> {
  if (dbPromise) {
    const db = await dbPromise;
    const tx = db.transaction(STORE_NAME, "readwrite");
    await requestToPromise(tx.objectStore(STORE_NAME).clear());
    await transactionDone(tx);
  }
  pendingCounts.clear();
  notifyAllSubscribers();
}

export function __resetOfflineQueueForTests(): void {
  startOfflineQueueReplayForTestsCleanup();
  dbPromise = null;
  drainPromise = null;
  if (retryTimer) window.clearTimeout(retryTimer);
  retryTimer = null;
  pendingCounts.clear();
  countSubscribers.clear();
  replaySubscribers.clear();
}

async function drainOfflineQueueOnce(
  options: DrainOfflineQueueOptions,
): Promise<DrainOfflineQueueResult> {
  const result: DrainOfflineQueueResult = { attempted: 0, succeeded: 0, failed: 0 };
  if (!isBrowserOnline()) return result;

  const now = options.now ?? Date.now;
  const entries = await listEntries();
  for (const entry of entries) {
    const currentTime = now();
    if (entry.nextAttemptAt > currentTime) break;

    result.attempted += 1;
    try {
      await replayEntry(entry);
      await deleteEntry(entry.storageKey);
      result.succeeded += 1;
      notifyReplaySubscribers(entry);
      await refreshPendingCount(entry.workspaceSlug);
    } catch {
      const attempts = entry.attempts + 1;
      await putEntry({
        ...entry,
        attempts,
        nextAttemptAt: currentTime + backoffMs(attempts),
      });
      result.failed += 1;
      await refreshPendingCount(entry.workspaceSlug);
      break;
    }
  }

  if (options.scheduleRetry !== false && !options.now) await scheduleNextDueDrain();
  return result;
}

async function replayEntry(entry: QueuedMutation): Promise<void> {
  await fetchJson<unknown>(entry.path, {
    method: entry.method,
    body: entry.body === null ? undefined : entry.body,
    headers: { "Idempotency-Key": entry.idempotencyKey },
  });
}

function requestBackgroundSync(): void {
  if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return;

  void navigator.serviceWorker.ready
    .then((registration: SyncRegistration) => registration.sync?.register(SYNC_TAG))
    .catch((err: unknown) => {
      console.debug("Background sync registration skipped", err);
    });
}

async function refreshPendingCount(workspaceSlug: string | null): Promise<void> {
  pendingCounts.set(workspaceKey(workspaceSlug), await pendingMutationCount(workspaceSlug));
  notifySubscribers(workspaceSlug);
}

function notifySubscribers(workspaceSlug: string | null): void {
  const count = getPendingMutationCountSnapshot(workspaceSlug);
  for (const subscriber of countSubscribers) {
    if (workspaceKey(subscriber.workspaceSlug) === workspaceKey(workspaceSlug)) {
      subscriber.listener(count);
    }
  }
}

function notifyAllSubscribers(): void {
  for (const subscriber of countSubscribers) {
    subscriber.listener(getPendingMutationCountSnapshot(subscriber.workspaceSlug));
  }
}

function notifyReplaySubscribers(entry: QueuedMutation): void {
  for (const listener of replaySubscribers) listener(entry);
}

async function putEntry(entry: QueuedMutation): Promise<void> {
  const db = await openQueueDb();
  const tx = db.transaction(STORE_NAME, "readwrite");
  await requestToPromise(tx.objectStore(STORE_NAME).put(entry));
  await transactionDone(tx);
}

async function deleteEntry(storageKey: string): Promise<void> {
  const db = await openQueueDb();
  const tx = db.transaction(STORE_NAME, "readwrite");
  await requestToPromise(tx.objectStore(STORE_NAME).delete(storageKey));
  await transactionDone(tx);
}

async function listEntries(
  options: { workspaceSlug?: string | null } = {},
): Promise<QueuedMutation[]> {
  const db = await openQueueDb();
  const tx = db.transaction(STORE_NAME, "readonly");
  const entries = await requestToPromise<QueuedMutation[]>(
    tx.objectStore(STORE_NAME).getAll(),
  );
  await transactionDone(tx);
  const filtered = Object.hasOwn(options, "workspaceSlug")
    ? entries.filter((entry) => workspaceKey(entry.workspaceSlug) === workspaceKey(options.workspaceSlug ?? null))
    : entries;
  return filtered.sort((a, b) => a.createdAt - b.createdAt || a.storageKey.localeCompare(b.storageKey));
}

function openQueueDb(): Promise<IDBDatabase> {
  if (dbPromise) return dbPromise;

  dbPromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME, { keyPath: "storageKey" });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("Failed to open offline queue"));
  });
  return dbPromise;
}

function requestToPromise<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB request failed"));
  });
}

function transactionDone(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error ?? new Error("IndexedDB transaction failed"));
    tx.onabort = () => reject(tx.error ?? new Error("IndexedDB transaction aborted"));
  });
}

function makeQueueId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function makeIdempotencyKey(storageKey: string): string {
  return `offline:${storageKey}`;
}

function makeStorageKey(workspaceSlug: string | null, id: string): string {
  return `${workspaceKey(workspaceSlug)}:${id}`;
}

function workspaceKey(workspaceSlug: string | null): string {
  return workspaceSlug ?? "_";
}

function workspaceSlugFromPath(path: string): string | null {
  const match = path.match(/^\/w\/([^/]+)\//);
  return match?.[1] ?? null;
}

function backoffMs(attempts: number): number {
  const exponent = Math.min(Math.max(attempts - 1, 0), 8);
  return Math.min(BASE_BACKOFF_MS * 2 ** exponent, MAX_BACKOFF_MS);
}

async function scheduleNextDueDrain(): Promise<void> {
  if (retryTimer) window.clearTimeout(retryTimer);
  retryTimer = null;
  if (!isBrowserOnline()) return;

  const entries = await listEntries();
  const nextAttemptAt = entries
    .filter((entry) => entry.nextAttemptAt > Date.now())
    .sort((a, b) => a.nextAttemptAt - b.nextAttemptAt)[0]?.nextAttemptAt;
  if (nextAttemptAt === undefined) return;

  const delay = Math.max(nextAttemptAt - Date.now(), BASE_BACKOFF_MS);
  retryTimer = window.setTimeout(() => {
    retryTimer = null;
    void drainOfflineQueue();
  }, delay);
}

function replayController(): StoredReplayController | null {
  return (window as unknown as Record<string, StoredReplayController | undefined>)[REPLAY_CONTROLLER_KEY] ?? null;
}

function setReplayController(controller: StoredReplayController | null): void {
  const storage = window as unknown as Record<string, StoredReplayController | undefined>;
  if (controller) storage[REPLAY_CONTROLLER_KEY] = controller;
  else delete storage[REPLAY_CONTROLLER_KEY];
}

function startOfflineQueueReplayForTestsCleanup(): void {
  replayController()?.stop();
}
