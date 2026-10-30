import { NextResponse, type NextRequest } from "next/server";

// Use the browser-facing origin rather than the release proxy's child-server port.
function redirectTo(url: URL, request: NextRequest) {
  const configured = Reflect.get(process.env, "LYPH_PUBLIC_ORIGIN");
  const protocol = request.headers.get("x-forwarded-proto") === "https" ? "https" : "http";
  const origin = configured || `${protocol}://${request.headers.get("host") || url.host}`;
  return NextResponse.redirect(new URL(url.pathname + url.search, origin), 307);
}

// Keep the former pages in source for rollback while the public site uses chat.
// Explicit matches leave administration, APIs, assets and previews alone.
export function middleware(request: NextRequest) {
  // Read at request time: the same image can enable or roll back the homepage.
  const enabled = Reflect.get(process.env, "LYPH_CHAT_FIRST_ENABLED") === "true";
  if (!enabled) {
    if (request.nextUrl.pathname.endsWith(".html")) {
      const legacy = request.nextUrl.clone();
      legacy.pathname = "/" + request.nextUrl.pathname.split("/").filter(Boolean).at(-1)!.replace(/\.html$/, "");
      return redirectTo(legacy, request);
    }
    return NextResponse.next();
  }
  // Restore the existing public directories while retaining share readers in chat.
  const pathname = request.nextUrl.pathname;
  const publicDirectory = /^(?:\/prototype2)?\/(projects|stack|interests|blog|documents|journal|collection)(?:\.html)?\/?$/.exec(pathname);
  if (publicDirectory) {
    if (pathname.endsWith(".html")) {
      const legacy = request.nextUrl.clone();
      legacy.pathname = `/${publicDirectory[1]}`;
      return redirectTo(legacy, request);
    }
    return NextResponse.next();
  }
  const destination = request.nextUrl.clone();
  const page = request.nextUrl.pathname
    .split("/")
    .filter(Boolean)
    .at(-1)
    ?.replace(/\.html$/, "");
  const kind =
    page === "project" ? "project" : page === "entry" ? "content" : null;
  destination.pathname = "/";
  destination.search = "";
  if (
    kind &&
    (request.nextUrl.searchParams.get("id") ||
      request.nextUrl.searchParams.get("date"))
  ) {
    destination.searchParams.set("entity", kind);
    for (const key of ["id", "date"])
      if (request.nextUrl.searchParams.get(key))
        destination.searchParams.set(
          key,
          request.nextUrl.searchParams.get(key)!,
        );
  }
  return redirectTo(destination, request);
}

export const config = {
  matcher: [
    "/projects/:path*",
    "/project/:path*",
    "/stack/:path*",
    "/interests/:path*",
    "/blog/:path*",
    "/documents/:path*",
    "/journal/:path*",
    "/entry/:path*",
    "/collection/:path*",
    // Nginx handles the deployed static app; cover direct Next.js requests too.
    "/companies/:path*",
    "/project.html",
    "/entry.html",
    "/projects.html", "/stack.html", "/interests.html", "/blog.html", "/documents.html", "/journal.html", "/collection.html",
    "/prototype2/project.html",
    "/prototype2/entry.html",
    "/prototype2/projects.html", "/prototype2/stack.html", "/prototype2/interests.html", "/prototype2/blog.html", "/prototype2/documents.html", "/prototype2/journal.html", "/prototype2/collection.html",
  ],
};
