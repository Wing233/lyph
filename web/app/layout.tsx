import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = {
  title: { default: "LYPH — 技术与生活", template: "%s · LYPH" },
  description: "技术项目、生活记录与个人 Agent。",
  manifest: "/manifest.webmanifest",
};
export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="zh-CN">
      <body>
        <a href="#main-content" className="skip-link">
          跳到正文
        </a>
        {children}
      </body>
    </html>
  );
}
