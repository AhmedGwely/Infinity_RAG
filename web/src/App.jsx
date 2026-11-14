import { useEffect, useState } from "react";
import { api } from "./api.js";
import { Assistant, Login, Logo, Research } from "./pages_core.jsx";
import { Compare, Ingestion, Library, Reports } from "./pages_work.jsx";
import { Settings, Sources, System, Users } from "./pages_admin.jsx";
import { Field, Modal, Spinner, ToastProvider, useToast } from "./ui.jsx";

const NAV = [
  { id: "assistant", label: "Assistant", roles: ["admin", "engineer", "viewer"], group: "Workspace", icon: "M4 5h16v10H9l-5 4z" },
  { id: "research", label: "Research", roles: ["admin", "engineer", "viewer"], group: "Workspace", icon: "M11 4a7 7 0 105 12l4 4 1.5-1.5-4-4A7 7 0 0011 4z" },
  { id: "compare", label: "Compare", roles: ["admin", "engineer"], group: "Workspace", icon: "M5 4h6v16H5zM13 4h6v16h-6z" },
  { id: "reports", label: "Reports", roles: ["admin", "engineer"], group: "Workspace", icon: "M6 3h9l4 4v14H6zM9 12h7M9 16h7" },
  { id: "library", label: "Library", roles: ["admin", "engineer", "viewer"], group: "Knowledge", icon: "M5 4h4v16H5zM11 4h4v16h-4zM17 6l3 1-3 13-3-1z" },
  { id: "ingestion", label: "Ingestion", roles: ["admin", "engineer"], group: "Knowledge", icon: "M12 3v12m0 0l-4-4m4 4l4-4M5 19h14" },
  { id: "sources", label: "Sources", roles: ["admin"], group: "Administration", icon: "M12 3a9 9 0 100 18 9 9 0 000-18zM3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18" },
  { id: "users", label: "Users", roles: ["admin"], group: "Administration", icon: "M9 11a4 4 0 100-8 4 4 0 000 8zM2 21c0-4 3-6 7-6s7 2 7 6M17 11a3 3 0 100-6M19 21c0-3-1-5-3-6" },
  { id: "settings", label: "Providers", roles: ["admin"], group: "Administration", icon: "M12 8a4 4 0 100 8 4 4 0 000-8zM12 2v3M12 19v3M2 12h3M19 12h3" },
  { id: "system", label: "System & logs", roles: ["admin"], group: "Administration", icon: "M3 12h4l3-8 4 16 3-8h4" },
];

const routeFromHash = () => (window.location.hash.replace(/^#\/?/, "").split(/[/?]/)[0] || "assistant");

function Shell({ user, onLogout }) {
  const [route, setRoute] = useState(routeFromHash());
  const [menu, setMenu] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const [pw, setPw] = useState(null);
  const toast = useToast();
  useEffect(() => {
    const on = () => { setRoute(routeFromHash()); setNavOpen(false); };
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const allowed = NAV.filter((n) => n.roles.includes(user.role));
  const current = allowed.find((n) => n.id === route) || allowed[0];
  const groups = [...new Set(allowed.map((n) => n.group))];

  const changePw = async () => {
    try { await api.post("/auth/password", pw); setPw(null); toast("Password changed; other sessions were signed out", "ok"); }
    catch (e) { toast(e.message, "error"); }
  };
  const page = { assistant: <Assistant user={user} />, research: <Research />, compare: <Compare />, reports: <Reports />, library: <Library user={user} />,
    ingestion: <Ingestion user={user} />, sources: <Sources />, users: <Users me={user} />, settings: <Settings />, system: <System /> }[current.id];

  return (
    <div className={"shell" + (navOpen ? " nav-open" : "")}>
      <nav className="sidebar" aria-label="Main">
        <a className="brand" href="#/assistant" aria-label="Infinity Egypt — home"><Logo tone="light" height={44} /><span className="brand-tag">AI Knowledge Platform</span></a>
        {groups.map((g) => (
          <div key={g} className="nav-group"><div className="nav-title">{g}</div>
            {allowed.filter((n) => n.group === g).map((n) => (
              <a key={n.id} href={"#/" + n.id} className={"nav-item" + (n.id === current.id ? " active" : "")} aria-current={n.id === current.id ? "page" : undefined}>
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={n.icon} /></svg>{n.label}
              </a>
            ))}
          </div>
        ))}
        <div className="sidebar-foot">
          <button className="user-btn" onClick={() => setMenu(!menu)} aria-expanded={menu}>
            <span className="avatar">{(user.name || user.email)[0].toUpperCase()}</span>
            <span className="who"><b dir="auto">{user.name || user.email}</b><small>{user.role}</small></span>
          </button>
          {menu && <div className="user-menu"><button onClick={() => { setMenu(false); setPw({ current_password: "", new_password: "" }); }}>Change password</button><button onClick={onLogout}>Sign out</button></div>}
        </div>
      </nav>
      <div className="main">
        <div className="topbar"><button className="icon-btn" onClick={() => setNavOpen(!navOpen)} aria-label="Menu">☰</button><Logo tone="light" height={26} /><span className="spacer" /><b>{current.label}</b></div>
        <main>{page}</main>
      </div>
      {pw && (
        <Modal title="Change password" onClose={() => setPw(null)} footer={<><button className="btn" onClick={() => setPw(null)}>Cancel</button><button className="btn primary" onClick={changePw}>Change</button></>}>
          <Field label="Current password"><input type="password" autoComplete="current-password" value={pw.current_password} onChange={(e) => setPw({ ...pw, current_password: e.target.value })} /></Field>
          <Field label="New password" hint="10+ characters, upper and lower case, and a digit."><input type="password" autoComplete="new-password" value={pw.new_password} onChange={(e) => setPw({ ...pw, new_password: e.target.value })} /></Field>
        </Modal>
      )}
    </div>
  );
}

export default function App() {
  const [user, setUser] = useState(undefined); // undefined = checking, null = signed out
  useEffect(() => {
    api.get("/auth/me").then(setUser).catch(() => setUser(null));
    const out = () => setUser(null);
    window.addEventListener("infinity:unauthorized", out);
    return () => window.removeEventListener("infinity:unauthorized", out);
  }, []);
  const logout = async () => { await api.post("/auth/logout").catch(() => {}); setUser(null); window.location.hash = "#/assistant"; };
  return (
    <ToastProvider>
      {user === undefined ? <div className="boot"><Spinner label="Loading…" /></div> : user === null ? <Login onLogin={setUser} /> : <Shell user={user} onLogout={logout} />}
    </ToastProvider>
  );
}
