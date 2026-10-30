import { openDB, type DBSchema } from "idb";
export type Capture = {
  capture_id: string;
  device_id: string;
  captured_at: string;
  source_kind: "note" | "photo";
  text: string;
  file?: Blob;
  filename?: string;
  status: "pending" | "synced";
  error?: string;
};
export type Pairing = { token: string; device_id: string };
interface OutboxDB extends DBSchema {
  captures: { key: string; value: Capture };
  settings: { key: string; value: unknown };
}
const db = () =>
  openDB<OutboxDB>("lyph-collector", 1, {
    upgrade(database) {
      database.createObjectStore("captures", { keyPath: "capture_id" });
      database.createObjectStore("settings");
    },
  });
export async function pairing() {
  return (await (await db()).get("settings", "pairing")) as Pairing | undefined;
}
export async function savePairing(value: Pairing | undefined) {
  const database = await db();
  if (value) await database.put("settings", value, "pairing");
  else await database.delete("settings", "pairing");
}
export async function captures() {
  return (await (await db()).getAll("captures")).sort((a, b) =>
    b.captured_at.localeCompare(a.captured_at),
  );
}
export async function enqueue(
  input: Omit<Capture, "capture_id" | "captured_at" | "status">,
) {
  const database = await db();
  const tx = database.transaction("captures", "readwrite");
  const rows = await tx.store.getAll();
  const bytes =
    rows.reduce(
      (n, c) => n + (c.file?.size || 0) + new Blob([c.text]).size,
      0,
    ) +
    (input.file?.size || 0) +
    new Blob([input.text]).size;
  if (bytes > 1024 * 1024 * 1024) {
    tx.abort();
    throw new Error("本地缓存已满，请同步或整理队列后继续采集。");
  }
  const value: Capture = {
    ...input,
    capture_id: crypto.randomUUID(),
    captured_at: new Date().toISOString(),
    status: "pending",
  };
  await tx.store.put(value);
  await tx.done;
  return value;
}
export async function removeCapture(id: string) {
  await (await db()).delete("captures", id);
}
async function deviceFetch(
  token: string,
  path: string,
  options: RequestInit = {},
) {
  const headers = new Headers(options.headers);
  headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`/api/v1/device${path}`, {
    ...options,
    headers,
    credentials: "omit",
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      typeof body.detail === "string"
        ? body.detail
        : `同步失败 (${response.status})`,
    );
  }
  return response.json();
}
export async function validateToken(token: string) {
  const config = await deviceFetch(token, "/config");
  return { token, device_id: config.device_id } as Pairing;
}
let syncing: Promise<number> | undefined;
export function syncOutbox(onProgress: () => void): Promise<number> {
  if (syncing) return syncing;
  syncing = (async () => {
    const database = await db();
    const claim = database.transaction("settings", "readwrite");
    const lease = (await claim.store.get("sync-lease")) as
      | { until: number }
      | undefined;
    if (lease && lease.until > Date.now()) {
      await claim.done;
      return 0;
    }
    const leaseId = crypto.randomUUID();
    await claim.store.put(
      { id: leaseId, until: Date.now() + 120000 },
      "sync-lease",
    );
    await claim.done;
    try {
      const paired = await pairing();
      if (!paired) throw new Error("请先在设置里填写设备凭证。");
      const config = await deviceFetch(paired.token, "/config");
      if (config.paused) throw new Error("此设备已暂停，请在后台恢复。");
      const rows = await captures();
      let count = 0;
      for (const record of rows.filter((c) => c.status === "pending")) {
        if (record.device_id !== paired.device_id) continue;
        const heartbeat = database.transaction("settings", "readwrite");
        await heartbeat.store.put(
          { id: leaseId, until: Date.now() + 120000 },
          "sync-lease",
        );
        await heartbeat.done;
        try {
          const form = new FormData();
          for (const key of [
            "capture_id",
            "captured_at",
            "source_kind",
            "text",
          ] as const)
            form.set(key, record[key]);
          if (record.file)
            form.set("file", record.file, record.filename || "photo.jpg");
          await deviceFetch(paired.token, "/captures", {
            method: "POST",
            body: form,
            signal: AbortSignal.timeout(90000),
          });
          await database.put("captures", {
            ...record,
            file: undefined,
            status: "synced",
            error: undefined,
          });
          count++;
          onProgress();
        } catch (e) {
          await database.put("captures", {
            ...record,
            error: e instanceof Error ? e.message : "同步未完成",
          });
          onProgress();
          throw e;
        }
      }
      const remaining = (await captures()).filter(
        (c) => c.status === "pending" && c.device_id === paired.device_id,
      ).length;
      await deviceFetch(paired.token, "/heartbeat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pending_count: remaining }),
      }).catch(() => {});
      return count;
    } finally {
      const release = database.transaction("settings", "readwrite");
      const current = (await release.store.get("sync-lease")) as
        | { id: string }
        | undefined;
      if (current?.id === leaseId) await release.store.delete("sync-lease");
      await release.done;
    }
  })().finally(() => {
    syncing = undefined;
  });
  return syncing;
}
