import type { Metadata } from "next";
import { Collector } from "@/components/collector";
export const metadata: Metadata = {
  title: "移动采集台",
  appleWebApp: { capable: true, title: "LYPH 采集", statusBarStyle: "default" },
};
export default function Page() {
  return <Collector />;
}
