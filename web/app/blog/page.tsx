import { ContentList } from "@/components/content-list";
export default async function Page({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | undefined>>;
}) {
  return (
    <ContentList
      kind="blog"
      title="读一些想法。"
      subtitle="关于技术、项目与一路上的思考。"
      params={await searchParams}
    />
  );
}
