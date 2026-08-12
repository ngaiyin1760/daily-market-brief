// Cloudflare Worker proxy for the Daily Market Brief semantic search.
//
// Why: the Search page's semantic mode calls Google's Gemini API directly
// from the browser. From Hong Kong that endpoint is geo-blocked (HTTP 400
// "user location is not supported"). This Worker runs on Cloudflare's US
// edge, forwards the request to Gemini, and returns the result — so the site
// works from HK without a VPN, and the API key stays server-side (never
// shipped to the browser).
//
// Deploy: Cloudflare dashboard -> Workers & Pages -> Create -> Worker ->
// paste this file -> Deploy. Set the KV binding GEMINI_KEY to your
// AIza... browser key (Settings -> Variables -> KV namespace), or paste the
// key directly into the GEMINI_KEY const below (less clean, still works).
//
// Endpoints (same shape as the Gemini REST API):
//   POST /embed  -> forwards to models/<EMBED_MODEL>:embedContent
//   POST /gen    -> forwards to models/<GEN_MODEL>:generateContent
//   GET  /ping   -> {"ok": true}

const EMBED_MODEL = "gemini-embedding-001";
const GEN_MODEL = "gemini-3.1-flash-lite";

// If you don't use a KV binding, paste your key here (then delete the
// binding). Keep the key secret — this Worker is public.
const GEMINI_KEY = (typeof GEMINI_KEY_BINDING !== "undefined" && GEMINI_KEY_BINDING)
  ? GEMINI_KEY_BINDING
  : "";

function json(resp, status) {
  return new Response(JSON.stringify(resp), {
    status: status || 200,
    headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" }
  });
}

async function handle(request) {
  const url = new URL(request.url);
  const path = url.pathname;

  // CORS preflight (browser fetch)
  if (request.method === "OPTIONS") {
    return new Response(null, {
      status: 204,
      headers: {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type"
      }
    });
  }

  if (request.method === "GET" && path === "/ping") {
    return json({ ok: true, keySet: !!GEMINI_KEY });
  }

  if (request.method !== "POST") return json({ error: "method not allowed" }, 405);

  let body;
  try { body = await request.json(); } catch (e) { return json({ error: "bad json" }, 400); }

  let model, apiPath;
  if (path === "/embed") { model = EMBED_MODEL; apiPath = "embedContent"; }
  else if (path === "/gen") { model = GEN_MODEL; apiPath = "generateContent"; }
  else return json({ error: "unknown path" }, 404);

  const upstream = "https://generativelanguage.googleapis.com/v1beta/models/"
    + model + ":" + apiPath + "?key=" + encodeURIComponent(GEMINI_KEY);

  try {
    const resp = await fetch(upstream, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    const data = await resp.json();
    if (!resp.ok) {
      const msg = data && data.error && data.error.message ? data.error.message : "";
      return json({ error: { message: "Upstream " + resp.status + (msg ? ": " + msg : "") } }, resp.status);
    }
    return json(data);
  } catch (e) {
    return json({ error: { message: "Proxy error: " + e.message } }, 500);
  }
}

addEventListener("fetch", (event) => {
  event.respondWith(handle(event.request));
});
