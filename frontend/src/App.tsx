import { useCallback, useState } from "react";
import { AppHeader } from "./components/AppHeader";
import { LoginScreen } from "./components/LoginScreen";
import { TransactionHistoryPanel } from "./components/TransactionHistoryPanel";
import { ViewSwitch } from "./components/ViewSwitch";
import type { AppView } from "./components/ViewSwitch";
import { useIsAuthenticated } from "./hooks/useAuth";
import { HomePage } from "./pages/HomePage";
import { ShopPage } from "./pages/ShopPage";
import { logout } from "./services/authService";

export default function App() {
  // The whole app is behind the operator login gate (Phase 11F-H). Until a
  // bearer token is held, nothing that could call a protected endpoint is
  // mounted. A 401 anywhere clears the token (httpClient), which flips this
  // back to the login screen automatically.
  const authenticated = useIsAuthenticated();
  if (!authenticated) return <LoginScreen />;
  return <AuthenticatedApp />;
}

function AuthenticatedApp() {
  // Transaction history drawer (Phase 9 Step 5). The chat stays primary;
  // history is an overlay opened from the header.
  const [activityOpen, setActivityOpen] = useState(false);

  // Which surface is showing. Chat remains the default and is untouched by
  // the comparison feature (Phase 10G): the two are independent surfaces,
  // and switching does not reset chat state.
  const [view, setView] = useState<AppView>("chat");

  // Bumped when a capture completes so an open (or next-opened) history
  // panel refetches and the new CAPTURED record appears — always from
  // GET /transactions, never from locally invented state.
  const [historyRefreshToken, setHistoryRefreshToken] = useState(0);
  const handleCaptureComplete = useCallback(
    () => setHistoryRefreshToken((n) => n + 1),
    [],
  );

  return (
    <div className="flex h-dvh flex-col">
      <AppHeader
        onOpenActivity={() => setActivityOpen(true)}
        nav={<ViewSwitch view={view} onChange={setView} />}
        onLogout={logout}
      />
      {/* Both surfaces stay mounted so switching away from the chat does
          not discard its session or messages. */}
      <div className={view === "chat" ? "flex min-h-0 flex-1" : "hidden"}>
        <HomePage onCaptureComplete={handleCaptureComplete} />
      </div>
      <div className={view === "shop" ? "flex min-h-0 flex-1" : "hidden"}>
        <ShopPage />
      </div>
      <TransactionHistoryPanel
        open={activityOpen}
        onClose={() => setActivityOpen(false)}
        refreshToken={historyRefreshToken}
      />
    </div>
  );
}
