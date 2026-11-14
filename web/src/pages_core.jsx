import { useEffect, useRef, useState } from "react";
import { api, ago } from "./api.js";
import { Badge, CitationDrawer, Empty, Markdown, PageHeader, Spinner, useConfirm, useToast } from "./ui.jsx";

/* ================= Login ================= */
export function Login({ onLogin }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try { onLogin(await api.post("/auth/login", { email, password })); }
    catch (err) { setError(err.message); }
    finally { setBusy(false); }
  };
  return (
    <div className="login-wrap">
      <aside className="login-brand">
        <Logo tone="light" height={64} />
        <div className="login-hero">
          <div className="eyebrow light">AI Engineering Knowledge Platform</div>
          <h1>Proven precision,<br />now searchable.</h1>
          <p>Ask questions across Infinity Egypt’s technical manuals, standards, datasheets and web sources — every answer is grounded in, and linked to, its original passage.</p>
          <ul className="login-points">
            <li><b>Cited answers</b><span>Each statement links to the exact page or section.</span></li>
            <li><b>Verified vs. inferred</b><span>Evidence is clearly separated from reasoning.</span></li>
            <li><b>English &amp; Arabic</b><span>Ask and read in either language.</span></li>
          </ul>
        </div>
        <div className="login-foot">Exclusive partner for ZERA test systems &amp; EDMI smart meters · <a href="https://infinity-egypt.com" target="_blank" rel="noreferrer noopener">infinity-egypt.com</a></div>
      </aside>
      <main className="login-side">
        <form className="login-card" onSubmit={submit}>
          <div className="login-card-logo"><Logo height={46} /></div>
          <h2>Sign in</h2>
          <p className="muted small">Use your Infinity Egypt platform account.</p>
          <label className="field"><span className="field-label">Email</span>
            <input type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus /></label>
          <label className="field"><span className="field-label">Password</span>
            <input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
          {error && <p className="error-text" role="alert">{error}</p>}
          <button className="btn primary block lg" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
          <p className="hint">Access is limited to authorised Infinity Egypt staff. Activity is audited.</p>
        </form>
        <p className="login-copy">© {new Date().getFullYear()} Infinity Egypt. All rights reserved.</p>
      </main>
    </div>
  );
}

/** Brand logo. variant: "full" (lockup), "mark" (icon only); tone: "dark" text on light bg, "light" text for dark bg. */
export function Logo({ variant = "full", tone = "dark", height = 40 }) {
  const src = variant === "mark" ? "/assets/mark.png" : tone === "light" ? "/assets/logo-white.png" : "/assets/logo.png";
  return <img className="logo" src={src} height={height} alt="Infinity Egypt — Proven Precision" style={{ height, width: "auto" }} />;
}

/* ================= Assistant ================= */
const SUGGESTIONS = [
  "What communication interfaces does the Mk6ESL support?",
  "Summarise the accuracy class requirements mentioned in the documents.",
  "ما هي واجهات الاتصال المتاحة في العداد؟",
];

export function Assistant({ user }) {
  const toast = useToast();
  const [convs, setConvs] = useState([]);
  const [active, setActive] = useState(null);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [cite, setCite] = useState(null);
  const [confirm, confirmNode] = useConfirm();
  const endRef = useRef(null);

  const loadConvs = () => api.get("/conversations").then(setConvs).catch(() => {});
  useEffect(() => { loadConvs(); }, []);
  useEffect(() => { endRef.current && endRef.current.scrollIntoView({ behavior: "smooth", block: "end" }); }, [messages, busy]);

  const open = async (id) => {
    setCite(null);
    if (!id) { setActive(null); setMessages([]); return; }
    try { const c = await api.get("/conversations/" + id); setActive(id); setMessages(c.messages); }
    catch (e) { toast(e.message, "error"); }
  };

  const send = async (text) => {
    const q = (text ?? input).trim();
    if (!q || busy) return;
    setInput(""); setBusy(true);
    setMessages((m) => [...m, { id: "tmp", role: "user", content: q }]);
    try {
      const res = await api.post("/chat", { question: q, conversation_id: active });
      setActive(res.conversation_id);
      setMessages((m) => [...m, res.message]);
      loadConvs();
    } catch (e) {
      setMessages((m) => [...m, { id: "err" + Date.now(), role: "error", content: e.message }]);
    } finally { setBusy(false); }
  };

  const remove = (id) => confirm("Delete this conversation?", async () => {
    await api.del("/conversations/" + id).catch((e) => toast(e.message, "error"));
    if (id === active) open(null);
    loadConvs();
  });

  return (
    <div className={"assistant" + (cite ? " with-drawer" : "")}>
      <section className="conv-list" aria-label="Conversations">
        <button className="btn primary block" onClick={() => open(null)}>+ New conversation</button>
        <div className="conv-items">
          {convs.length === 0 && <p className="hint pad">No conversations yet.</p>}
          {convs.map((c) => (
            <div key={c.id} className={"conv-item" + (c.id === active ? " active" : "")}>
              <button onClick={() => open(c.id)} title={c.title} dir="auto"><span>{c.title}</span><small>{ago(c.updated_at)}</small></button>
              <button className="icon-btn sm" onClick={() => remove(c.id)} aria-label="Delete conversation">×</button>
            </div>
          ))}
        </div>
      </section>

      <section className="chat">
        <div className="chat-scroll">
          {messages.length === 0 && (
            <div className="chat-empty">
              <Logo variant="mark" height={56} />
              <div className="eyebrow accent">INFINITY AI</div>
              <h2>Ask your engineering knowledge base</h2>
              <p className="muted">Answers are generated only from documents, websites and APIs you are cleared to access, and each statement links to its source.</p>
              <div className="chips">{SUGGESTIONS.map((s) => <button key={s} className="chip" dir="auto" onClick={() => send(s)}>{s}</button>)}</div>
            </div>
          )}
          {messages.map((m) => <Message key={m.id} m={m} onCite={setCite} />)}
          {busy && <div className="msg assistant"><span className="ai-avatar"><Logo variant="mark" height={20} /></span><div className="bubble typing"><Spinner label="Searching sources and drafting an answer…" /></div></div>}
          <div ref={endRef} />
        </div>
        <form className="composer" onSubmit={(e) => { e.preventDefault(); send(); }}>
          <textarea dir="auto" rows={2} value={input} placeholder="Ask a technical question (English or Arabic)…" maxLength={4000}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />
          <button className="btn primary" disabled={busy || input.trim().length < 2}>Ask</button>
        </form>
        <p className="disclaimer">AI-generated from retrieved sources. Verify critical values against the cited original before use.</p>
      </section>
      {cite && <CitationDrawer citation={cite} onClose={() => setCite(null)} />}
      {confirmNode}
    </div>
  );
}

function Message({ m, onCite }) {
  if (m.role === "user") return <div className="msg user"><div className="bubble" dir="auto">{m.content}</div></div>;
  if (m.role === "error") return <div className="msg assistant"><span className="ai-avatar err">!</span><div className="bubble error" role="alert">{m.content}</div></div>;
  const notFound = m.meta && m.meta.found === false;
  return (
    <div className="msg assistant">
      <span className="ai-avatar"><Logo variant="mark" height={20} /></span>
      <div className={"bubble" + (notFound ? " nf" : "")}>
        <Markdown text={m.content} citations={m.citations} onCite={onCite} />
        {m.citations && m.citations.length > 0 && (
          <div className="sources">
            <div className="eyebrow">Sources</div>
            {m.citations.map((c) => (
              <button key={c.id} className="source-row" onClick={() => onCite(c)}>
                <span className="cite">{c.id}</span><span className="src-title" dir="auto">{c.document_title}</span><span className="src-loc">{c.location}</span>
              </button>
            ))}
          </div>
        )}
        {m.meta && m.meta.model && <div className="msg-meta">{m.meta.provider} · {m.meta.model} · {m.meta.evidence_retrieved} passages retrieved</div>}
      </div>
    </div>
  );
}

/* ================= Research (retrieval workspace) ================= */
export function Research() {
  const toast = useToast();
  const [q, setQ] = useState("");
  const [results, setResults] = useState(null);
  const [busy, setBusy] = useState(false);
  const [cite, setCite] = useState(null);
  const run = async (e) => {
    e.preventDefault();
    if (q.trim().length < 2) return;
    setBusy(true);
    try { setResults((await api.post("/search", { query: q.trim(), limit: 15 })).results); }
    catch (err) { toast(err.message, "error"); }
    finally { setBusy(false); }
  };
  const warn = results && results.some((r) => (r.channels || []).some((c) => c.startsWith("semantic-unavailable")));
  return (
    <div className={"page" + (cite ? " with-drawer" : "")}>
      <PageHeader title="Research" subtitle="Hybrid search across every source you can access — semantic, keyword and exact part-number matching, fused by rank. No AI generation, just evidence." />
      <form className="searchbar" onSubmit={run}>
        <input dir="auto" value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. OBIS 1.0.1.8.0.255, Mk6ESL optical port, IEC 62053-22…" />
        <button className="btn primary" disabled={busy}>{busy ? "Searching…" : "Search"}</button>
      </form>
      {warn && <div className="banner warn">Semantic search is unavailable right now (embedding service or Qdrant not ready); showing keyword and exact matches only.</div>}
      {results && results.length === 0 && <Empty title="No matching passages">Try different terms, or check the Library for sources you can access.</Empty>}
      <div className="results">
        {(results || []).map((r, i) => (
          <article key={r.chunk_id} className="result" onClick={() => setCite({ ...r, id: null })}>
            <div className="result-head">
              <span className="rank">{i + 1}</span>
              <strong dir="auto">{r.document_title}</strong>
              <span className="muted">{r.location}</span>
              <span className="spacer" />
              {r.channels.filter((c) => !c.startsWith("semantic-unavailable")).map((c) => <Badge key={c} tone={c === "exact" ? "warn" : c === "keyword" ? "info" : "ok"}>{c}</Badge>)}
            </div>
            <p dir="auto">{r.passage.length > 420 ? r.passage.slice(0, 420) + "…" : r.passage}</p>
          </article>
        ))}
      </div>
      {cite && <CitationDrawer citation={cite} onClose={() => setCite(null)} />}
    </div>
  );
}
