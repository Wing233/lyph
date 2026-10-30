"use client";
import { useState } from "react";
import { api, errorText, items } from "@/lib/api";
import { Empty, ResourceStatus, useResource } from "./common";
type Media = {
  id: string;
  filename: string;
  size: number;
  content_type: string;
  references: { owner_type: string; owner_id: string; role: string }[];
};
export function Files() {
  const list = useResource<{ items: Media[]; total_bytes: number }>(
    "/admin/files",
  );
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <section style={{ marginTop: 36 }}>
      <div className="section-head">
        <div>
          <h2>文件与媒体</h2>
          <p>原始文件始终按权限访问，引用中的文件不能删除。</p>
        </div>
        <button className="button secondary" onClick={list.reload}>
          刷新
        </button>
      </div>
      <label className="field">
        上传图片或附件
        <input
          type="file"
          disabled={busy}
          onChange={async (e) => {
            const file = e.target.files?.[0];
            if (!file) return;
            setBusy(true);
            try {
              if (file.size > 20 * 1024 * 1024)
                throw new Error("单文件最多20MiB。");
              const form = new FormData();
              form.set("file", file);
              await api("/admin/files", { method: "POST", body: form });
              setNotice("原文件已保存到私有存储。可以在写作台中添加引用。");
              void list.reload();
            } catch (err) {
              setNotice(errorText(err));
            } finally {
              setBusy(false);
              e.target.value = "";
            }
          }}
        />
      </label>
      {notice && (
        <p className="inline-notice" role="status">
          {notice}
        </p>
      )}
      <ResourceStatus {...list} retry={list.reload} />
      {items<Media>(list.data).length ? (
        <>
          <p className="muted">
            当前列表共{" "}
            {(Number(list.data?.total_bytes || 0) / 1024 / 1024).toFixed(2)} MiB
          </p>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>文件</th>
                  <th>大小</th>
                  <th>引用</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {items<Media>(list.data).map((m) => (
                  <tr key={m.id}>
                    <td>
                      <a
                        href={`/api/v1/admin/files/${m.id}/download`}
                        target="_blank"
                        rel="noreferrer"
                      >
                        {m.filename} ↗
                      </a>
                    </td>
                    <td>{(m.size / 1024).toFixed(1)} KiB</td>
                    <td>
                      {m.references?.length
                        ? m.references
                            .map((r) => `${r.owner_type} · ${r.role}`)
                            .join(" / ")
                        : "未引用"}
                    </td>
                    <td>
                      <div className="row-actions">
                        <button
                          className="text-link"
                          onClick={() =>
                            void navigator.clipboard
                              .writeText(`/api/v1/public/media/${m.id}`)
                              .then(
                                () =>
                                  setNotice(
                                    "公开引用地址已复制。公开内容发布并引用该文件后可访问。",
                                  ),
                                () =>
                                  setNotice("无法复制，请在写作台上传附件。"),
                              )
                          }
                        >
                          复制引用地址
                        </button>
                        <button
                          className="text-link"
                          disabled={!!m.references?.length}
                          onClick={async () => {
                            try {
                              await api(`/admin/files/${m.id}`, {
                                method: "DELETE",
                              });
                              void list.reload();
                            } catch (e) {
                              setNotice(errorText(e));
                            }
                          }}
                        >
                          删除
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : (
        !list.loading &&
        !list.error && (
          <Empty
            title="还没有原文件"
            description="附件、图片、资料与截图的引用均由服务端记录。"
          />
        )
      )}
    </section>
  );
}
