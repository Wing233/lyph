"use client";
import { useEffect } from "react";
import { usePathname } from "next/navigation";
import { ensureVisitor } from "@/lib/visitor";
export function track(event_name: string, content_id?: string) {
  if (
    location.pathname.startsWith("/admin") ||
    location.pathname.startsWith("/collector")
  )
    return;
  const body = JSON.stringify({
    event_name,
    content_id,
    occurrence_id: crypto.randomUUID(),
    path: location.pathname,
    device:
      innerWidth < 768 ? "mobile" : innerWidth < 1024 ? "tablet" : "desktop",
  });
  void ensureVisitor()
    .then(() =>
      fetch("/api/v1/public/events", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body,
      }),
    )
    .catch(() => {});
}
export function PageEvent({ contentId }: { contentId?: string }) {
  const pathname = usePathname();
  useEffect(() => {
    track("page_view");
    if (contentId) track("content_view", contentId);
    const click = (event: MouseEvent) => {
      const name = (event.target as Element)
        ?.closest?.("[data-event]")
        ?.getAttribute("data-event");
      if (
        name &&
        [
          "nav.blog.open",
          "nav.documents.open",
          "nav.journal.open",
          "nav.chat.open",
          "content.open",
          "contact.open",
        ].includes(name)
      )
        track(name);
    };
    document.addEventListener("click", click);
    return () => document.removeEventListener("click", click);
  }, [pathname, contentId]);
  return null;
}
