# lyph Web

Next.js 15 / React 19 / TypeScript frontend: public site, admin console, PWA collector, and chat UI.

## Requirements

- Node.js 22+
- A running API (see [`../api/README.md`](../api/README.md))

## Local run

```bash
cd web
npm ci
API_BASE_URL=http://127.0.0.1:8000 npm run dev
```

Open <http://localhost:3000>. Admin login is at `/admin/login`.

`API_BASE_URL` is a **server-side** variable. The browser only talks to the same-origin `/api/v1` proxy.

## Build

```bash
npm run typecheck
npm run lint
npm run build
npm run start
```

The Dockerfile builds a standalone Next.js output.

## Environment

| Variable | Default | Description |
|---|---|---|
| `API_BASE_URL` | `http://api:8000` | API base for server-side proxy |
| `LYPH_CHAT_FIRST_ENABLED` | `false` | Use the chat-first homepage |
| `LYPH_PUBLIC_ORIGIN` | `http://localhost:3000` | Public origin for metadata |

## Structure

```text
app/          routes, layouts, API proxy
components/   chat, admin, site components
lib/          client helpers, types, API wrappers
public/       icons, manifest, service worker, font
tests/        Playwright tests
```

Do not commit real `.env` files, screenshots with visitor data, or test artifacts.
