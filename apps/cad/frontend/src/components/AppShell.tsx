import { TabBar } from "./TabBar";
import { ChatPanel } from "./ChatPanel";
import { RightSidebar } from "./RightSidebar";
import { ViewerPane } from "./ViewerPane";
import { Timeline } from "./Timeline";
import { BrowserPanel } from "./BrowserPanel";
import { PrintPane } from "./PrintPane";
import { ContextMenuHost } from "@/lib/contextMenu";
import { usePrint } from "@/lib/print";

export function AppShell() {
  // The print phase replaces the viewer + right sidebar + timeline with
  // a single full-area PrintPane. The chat panel stays in place so the
  // user can talk to the (phase-aware) agent while the print is being
  // prepared.
  const { active: printActive } = usePrint();

  return (
    <ContextMenuHost>
      <div className="flex h-screen w-screen flex-col bg-[var(--color-bg)] text-[var(--color-text)]">
        <TabBar />
        <div className="flex min-h-0 flex-1">
          <aside className="w-[400px] shrink-0 border-r border-[var(--color-border)] bg-[var(--color-panel)]">
            <ChatPanel />
          </aside>
          <main className="flex min-w-0 flex-1 flex-col">
            {printActive ? (
              <div className="relative min-w-0 flex-1">
                <PrintPane />
              </div>
            ) : (
              <>
                <div className="flex min-h-0 flex-1">
                  <div className="relative min-w-0 flex-1">
                    <ViewerPane />
                  </div>
                  <aside className="w-[280px] shrink-0 border-l border-[var(--color-border)] bg-[var(--color-panel)]">
                    <RightSidebar />
                  </aside>
                </div>
                <div className="border-t border-[var(--color-border)] bg-[var(--color-panel)]">
                  <Timeline />
                </div>
              </>
            )}
          </main>
        </div>
        {/* Floating browser preview lives outside the layout flow so it
            can be dragged anywhere. Auto-shows on first session. */}
        <BrowserPanel />
      </div>
    </ContextMenuHost>
  );
}
