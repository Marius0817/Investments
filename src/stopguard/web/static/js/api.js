// Zugriff auf die stopguard-API (relativ, funktioniert auch unter einem Unterpfad).

export class ApiError extends Error {
  constructor(status, body) {
    super((body && body.message) || `Fehler ${status}`);
    this.status = status;
    this.code = (body && body.error) || "http_" + status;
    this.body = body || {};
  }
}

const listeners = new Set();
export function onAuthRequired(fn) { listeners.add(fn); return () => listeners.delete(fn); }

async function request(method, path, body) {
  const opts = { method, headers: { Accept: "application/json" }, credentials: "same-origin" };
  if (method !== "GET") {
    opts.headers["Content-Type"] = "application/json";
    opts.headers["X-Stopguard"] = "1";
    opts.body = JSON.stringify(body || {});
  }
  let res;
  try {
    res = await fetch("api/v1/" + path, opts);
  } catch (err) {
    throw new ApiError(0, {
      error: "offline",
      message: "Der stopguard-Server ist nicht erreichbar. Läuft „stopguard web“ noch?",
    });
  }
  let data = null;
  const text = await res.text();
  try { data = text ? JSON.parse(text) : null; } catch { data = { message: text }; }
  if (!res.ok) {
    const err = new ApiError(res.status, data);
    if (res.status === 401 && err.code === "login_required") listeners.forEach((fn) => fn());
    throw err;
  }
  return data;
}

export const api = {
  get: (path) => request("GET", path),
  post: (path, body) => request("POST", path, body),
};
