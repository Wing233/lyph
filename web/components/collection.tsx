"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { track } from "./tracking";
export type Saved = {
  kind: "project" | "entry" | "interest";
  id: string;
  title: string;
  url: string;
  addedAt?: number;
};
const KEY = "lyph:prototype2:collection:v1";
let volatile: Saved[] = [];
function safeUrl(raw: string) {
  try {
    const url = new URL(raw, location.origin);
    if (url.origin !== location.origin) return "#";
    return (url.pathname + url.search + url.hash)
      .replace(/^.*\/prototype2\//, "/")
      .replace(/\.html(?=\?|#|$)/, "");
  } catch {
    return "#";
  }
}
function read() {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) || "{}");
    return (Array.isArray(v.items) ? v.items : [])
      .filter(
        (i: Saved) =>
          ["project", "entry", "interest"].includes(i.kind) &&
          typeof i.id === "string" &&
          typeof i.title === "string",
      )
      .slice(0, 100) as Saved[];
  } catch {
    return volatile;
  }
}
function write(v: Saved[]) {
  volatile = v;
  try {
    localStorage.setItem(
      KEY,
      JSON.stringify({ version: 1, items: v.slice(0, 100) }),
    );
  } catch {}
  window.dispatchEvent(new Event("lyph:collectionchange"));
}
function useSaved() {
  const [v, set] = useState<Saved[]>([]);
  useEffect(() => {
    const refresh = () => set(read());
    refresh();
    window.addEventListener("storage", refresh);
    window.addEventListener("lyph:collectionchange", refresh);
    return () => {
      window.removeEventListener("storage", refresh);
      window.removeEventListener("lyph:collectionchange", refresh);
    };
  }, []);
  return v;
}
export function SaveButton({ item }: { item: Saved }) {
  const v = useSaved();
  const saved = v.some((i) => i.kind === item.kind && i.id === item.id);
  const [notice, setNotice] = useState("");
  return (
    <>
      <button
        className="text-link"
        aria-pressed={saved}
        onClick={() => {
          if (!saved && v.length >= 100) {
            setNotice("最多收藏 100 条，请先整理已有收藏。");
            return;
          }
          write(
            saved
              ? v.filter((i) => i.kind !== item.kind || i.id !== item.id)
              : [{ ...item, addedAt: Date.now() }, ...v],
          );
          track(
            "collection.toggle",
            item.kind === "entry" ? item.id : undefined,
          );
        }}
      >
        {saved ? "取消收藏" : "收藏这篇 ↗"}
      </button>
      {notice && <span role="status">{notice}</span>}
    </>
  );
}
export function Collection() {
  const v = useSaved();
  const [filter, setFilter] = useState("all");
  const [cleared, setCleared] = useState<Saved[]>([]);
  const labels = { project: "项目", entry: "文字", interest: "兴趣记录" };
  return (
    <>
      <p className="page-description">只保存在当前浏览器，最多 100 条。</p>
      <div className="toolbar">
        <div className="segmented">
          {["all", "project", "entry", "interest"].map((t) => (
            <button
              key={t}
              aria-pressed={filter === t}
              onClick={() => setFilter(t)}
            >
              {t === "all" ? "全部" : labels[t as keyof typeof labels]}
            </button>
          ))}
        </div>
        <button
          className="button secondary"
          disabled={!v.length}
          onClick={() => {
            setCleared(v);
            write([]);
          }}
        >
          清空收藏
        </button>
      </div>
      {cleared.length > 0 && (
        <p className="inline-notice">
          收藏已清空。
          <button
            className="text-link"
            onClick={() => {
              write(cleared);
              setCleared([]);
            }}
          >
            撤销
          </button>
        </p>
      )}
      <div className="content-list">
        {v
          .filter((i) => filter === "all" || i.kind === filter)
          .map((i) => (
            <article key={`${i.kind}:${i.id}`}>
              <p className="eyebrow">{labels[i.kind]}</p>
              <h2>
                <Link href={safeUrl(i.url)}>{i.title} ↗</Link>
              </h2>
              <SaveButton item={i} />
            </article>
          ))}
      </div>
      {!v.length && (
        <div className="empty-state">
          <h2>还没有收藏。</h2>
          <p>阅读时，留下想再看一次的文字。</p>
        </div>
      )}
    </>
  );
}
