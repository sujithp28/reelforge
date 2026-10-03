/** Present only inside the Windows app. The website does not have this bridge. */

export type DesktopSaveResult =
  | { ok: true; filename: string }
  | { ok: false; message: string };

export type DesktopBridge = {
  saveExport: (payload: { projectId: string }) => Promise<DesktopSaveResult>;
  revealExport: (filename: string) => Promise<{ ok: boolean; message?: string }>;
  openDownloads: () => Promise<{ ok: boolean; message?: string }>;
};

export function desktopBridge(): DesktopBridge | null {
  if (typeof window === "undefined") return null;
  const bridge = (window as Window & { reelforgeDesktop?: DesktopBridge }).reelforgeDesktop;
  if (!bridge || typeof bridge.saveExport !== "function") return null;
  return bridge;
}
