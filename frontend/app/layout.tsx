import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "ReelForge — Your idea. Your scenes. Your reel.",
  description: "Turn an idea, photo, product or person into a reel with scenes, captions and music.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
