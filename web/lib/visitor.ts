// One browser identity handshake for concurrent page events and chat mounts.
let initialization: Promise<void> | undefined;
export function ensureVisitor(): Promise<void> {
  if (!initialization)
    initialization = fetch("/api/v1/chat/access", { credentials: "include" })
      .then((response) => {
        if (!response.ok) throw new Error("访客身份初始化失败。");
      })
      .catch((error) => {
        initialization = undefined;
        throw error;
      });
  return initialization;
}
