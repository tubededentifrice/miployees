interface StoreShape {
  keyPath: string;
  records: Map<string, unknown>;
}

interface DatabaseShape {
  stores: Map<string, StoreShape>;
}

class FakeIdbRequest<T> {
  onsuccess: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  result!: T;
  error: DOMException | null = null;

  succeed(value: T): void {
    queueMicrotask(() => {
      this.result = value;
      this.onsuccess?.(new Event("success"));
    });
  }
}

class FakeIdbOpenRequest extends FakeIdbRequest<IDBDatabase> {
  onupgradeneeded: ((event: IDBVersionChangeEvent) => void) | null = null;
}

class FakeObjectStore {
  constructor(private readonly store: StoreShape) {}

  put(value: unknown): IDBRequest<IDBValidKey> {
    const key = readRecordKey(value, this.store.keyPath);
    this.store.records.set(key, structuredClone(value));
    const request = new FakeIdbRequest<IDBValidKey>();
    request.succeed(key);
    return asIdbRequest(request);
  }

  getAll(): IDBRequest<unknown[]> {
    const request = new FakeIdbRequest<unknown[]>();
    request.succeed(Array.from(this.store.records.values()).map((value) => structuredClone(value)));
    return asIdbRequest(request);
  }

  count(): IDBRequest<number> {
    const request = new FakeIdbRequest<number>();
    request.succeed(this.store.records.size);
    return asIdbRequest(request);
  }

  delete(query: IDBValidKey | IDBKeyRange): IDBRequest<undefined> {
    if (typeof query === "string") this.store.records.delete(query);
    const request = new FakeIdbRequest<undefined>();
    request.succeed(undefined);
    return asIdbRequest(request);
  }

  clear(): IDBRequest<undefined> {
    this.store.records.clear();
    const request = new FakeIdbRequest<undefined>();
    request.succeed(undefined);
    return asIdbRequest(request);
  }
}

class FakeTransaction {
  oncomplete: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onabort: ((event: Event) => void) | null = null;
  error: DOMException | null = null;

  constructor(private readonly database: DatabaseShape) {}

  objectStore(name: string): IDBObjectStore {
    const store = this.database.stores.get(name);
    if (!store) throw new DOMException(`Unknown object store: ${name}`, "NotFoundError");
    return asIdbObjectStore(new FakeObjectStore(store));
  }

  complete(): void {
    setTimeout(() => {
      this.oncomplete?.(new Event("complete"));
    }, 0);
  }
}

class FakeDatabase {
  readonly objectStoreNames: DOMStringList;

  constructor(private readonly database: DatabaseShape) {
    this.objectStoreNames = {
      contains: (name: string) => this.database.stores.has(name),
      item: (index: number) => Array.from(this.database.stores.keys())[index] ?? null,
      length: this.database.stores.size,
    } as DOMStringList;
  }

  createObjectStore(name: string, options?: IDBObjectStoreParameters): IDBObjectStore {
    const keyPath = typeof options?.keyPath === "string" ? options.keyPath : "id";
    const store = { keyPath, records: new Map<string, unknown>() };
    this.database.stores.set(name, store);
    return asIdbObjectStore(new FakeObjectStore(store));
  }

  transaction(storeNames: string | string[], _mode?: IDBTransactionMode): IDBTransaction {
    const names = Array.isArray(storeNames) ? storeNames : [storeNames];
    for (const name of names) {
      if (!this.database.stores.has(name)) {
        throw new DOMException(`Unknown object store: ${name}`, "NotFoundError");
      }
    }
    const tx = new FakeTransaction(this.database);
    tx.complete();
    return tx as unknown as IDBTransaction;
  }
}

export function installFakeIndexedDb(): () => void {
  const original = globalThis.indexedDB;
  const databases = new Map<string, DatabaseShape>();
  const fake: Pick<IDBFactory, "open"> = {
    open: (name: string, _version?: number) => {
      const request = new FakeIdbOpenRequest();
      queueMicrotask(() => {
        let database = databases.get(name);
        const isNew = !database;
        if (!database) {
          database = { stores: new Map<string, StoreShape>() };
          databases.set(name, database);
        }
        const db = new FakeDatabase(database) as unknown as IDBDatabase;
        request.result = db;
        if (isNew) request.onupgradeneeded?.(new Event("upgradeneeded") as IDBVersionChangeEvent);
        request.onsuccess?.(new Event("success"));
      });
      return request as unknown as IDBOpenDBRequest;
    },
  };
  (globalThis as { indexedDB: IDBFactory }).indexedDB = fake as IDBFactory;

  return () => {
    (globalThis as { indexedDB: IDBFactory }).indexedDB = original;
  };
}

function readRecordKey(value: unknown, keyPath: string): string {
  if (typeof value !== "object" || value === null || !(keyPath in value)) {
    throw new DOMException(`Missing keyPath: ${keyPath}`, "DataError");
  }
  const key = (value as Record<string, unknown>)[keyPath];
  if (typeof key !== "string") {
    throw new DOMException(`Invalid keyPath: ${keyPath}`, "DataError");
  }
  return key;
}

function asIdbRequest<T>(request: FakeIdbRequest<T>): IDBRequest<T> {
  return request as unknown as IDBRequest<T>;
}

function asIdbObjectStore(store: FakeObjectStore): IDBObjectStore {
  return store as unknown as IDBObjectStore;
}
