import { NextResponse } from "next/server";

const DOWNLOAD_URL =
  "https://github.com/Wing233/wechat-agent-router/releases/latest/download/WeChatAgentDesktop.exe";

export function GET() {
  return NextResponse.redirect(DOWNLOAD_URL, 302);
}
