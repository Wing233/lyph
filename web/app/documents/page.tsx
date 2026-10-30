import { ContentList } from "@/components/content-list";
export default async function Page({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | undefined>>;
}) {
  return (
    <ContentList
      kind="document"
      title="翻翻文档。"
      subtitle="让有用的知识有一个可以回来的地方。"
      params={await searchParams}
    />
  );
}
