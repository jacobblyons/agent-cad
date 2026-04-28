import { TabBar } from "./TabBar";
import { ChatPanel } from "./ChatPanel";
import { RightSidebar } from "./RightSidebar";
import { ViewerPane } from "./ViewerPane";
import { Timeline } from "./Timeline";
import { BrowserPanel } from "./BrowserPanel";
import { ContextMenuHost } from "@/lib/contextMenu";

export function AppShell() {
  return (
    <ContextMenuHost>
      <div className="flex h-screen w-screen flex-col bg-[var(--color-bg)] text-[var(--color-text)]">
        <TabBar />
        <div className="flex min-h-0 flex-1">
          <aside className="w-[400px] shrink-0 border-r border-[var(--color-border)] bg-[var(--color-panel)]">
            <ChatPanel />
          </aside>
          <main className="flex min-w-0 flex-1 flex-col">
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
          </main>
        </div>
        {/* Floating browser preview lives outside the layout flow so it
            can be dragged anywhere. Auto-shows on first session. */}
        <BrowserPanel />
      </div>
    </ContextMenuHost>
  );
}
