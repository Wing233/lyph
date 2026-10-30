"use client";
import { useEffect, useRef, useState } from "react";
import { api, errorText, json } from "@/lib/api";
import { Markdown } from "./markdown";
import { ensureVisitor } from "@/lib/visitor";
import { track } from "./tracking";
type Access = {
  free_remaining?: number;
  authorized?: boolean;
  mode?: string;
  remaining?: number;
  expires_at?: string;
};
type Message = {
  role: "user" | "assistant";
  text: string;
  citations?: {
    title?: string;
    filename?: string;
    source_id?: string;
    id?: string;
  }[];
};
export function Chat() {
  const [access, setAccess] = useState<Access>({});
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [contact, setContact] = useState(false);
  const [authorize, setAuthorize] = useState(false);
  const request = useRef<string>("");
  const conversation = useRef<string>("");
  const controller = useRef<AbortController | undefined>(undefined);
  const pendingQuestion = useRef<string>("");
  const logicalRequest = useRef<{ id: string; text: string } | undefined>(
    undefined,
  );
  const refresh = () =>
    api<Access>("/chat/access")
      .then(setAccess)
      .catch((e) => setError(errorText(e)));
  useEffect(() => {
    conversation.current = crypto.randomUUID();
    void ensureVisitor()
      .then(refresh)
      .catch((e) => setError(errorText(e)));
    return () => controller.current?.abort();
  }, []);
  async function send(text = draft, authorizationConfirmed = false) {
    if (!text.trim() || busy) return;
    const retry = logicalRequest.current?.text === text;
    if (
      !retry &&
      !authorizationConfirmed &&
      !access.free_remaining &&
      !access.authorized
    ) {
      pendingQuestion.current = text;
      setAuthorize(true);
      return;
    }
    setBusy(true);
    track("chat.submit");
    setError("");
    if (!retry)
      setMessages((v) => [
        ...v,
        { role: "user", text },
        { role: "assistant", text: "" },
      ]);
    request.current = retry ? logicalRequest.current!.id : crypto.randomUUID();
    logicalRequest.current = { id: request.current, text };
    controller.current = new AbortController();
    let completed = false;
    const update = (patch: Partial<Message>, append = false) =>
      setMessages((v) =>
        v.map((m, i) =>
          i === v.length - 1
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
    if (retry) update({ text: "", citations: [] });
    try {
      const response = await fetch("/api/v1/chat/requests", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: json({
          request_id: request.current,
          conversation_id: conversation.current,
          message: text,
          retry,
        }),
        signal: controller.current.signal,
      });
      if (!response.ok) {
        const result = await response.json();
        throw new Error(
          typeof result.detail === "string"
            ? result.detail
            : "对话暂时不可用。",
        );
      }
      if (!response.body) throw new Error("未收到回复流。");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
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
            .find((l) => l.startsWith("event:"))
            ?.slice(6)
            .trim();
          const dataText = block
            .split("\n")
            .filter((l) => l.startsWith("data:"))
            .map((l) => l.slice(5).trim())
            .join("\n");
          if (!dataText) continue;
          const data = JSON.parse(dataText);
          if (event === "delta")
            update({ text: data.text || data.delta || "" }, true);
          if (event === "citations")
            update({ citations: data.citations || data.items || [] });
          if (event === "done") {
            completed = true;
            update({ text: data.answer, citations: data.citations });
            setDraft("");
          }
          if (event === "error")
            throw new Error(
              data.message || data.error || "回答没有完成，请重试。",
            );
        }
        if (chunk.done) break;
      }
      if (!completed) {
        const state = await api<{
          status: string;
          answer: string;
          citations: Message["citations"];
        }>(`/chat/requests/${request.current}`);
        if (state.status === "succeeded" || state.status === "completed") {
          completed = true;
          update({ text: state.answer, citations: state.citations });
          setDraft("");
        } else throw new Error("连接中断，问题草稿已保留。");
      }
    } catch (e) {
      // A transport error can arrive after the server committed its answer.
      const state = await api<{
        status: string;
        answer: string;
        citations: Message["citations"];
      }>(`/chat/requests/${request.current}`).catch(() => undefined);
      if (state?.status === "completed") {
        completed = true;
        update({ text: state.answer, citations: state.citations });
        setDraft("");
      } else
        setError(
          state?.status === "running"
            ? "请求仍在执行。稍后重新发送同一问题可查询结果。"
            : errorText(e),
        );
    } finally {
      if (completed) logicalRequest.current = undefined;
      setBusy(false);
      await refresh();
    }
  }
  const label = access.free_remaining
    ? "首次提问免费"
    : access.authorized
      ? access.mode === "count"
        ? `剩余 ${access.remaining} 次`
        : `有效至 ${access.expires_at ? new Date(access.expires_at).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" }) : "—"}`
      : "请输入临时密码";
  return (
    <div className="chat-layout">
      <div className="chat-intro">
        <h2>
          聊聊代码，
          <br />
          <em>也聊聊日常。</em>
        </h2>
        <p>从一个问题开始，了解我的项目、想法与生活记录。</p>
        <p className="invitation">
          第一次成功回答免费。
          <br />
          继续聊，向管理员获取临时密码。
        </p>
      </div>
      <div className="chat-frame">
        <header>
          <span>LYPH / 个人 Agent</span>
          <span className="pill">{label}</span>
        </header>
        <div
          className="chat-messages"
          role="log"
          aria-label="对话记录"
          tabIndex={0}
        >
          {messages.length === 0 ? (
            <div className="chat-welcome">
              <span>“</span>
              <h3>从一个好奇开始。</h3>
              <p>你想先了解哪一部分？</p>
              {[
                "这个主页在做什么？",
                "你用了哪些技术？",
                "生活日报怎么来的？",
              ].map((q) => (
                <button
                  key={q}
                  onClick={() => {
                    setDraft(q);
                    void send(q);
                  }}
                >
                  {q} ↗
                </button>
              ))}
            </div>
          ) : (
            messages.map((m, i) => (
              <article className={`chat-message ${m.role}`} key={i}>
                <span>{m.role === "user" ? "你" : "LYPH"}</span>
                <Markdown>
                  {m.text || (busy ? "正在整理回复…" : "这次回答没有完成。")}
                </Markdown>
                {m.citations?.length ? (
                  <div className="citations">
                    来源：
                    {m.citations.map((c, j) => (
                      <span key={j}>
                        {c.title || c.filename || c.source_id || c.id}
                      </span>
                    ))}
                  </div>
                ) : null}
              </article>
            ))
          )}
        </div>
        {(authorize || (!access.free_remaining && !access.authorized)) && (
          <div className="chat-authorize">
            <h3>继续聊，输入临时密码。</h3>
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
                  await refresh();
                  if (pendingQuestion.current) {
                    const question = pendingQuestion.current;
                    pendingQuestion.current = "";
                    void send(question, true);
                  }
                } catch (err) {
                  setError(errorText(err));
                }
              }}
            >
              <label className="sr-only" htmlFor="chat-password">
                临时密码
              </label>
              <input
                id="chat-password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="off"
                required
              />
              <button className="button">验证密码</button>
            </form>
            <button className="text-link" onClick={() => setContact(!contact)}>
              联系管理员 ↗
            </button>
            {contact && (
              <p>
                管理员微信：example-wechat{" "}
                <button
                  className="text-link"
                  onClick={() =>
                    void navigator.clipboard
                      .writeText("example-wechat")
                      .catch(() => setError("无法复制，请手动复制微信号。"))
                  }
                >
                  复制
                </button>
              </p>
            )}
          </div>
        )}
        <form
          className="chat-input"
          onSubmit={(e) => {
            e.preventDefault();
            void send();
          }}
        >
          <label className="sr-only" htmlFor="chat-input">
            与个人 Agent 对话
          </label>
          <input
            id="chat-input"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            maxLength={600}
            required
            placeholder="聊聊技术、项目，或我的日常…"
          />
          <button className="button" disabled={busy}>
            发送 ↗
          </button>
          {busy && (
            <button
              type="button"
              className="button secondary"
              onClick={async () => {
                await api(`/chat/requests/${request.current}/cancel`, {
                  method: "POST",
                }).catch((e) => setError(errorText(e)));
                controller.current?.abort();
              }}
            >
              取消
            </button>
          )}
        </form>
        {error && (
          <p className="error-text" role="alert">
            {error}
          </p>
        )}
      </div>
    </div>
  );
}
