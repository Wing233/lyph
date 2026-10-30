"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  LayoutDashboard,
  FileText,
  Library,
  Brain,
  KeyRound,
  ChartNoAxesCombined,
  Monitor,
  Palette,
  Settings,
  Menu,
  LogOut,
  Search,
} from "lucide-react";
import { Dialog } from "@/components/ui/dialog";
import { api, setCsrf, errorText } from "@/lib/api";
export const modules = [
  { id: "dashboard", title: "总览", icon: LayoutDashboard },
  { id: "content", title: "内容", icon: FileText },
  { id: "knowledge", title: "资料库", icon: Library },
  { id: "models", title: "模型与 Agent", icon: Brain },
  { id: "agent", title: "答案缓存与 MCP", icon: Brain },
  { id: "grants", title: "对话密码", icon: KeyRound },
  { id: "observability", title: "访问与对话", icon: ChartNoAxesCombined },
  { id: "desktop", title: "桌面与日报", icon: Monitor },
  { id: "ui", title: "UI 改动", icon: Palette },
  { id: "settings", title: "系统设置", icon: Settings },
];
export function AdminShell({ children }: { children: React.ReactNode }) {
  const path = usePathname();
  const router = useRouter();
  const [authenticated, setAuthenticated] = useState<boolean>();
  const [error, setError] = useState("");
  const [drawer, setDrawer] = useState(false);
  const [search, setSearch] = useState(false);
  const [query, setQuery] = useState("");
  useEffect(() => {
    api<{ authenticated: boolean; csrf_token: string }>("/auth/session")
      .then((s) => {
        setCsrf(s.csrf_token);
        setAuthenticated(s.authenticated);
        if (!s.authenticated) router.replace("/admin/login");
      })
      .catch((e) => setError(errorText(e)));
  }, [router]);
  useEffect(() => {
    setDrawer(false);
  }, [path]);
  useEffect(() => {
    const expire = () => {
      setAuthenticated(false);
      setCsrf("");
      router.replace("/admin/login");
    };
    window.addEventListener("lyph:auth-expired", expire);
    return () => window.removeEventListener("lyph:auth-expired", expire);
  }, [router]);
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "k") {
        e.preventDefault();
        setSearch((v) => !v);
      }
    };
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, []);
  const title =
    modules.find((m) => path.includes(`/admin/${m.id}`))?.title || "工作台";
  const nav = (
    <>
      <div className="sidebar-brand">
        <Link className="brand" href="/admin/dashboard">
          LYPH<span>.</span>
        </Link>
        <p>私人编辑台</p>
      </div>
      <nav aria-label="后台模块">
        {modules.map((m) => (
          <Link
            className="nav-link"
            href={`/admin/${m.id}`}
            aria-current={path === `/admin/${m.id}` ? "page" : undefined}
            key={m.id}
          >
            <m.icon size={18} />
            {m.title}
          </Link>
        ))}
      </nav>
      <div className="sidebar-footer">
        <p className="muted">站主 · 真实服务</p>
        <button
          className="text-link"
          onClick={async () => {
            try {
              await api("/auth/logout", { method: "POST" });
              setCsrf("");
              router.replace("/admin/login");
            } catch (e) {
              setError(errorText(e));
            }
          }}
        >
          <LogOut size={15} />
          退出登录
        </button>
      </div>
    </>
  );
  if (error && !authenticated)
    return (
      <main className="reading-shell" id="main-content">
        <h1>工作台暂时不可用。</h1>
        <p role="alert">{error}</p>
        <button className="button" onClick={() => location.reload()}>
          重试
        </button>
      </main>
    );
  if (!authenticated)
    return (
      <main className="initial-loading" id="main-content">
        <span className="brand">
          LYPH<span>.</span>
        </span>
        <p>正在验证站主身份…</p>
      </main>
    );
  return (
    <>
      <aside className="sidebar desktop-sidebar">{nav}</aside>
      <div className="workspace">
        <header className="topbar">
          <div className="breadcrumb">
            <button
              className="icon-button mobile-menu-button"
              aria-label="打开后台导航"
              onClick={() => setDrawer(true)}
            >
              <Menu size={20} />
            </button>
            <span>工作台</span>
            <span>/</span>
            <span>{title}</span>
          </div>
          <div className="topbar-actions">
            <button className="search-trigger" onClick={() => setSearch(true)}>
              <Search size={16} />
              <span>查找</span>
              <kbd>⌘ K</kbd>
            </button>
            <Link className="public-link" href="/" target="_blank">
              访问官网 ↗
            </Link>
          </div>
        </header>
        <main className="main" id="main-content">
          {error && (
            <p className="error-text" role="alert">
              {error}
            </p>
          )}
          {children}
          <footer className="footer-note">
            <span>内容、权限与任务状态均来自服务端</span>
            <Link href="/admin/settings">任务与操作记录 ↗</Link>
          </footer>
        </main>
      </div>
      <Dialog open={drawer} onOpenChange={setDrawer} title="后台导航">
        <div className="drawer-nav">{nav}</div>
      </Dialog>
      <Dialog open={search} onOpenChange={setSearch} title="查找工作区">
        <label className="field">
          搜索入口
          <input
            autoFocus
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        <div className="command-results">
          {modules
            .filter((m) => m.title.includes(query))
            .map((m) => (
              <Link
                href={`/admin/${m.id}`}
                key={m.id}
                className="command-result"
                onClick={() => setSearch(false)}
              >
                <m.icon size={18} />
                {m.title} ↗
              </Link>
            ))}
        </div>
      </Dialog>
    </>
  );
}
