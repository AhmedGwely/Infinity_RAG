import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { api, fmtDate } from "./api.js";

/* ---------- toasts ---------- */
const ToastCtx = createContext(() => {});
export const useToast = () => useContext(ToastCtx);
export function ToastProvider({ children }) {
  const [items, setItems] = useState([]);
  const push = useCallback((message, kind = "info") => {
    const id = Math.random().toString(36).slice(2);
    setItems((l) => [...l, { id, message, kind }]);
    setTimeout(() => setItems((l) => l.filter((t) => t.id !== id)), kind === "error" ? 7000 : 3500);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {items.map((t) => <div key={t.id} className={"toast " + t.kind}>{t.message}</div>)}
      </div>
    </ToastCtx.Provider>
  );
}

/* ---------- small components ---------- */
export const Spinner = ({ label }) => <div className="spinner-row"><span className="spinner" />{label && <span>{label}</span>}</div>;
export const Empty = ({ title, children }) => <div className="empty"><strong>{title}</strong><div>{children}</div></div>;
export const Badge = ({ tone = "neutral", children }) => <span className={"badge " + tone}>{children}</span>;
const STATUS_TONE = { ready: "ok", ok: "ok", done: "ok", idle: "neutral", queued: "warn", processing: "info", running: "info", syncing: "info", failed: "bad", error: "bad" };
export const StatusBadge = ({ status }) => <Badge tone={STATUS_TONE[status] || "neutral"}>{status}</Badge>;
export const VisibilityBadge = ({ value }) => <Badge tone={value === "confidential" ? "bad" : value === "internal" ? "warn" : "ok"}>{value}</Badge>;

export function PageHeader({ title, subtitle, children }) {
  return (
    <header className="page-head">
      <div><h1>{title}</h1>{subtitle && <p className="muted">{subtitle}</p>}</div>
      <div className="page-actions">{children}</div>
    </header>
  );
}

export function Field({ label, hint, children }) {
  return <label className="field"><span className="field-label">{label}</span>{children}{hint && <span className="hint">{hint}</span>}</label>;
}

export function Modal({ title, onClose, children, wide, footer }) {
  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={"modal" + (wide ? " wide" : "")} role="dialog" aria-modal="true" aria-label={title}>
        <div className="modal-head"><h2>{title}</h2><button className="icon-btn" onClick={onClose} aria-label="Close">×</button></div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}

export function useConfirm() {
  const [state, setState] = useState(null);
  const ask = (message, onYes, { danger = true, yes = "Delete" } = {}) => setState({ message, onYes, danger, yes });
  const node = state && (
    <Modal title="Please confirm" onClose={() => setState(null)}
      footer={<><button className="btn" onClick={() => setState(null)}>Cancel</button>
        <button className={"btn " + (state.danger ? "danger" : "primary")} onClick={() => { const f = state.onYes; setState(null); f(); }}>{state.yes}</button></>}>
      <p>{state.message}</p>
    </Modal>
  );
  return [ask, node];
}

/** Polls `load` every `ms` while `active` is true (or always when active is undefined). */
export function usePolling(load, ms, deps = [], active = true) {
  const ref = useRef(load);
  ref.current = load;
  useEffect(() => {
    let alive = true;
    ref.current();
    if (!active) return undefined;
    const t = setInterval(() => alive && ref.current(), ms);
    return () => { alive = false; clearInterval(t); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ms, active, ...deps]);
}

/* ---------- markdown (safe: React escapes everything; no HTML injection) ---------- */
const INLINE = /(\*\*[^*]+\*\*|`[^`]+`|\[S\d+\])/g;

export function Inline({ text, citations, onCite }) {
  return text.split(INLINE).map((part, i) => {
    if (/^\*\*[^*]+\*\*$/.test(part)) return <strong key={i}>{part.slice(2, -2)}</strong>;
    if (/^`[^`]+`$/.test(part)) return <code key={i}>{part.slice(1, -1)}</code>;
    const m = /^\[(S\d+)\]$/.exec(part);
    if (m) {
      const c = (citations || []).find((x) => x.id === m[1]);
      return c
        ? <button key={i} className="cite" title={`${c.document_title} — ${c.location}`} onClick={() => onCite && onCite(c)}>{m[1]}</button>
        : <span key={i} className="cite dead">{m[1]}</span>;
    }
    return part;
  });
}

export function Markdown({ text, citations, onCite }) {
  const lines = (text || "").replace(/\r/g, "").split("\n");
  const out = [];
  let i = 0;
  const inl = (t) => <Inline text={t} citations={citations} onCite={onCite} />;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    if (h) { const L = Math.min(h[1].length + 1, 5); const Tag = "h" + L; out.push(<Tag key={i} dir="auto">{inl(h[2])}</Tag>); i++; continue; }
    if (/^\s*\|/.test(line) && /^\s*\|?[\s:-]+\|/.test(lines[i + 1] || "")) {
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) { rows.push(lines[i]); i++; }
      const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const head = cells(rows[0]);
      out.push(
        <div className="table-wrap" key={"t" + i}><table className="md-table"><thead><tr>{head.map((c, k) => <th key={k} dir="auto">{inl(c)}</th>)}</tr></thead>
          <tbody>{rows.slice(2).map((r, k) => <tr key={k}>{cells(r).map((c, j) => <td key={j} dir="auto">{inl(c)}</td>)}</tr>)}</tbody></table></div>
      );
      continue;
    }
    if (/^\s*([-*]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]/.test(line);
      const items = [];
      while (i < lines.length && /^\s*([-*]|\d+[.)])\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*([-*]|\d+[.)])\s+/, "")); i++; }
      const Tag = ordered ? "ol" : "ul";
      out.push(<Tag key={"l" + i} dir="auto">{items.map((t, k) => <li key={k}>{inl(t)}</li>)}</Tag>);
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|\s*([-*]|\d+[.)])\s+|\s*\|)/.test(lines[i])) { para.push(lines[i]); i++; }
    out.push(<p key={"p" + i} dir="auto">{inl(para.join(" "))}</p>);
  }
  return <div className="md">{out}</div>;
}

/* ---------- citation inspector ---------- */
export function CitationDrawer({ citation, onClose }) {
  const [ctx, setCtx] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => {
    setCtx(null); setErr(null);
    api.get("/chunks/" + citation.chunk_id).then(setCtx).catch((e) => setErr(e.message));
  }, [citation.chunk_id]);
  const loc = citation.locator || {};
  const page = loc.type === "pdf" && loc.pdf_pages && loc.pdf_pages[0];
  const fileUrl = `/api/documents/${citation.document_id}/file` + (page ? `#page=${page}` : "");
  return (
    <aside className="drawer" aria-label="Source passage">
      <div className="drawer-head">
        <div><div className="eyebrow">{citation.id ? `Source ${citation.id}` : "Passage"}</div><h3 dir="auto">{citation.document_title}</h3></div>
        <button className="icon-btn" onClick={onClose} aria-label="Close source panel">×</button>
      </div>
      <dl className="meta">
        <dt>Location</dt><dd>{citation.location}</dd>
        {citation.section && <><dt>Section</dt><dd dir="auto">{citation.section}</dd></>}
        <dt>Type</dt><dd>{(citation.doc_type || "").toUpperCase()}</dd>
        <dt>Indexed</dt><dd>{fmtDate(citation.indexed_at)}</dd>
        <dt>Visibility</dt><dd><VisibilityBadge value={citation.visibility} /></dd>
        {citation.channels && citation.channels.length > 0 && <><dt>Found by</dt><dd>{citation.channels.filter((c) => !c.startsWith("semantic-unavailable")).join(", ")}</dd></>}
      </dl>
      <div className="drawer-actions">
        {citation.has_file && <a className="btn primary" href={fileUrl} target="_blank" rel="noreferrer">Open original file</a>}
        {citation.uri && /^https?:/.test(citation.uri) && <a className="btn" href={citation.uri} target="_blank" rel="noreferrer noopener">Open web source ↗</a>}
        {!citation.has_file && !(citation.uri && /^https?:/.test(citation.uri)) && <span className="hint">The original file is not stored on this server; an administrator can attach it from the Library.</span>}
      </div>
      <h4>Cited passage</h4>
      {err && <p className="error-text">{err}</p>}
      {!ctx && !err && <Spinner label="Loading context…" />}
      {ctx && ctx.context.map((c) => (
        <blockquote key={c.id} className={c.is_cited ? "passage cited" : "passage"} dir="auto">
          <div className="passage-loc">{c.is_cited ? "Cited passage" : "Adjacent context"} · {c.location}</div>
          <div className="passage-text">{c.text}</div>
        </blockquote>
      ))}
    </aside>
  );
}
