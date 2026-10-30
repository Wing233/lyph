import { notFound } from "next/navigation";
import { AdminShell } from "@/components/admin/admin-shell";
import { AdminModule } from "@/components/admin/modules";
const ids = [
  "dashboard",
  "content",
  "knowledge",
  "models",
  "agent",
  "grants",
  "observability",
  "desktop",
  "ui",
  "settings",
];
export default async function Page({
  params,
}: {
  params: Promise<{ module: string }>;
}) {
  const { module } = await params;
  if (!ids.includes(module)) notFound();
  return (
    <AdminShell>
      <AdminModule module={module} />
    </AdminShell>
  );
}
