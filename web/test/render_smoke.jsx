import { renderToStaticMarkup } from "react-dom/server";
import { Markdown, ToastProvider } from "../src/ui.jsx";
import { Assistant, Login, Research } from "../src/pages_core.jsx";
import { Compare, Ingestion, Library, Reports } from "../src/pages_work.jsx";
import { Settings, Sources, System, Users } from "../src/pages_admin.jsx";
import App from "../src/App.jsx";

const out = (x) => renderToStaticMarkup(<ToastProvider>{x}</ToastProvider>);
const admin = { id: "u1", email: "a@x.com", name: "Admin", role: "admin" };
const results = {};
const cites = [{ id: "S1", document_title: "Mk6ESL Manual", location: "PDF page 12 (printed 10)", chunk_id: "c1" }];

const md = out(<Markdown citations={cites} text={"## Verified from sources\nThe meter is Class 0.2S [S1]. **Bold** and `code` and [S7].\n\n- item one [S1]\n- item two\n\n| A | B |\n|---|---|\n| x [S1] | <img src=x onerror=alert(1)> |\n\n<script>alert(1)</script>"} />);
results.markdown_chip = md.includes('class="cite"') && md.includes(">S1<");
results.markdown_unknown_marker_dead = md.includes("cite dead");
results.markdown_table = md.includes("<table") && md.includes("<th");
results.markdown_list = md.includes("<ul");
results.markdown_no_raw_html = !md.includes("<script>") && !md.includes("<img");
results.markdown_escaped = md.includes("&lt;script&gt;") && md.includes("&lt;img");

for (const [name, el] of Object.entries({
  Login: <Login onLogin={() => {}} />, Assistant: <Assistant user={admin} />, Research: <Research />,
  Library: <Library user={admin} />, LibraryViewer: <Library user={{ ...admin, role: "viewer" }} />, Ingestion: <Ingestion user={admin} />,
  Compare: <Compare />, Reports: <Reports />, Sources: <Sources />, Users: <Users me={admin} />, Settings: <Settings />, System: <System />, App: <App />,
})) {
  try { results[name] = out(el).length > 20; } catch (e) { results[name] = "CRASH: " + e.message; }
}
console.log(JSON.stringify(results, null, 1));
