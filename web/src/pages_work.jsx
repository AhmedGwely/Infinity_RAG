import { Fragment, useRef, useState } from "react";
import { api, ago, fmtDate, fmtSize } from "./api.js";
import { Badge, CitationDrawer, Empty, Field, Markdown, Modal, PageHeader, Spinner, StatusBadge, VisibilityBadge, useConfirm, usePolling, useToast } from "./ui.jsx";

const ACCEPT = ".pdf,.docx,.xlsx,.xlsm,.pptx,.csv,.tsv,.txt,.md,.log,.json,.xml,.html,.htm";

/* ================= Library ================= */
export function Library({ user }) {
  const toast = useToast();
  const [docs, setDocs] = useState(null);
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const [visibility, setVisibility] = useState("internal");
  const [uploading, setUploading] = useState(false);
  const [drag, setDrag] = useState(false);
  const [detail, setDetail] = useState(null);
  const [confirm, confirmNode] = useConfirm();
  const fileInput = useRef(null);
  const canUpload = user.role !== "viewer";
  const levels = user.role === "admin" ? ["public", "internal", "confidential"] : ["public", "internal"];

  const load = () => api.get(`/documents?q=${encodeURIComponent(q)}&status=${status}`).then(setDocs).catch((e) => toast(e.message, "error"));
  const busyDocs = docs && docs.some((d) => d.status === "queued" || d.status === "processing");
  usePolling(load, 3000, [q, status], busyDocs || docs === null);

  const upload = async (files) => {
    if (!files.length) return;
    const form = new FormData();
    [...files].forEach((f) => form.append("files", f));
    form.append("visibility", visibility);
    setUploading(true);
    try {
      const res = await api.upload("/documents/upload", form);
      if (res.created.length) toast(`${res.created.length} file(s) queued for indexing`, "ok");
      res.rejected.forEach((r) => toast(`${r.filename}: ${r.reason}`, "error"));
      load();
    } catch (e) { toast(e.message, "error"); }
    finally { setUploading(false); if (fileInput.current) fileInput.current.value = ""; }
  };

  const remove = (d) => confirm(`Delete “${d.title}” and remove its passages from the index? This cannot be undone.`, async () => {
    try { await api.del("/documents/" + d.id); toast("Document deleted", "ok"); load(); } catch (e) { toast(e.message, "error"); }
  });
  const reindex = async (d) => { try { await api.post(`/documents/${d.id}/reindex`); toast("Re-index queued", "ok"); load(); } catch (e) { toast(e.message, "error"); } };

  return (
    <div className="page">
      <PageHeader title="Document library" subtitle="Everything searchable by the assistant. Upload manuals, datasheets, spreadsheets and presentations; files are parsed, embedded and indexed in the background.">
        <input className="search" placeholder="Filter by title…" value={q} onChange={(e) => setQ(e.target.value)} />
        <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status filter">
          <option value="">All statuses</option><option value="ready">Ready</option><option value="queued">Queued</option><option value="processing">Processing</option><option value="failed">Failed</option>
        </select>
      </PageHeader>

      {canUpload && (
        <div className={"dropzone" + (drag ? " drag" : "")}
          onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)}
          onDrop={(e) => { e.preventDefault(); setDrag(false); upload(e.dataTransfer.files); }}>
          <div>
            <strong>Drop files here</strong> or <button className="link" onClick={() => fileInput.current.click()}>browse</button>
            <div className="hint">PDF, DOCX, XLSX, PPTX, CSV, TXT, MD, JSON, XML, HTML · max 100 MB each</div>
          </div>
          <div className="dz-side">
            <label className="inline">Visibility
              <select value={visibility} onChange={(e) => setVisibility(e.target.value)}>{levels.map((l) => <option key={l}>{l}</option>)}</select>
            </label>
            {uploading && <Spinner label="Uploading…" />}
          </div>
          <input ref={fileInput} type="file" multiple accept={ACCEPT} hidden onChange={(e) => upload(e.target.files)} />
        </div>
      )}

      {docs === null && <Spinner label="Loading documents…" />}
      {docs && docs.length === 0 && <Empty title="No documents yet">{canUpload ? "Upload your first document above." : "No documents are available to your role."}</Empty>}
      {docs && docs.length > 0 && (
        <div className="table-wrap"><table className="table">
          <thead><tr><th>Title</th><th>Type</th><th>Status</th><th>Visibility</th><th>Passages</th><th>Origin</th><th>Updated</th><th /></tr></thead>
          <tbody>
            {docs.map((d) => (
              <tr key={d.id}>
                <td><button className="link strong" onClick={() => setDetail(d)} dir="auto">{d.title}</button>{d.legacy && <Badge tone="info">pre-existing index</Badge>}</td>
                <td>{(d.doc_type || "").toUpperCase()}</td>
                <td><StatusBadge status={d.status} />{d.error && <div className="error-text small" title={d.error}>{d.error.slice(0, 80)}</div>}</td>
                <td><VisibilityBadge value={d.visibility} /></td>
                <td>{d.chunk_count}</td>
                <td className="muted">{d.source_name || (d.legacy ? "legacy collection" : "upload")}</td>
                <td className="muted">{ago(d.updated_at)}</td>
                <td className="actions">
                  {d.has_file && <a className="btn sm" href={`/api/documents/${d.id}/file`} target="_blank" rel="noreferrer">Open</a>}
                  {d.uri && /^https?:/.test(d.uri) && <a className="btn sm" href={d.uri} target="_blank" rel="noreferrer noopener">Web ↗</a>}
                  {canUpload && !d.legacy && d.has_file && <button className="btn sm" onClick={() => reindex(d)}>Re-index</button>}
                  {canUpload && !d.legacy && <button className="btn sm danger-o" onClick={() => remove(d)}>Delete</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table></div>
      )}
      {detail && <DocDetail id={detail.id} user={user} onClose={() => setDetail(null)} onChanged={load} />}
      {confirmNode}
    </div>
  );
}

function DocDetail({ id, user, onClose, onChanged }) {
  const toast = useToast();
  const [d, setD] = useState(null);
  const [visibility, setVisibility] = useState(null);
  const load = () => api.get("/documents/" + id).then((x) => { setD(x); setVisibility(x.visibility); }).catch((e) => toast(e.message, "error"));
  usePolling(load, 5000, [id], false);
  const levels = user.role === "admin" ? ["public", "internal", "confidential"] : ["public", "internal"];
  const saveVisibility = async () => { try { await api.patch("/documents/" + id, { visibility }); toast("Visibility updated", "ok"); onChanged(); load(); } catch (e) { toast(e.message, "error"); } };
  const attach = async (file) => {
    if (!file) return;
    const form = new FormData(); form.append("file", file);
    try { await api.upload(`/documents/${id}/attach-file`, form); toast("Original file attached (index unchanged)", "ok"); onChanged(); load(); } catch (e) { toast(e.message, "error"); }
  };
  return (
    <Modal title={d ? d.title : "Document"} onClose={onClose} wide>
      {!d ? <Spinner /> : (
        <>
          <dl className="meta">
            <dt>Status</dt><dd><StatusBadge status={d.status} /></dd>
            <dt>Type</dt><dd>{(d.doc_type || "").toUpperCase()} {d.page_count ? `· ${d.page_count} pages/slides` : ""}</dd>
            <dt>Passages</dt><dd>{d.chunk_count}</dd>
            <dt>Size</dt><dd>{fmtSize(d.size_bytes)}</dd>
            <dt>Created</dt><dd>{fmtDate(d.created_at)}</dd>
            <dt>Last updated</dt><dd>{fmtDate(d.updated_at)}</dd>
            {d.last_seen_at && <><dt>Last seen at source</dt><dd>{fmtDate(d.last_seen_at)}</dd></>}
            {d.uri && <><dt>Source URL</dt><dd className="break">{d.uri}</dd></>}
            {d.origin_path && <><dt>Original path</dt><dd className="break">{d.origin_path}</dd></>}
          </dl>
          {d.error && <div className="banner bad">{d.error}</div>}
          {d.issues && d.issues.length > 0 && <><h4>Extraction notes</h4><ul className="plain">{d.issues.map((x, i) => <li key={i}>{x}</li>)}</ul></>}
          {user.role !== "viewer" && (
            <div className="row-gap">
              <Field label="Visibility (who may see this document)">
                <select value={visibility || ""} onChange={(e) => setVisibility(e.target.value)}>{levels.map((l) => <option key={l}>{l}</option>)}</select>
              </Field>
              <button className="btn" disabled={visibility === d.visibility} onClick={saveVisibility}>Save visibility</button>
            </div>
          )}
          {user.role === "admin" && d.legacy && !d.has_file && (
            <div className="banner info">
              The original PDF for this pre-existing document is not stored on the server, so citations cannot open it.
              Attach it here to enable “Open original file” (the index and its vectors are not changed).
              <div><input type="file" accept=".pdf" onChange={(e) => attach(e.target.files[0])} /></div>
            </div>
          )}
        </>
      )}
    </Modal>
  );
}

/* ================= Ingestion monitor ================= */
export function Ingestion({ user }) {
  const toast = useToast();
  const [jobs, setJobs] = useState(null);
  const [overview, setOverview] = useState(null);
  const [failed, setFailed] = useState([]);
  const [open, setOpen] = useState(null);
  const load = async () => {
    try {
      const [j, o, f] = await Promise.all([api.get("/jobs"), api.get("/overview"), api.get("/documents?status=failed")]);
      setJobs(j); setOverview(o); setFailed(f);
    } catch (e) { toast(e.message, "error"); }
  };
  usePolling(load, 3000, []);
  const retry = async (id) => { try { await api.post(`/jobs/${id}/retry`); load(); } catch (e) { toast(e.message, "error"); } };
  const tone = (s) => (s === "done" ? "ok" : s === "failed" ? "bad" : s === "running" ? "info" : "warn");
  return (
    <div className="page">
      <PageHeader title="Ingestion monitor" subtitle="Live view of background indexing, source synchronisation and failures." />
      {overview && (
        <div className="stats">
          <Stat label="Documents ready" value={overview.documents.ready || 0} />
          <Stat label="Indexed passages" value={overview.chunks} />
          <Stat label="Active jobs" value={overview.jobs_active} tone={overview.jobs_active ? "info" : ""} />
          <Stat label="Failed jobs" value={overview.jobs_failed} tone={overview.jobs_failed ? "bad" : ""} />
          <Stat label="Sources" value={overview.sources} />
        </div>
      )}
      {failed.length > 0 && (
        <div className="banner bad"><strong>{failed.length} document(s) failed to index.</strong>
          <ul className="plain">{failed.slice(0, 6).map((d) => <li key={d.id}><b dir="auto">{d.title}</b> — {d.error}</li>)}</ul></div>
      )}
      {!jobs ? <Spinner /> : jobs.length === 0 ? <Empty title="No jobs yet">Jobs appear here when you upload documents or synchronise a source.</Empty> : (
        <div className="table-wrap"><table className="table">
          <thead><tr><th>#</th><th>Task</th><th>Status</th><th>Progress / result</th><th>Started</th><th /></tr></thead>
          <tbody>{jobs.map((j) => (
            <Fragment key={j.id}>
              <tr>
                <td>{j.id}</td><td dir="auto">{j.label}</td>
                <td><Badge tone={tone(j.status)}>{j.status}</Badge>{j.attempts > 1 && <span className="muted small"> attempt {j.attempts}</span>}</td>
                <td className="muted">{j.error ? <span className="error-text">{j.error.slice(0, 140)}</span> : j.message}</td>
                <td className="muted">{ago(j.started_at || j.created_at)}</td>
                <td className="actions">
                  {Object.keys(j.stats || {}).length > 0 && <button className="btn sm" onClick={() => setOpen(open === j.id ? null : j.id)}>Details</button>}
                  {user.role === "admin" && j.status === "failed" && <button className="btn sm" onClick={() => retry(j.id)}>Retry</button>}
                </td>
              </tr>
              {open === j.id && <tr><td colSpan={6}><pre className="pre">{JSON.stringify(j.stats, null, 2)}</pre></td></tr>}
            </Fragment>
          ))}</tbody>
        </table></div>
      )}
    </div>
  );
}
export const Stat = ({ label, value, tone = "" }) => <div className={"stat " + tone}><div className="stat-value">{value}</div><div className="stat-label">{label}</div></div>;

/* ================= Compare ================= */
const PRESET = ["Accuracy class", "Communication interfaces", "Supported protocols", "Operating temperature range", "Supply voltage range"];

export function Compare() {
  const toast = useToast();
  const [subjects, setSubjects] = useState(["", ""]);
  const [aspects, setAspects] = useState(PRESET.join("\n"));
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [cite, setCite] = useState(null);
  const run = async (e) => {
    e.preventDefault();
    const subj = subjects.map((s) => s.trim()).filter(Boolean);
    const asp = aspects.split("\n").map((s) => s.trim()).filter(Boolean);
    if (subj.length < 2 || !asp.length) return toast("Enter at least two subjects and one aspect.", "error");
    setBusy(true); setResult(null);
    try { setResult(await api.post("/compare", { subjects: subj, aspects: asp })); }
    catch (err) { toast(err.message, "error"); }
    finally { setBusy(false); }
  };
  const citeFor = (m) => result.citations.find((c) => c.id === m);
  return (
    <div className={"page" + (cite ? " with-drawer" : "")}>
      <PageHeader title="Technical comparison" subtitle="Compare products, meters or standards side by side. A value appears only if a source states it; every cell links to its evidence." />
      <form className="card form-grid" onSubmit={run}>
        <div>
          <span className="field-label">Subjects (2–4)</span>
          {subjects.map((s, i) => (
            <input key={i} dir="auto" value={s} placeholder={`Subject ${i + 1}, e.g. “EDMI Mk6N”`} onChange={(e) => setSubjects(subjects.map((x, k) => (k === i ? e.target.value : x)))} />
          ))}
          <div className="row-gap">
            {subjects.length < 4 && <button type="button" className="btn sm" onClick={() => setSubjects([...subjects, ""])}>+ Add subject</button>}
            {subjects.length > 2 && <button type="button" className="btn sm" onClick={() => setSubjects(subjects.slice(0, -1))}>Remove last</button>}
          </div>
        </div>
        <Field label="Aspects to compare (one per line)"><textarea rows={6} dir="auto" value={aspects} onChange={(e) => setAspects(e.target.value)} /></Field>
        <div><button className="btn primary" disabled={busy}>{busy ? "Comparing…" : "Generate comparison"}</button></div>
      </form>
      {busy && <Spinner label="Retrieving evidence for each subject and extracting values…" />}
      {result && (
        <div className="card">
          <div className="card-head"><h3>{result.title}</h3>
            <div className="row-gap"><a className="btn sm" href={`/api/reports/${result.id}/export?format=csv`}>Export CSV</a><a className="btn sm" href={`/api/reports/${result.id}/export?format=md`}>Export Markdown</a></div></div>
          <div className="table-wrap"><table className="table compare">
            <thead><tr><th>Aspect</th>{result.data.subjects.map((s) => <th key={s} dir="auto">{s}</th>)}</tr></thead>
            <tbody>{result.data.rows.map((r) => (
              <tr key={r.aspect}><th dir="auto">{r.aspect}</th>
                {result.data.subjects.map((s) => {
                  const c = r.values[s];
                  return c.status === "verified"
                    ? <td key={s} className="verified"><span dir="auto">{c.value}</span> {c.citations.map((m) => <button key={m} className="cite" onClick={() => citeFor(m) && setCite(citeFor(m))}>{m}</button>)}</td>
                    : <td key={s} className="notfound">not found in sources</td>;
                })}
              </tr>
            ))}</tbody>
          </table></div>
          {result.data.conflicts.length > 0 && <div className="banner warn"><strong>Possible conflicts between sources</strong><ul className="plain">{result.data.conflicts.map((c, i) => <li key={i}>{c}</li>)}</ul></div>}
          {result.data.downgraded_cells > 0 && <p className="hint">{result.data.downgraded_cells} cell(s) were set to “not found” because the model gave no valid citation for them.</p>}
          <p className="hint">Values are extracted by an AI model from the cited passages and checked to cite real sources. Open each citation to verify the exact wording.</p>
        </div>
      )}
      {cite && <CitationDrawer citation={cite} onClose={() => setCite(null)} />}
    </div>
  );
}

/* ================= Reports ================= */
export function Reports() {
  const toast = useToast();
  const [list, setList] = useState(null);
  const [view, setView] = useState(null);
  const [title, setTitle] = useState("");
  const [instruction, setInstruction] = useState("");
  const [busy, setBusy] = useState(false);
  const [cite, setCite] = useState(null);
  const [confirm, confirmNode] = useConfirm();
  const load = () => api.get("/reports").then(setList).catch((e) => toast(e.message, "error"));
  usePolling(load, 0, [], false);
  const open = async (id) => { try { setView(await api.get("/reports/" + id)); setCite(null); } catch (e) { toast(e.message, "error"); } };
  const generate = async (e) => {
    e.preventDefault(); setBusy(true);
    try { const r = await api.post("/reports/generate", { title, instruction }); setView(r); setTitle(""); setInstruction(""); load(); }
    catch (err) { toast(err.message, "error"); }
    finally { setBusy(false); }
  };
  const remove = (r) => confirm(`Delete “${r.title}”?`, async () => { await api.del("/reports/" + r.id).catch((e) => toast(e.message, "error")); if (view && view.id === r.id) setView(null); load(); });
  return (
    <div className={"page" + (cite ? " with-drawer" : "")}>
      <PageHeader title="Reports" subtitle="Generate structured, fully-cited engineering reports from your sources. Comparisons are saved here too." />
      <div className="split">
        <div>
          <form className="card" onSubmit={generate}>
            <h3>New report</h3>
            <Field label="Title"><input dir="auto" value={title} onChange={(e) => setTitle(e.target.value)} required minLength={3} /></Field>
            <Field label="What should the report cover?"><textarea rows={4} dir="auto" value={instruction} onChange={(e) => setInstruction(e.target.value)} required minLength={5}
              placeholder="e.g. Summarise the communication and security features of the Mk6ESL and the standards it references." /></Field>
            <button className="btn primary" disabled={busy}>{busy ? "Generating…" : "Generate report"}</button>
          </form>
          <div className="card list-card">
            <h3>History</h3>
            {!list ? <Spinner /> : list.length === 0 ? <p className="hint">Nothing yet.</p> : list.map((r) => (
              <div key={r.id} className={"list-row" + (view && view.id === r.id ? " active" : "")}>
                <button onClick={() => open(r.id)} dir="auto"><span>{r.title}</span><small>{r.kind} · {ago(r.created_at)}</small></button>
                <button className="icon-btn sm" aria-label="Delete" onClick={() => remove(r)}>×</button>
              </div>
            ))}
          </div>
        </div>
        <div>
          {busy && <Spinner label="Retrieving evidence and writing the report…" />}
          {view ? (
            <article className="card report">
              <div className="card-head"><h3 dir="auto">{view.title}</h3>
                <div className="row-gap">
                  <a className="btn sm" href={`/api/reports/${view.id}/export?format=md`}>Export Markdown</a>
                  {view.kind === "comparison" && <a className="btn sm" href={`/api/reports/${view.id}/export?format=csv`}>Export CSV</a>}
                </div></div>
              {view.kind === "report" ? <Markdown text={view.content} citations={view.citations} onCite={setCite} /> : (
                <div className="table-wrap"><table className="table compare"><thead><tr><th>Aspect</th>{view.data.subjects.map((s) => <th key={s}>{s}</th>)}</tr></thead>
                  <tbody>{view.data.rows.map((r) => <tr key={r.aspect}><th>{r.aspect}</th>{view.data.subjects.map((s) => { const c = r.values[s];
                    return c.status === "verified" ? <td key={s} className="verified">{c.value} {c.citations.map((m) => <button key={m} className="cite" onClick={() => setCite(view.citations.find((x) => x.id === m))}>{m}</button>)}</td> : <td key={s} className="notfound">not found in sources</td>; })}</tr>)}</tbody></table></div>
              )}
              <div className="sources"><div className="eyebrow">Sources</div>
                {view.citations.map((c) => <button key={c.id} className="source-row" onClick={() => setCite(c)}><span className="cite">{c.id}</span><span className="src-title" dir="auto">{c.document_title}</span><span className="src-loc">{c.location}</span></button>)}
              </div>
            </article>
          ) : !busy && <Empty title="Select or generate a report">Reports and comparisons you create appear in the history list.</Empty>}
        </div>
      </div>
      {cite && <CitationDrawer citation={cite} onClose={() => setCite(null)} />}
      {confirmNode}
    </div>
  );
}
