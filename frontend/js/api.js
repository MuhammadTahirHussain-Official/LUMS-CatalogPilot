// API client: same-origin, cookie session, CSRF header on every state change.
// Errors are RFC 9457 problem objects; ApiError carries them to the UI unchanged.

export class ApiError extends Error {
  constructor(status, problem) {
    super(problem.detail || problem.title || `Request failed (${status})`);
    this.status = status;
    this.code = problem.code || "error";
    this.problem = problem;
  }
}

let csrf = "";
let onStepUp = null;
let onSignedOut = null;

export function setCsrf(token) { csrf = token || ""; }
export function setHandlers({ stepUp, signedOut }) { onStepUp = stepUp; onSignedOut = signedOut; }

async function parse(response) {
  const type = response.headers.get("content-type") || "";
  if (response.status === 204) return null;
  if (type.includes("json")) return response.json();
  return response;
}

export async function request(method, path, { body, headers = {}, raw = false, retried = false } = {}) {
  const init = { method, headers: { ...headers }, credentials: "same-origin" };
  if (method !== "GET" && method !== "HEAD") init.headers["X-CSRF-Token"] = csrf;
  if (body !== undefined) {
    if (body instanceof Blob || body instanceof ArrayBuffer || ArrayBuffer.isView(body)) {
      init.body = body;
    } else {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
  }
  let response;
  try {
    response = await fetch(`/v1${path}`, init);
  } catch {
    throw new ApiError(0, { code: "network", detail: "Cannot reach the server. Check your connection and try again." });
  }
  if (response.ok) return raw ? response : parse(response);
  let problem = {};
  try { problem = await response.json(); } catch { /* non-JSON error body */ }
  if (problem.code === "step_up_required" && onStepUp && !retried) {
    // Sensitive action: confirm identity, then repeat the same request once.
    if (await onStepUp()) return request(method, path, { body, headers, raw, retried: true });
  }
  if (response.status === 401 && onSignedOut && !path.startsWith("/auth/")) onSignedOut();
  throw new ApiError(response.status, problem);
}

export const get = (path) => request("GET", path);
export const post = (path, body, headers) => request("POST", path, { body: body ?? {}, headers });
export const put = (path, body) => request("PUT", path, { body });
export const patch = (path, body, headers) => request("PATCH", path, { body, headers });
export const del = (path) => request("DELETE", path);

export async function download(method, path, fallbackName) {
  const response = await request(method, path, { raw: true, body: method === "POST" ? {} : undefined });
  const blob = await response.blob();
  const match = /filename="([^"]+)"/.exec(response.headers.get("content-disposition") || "");
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = match ? match[1] : fallbackName;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 5000);
}

async function sha256Hex(buffer) {
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Resumable chunked upload. After any failure the server is asked for its durable offset
// and sending continues from there, so a dropped connection never restarts the file.
export async function uploadFile(catalogId, file, onProgress) {
  const CHUNK = 4 * 1024 * 1024;
  if (file.size > 1024 * 1024 * 1024) {
    throw new ApiError(413, { code: "payload_too_large", detail: "Files over 1 GB cannot be uploaded from the browser yet." });
  }
  onProgress(0, "Checking file…");
  const whole = await sha256Hex(await file.arrayBuffer());
  const created = await post("/uploads", { catalog_id: catalogId, files: [{ name: file.name, size: file.size, sha256: whole }] });
  const target = created.files[0].upload_url.replace(/^\/v1/, "");
  let offset = 0;
  let failures = 0;
  while (offset < file.size) {
    const part = await file.slice(offset, offset + CHUNK).arrayBuffer();
    try {
      const response = await request("PATCH", target, {
        body: part, raw: true,
        headers: { "Upload-Offset": String(offset), "Upload-Checksum": `sha256 ${await sha256Hex(part)}`, "Content-Type": "application/octet-stream" },
      });
      offset = Number(response.headers.get("upload-offset"));
      failures = 0;
      onProgress(offset / file.size, "Uploading…");
    } catch (error) {
      failures += 1;
      const retryable = error.status === 0 || error.status === 409 || error.status === 460 || error.status >= 500;
      if (!retryable || failures > 5) throw error;
      await new Promise((resolve) => setTimeout(resolve, 500 * 2 ** failures));
      const head = await request("HEAD", target, { raw: true });
      offset = Number(head.headers.get("upload-offset")) || 0;
    }
  }
  return { uploadId: created.id, fileId: created.files[0].id };
}
