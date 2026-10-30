import { PublicShell } from "@/components/public-shell";
import { Collection } from "@/components/collection";
export default function Page() {
  return (
    <PublicShell>
      <p className="kicker">本地收藏</p>
      <h1>想再看一次。</h1>
      <Collection />
    </PublicShell>
  );
}
