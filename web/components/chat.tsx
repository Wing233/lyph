"use client";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import {
  AssistantRuntimeProvider,
  ThreadPrimitive,
  useExternalStoreRuntime,
} from "@assistant-ui/react";
import {
  ArrowUp,
  Check,
  Copy,
  Menu,
  MessageSquare,
  Plus,
  Sparkles,
  Square,
  X,
} from "lucide-react";
import { api, errorText, json, type NavigationItem } from "@/lib/api";
import { Markdown } from "./markdown";
import { AnswerBlocks, restoreAnswerViews, safeAnswerUrl, type AnswerBlock } from "./chat-answer";
import { ensureVisitor } from "@/lib/visitor";
import { track } from "./tracking";
import "./chat-shell.css";

type Access = {
  free_remaining?: number | null;
  unlimited?: boolean;
  authorized?: boolean;
  mode?: string;
  remaining?: number;
  expires_at?: string;
  model_mode?: "public-free-test" | "standard";
};
type Message = {
  id: string;
  role: "user" | "assistant";
  text: string;
  request_id?: string;
  blocks?: AnswerBlock[];
  suggestions?: string[];
  cache?: { type: string; generated_at?: string } | null;
  citations?: {
    title?: string;
    filename?: string;
    source_id?: string;
    id?: string;
  }[];
};
type StoredRequest = { id: string; conversation: string; time: number };
type RequestState = Pick<
  Message,
  "blocks" | "suggestions" | "cache" | "citations"
> & {
  status: string;
  answer: string;
  message?: string;
  conversation_id?: string;
  error?: string;
};
const HISTORY_KEY = "lyph:chat:request-index:v1";
const starters = [
  "有哪些项目并介绍技术栈",
  "帮我找广东做人工智能的企业，比较招聘情况",
  "这些企业按省份如何分布？",
  "有哪些文章",
];
const toolLabels: Record<string, string> = {
  get_public_profile: "读取公开介绍与项目",
  search_public_content: "查找公开内容",
  get_public_content: "阅读授权内容",
  search_companies: "查询企业快照",
  aggregate_companies: "统计企业数据",
  suggest_followups: "整理后续问题",
};

function trapDialogFocus(event: KeyboardEvent<HTMLElement>) {
  if (event.key !== "Tab") return;
  const elements = event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled),a[href],input:not(:disabled),textarea:not(:disabled),select:not(:disabled),summary,[tabindex="0"]');
  if (!elements.length) return;
  const first = elements[0], last = elements[elements.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
}
export function Chat() {
  const [access, setAccess] = useState<Access>({});
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [recovering, setRecovering] = useState(false);
  const [authorize, setAuthorize] = useState(false);
  const [contact, setContact] = useState(false);
  const [menu, setMenu] = useState(false);
  const [toolStatus, setToolStatus] = useState("");
  const [navigation, setNavigation] = useState<NavigationItem[]>([]);
  const [history, setHistory] = useState<StoredRequest[]>([]);
  const [reading, setReading] = useState<AnswerBlock[] | null>(null);
  const [copied, setCopied] = useState("");
  const request = useRef("");
  const conversation = useRef("");
  const controller = useRef<AbortController | undefined>(undefined);
  const busyRef = useRef(false);
  const activeMessage = useRef("");
  const pendingQuestion = useRef<{ text: string; refresh: boolean } | null>(
    null,
  );
  const logicalRequest = useRef<{
    id: string;
    text: string;
    refresh: boolean;
  } | null>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const readingClose = useRef<HTMLButtonElement>(null);
  const readingTrigger = useRef<HTMLElement | null>(null);
  const authTrigger = useRef<HTMLElement | null>(null);

  const refreshAccess = async () => {
    try {
      setAccess(await api<Access>("/chat/access"));
    } catch (e) {
      setError(errorText(e));
    }
  };
  useEffect(() => {
    conversation.current = crypto.randomUUID();
    void ensureVisitor()
      .then(refreshAccess)
      .catch((e) => setError(errorText(e)));
    void api<{ items: NavigationItem[] }>("/public/navigation")
      .then((v) =>
        setNavigation(
          v.items.filter(
            (item) => safeAnswerUrl(item.href) || (item.id === "about" && item.href === "#intro") || (item.id === "chat" && item.href === "#chat"),
          ),
        ),
      )
      .catch(() => undefined);
    try {
      const value = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
      if (Array.isArray(value))
        setHistory(
          value
            .filter(
              (x): x is StoredRequest =>
                typeof x?.id === "string" &&
                typeof x?.conversation === "string" &&
                typeof x?.time === "number" &&
                Date.now() - x.time < 30 * 86400000,
            )
            .slice(0, 20),
        );
    } catch {
      /* Storage is optional; do not persist message bodies. */
    }
    const params = new URLSearchParams(location.search);
    if (params.has("entity")) {
      const kind = params.get("entity");
      if (kind === "project" || kind === "content" || kind === "company") {
        const query = new URLSearchParams({ kind });
        if (params.get("id")) query.set("id", params.get("id")!);
        if (params.get("date")) query.set("date", params.get("date")!);
        void ensureVisitor()
          .then(() => api<{ blocks: AnswerBlock[] }>(`/chat/entities?${query}`))
          .then((value) => setReading(value.blocks))
          .catch((e) => setError(errorText(e)));
      }
    }
    return () => controller.current?.abort();
  }, []);
  useEffect(() => {
    if (reading) readingClose.current?.focus();
  }, [reading]);
  useEffect(() => {
    if (!authorize) return;
    authTrigger.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    document.getElementById("chat-password")?.focus();
    return () => authTrigger.current?.focus();
  }, [authorize]);
  function closeReading() {
    setReading(null);
    readingTrigger.current?.focus();
  }
  async function stop() {
    if (request.current)
      await api(`/chat/requests/${request.current}/cancel`, {
        method: "POST",
      }).catch((e) => setError(errorText(e)));
    controller.current?.abort();
  }
  async function newChat() {
    if (busyRef.current) return;
    conversation.current = crypto.randomUUID();
    request.current = "";
    logicalRequest.current = null;
    setMessages([]);
    setDraft("");
    setError("");
    setMenu(false);
    setReading(null);
    setToolStatus("");
    window.history.replaceState({}, "", location.pathname);
    textarea.current?.focus();
  }
  function saveHistory(id: string) {
    const item = { id, conversation: conversation.current, time: Date.now() };
    setHistory((previous) => {
      const next = [item, ...previous.filter((row) => row.id !== id)].slice(
        0,
        20,
      );
      try {
        localStorage.setItem(HISTORY_KEY, JSON.stringify(next));
      } catch {
        /* Optional storage. */
      }
      return next;
    });
  }
  async function restore(row: StoredRequest) {
    if (busyRef.current) return;
    busyRef.current = true; setRecovering(true); setToolStatus("正在恢复保存的回答与最新视图…");
    try {
      const state = await api<RequestState>(
        `/chat/requests/${encodeURIComponent(row.id)}`,
      );
      if (state.status === "stale") {
        conversation.current = crypto.randomUUID();
        setMessages([{ id: `${row.id}-assistant`, role: "assistant", text: state.error || "资料权限或版本变化，请重新查询。", blocks: [], citations: [], suggestions: [] }]);
        setDraft(state.message || ""); setError(state.error || "资料权限或版本变化，请重新查询。"); setMenu(false); logicalRequest.current = null;
        return;
      }
      if (!state.message || !state.answer)
        throw new Error("这段对话已清理或尚未完成。");
      const restored = await restoreAnswerViews(state.blocks || []);
      conversation.current = row.conversation;
      setMessages([
        { id: `${row.id}-user`, role: "user", text: state.message },
        {
          id: `${row.id}-assistant`,
          role: "assistant",
          text: state.answer,
          request_id: row.id,
          ...state,
          blocks: restored.blocks,
        },
      ]);
      setMenu(false);
      setError(restored.notice);
      logicalRequest.current = null;
    } catch (e) {
      setError(errorText(e));
    } finally {
      busyRef.current = false; setRecovering(false); setToolStatus("");
    }
  }
  async function send(
    text = draft,
    authorizationConfirmed = false,
    forceRefresh = false,
  ) {
    text = text.trim();
    if (!text || busyRef.current) return;
    const retry = !forceRefresh && logicalRequest.current?.text === text;
    const refreshMode = retry ? logicalRequest.current!.refresh : forceRefresh;
    if (
      !retry &&
      !authorizationConfirmed &&
      !access.unlimited &&
      !access.free_remaining &&
      !access.authorized
    ) {
      setDraft(text);
      pendingQuestion.current = { text, refresh: forceRefresh };
      setAuthorize(true);
      return;
    }
    busyRef.current = true;
    setBusy(true);
    setDraft((current) => current.trim() === text ? "" : current);
    setToolStatus(access.model_mode === "public-free-test" ? "正在理解问题，免费模型可能需要排队…" : "正在理解你的问题…");
    setError("");
    setMenu(false);
    track("chat.submit");
    const id = retry ? logicalRequest.current!.id : crypto.randomUUID();
    const messageId = `${id}-assistant`;
    request.current = id;
    activeMessage.current = messageId;
    logicalRequest.current = { id, text, refresh: refreshMode };
    if (!retry)
      setMessages((values) => [
        ...values,
        { id: `${id}-user`, role: "user", text },
        { id: messageId, role: "assistant", text: "", request_id: id },
      ]);
    const update = (patch: Partial<Message>, append = false) =>
      setMessages((values) =>
        values.map((m) =>
          m.id === messageId
            ? {
                ...m,
                ...patch,
                text: append
                  ? m.text + (patch.text || "")
                  : (patch.text ?? m.text),
              }
            : m,
        ),
      );
    if (retry)
      update({
        text: "",
        blocks: [],
        suggestions: [],
        citations: [],
        cache: null,
      });
    controller.current = new AbortController();
    let completed = false;
    let stale = false;
    const applyStale = (state: RequestState) => {
      stale = true;
      conversation.current = crypto.randomUUID();
      setDraft((current) => current || text);
      update({ text: state.error || "资料权限或版本变化，请重新查询。", blocks: [], citations: [], suggestions: [], cache: null });
      setError(state.error || "资料权限或版本变化，请重新查询。");
    };
    const applyState = (state: RequestState) => {
      if (state.status === "stale") { applyStale(state); return; }
      update({
        text: state.answer,
        blocks: state.blocks || [],
        suggestions: state.suggestions || [],
        citations: state.citations || [],
        cache: state.cache || null,
      });
      completed = true;
      saveHistory(id);
    };
    try {
      const response = await fetch("/api/v1/chat/requests", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: json({
          request_id: id,
          conversation_id: conversation.current,
          message: text,
          retry,
          cache_mode: refreshMode ? "refresh" : "auto",
        }),
        signal: controller.current.signal,
      });
      if (!response.ok) {
        const data = await response.json();
        if (response.status === 403) {
          pendingQuestion.current = { text, refresh: forceRefresh };
          setAuthorize(true);
        }
        throw new Error(
          typeof data.detail === "string"
            ? data.detail
            : "对话暂时不可用，请稍后重试。",
        );
      }
      if (!response.body) throw new Error("未收到回复流。");
      const reader = response.body.getReader(),
        decoder = new TextDecoder();
      let pending = "";
      while (true) {
        const chunk = await reader.read();
        pending += decoder
          .decode(chunk.value, { stream: !chunk.done })
          .replace(/\r\n/g, "\n");
        let end;
        while ((end = pending.indexOf("\n\n")) >= 0) {
          const block = pending.slice(0, end);
          pending = pending.slice(end + 2);
          const event = block
            .split("\n")
            .find((line) => line.startsWith("event:"))
            ?.slice(6)
            .trim();
          const raw = block
            .split("\n")
            .filter((line) => line.startsWith("data:"))
            .map((line) => line.slice(5).trim())
            .join("\n");
          if (!raw) continue;
          const data = JSON.parse(raw);
          if (event === "delta") {
            setToolStatus("正在生成回答…");
            update({ text: data.text || data.delta || "" }, true);
          }
          else if (event === "citations")
            update({ citations: data.items || data.citations || [] });
          else if (event === "blocks") update({ blocks: data.items || [] });
          else if (event === "suggestions")
            update({ suggestions: data.items || [] });
          else if (event === "tool")
            setToolStatus(
              data.status === "completed"
                ? `${toolLabels[data.name] || "只读查询"}已完成，正在整理回答…`
                : `${toolLabels[data.name] || "处理只读查询"}…`,
            );
          else if (event === "done") applyState(data);
          else if (event === "error")
            throw new Error(
              data.message || data.error || "回答没有完成，请重试。",
            );
        }
        if (chunk.done) break;
      }
      if (!completed && !stale) {
        const state = await api<RequestState>(`/chat/requests/${id}`);
        if (state.status === "completed") applyState(state);
        else if (state.status === "stale") applyStale(state);
        else throw new Error("连接中断，可以重试刚才的问题。");
      }
    } catch (e) {
      const state = await api<RequestState>(`/chat/requests/${id}`).catch(
        () => undefined,
      );
      if (state?.status === "completed") applyState(state);
      else if (state?.status === "stale") applyStale(state);
      else {
        setDraft((current) => current || text);
        setError(
          state?.status === "running"
            ? "请求仍在执行，稍后重新发送同一问题可恢复结果。"
            : state?.status === "cancelled"
              ? "已停止，可以重试刚才的问题。"
              : errorText(e),
        );
      }
    } finally {
      if (completed || stale) logicalRequest.current = null;
      busyRef.current = false;
      setBusy(false);
      setToolStatus("");
      await refreshAccess();
    }
  }
  const runtime = useExternalStoreRuntime<Message>({
    messages,
    isRunning: busy,
    isSendDisabled: recovering,
    convertMessage: (message) => ({
      id: message.id,
      role: message.role,
      content: [{ type: "text", text: message.text }],
    }),
    onNew: async (message) => {
      await send(
        message.content
          .filter((part) => part.type === "text")
          .map((part) => part.text)
          .join("\n"),
      );
    },
    onCancel: stop,
  });
  const label = access.unlimited
    ? "不限次数"
    : access.free_remaining
    ? "首次成功回答免费"
    : access.authorized
      ? access.mode === "count"
        ? `剩余 ${access.remaining} 次`
        : `有效至 ${access.expires_at ? new Date(access.expires_at).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" }) : "—"}`
      : "续聊需临时密码";
  const suggestions = messages.at(-1)?.suggestions || [];
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ThreadPrimitive.Root className="chat-shell" id="main-content">
        {menu && (
          <button
            className="chat-sidebar-backdrop"
            aria-label="关闭侧栏"
            onClick={() => setMenu(false)}
          />
        )}
        <aside
          className={`chat-sidebar ${menu ? "is-open" : ""}`}
          aria-label="对话导航"
        >
          <a className="chat-wordmark" href="/">
            lyph<span>.</span>
          </a>
          <button
            className="chat-new"
            disabled={busy || recovering}
            onClick={() => void newChat()}
          >
            <Plus size={17} /> 新对话
          </button>
          <p className="chat-sidebar-label">最近回答</p>
          <div className="chat-history">
            {history.length ? (
              history.map((row) => (
                <button
                  key={row.id}
                  disabled={busy || recovering}
                  onClick={() => void restore(row)}
                >
                  <MessageSquare size={15} />
                  <span>
                    {new Date(row.time).toLocaleString("zh-CN", {
                      month: "numeric",
                      day: "numeric",
                      hour: "2-digit",
                      minute: "2-digit",
                    })}{" "}
                    的对话
                  </span>
                </button>
              ))
            ) : (
              <p>从一个问题开始。</p>
            )}
          </div>
          <div className="chat-external">
            <p className="chat-sidebar-label">应用入口</p>
            {navigation.map((item) => (
              <a
                key={item.id}
                href={item.id === "about" && item.href === "#intro" ? "/about" : item.id === "chat" && item.href === "#chat" ? "/" : safeAnswerUrl(item.href)}
                target={/^https?:\/\//i.test(item.href) ? "_blank" : undefined}
                rel={/^https?:\/\//i.test(item.href) ? "noreferrer" : undefined}
              >
                {item.label} ↗
              </a>
            ))}
            <a href="/admin/login">私人编辑台 ↗</a>
          </div>
          <div className="chat-sidebar-footer">
            <span className="chat-profile">L</span>
            <div>
              LYPH<small>技术 · 生活 · 一点好奇</small>
            </div>
          </div>
        </aside>
        <div className="chat-workspace">
          <header className="chat-topbar">
            <div>
              <button
                className="chat-menu"
                aria-label="展开侧栏"
                aria-expanded={menu}
                onClick={() => setMenu(!menu)}
              >
                <Menu size={20} />
              </button>
              <strong>LYPH</strong>
              <span>随问随看</span>
            </div>
            {access.unlimited ? <span className="chat-access">{label}</span> :
              <button className="chat-access" onClick={() => setAuthorize(true)}>{label}</button>}
          </header>
          {access.model_mode === "public-free-test" && (
            <p className="chat-free-notice" role="note">
              免费模型试运行：智能分析仅限公开企业数据，请只输入可公开的问题；繁忙时可能排队。
            </p>
          )}
          <ThreadPrimitive.Viewport
            className="chat-viewport"
            autoScroll
            role="log"
            aria-label="对话记录"
            tabIndex={0}
          >
            {!messages.length && (
              <section className="chat-start">
                <div className="chat-start-symbol" aria-hidden="true">
                  <Sparkles size={40} strokeWidth={1.2} />
                </div>
                <p>很高兴在这里遇见你</p>
                <h1>
                  想了解什么，<span>直接问。</span>
                </h1>
                <p>
                  项目、生活，或者你关心的数据。
                  <br />
                  从一个问题开始，用合适的方式看见答案。
                </p>
                <div className="chat-starters">
                  {starters.map((question, i) => (
                    <button
                      key={question}
                      onClick={() => {
                        setDraft(question);
                        void send(question);
                      }}
                    >
                      <small>
                        {["个人作品", "企业发现", "数据可视化", "公开内容"][i]}
                      </small>
                      <strong>{question}</strong>
                      <span>↗</span>
                    </button>
                  ))}
                </div>
              </section>
            )}
            <div className="chat-turns">
              {messages.map((message, index) => (
                <article
                  key={message.id}
                  className={`chat-turn chat-turn-${message.role}`}
                >
                  <span className="chat-avatar">
                    {message.role === "user" ? "你" : "ly"}
                  </span>
                  <div className="chat-turn-body">
                    {message.role === "assistant" && (
                      <small className="chat-author">LYPH</small>
                    )}
                    {message.text ? <Markdown>{message.text}</Markdown> :
                      !(busy && message.id === activeMessage.current) && <Markdown>这次回答没有完成。</Markdown>}
                    {message.role === "assistant" && busy && message.id === activeMessage.current && (
                      <p className="chat-progress" role="status" aria-live="polite">
                        <span className="chat-progress-dot" aria-hidden="true" />
                        {toolStatus || "正在整理回复…"}
                      </p>
                    )}
                    {message.blocks?.length ? (
                      <AnswerBlocks
                        blocks={message.blocks}
                        onRead={(blocks) => { readingTrigger.current = document.activeElement instanceof HTMLElement ? document.activeElement : null; setReading(blocks); }}
                      />
                    ) : null}
                    {message.cache && message.cache.type !== "miss" && (
                      <p className="chat-result-note">
                        基于已保存的结果
                        {message.cache.generated_at
                          ? ` · ${new Date(message.cache.generated_at).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" })}`
                          : ""}
                      </p>
                    )}
                    {message.citations?.length ? (
                      <details className="chat-citations">
                        <summary>
                          查看 {message.citations.length} 个资料来源
                        </summary>
                        {message.citations.map((c, j) => (
                          <p key={j}>
                            {c.title || c.filename || c.source_id || c.id}
                          </p>
                        ))}
                      </details>
                    ) : null}
                    {message.role === "assistant" && message.text && (
                      <div className="chat-message-actions">
                        <button
                          onClick={async () => {
                            try {
                              await navigator.clipboard.writeText(message.text);
                              setCopied(message.id);
                            } catch {
                              setError("复制不可用，请选择文字复制。");
                            }
                          }}
                        >
                          {copied === message.id ? (
                            <Check size={14} />
                          ) : (
                            <Copy size={14} />
                          )}
                          {copied === message.id ? "已复制" : "复制回答"}
                        </button>
                        {messages[index - 1]?.role === "user" && (
                          <button
                            disabled={busy || recovering}
                            onClick={() =>
                              void send(messages[index - 1].text, false, true)
                            }
                          >
                            重新查询
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                </article>
              ))}
            </div>
          </ThreadPrimitive.Viewport>
          <div className="chat-composer-dock">
            {!busy && !recovering && suggestions.length > 0 && (
              <div className="chat-followups" aria-label="继续追问">
                {suggestions.slice(0, 3).map((question) => (
                  <ThreadPrimitive.Suggestion
                    key={question}
                    prompt={question}
                    send
                  >
                    {question} ↗
                  </ThreadPrimitive.Suggestion>
                ))}
              </div>
            )}
            {error && (
              <p className="chat-error" role="alert">
                {error}{" "}
                {logicalRequest.current && !busy && (
                  <button
                    onClick={() => void send(logicalRequest.current!.text)}
                  >
                    重试
                  </button>
                )}
              </p>
            )}
            <form
              className="chat-composer"
              onSubmit={(e) => {
                e.preventDefault();
                if (!busy && !recovering) void send();
              }}
            >
              <label className="sr-only" htmlFor="chat-input">
                与个人 Agent 对话
              </label>
              <textarea
                ref={textarea}
                id="chat-input"
                rows={2}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (
                    e.key === "Enter" &&
                    !e.shiftKey &&
                    !e.nativeEvent.isComposing &&
                    e.keyCode !== 229
                  ) {
                    e.preventDefault();
                    if (!busy && !recovering) void send();
                  }
                }}
                maxLength={16000}
                placeholder="问问 LYPH，或者描述你想了解的事…"
              />
              <div>
                <span>
                  {busy || recovering
                    ? toolStatus || "正在回答…"
                    : "Enter 发送 · Shift + Enter 换行"}
                </span>
                {busy ? (
                  <button
                    type="button"
                    aria-label="停止回答"
                    onClick={() => void stop()}
                  >
                    <Square size={15} />
                  </button>
                ) : (
                  <button
                    type="submit"
                    aria-label="发送消息"
                    disabled={!draft.trim() || recovering}
                  >
                    <ArrowUp size={20} />
                  </button>
                )}
              </div>
            </form>
            <p className="chat-footer-note">
              回答基于公开资料及数据快照，请结合来源与更新时间判断。
            </p>
          </div>
        </div>
        {authorize && (
          <div
            className="chat-dialog-shade"
            onKeyDown={(e) => {
              if (e.key === "Escape") setAuthorize(false);
              trapDialogFocus(e);
            }}
          >
            <section
              className="chat-dialog"
              role="dialog"
              aria-modal="true"
              aria-labelledby="chat-auth-title"
            >
              <button
                className="chat-dialog-close"
                aria-label="关闭授权"
                onClick={() => setAuthorize(false)}
              >
                <X size={20} />
              </button>
              <h2 id="chat-auth-title">继续聊，输入临时密码</h2>
              <p>第一次成功回答免费。后续可使用管理员提供的临时密码。</p>
              <form
                onSubmit={async (e) => {
                  e.preventDefault();
                  try {
                    await api("/chat/authorize", {
                      method: "POST",
                      body: json({ password }),
                    });
                    setPassword("");
                    setAuthorize(false);
                    await refreshAccess();
                    const pending = pendingQuestion.current;
                    pendingQuestion.current = null;
                    if (pending) void send(pending.text, true, pending.refresh);
                  } catch (e) {
                    setError(errorText(e));
                  }
                }}
              >
                <label htmlFor="chat-password">临时密码</label>
                <input
                  id="chat-password"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete="off"
                  required
                />
                <button type="submit">验证并继续</button>
              </form>
              <button
                className="chat-contact"
                onClick={() => setContact(!contact)}
              >
                联系管理员
              </button>
              {contact && <p>管理员微信：example-wechat</p>}
              {error && (
                <p className="chat-error" role="alert">
                  {error}
                </p>
              )}
            </section>
          </div>
        )}
        {reading && (
          <div
            className="chat-dialog-shade"
            onKeyDown={(e) => {
              if (e.key === "Escape") closeReading();
              if (e.key === "Tab") {
                const panel = e.currentTarget.querySelector("[role=dialog]");
                const focusable = panel?.querySelectorAll<HTMLElement>(
                  "button,a[href],input,select,summary,[tabindex='0']",
                );
                if (focusable?.length) {
                  const first = focusable[0],
                    last = focusable[focusable.length - 1];
                  if (e.shiftKey && document.activeElement === first) {
                    e.preventDefault();
                    last.focus();
                  } else if (!e.shiftKey && document.activeElement === last) {
                    e.preventDefault();
                    first.focus();
                  }
                }
              }
            }}
          >
            <section
              className="chat-reading"
              role="dialog"
              aria-modal="true"
              aria-label="内容阅读"
            >
              <button
                ref={readingClose}
                className="chat-dialog-close"
                aria-label="关闭阅读"
                onClick={closeReading}
              >
                <X size={20} />
              </button>
              <AnswerBlocks blocks={reading} />
            </section>
          </div>
        )}
      </ThreadPrimitive.Root>
    </AssistantRuntimeProvider>
  );
}
