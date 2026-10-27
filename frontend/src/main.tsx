import { createRoot } from "react-dom/client";
import { useEffect, useState } from "react";
import { api } from "./api";
import { Login } from "./components";
import { Customer } from "./customer";
import { Workbench } from "./workbench";
import type { Account } from "./types";
import "./style.css";
function App() {
  const [account, setAccount] = useState<Account | null>(null);
  useEffect(() => {
    api.onLost = () => setAccount(null);
    return () => {
      api.onLost = () => {};
    };
  }, []);
  if (!account)
    return (
      <Login
        staff={location.pathname.startsWith("/workbench")}
        onLogin={setAccount}
      />
    );
  return account.role === "customer" ? (
    <Customer key={account.id} account={account} />
  ) : (
    <Workbench key={account.id} account={account} />
  );
}
createRoot(document.getElementById("root")!).render(<App />);
