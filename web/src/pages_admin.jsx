import { useState } from "react";
import { api, ago, fmtDate } from "./api.js";
import { Badge, Empty, Field, Modal, PageHeader, Spinner, StatusBadge, VisibilityBadge, useConfirm, usePolling, useToast } from "./ui.jsx";

/* ================= Sources ================= */
const blankWebsite = { name: "", visibility: "public", schedule: "", urls: "https://", domains: "", max_pages: 50, max_depth: 2, include: "", exclude: "", download: true };
const blankApi = { name: "", visibility: "internal", schedule: "", url: "https://", auth: "none", header_name: "X-API-Key", secret: "", records_path: "", id_field: "", title_field: "", format: "auto" };
const lines = (t) => t.split("\n").map((x) => x.trim()).filter(Boolean);

export function Sources() {
  const toast = useToast();
  const [sources, setSources] = useState(null);
  const [edit, setEdit] = useState(null); // {kind, id?, form}
  const [tests, setTests] = useState({});
  const [confirm, confirmNode] = useConfirm();
  const load = () => api.get("/sources").then(setSources).catch((e) => toast(e.message, "error"));
  usePolling(load, 4000, [], sources === null || sources.some((s) => s.status === "syncing"));

  const test = async (s) => {
    setTests((t) => ({ ...t, [s.id]: { busy: true } }));
    try {
      const res = await api.post(`/sources/${s.id}/test`);
      setTests((t) => ({ ...t, [s.id]: res }));
    } catch (e) { setTests((t) => ({ ...t, [s.id]: { ok: false, message: e.message } })); }
  };
  const sync = async (s) => { try { await api.post(`/sources/${s.id}/sync`); toast("Synchronisation queued — follow it in Ingestion", "ok"); load(); } catch (e) { toast(e.message, "error"); } };
  const toggle = async (s) => { try { await api.patch("/sources/" + s.id, { enabled: !s.enabled }); load(); } catch (e) { toast(e.message, "error"); } };
  const remove = (s) => confirm(`Delete source “${s.name}” and remove all of its indexed documents and passages?`, async () => {
    try { const r = await api.del("/sources/" + s.id); toast(`Source removed (${r.points_removed} passages deleted)`, "ok"); load(); } catch (e) { toast(e.message, "error"); }
  });
  const openEdit = (s) => {
    const c = s.config;
    const form = s.kind === "website"
      ? { name: s.name, visibility: s.visibility, schedule: s.schedule_minutes || "", urls: c.start_urls.join("\n"), domains: c.allowed_domains.join("\n"), max_pages: c.max_pages, max_depth: c.max_depth, include: (c.include_patterns || []).join("\n"), exclude: (c.exclude_patterns || []).join("\n"), download: c.download_documents }
      : { name: s.name, visibility: s.visibility, schedule: s.schedule_minutes || "", url: c.url, auth: c.auth, header_name: c.header_name, secret: "", records_path: c.records_path, id_field: c.id_field, title_field: c.title_field, format: c.format };
    setEdit({ kind: s.kind, id: s.id, form, hasSecret: s.has_secret });
  };

  return (
    <div className="page">
      <PageHeader title="Knowledge sources" subtitle="Websites and REST/JSON/XML APIs that are crawled or polled into the index. Read-only; robots.txt is always honoured.">
        <button className="btn primary" onClick={() => setEdit({ kind: "website", form: { ...blankWebsite } })}>+ Website</button>
        <button className="btn primary" onClick={() => setEdit({ kind: "api", form: { ...blankApi } })}>+ API</button>
      </PageHeader>
      <div className="banner info">Uploaded files are managed in the <a href="#/library">Library</a>. Direct database connectors are not implemented in this version.</div>
      {!sources ? <Spinner /> : sources.length === 0 ? <Empty title="No sources configured">Add a manufacturer website or an API to start building knowledge automatically.</Empty> : (
        <div className="cards">{sources.map((s) => {
          const t = tests[s.id];
          const docs = Object.values(s.documents).reduce((a, b) => a + b, 0);
          return (
            <div key={s.id} className={"card source" + (s.enabled ? "" : " disabled")}>
              <div className="card-head"><div><h3 dir="auto">{s.name}</h3><div className="muted small break">{s.kind === "website" ? s.config.start_urls[0] : s.config.url}</div></div>
                <div className="row-gap"><Badge tone="neutral">{s.kind}</Badge><VisibilityBadge value={s.visibility} /><StatusBadge status={s.enabled ? s.status : "disabled"} /></div></div>
              <dl className="meta compact">
                <dt>Documents</dt><dd>{docs} {s.documents.failed ? <span className="error-text">({s.documents.failed} failed)</span> : null}</dd>
                <dt>Last sync</dt><dd>{s.last_sync_at ? `${ago(s.last_sync_at)} (${fmtDate(s.last_sync_at)})` : "never"}</dd>
                <dt>Schedule</dt><dd>{s.schedule_minutes ? `every ${s.schedule_minutes} min` : "manual only"}</dd>
                {s.kind === "api" && <><dt>Credential</dt><dd>{s.has_secret ? "stored encrypted" : "none"}</dd></>}
              </dl>
              {s.last_error && <div className="banner bad small">{s.last_error}</div>}
              {t && (t.busy ? <Spinner label="Testing…" /> : <div className={"banner small " + (t.ok ? "ok" : "bad")}>{t.message}{t.title ? ` — “${t.title}”` : ""}{t.links_found != null ? ` · ${t.links_found} links` : ""}{t.sample && t.sample.length > 0 && <pre className="pre">{t.sample.join("\n")}</pre>}</div>)}
              <div className="row-gap wrap">
                <button className="btn sm" onClick={() => test(s)}>Test</button>
                <button className="btn sm primary" disabled={!s.enabled || s.status === "syncing"} onClick={() => sync(s)}>Sync now</button>
                <button className="btn sm" onClick={() => toggle(s)}>{s.enabled ? "Disable" : "Enable"}</button>
                <button className="btn sm" onClick={() => openEdit(s)}>Edit</button>
                <button className="btn sm danger-o" onClick={() => remove(s)}>Delete</button>
              </div>
            </div>
          );
        })}</div>
      )}
      {edit && <SourceForm state={edit} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); load(); }} />}
      {confirmNode}
    </div>
  );
}

function SourceForm({ state, onClose, onSaved }) {
  const toast = useToast();
  const [f, setF] = useState(state.form);
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  const isWeb = state.kind === "website";
  const save = async () => {
    const schedule = f.schedule ? parseInt(f.schedule, 10) : null;
    const config = isWeb
      ? { start_urls: lines(f.urls), allowed_domains: lines(f.domains), max_pages: +f.max_pages, max_depth: +f.max_depth, include_patterns: lines(f.include), exclude_patterns: lines(f.exclude), download_documents: f.download }
      : { url: f.url.trim(), auth: f.auth, header_name: f.header_name, records_path: f.records_path, id_field: f.id_field, title_field: f.title_field, format: f.format };
    setBusy(true);
    try {
      if (state.id) {
        await api.patch("/sources/" + state.id, { name: f.name, config, visibility: f.visibility, schedule_minutes: schedule, clear_schedule: !schedule, secret: f.secret || undefined });
      } else {
        await api.post("/sources", { name: f.name, kind: state.kind, config, visibility: f.visibility, schedule_minutes: schedule, secret: f.secret || undefined });
      }
      toast("Source saved", "ok"); onSaved();
    } catch (e) { toast(e.message, "error"); } finally { setBusy(false); }
  };
  return (
    <Modal title={(state.id ? "Edit " : "Add ") + (isWeb ? "website" : "API") + " source"} onClose={onClose} wide
      footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={busy || !f.name.trim()} onClick={save}>{busy ? "Saving…" : "Save source"}</button></>}>
      <div className="form-grid">
        <Field label="Name"><input value={f.name} onChange={set("name")} dir="auto" /></Field>
        <Field label="Visibility" hint="Who may retrieve content from this source."><select value={f.visibility} onChange={set("visibility")}><option>public</option><option>internal</option><option>confidential</option></select></Field>
        {isWeb ? (
          <>
            <Field label="Start URLs (one per line)"><textarea rows={3} value={f.urls} onChange={set("urls")} /></Field>
            <Field label="Allowed domains (optional)" hint="Defaults to the start URLs’ domains."><textarea rows={3} value={f.domains} onChange={set("domains")} /></Field>
            <Field label="Max pages (≤ 500)"><input type="number" min="1" max="500" value={f.max_pages} onChange={set("max_pages")} /></Field>
            <Field label="Max link depth (≤ 6)"><input type="number" min="0" max="6" value={f.max_depth} onChange={set("max_depth")} /></Field>
            <Field label="Only URLs matching (regex, one per line)"><textarea rows={2} value={f.include} onChange={set("include")} /></Field>
            <Field label="Skip URLs matching (regex, one per line)"><textarea rows={2} value={f.exclude} onChange={set("exclude")} /></Field>
            <label className="check"><input type="checkbox" checked={f.download} onChange={set("download")} /> Discover and download linked PDF / DOCX / XLSX / PPTX / CSV files</label>
          </>
        ) : (
          <>
            <Field label="Endpoint URL (GET)"><input value={f.url} onChange={set("url")} /></Field>
            <Field label="Response format"><select value={f.format} onChange={set("format")}><option value="auto">auto-detect</option><option value="json">JSON</option><option value="xml">XML</option></select></Field>
            <Field label="Authentication"><select value={f.auth} onChange={set("auth")}><option value="none">none</option><option value="bearer">Bearer token</option><option value="header">Custom header</option></select></Field>
            {f.auth === "header" && <Field label="Header name"><input value={f.header_name} onChange={set("header_name")} /></Field>}
            {f.auth !== "none" && <Field label={state.hasSecret ? "Token / key (leave blank to keep the stored one)" : "Token / key"} hint="Stored encrypted; never shown again."><input type="password" autoComplete="new-password" value={f.secret} onChange={set("secret")} /></Field>}
            <Field label="Records path" hint="Dotted path to the list of records, e.g. data.items"><input value={f.records_path} onChange={set("records_path")} /></Field>
            <Field label="ID field (optional)"><input value={f.id_field} onChange={set("id_field")} /></Field>
            <Field label="Title field (optional)"><input value={f.title_field} onChange={set("title_field")} /></Field>
          </>
        )}
        <Field label="Automatic sync every (minutes)" hint="Empty = manual only. Minimum 15."><input type="number" min="15" value={f.schedule} onChange={set("schedule")} /></Field>
      </div>
      {isWeb && <p className="hint">The crawler honours robots.txt (an unreachable robots.txt blocks crawling), waits at least one second between requests, and refuses private-network addresses.</p>}
    </Modal>
  );
}

/* ================= Users ================= */
export function Users({ me }) {
  const toast = useToast();
  const [users, setUsers] = useState(null);
  const [form, setForm] = useState(null);
  const [reset, setReset] = useState(null);
  const [confirm, confirmNode] = useConfirm();
  const load = () => api.get("/users").then(setUsers).catch((e) => toast(e.message, "error"));
  usePolling(load, 0, [], false);
  const patch = async (u, body) => { try { await api.patch("/users/" + u.id, body); load(); } catch (e) { toast(e.message, "error"); } };
  const create = async () => { try { await api.post("/users", form); setForm(null); toast("User created", "ok"); load(); } catch (e) { toast(e.message, "error"); } };
  const doReset = async () => { try { await api.patch("/users/" + reset.user.id, { password: reset.password }); setReset(null); toast("Password reset; user was signed out", "ok"); } catch (e) { toast(e.message, "error"); } };
  const remove = (u) => confirm(`Delete ${u.email}? Their conversations and reports are deleted too. Consider disabling instead.`, async () => { try { await api.del("/users/" + u.id); load(); } catch (e) { toast(e.message, "error"); } });
  return (
    <div className="page">
      <PageHeader title="Users & permissions" subtitle="Roles control what people can do; clearance controls which sources they can read.">
        <button className="btn primary" onClick={() => setForm({ email: "", name: "", password: "", role: "engineer" })}>+ New user</button>
      </PageHeader>
      <div className="banner info small"><strong>Viewer</strong>: public sources, read-only. <strong>Engineer</strong>: public + internal; upload, compare, report. <strong>Admin</strong>: everything incl. confidential, sources, users, settings.</div>
      {!users ? <Spinner /> : (
        <div className="table-wrap"><table className="table">
          <thead><tr><th>User</th><th>Role</th><th>Status</th><th>Last sign-in</th><th /></tr></thead>
          <tbody>{users.map((u) => (
            <tr key={u.id}>
              <td><strong dir="auto">{u.name || u.email}</strong><div className="muted small">{u.email}</div></td>
              <td><select value={u.role} onChange={(e) => patch(u, { role: e.target.value })}><option>admin</option><option>engineer</option><option>viewer</option></select></td>
              <td><Badge tone={u.is_active ? "ok" : "bad"}>{u.is_active ? "active" : "disabled"}</Badge></td>
              <td className="muted">{ago(u.last_login_at)}</td>
              <td className="actions">
                <button className="btn sm" onClick={() => patch(u, { is_active: !u.is_active })}>{u.is_active ? "Disable" : "Enable"}</button>
                <button className="btn sm" onClick={() => setReset({ user: u, password: "" })}>Reset password</button>
                {u.id !== me.id && <button className="btn sm danger-o" onClick={() => remove(u)}>Delete</button>}
              </td>
            </tr>
          ))}</tbody>
        </table></div>
      )}
      {form && (
        <Modal title="New user" onClose={() => setForm(null)} footer={<><button className="btn" onClick={() => setForm(null)}>Cancel</button><button className="btn primary" onClick={create}>Create</button></>}>
          <Field label="Email"><input type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} /></Field>
          <Field label="Full name"><input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} dir="auto" /></Field>
          <Field label="Initial password" hint="10+ characters, upper and lower case, and a digit."><input type="password" autoComplete="new-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} /></Field>
          <Field label="Role"><select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}><option>engineer</option><option>viewer</option><option>admin</option></select></Field>
        </Modal>
      )}
      {reset && (
        <Modal title={`Reset password — ${reset.user.email}`} onClose={() => setReset(null)} footer={<><button className="btn" onClick={() => setReset(null)}>Cancel</button><button className="btn primary" onClick={doReset}>Reset</button></>}>
          <Field label="New password"><input type="password" autoComplete="new-password" value={reset.password} onChange={(e) => setReset({ ...reset, password: e.target.value })} /></Field>
        </Modal>
      )}
      {confirmNode}
    </div>
  );
}

/* ================= Settings (AI providers) ================= */
const PROVIDER_LABEL = { gemini: "Google Gemini", openai: "OpenAI", ollama: "Ollama (local model)" };

export function Settings() {
  const toast = useToast();
  const [s, setS] = useState(null);
  const [draft, setDraft] = useState({ models: {}, keys: {} });
  const [provider, setProvider] = useState("gemini");
  const [ollama, setOllama] = useState("");
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState(null);
  const [health, setHealth] = useState(null);
  const [keyMsg, setKeyMsg] = useState(null); // outcome of the last key replacement: { ok, text, warning, canForce }
  const load = () => api.get("/admin/settings/llm").then((x) => { setS(x); setProvider(x.provider); setOllama(x.ollama_base_url); }).catch((e) => toast(e.message, "error"));
  usePolling(() => { load(); api.get("/admin/health").then(setHealth).catch(() => {}); }, 0, [], false);
  // Replace ONE provider key via the explicit endpoint: verified with the provider, encrypted with the current key, one transaction.
  // Works even when the previously stored credential is unreadable. Returns true on success.
  const replaceKey = async (name, value, verify = true) => {
    setKeyMsg(null);
    try {
      const x = await api.post("/admin/settings/llm/credentials", { provider: name, api_key: value, verify });
      setS(x); setDraft((d) => ({ ...d, keys: { ...d.keys, [name]: "" } }));
      setKeyMsg({ ok: true, text: x.result.message, warning: x.result.warning });
      toast("API key saved", "ok");
      return true;
    } catch (e) {
      setKeyMsg({ ok: false, text: e.message, canForce: e.code === "validation_unavailable" });
      return false;
    }
  };
  const save = async () => {
    const newKey = provider !== "ollama" ? (draft.keys[provider] || "").trim() : "";
    try {
      // 1) non-secret settings (provider, model, Ollama URL). Never touches stored credentials.
      const x = await api.put("/admin/settings/llm", { provider, models: draft.models, ollama_base_url: ollama });
      setS(x); setDraft((d) => ({ models: {}, keys: d.keys }));
    } catch (e) { toast(e.message, "error"); return; }
    // 2) the new key, verified with the provider before anything is stored
    if (newKey) { await replaceKey(provider, newKey, true); } else { setKeyMsg(null); toast("Settings saved", "ok"); }
    setResult(null);
  };
  const test = async () => { setTesting(true); setResult(null); try { setResult(await api.post("/admin/settings/llm/test", { provider })); } catch (e) { setResult({ ok: false, error: e.message }); } finally { setTesting(false); } };
  const clearKey = async (name) => { try { setS(await api.put("/admin/settings/llm", { keys: { [name]: "" } })); toast("Key removed", "ok"); } catch (e) { toast(e.message, "error"); } };
  if (!s) return <div className="page"><Spinner /></div>;
  return (
    <div className="page">
      <PageHeader title="AI providers & integrations" subtitle="Switch the answer-generation model without editing code. Credentials are encrypted at rest and never sent back to the browser." />
      {s.credentials_unreadable && (
        <div className="banner bad" role="alert">
          <b>Saved credentials can’t be read.</b> {s.message} Enter the {PROVIDER_LABEL[provider]} API key below and press <b>Save</b>: the key is verified with the provider, stored with the current encryption key, and replaces the unreadable value (the old one is kept encrypted, set aside).
        </div>
      )}
      <div className="card">
        <h3>Answer generation (LLM)</h3>
        <p className="hint">Active configuration source: <b>{s.source === "gui" ? "saved in this interface" : "environment (.env) fallback"}</b></p>
        <div className="form-grid">
          <Field label="Active provider"><select value={provider} onChange={(e) => setProvider(e.target.value)}>{s.supported.map((p) => <option key={p} value={p}>{PROVIDER_LABEL[p]}</option>)}</select></Field>
          <Field label={`Model name (${PROVIDER_LABEL[provider]})`}><input value={draft.models[provider] ?? s.models[provider] ?? ""} onChange={(e) => setDraft({ ...draft, models: { ...draft.models, [provider]: e.target.value } })} /></Field>
          {provider !== "ollama" ? (
            <Field label={`${PROVIDER_LABEL[provider]} API key`} hint={s.keys[provider].configured ? `Stored: ${s.keys[provider].preview}` : s.credentials_unreadable ? "Stored key unreadable — re-enter it" : "Not configured"}>
              <input type="password" autoComplete="new-password" placeholder={s.keys[provider].configured ? "Leave blank to keep the stored key" : "Paste API key"} value={draft.keys[provider] ?? ""} onChange={(e) => setDraft({ ...draft, keys: { ...draft.keys, [provider]: e.target.value } })} />
            </Field>
          ) : (
            <Field label="Ollama base URL" hint="From inside Docker, the host machine is host.docker.internal."><input value={ollama} onChange={(e) => setOllama(e.target.value)} /></Field>
          )}
        </div>
        <div className="row-gap wrap">
          <button className="btn primary" onClick={save}>Save</button>
          <button className="btn" onClick={test} disabled={testing}>{testing ? "Testing…" : "Test connection"}</button>
          {provider !== "ollama" && s.keys[provider].configured && s.source === "gui" && <button className="btn danger-o" onClick={() => clearKey(provider)}>Remove stored key</button>}
        </div>
        {keyMsg && (
          <div className={"banner " + (keyMsg.ok ? "ok" : "bad")} role="status">
            {keyMsg.ok ? <><b>Key saved.</b> {keyMsg.text}{keyMsg.warning && <> <b>Note:</b> {keyMsg.warning}</>}</> : <><b>Key not saved.</b> {keyMsg.text}</>}
            {keyMsg.canForce && <> <button className="btn" onClick={() => replaceKey(provider, (draft.keys[provider] || "").trim(), false)}>Save without validating</button></>}
          </div>
        )}
        {result && <div className={"banner " + (result.ok ? "ok" : "bad")}>{result.ok ? `Connected to ${result.provider} / ${result.model} in ${result.latency_ms} ms.` : `Failed: ${result.error}`}</div>}
        <p className="hint">“Test connection” sends one tiny prompt using the <em>saved</em> settings, so save first.</p>
      </div>
      <div className="card">
        <h3>Other components</h3>
        <p className="hint">These are fixed in this version and shown for transparency; they are configured in <code>.env</code> and the Compose file.</p>
        <table className="table"><tbody>
          <tr><th>Embeddings</th><td>BAAI/bge-m3 (local, CPU)</td><td>{health ? <StatusBadge status={health.embedder.ok ? "ok" : health.embedder.status === "loading" ? "processing" : "error"} /> : "…"}</td></tr>
          <tr><th>Vector database</th><td>Qdrant · collection <code>{health ? health.qdrant.collection : "…"}</code></td><td>{health ? <StatusBadge status={health.qdrant.ok ? "ok" : "error"} /> : "…"}</td></tr>
          <tr><th>Web search / extraction services</th><td colSpan={2} className="muted">Not integrated. Use Website sources for crawling.</td></tr>
          <tr><th>OpenAI / other embedding providers</th><td colSpan={2} className="muted">Not available: changing embeddings would require re-indexing the whole collection.</td></tr>
        </tbody></table>
      </div>
    </div>
  );
}

/* ================= System: health, logs, audit ================= */
export function System() {
  const toast = useToast();
  const [h, setH] = useState(null);
  const [audit, setAudit] = useState(null);
  const [action, setAction] = useState("");
  const load = () => {
    api.get("/admin/health").then(setH).catch((e) => toast(e.message, "error"));
    api.get(`/admin/audit?action=${encodeURIComponent(action)}`).then(setAudit).catch(() => {});
  };
  usePolling(load, 10000, [action]);
  const mirror = async () => { try { await api.post("/admin/adopt-legacy"); toast("Mirror job queued (read-only)", "ok"); } catch (e) { toast(e.message, "error"); } };
  const row = (name, ok, detail) => <div className="health-row"><StatusBadge status={ok ? "ok" : "error"} /><strong>{name}</strong><span className="muted">{detail}</span></div>;
  return (
    <div className="page">
      <PageHeader title="System & logs" subtitle="Operational health and the audit trail. Refreshes every 10 seconds." />
      {!h ? <Spinner /> : (
        <div className="card">
          <h3>Component health</h3>
          {row("PostgreSQL", h.postgres.ok, h.postgres.ok ? `${h.postgres.ms} ms` : h.postgres.error)}
          {row("Redis", h.redis.ok, h.redis.ok ? "rate limiting & heartbeat" : h.redis.error)}
          {row("Background worker", h.worker.ok, h.worker.detail)}
          {row("Embedding service", h.embedder.ok, h.embedder.detail || h.embedder.status)}
          {row("Qdrant", h.qdrant.ok, h.qdrant.ok ? `${h.qdrant.url} · ${h.qdrant.collection}: ${h.qdrant.points} points, ${h.qdrant.mirrored_chunks} mirrored for keyword search` : h.qdrant.error)}
          {row("LLM provider", h.llm.configured, `${h.llm.provider} / ${h.llm.model}${h.llm.configured ? "" : " — no API key configured"}`)}
          {h.qdrant.ok && h.qdrant.points > h.qdrant.mirrored_chunks && (
            <div className="banner warn">Qdrant holds {h.qdrant.points - h.qdrant.mirrored_chunks} point(s) not yet mirrored into PostgreSQL (needed for keyword search and citations).
              <button className="btn sm" onClick={mirror}>Run read-only mirror</button></div>
          )}
        </div>
      )}
      <div className="card">
        <div className="card-head"><h3>Audit log</h3>
          <select value={action} onChange={(e) => setAction(e.target.value)} aria-label="Filter by action"><option value="">All actions</option>
            {["login", "login_failed", "documents_uploaded", "document_deleted", "source_created", "source_updated", "source_deleted", "llm_settings_changed", "user_created", "user_updated", "chat_query", "report_generated", "comparison_generated"].map((a) => <option key={a}>{a}</option>)}</select></div>
        {!audit ? <Spinner /> : (
          <div className="table-wrap"><table className="table dense">
            <thead><tr><th>Time</th><th>User</th><th>Action</th><th>Target</th><th>IP</th></tr></thead>
            <tbody>{audit.map((a) => <tr key={a.id}><td className="muted nowrap">{fmtDate(a.ts)}</td><td>{a.user || "—"}</td><td><Badge tone={a.action.includes("fail") ? "bad" : "neutral"}>{a.action}</Badge></td><td className="break" dir="auto">{a.target || ""}</td><td className="muted">{a.ip || ""}</td></tr>)}</tbody>
          </table></div>
        )}
      </div>
    </div>
  );
}
