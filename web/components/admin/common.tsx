"use client";
import { useCallback, useEffect, useState } from "react";
import { api, errorText } from "@/lib/api";
export function useResource<T>(path: string) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setData(await api<T>(path));
      setError("");
    } catch (e) {
      setError(errorText(e));
    } finally {
      setLoading(false);
    }
  }, [path]);
  useEffect(() => {
    void reload();
  }, [reload]);
  return { data, setData, error, setError, loading, reload };
}
export function PageIntro({
  eyebrow,
  title,
  description,
  action,
}: {
  eyebrow: string;
  title: string;
  description: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="page-intro">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action && <div className="page-actions">{action}</div>}
    </div>
  );
}
export function ResourceStatus({
  error,
  loading,
  retry,
}: {
  error?: string;
  loading?: boolean;
  retry?: () => void;
}) {
  return (
    <>
      {loading && (
        <p className="muted" role="status">
          正在读取…
        </p>
      )}
      {error && (
        <div className="inline-notice" role="alert">
          {error}{" "}
          {retry && (
            <button className="text-link" onClick={retry}>
              重新读取
            </button>
          )}
        </div>
      )}
    </>
  );
}
export function Empty({
  title = "还没有记录",
  description = "可以从上方开始创建。",
}: {
  title?: string;
  description?: string;
}) {
  return (
    <div className="empty-state">
      <h2>{title}</h2>
      <p>{description}</p>
    </div>
  );
}
export function DataView({ value }: { value: unknown }) {
  return <pre className="data-view">{JSON.stringify(value, null, 2)}</pre>;
}
export function formatTime(value: unknown) {
  return typeof value === "string"
    ? new Date(value).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" })
    : "—";
}
