// Thin fetch wrapper. All calls are same-origin (/api via nginx); the session lives in an HttpOnly cookie.
export class ApiError extends Error {
  constructor(message, status, code) { super(message); this.status = status; this.code = code; }
}

async function request(method, path, body, isForm = false) {
  const headers = { "X-Requested-With": "infinity" };
  if (body !== undefined && !isForm) headers["Content-Type"] = "application/json";
  let res;
  try {
    res = await fetch("/api" + path, { method, headers, credentials: "same-origin", body: body === undefined ? undefined : isForm ? body : JSON.stringify(body) });
  } catch {
    throw new ApiError("Cannot reach the server. Check that the platform is running.", 0);
  }
  if (res.status === 401 && !path.startsWith("/auth/login")) window.dispatchEvent(new Event("infinity:unauthorized"));
  let data = null;
  const text = await res.text();
  if (text) { try { data = JSON.parse(text); } catch { data = text; } }
  if (!res.ok) {
    let detail = data && data.detail;
    if (Array.isArray(detail)) detail = detail.map((d) => d.msg).join("; ");
    throw new ApiError(detail || `Request failed (${res.status})`, res.status, data && data.code);
  }
  return data;
}

export const api = {
  get: (p) => request("GET", p),
  post: (p, b) => request("POST", p, b === undefined ? {} : b),
  put: (p, b) => request("PUT", p, b),
  patch: (p, b) => request("PATCH", p, b),
  del: (p) => request("DELETE", p),
  upload: (p, form) => request("POST", p, form, true),
};

export const fmtDate = (v) => (v ? new Date(v).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—");
export const fmtSize = (n) => (n == null ? "—" : n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB");
export const ago = (v) => {
  if (!v) return "never";
  const s = Math.max(0, (Date.now() - new Date(v).getTime()) / 1000);
  if (s < 90) return "just now";
  if (s < 5400) return Math.round(s / 60) + " min ago";
  if (s < 129600) return Math.round(s / 3600) + " h ago";
  return Math.round(s / 86400) + " d ago";
};
