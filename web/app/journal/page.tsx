import { ContentList } from "@/components/content-list";
export default async function Page({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | undefined>>;
}) {
  return (
    <ContentList
      kind="journal"
      title="日常，慢慢记。"
      subtitle="手写日记与经过审核的截图日报。"
      params={await searchParams}
    />
  );
}
