import { Chat } from "@/components/chat";
import LegacyHome, { generateMetadata } from "@/components/legacy-home-page";
import { PageEvent } from "@/components/tracking";

export const dynamic = "force-dynamic";
export { generateMetadata };
export default function Home() {
  return Reflect.get(process.env, "LYPH_CHAT_FIRST_ENABLED") === "true" ? (
    <>
      <PageEvent />
      <Chat />
    </>
  ) : (
    <LegacyHome />
  );
}
