"use client";
import { useEffect, useRef, useState } from "react";
import {
  api,
  errorText,
  items,
  json,
  type Content,
  typeNames,
} from "@/lib/api";
import { Dialog } from "@/components/ui/dialog";
import { Markdown } from "@/components/markdown";
import { useResource, ResourceStatus, PageIntro, Empty } from "./common";
const fresh = (): Content => ({
  id: "",
  type: "blog",
  title: "",
  summary: "",
  markdown: "",
  date: new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Shanghai" }),
  visibility: "private",
  rag_enabled: false,
  category: "",
  tags: [],
  revision: 0,
});
export function ContentModule() {
  const list = useResource<{ items: Content[] }>("/admin/contents");
  const [editing, setEditing] = useState<Content>();
  const [preview, setPreview] = useState(false);
  const [state, setState] = useState("");
  const [versions, setVersions] = useState<
    { id: string; title: string; created_at: string }[]
  >([]);
  const [filter, setFilter] = useState("all");
  const [busy, setBusy] = useState(false);
  const pending = useRef<Content | undefined>(undefined);
  const saving = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const [dirty, setDirty] = useState(false);
  async function save(): Promise<Content | undefined> {
    const draft = pending.current;
    if (!draft || saving.current) return;
    clearTimeout(timer.current);
    saving.current = true;
    setState("保存中…");
    try {
      const result = await api<Content>(
        `/admin/contents${draft.id ? `/${draft.id}` : ""}`,
        {
          method: draft.id ? "PUT" : "POST",
          body: json({
            ...Object.fromEntries(
              [
                "type",
                "title",
                "summary",
                "markdown",
                "date",
                "visibility",
                "rag_enabled",
                "category",
                "tags",
                "media_ids",
              ]
                .filter((k) => draft[k] !== undefined)
                .map((k) => [k, draft[k]]),
            ),
            ...(draft.id ? { revision: draft.revision } : {}),
          }),
        },
      );
      const current = pending.current;
      if (current === draft) {
        pending.current = result;
        setEditing(result);
        setDirty(false);
        setState("已保存");
      } else if (current) {
        pending.current = {
          ...current,
          id: result.id,
          revision: result.revision,
        };
        setEditing(pending.current);
        setState("有新的修改，继续保存…");
        timer.current = setTimeout(() => void save(), 800);
      }
      void list.reload();
      return result;
    } catch (e) {
      setState(`保存失败，编辑内容仍在本页：${errorText(e)}`);
    } finally {
      saving.current = false;
    }
  }
  function update(patch: Partial<Content>) {
    const next = { ...pending.current!, ...patch };
    pending.current = next;
    setEditing(next);
    setDirty(true);
    setState("尚未保存");
    clearTimeout(timer.current);
    timer.current = setTimeout(() => void save(), 1000);
  }
  useEffect(() => {
    const prevent = (e: BeforeUnloadEvent) => {
      if (pending.current && dirty) {
        e.preventDefault();
      }
    };
    window.addEventListener("beforeunload", prevent);
    return () => {
      window.removeEventListener("beforeunload", prevent);
    };
  }, [dirty]);
  useEffect(() => () => clearTimeout(timer.current), []);
  async function start(c: Content) {
    if (c.id) {
      try {
        c = await api<Content>(`/admin/contents/${c.id}`);
      } catch (e) {
        setState(errorText(e));
        list.setError(errorText(e));
        return;
      }
    }
    pending.current = { ...c };
    setEditing(pending.current);
    setDirty(false);
    setState(c.id ? "已载入草稿" : "新草稿 · 默认私密");
    setVersions([]);
    setPreview(false);
  }
  async function action(name: "publish" | "withdraw") {
    setBusy(true);
    try {
      let draft = pending.current;
      if (dirty || !draft?.id) draft = await save();
      if (!draft?.id) throw new Error("请先成功保存草稿。");
      const result = await api<Content>(`/admin/contents/${draft.id}/${name}`, {
        method: "POST",
        body: json({ revision: draft.revision }),
      });
      pending.current = result;
      setEditing(result);
      setState(name === "publish" ? "发布完成" : "已撤稿");
      void list.reload();
    } catch (e) {
      setState(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  const entries = items<Content>(list.data).filter(
    (c) => filter === "all" || c.type === filter,
  );
  return (
    <>
      <PageIntro
        eyebrow="CONTENT STUDIO"
        title="把想法，写成文字。"
        description="草稿与公开版本分别保存。重新发布，官网才会更新。"
        action={
          <button className="button" onClick={() => start(fresh())}>
            新建内容 +
          </button>
        }
      />
      <div className="segmented">
        {["all", ...Object.keys(typeNames)].map((t) => (
          <button
            key={t}
            aria-pressed={filter === t}
            onClick={() => setFilter(t)}
          >
            {t === "all" ? "全部" : typeNames[t as keyof typeof typeNames]}
          </button>
        ))}
      </div>
      <ResourceStatus {...list} retry={list.reload} />
      {entries.length ? (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>内容</th>
                <th>类型</th>
                <th>可见性</th>
                <th>发布</th>
                <th>修改</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((c) => (
                <tr key={c.id}>
                  <td>
                    <button className="table-link" onClick={() => start(c)}>
                      <span className="item-title">{c.title || "未命名"}</span>
                      <span className="item-subtitle">{c.summary}</span>
                    </button>
                  </td>
                  <td>{typeNames[c.type]}</td>
                  <td>{c.visibility === "public" ? "公开" : "私密"}</td>
                  <td>
                    {c.published || c.published_version_id ? "已发布" : "草稿"}
                  </td>
                  <td>
                    <button className="text-link" onClick={() => start(c)}>
                      编辑 ↗
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        !list.loading &&
        !list.error && (
          <Empty
            title="第一篇，从这里开始。"
            description="博客、文档、手写日记与自动日报，共用同一个写作台。"
          />
        )
      )}
      <Dialog
        open={!!editing}
        onOpenChange={(open) => {
          if (!open) {
            if (dirty) {
              setState("请先保存编辑内容，再关闭写作台。");
              return;
            }
            clearTimeout(timer.current);
            pending.current = undefined;
            setEditing(undefined);
          }
        }}
        title="内容写作"
      >
        {editing && (
          <>
            <div className="editor-toolbar">
              <span className="save-state" role="status">
                {state}
              </span>
              <div className="row-actions">
                <label className="button secondary small">
                  添加附件
                  <input
                    className="sr-only"
                    type="file"
                    onChange={async (e) => {
                      const file = e.target.files?.[0];
                      if (!file) return;
                      try {
                        if (file.size > 20 * 1024 * 1024)
                          throw new Error("附件最多20MiB。");
                        const form = new FormData();
                        form.set("file", file);
                        const media = await api<{ id: string }>(
                          "/admin/files",
                          { method: "POST", body: form },
                        );
                        const name = file.name.replace(/[[\]\\\n\r]/g, "");
                        const image = /^image\/(png|jpeg|webp|gif|avif)$/.test(
                          file.type,
                        );
                        update({
                          markdown: `${pending.current?.markdown || ""}\n\n${image ? "!" : ""}[${name}](/api/v1/public/media/${media.id})`,
                          media_ids: [
                            ...((pending.current?.media_ids || []) as string[]),
                            media.id,
                          ],
                        });
                      } catch (error) {
                        setState(errorText(error));
                      } finally {
                        e.target.value = "";
                      }
                    }}
                  />
                </label>
                <button
                  className="button secondary small"
                  onClick={() => setPreview(!preview)}
                >
                  {preview ? "继续编辑" : "预览"}
                </button>
                <button
                  className="button secondary small"
                  disabled={busy || saving.current}
                  onClick={() => void save()}
                >
                  保存
                </button>
                <button
                  className="button small"
                  disabled={busy}
                  onClick={() => void action("publish")}
                >
                  发布
                </button>
                <button
                  className="button danger small"
                  disabled={busy || !editing.id}
                  onClick={() => void action("withdraw")}
                >
                  撤稿
                </button>
              </div>
            </div>
            <div className="editor-grid">
              <div className="editor-main">
                <label className="sr-only" htmlFor="content-title">
                  标题
                </label>
                <input
                  id="content-title"
                  className="title-input"
                  value={editing.title}
                  onChange={(e) => update({ title: e.target.value })}
                  placeholder="写一个标题"
                />
                <label className="field">
                  摘要
                  <textarea
                    rows={2}
                    value={editing.summary}
                    onChange={(e) => update({ summary: e.target.value })}
                  />
                </label>
                {preview ? (
                  <Markdown>{editing.markdown}</Markdown>
                ) : (
                  <label className="field">
                    Markdown 正文
                    <textarea
                      value={editing.markdown}
                      onChange={(e) => update({ markdown: e.target.value })}
                      placeholder="从一个想法开始…"
                    />
                  </label>
                )}
              </div>
              <aside className="editor-aside">
                <label className="field">
                  内容类型
                  <select
                    value={editing.type}
                    onChange={(e) =>
                      update({ type: e.target.value as Content["type"] })
                    }
                  >
                    {Object.entries(typeNames).map(([id, title]) => (
                      <option key={id} value={id}>
                        {title}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  日期
                  <input
                    type="date"
                    value={editing.date}
                    onChange={(e) => update({ date: e.target.value })}
                  />
                </label>
                <label className="field">
                  可见性
                  <select
                    value={editing.visibility}
                    onChange={(e) =>
                      update({
                        visibility: e.target.value as Content["visibility"],
                      })
                    }
                  >
                    <option value="private">私密</option>
                    <option value="public">公开</option>
                  </select>
                </label>
                <label className="checkbox-field">
                  <input
                    type="checkbox"
                    checked={editing.rag_enabled}
                    onChange={(e) => update({ rag_enabled: e.target.checked })}
                  />
                  <span>
                    授权访客 RAG<small>仍须公开发布后才生效。</small>
                  </span>
                </label>
                <label className="field">
                  分类
                  <input
                    value={editing.category}
                    onChange={(e) => update({ category: e.target.value })}
                  />
                </label>
                <label className="field">
                  标签，用逗号分隔
                  <input
                    value={editing.tags.join(",")}
                    onChange={(e) =>
                      update({
                        tags: e.target.value
                          .split(",")
                          .map((x) => x.trim())
                          .filter(Boolean),
                      })
                    }
                  />
                </label>
                <button
                  className="text-link"
                  disabled={!editing.id}
                  onClick={async () => {
                    try {
                      setVersions(
                        items(
                          await api(`/admin/contents/${editing.id}/versions`),
                        ),
                      );
                    } catch (e) {
                      setState(errorText(e));
                    }
                  }}
                >
                  查看发布版本 ↗
                </button>
                {versions.map((v) => (
                  <div className="version-row" key={v.id}>
                    <span>{v.title || v.id}</span>
                    <button
                      className="text-link"
                      onClick={async () => {
                        if (dirty) {
                          setState("请先保存当前编辑，再恢复历史。");
                          return;
                        }
                        try {
                          const c = await api<Content>(
                            `/admin/contents/${editing.id}/restore`,
                            {
                              method: "POST",
                              body: json({
                                revision: editing.revision,
                                version_id: v.id,
                              }),
                            },
                          );
                          start(c);
                          setState("旧版本已恢复为草稿，发布后生效。");
                          void list.reload();
                        } catch (e) {
                          setState(errorText(e));
                        }
                      }}
                    >
                      恢复
                    </button>
                  </div>
                ))}
              </aside>
            </div>
          </>
        )}
      </Dialog>
    </>
  );
}
