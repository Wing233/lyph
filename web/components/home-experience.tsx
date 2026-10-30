"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import type { NavigationItem } from "@/lib/api";
import { Dialog } from "./ui/dialog";
const KEY = "lyph:prototype2:unlocks:v3";
const EMPTY_NAVIGATION: NavigationItem[] = [];

type Route = {
  id: string;
  label: string;
  href: string;
  event?: string;
  showAsBot: boolean;
  botSize: number;
};

function safeHref(value: unknown) {
  const href = typeof value === "string" ? value.trim() : "";
  if (/[\\\n\r\t]/.test(href)) return "#";
  if (href.startsWith("#") && href.length > 1) return href;
  if (href.startsWith("/") && !href.startsWith("//")) return href;
  if (/^https?:\/\//i.test(href)) return href;
  return "#";
}

function RouteLink({
  route,
  hidden,
  botRoute,
  arrow,
}: {
  route: Route;
  hidden?: boolean;
  botRoute?: boolean;
  arrow?: boolean;
}) {
  const botId = botRoute ? route.id : undefined;
  if (/^https?:\/\//i.test(route.href)) {
    return (
      <a
        href={route.href}
        target="_blank"
        rel="noreferrer"
        hidden={hidden}
        data-event={route.event}
        data-bot-route={botId}
      >
        {route.label}
        {arrow ? " ↗" : null}
      </a>
    );
  }
  return (
    <Link
      href={route.href}
      hidden={hidden}
      data-event={route.event}
      data-bot-route={botId}
    >
      {route.label}
      {arrow ? " ↗" : null}
    </Link>
  );
}

export function HomeExperience({
  navigation = EMPTY_NAVIGATION,
}: {
  navigation?: NavigationItem[];
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const game = useRef<
    ReturnType<typeof import("@/lib/agar").initAgar> | undefined
  >(undefined);
  const [found, setFound] = useState<string[]>([]);
  const [secret, setSecret] = useState(false);
  const [open, setOpen] = useState(false);
  const [paused, setPaused] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const routes = useMemo<Route[]>(() => {
    const seen = new Set<string>();
    return navigation.flatMap((item) => {
      const id = typeof item?.id === "string" ? item.id.trim() : "";
      const label = typeof item?.label === "string" ? item.label.trim() : "";
      if (!/^[A-Za-z0-9_-]{1,64}$/.test(id) || !label || seen.has(id))
        return [];
      seen.add(id);
      return [
        {
          id,
          label,
          href: safeHref(item.href),
          event:
            typeof item.event_name === "string" && item.event_name
              ? item.event_name
              : undefined,
          showAsBot: item.show_as_bot !== false,
          botSize:
            typeof item.bot_size === "number" && Number.isFinite(item.bot_size)
              ? item.bot_size
              : 1,
        },
      ];
    });
  }, [navigation]);
  const botMenus = useMemo(
    () =>
      routes
        .filter((route) => route.showAsBot)
        .map((route) => ({
          id: route.id,
          name: route.label,
          href: route.href,
          size: route.botSize,
        })),
    [routes],
  );
  const discoveredBots = routes.filter(
    (route) => route.showAsBot && found.includes(route.id),
  );
  useEffect(() => {
    let disposed = false;
    try {
      const saved = JSON.parse(localStorage.getItem(KEY) || "{}");
      setSecret(
        saved.version === 3 &&
          saved.source === "nameless-largest-bot" &&
          saved.unlocked === true,
      );
    } catch {}
    const consume = (event: Event) => {
      const d = (event as CustomEvent).detail;
      if (d.gameVersion !== "menu-bots-v3") return;
      if (d.menuId) {
        setFound((v) => (v.includes(d.menuId) ? v : [...v, d.menuId]));
        setDismissed(false);
      }
      if (d.secret && d.wasLargest) {
        setSecret(true);
        try {
          localStorage.setItem(
            KEY,
            JSON.stringify({
              version: 3,
              source: "nameless-largest-bot",
              unlocked: true,
            }),
          );
        } catch {}
      }
    };
    window.addEventListener("agar:consume", consume);
    void import("@/lib/agar").then(({ initAgar }) => {
      if (!disposed && canvas.current) {
        game.current = initAgar(canvas.current, botMenus);
      }
    });
    return () => {
      disposed = true;
      window.removeEventListener("agar:consume", consume);
      game.current?.dispose();
    };
  }, [botMenus]);
  return (
    <>
      <canvas ref={canvas} id="bg" aria-hidden="true" />
      <header className="home-nav">
        <Link href="/" className="home-brand">
          LYPH<span>.</span>
        </Link>
        <nav aria-label="已发现的菜单">
          {routes.map((route) => (
            <RouteLink
              key={route.id}
              route={route}
              botRoute={route.showAsBot}
              hidden={route.showAsBot && !found.includes(route.id)}
            />
          ))}
          {secret && (
            <button className="text-link" onClick={() => setOpen(true)}>
              隐藏实验室 ↗
            </button>
          )}
        </nav>
      </header>
      <div className="background-controls">
        <button
          onClick={() => {
            const next = !paused;
            game.current?.setPaused(next);
            setPaused(next);
          }}
          aria-pressed={paused}
        >
          {paused ? "继续背景" : "暂停背景"}
        </button>
        <a href="#thanks">致谢 ↗</a>
      </div>
      {discoveredBots.length > 0 && !dismissed && (
        <aside className="bot-menu-discovery" aria-label="发现的页面入口">
          <div>
            {discoveredBots.map((route) => (
              <RouteLink key={route.id} route={route} arrow />
            ))}
          </div>
          <button
            aria-label="收起发现的页面入口"
            onClick={() => setDismissed(true)}
          >
            ×
          </button>
        </aside>
      )}
      <span role="status" className="sr-only">
        {discoveredBots.length > 0
          ? `已发现${discoveredBots.map((route) => route.label).join("、")}: 页面入口`
          : ""}
      </span>
      <Dialog open={open} onOpenChange={setOpen} title="隐藏实验室">
        <p>一些关于技术、日常和这个主页的手记。</p>
        <h3>把技术写进日常。</h3>
        <p>记录一次尝试、一段生活，再让对话把零散的资料串起来。</p>
        <div className="reading-links">
          <a href="#works" onClick={() => setOpen(false)}>
            查看作品 ↗
          </a>
          <a href="#diary" onClick={() => setOpen(false)}>
            阅读日记 ↗
          </a>
          <a href="#thanks" onClick={() => setOpen(false)}>
            灵感与致谢 ↗
          </a>
        </div>
      </Dialog>
    </>
  );
}
