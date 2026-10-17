/* HTTP, and the one place the session token lives.
 *
 * The desktop launcher hands the page a per-launch token in the URL fragment.
 * A fragment is never sent to the server and never appears in a Referer, so it
 * cannot leak through the page itself -- which is the whole reason it is not a
 * query parameter.
 */

export const TOKEN = (() => {
  const raw = location.hash.replace(/^#/, "");
  if (!raw) return "";
  // Keep it out of the address bar once we have it.
  history.replaceState(null, "", location.pathname + location.search);
  return raw;
})();

export function authHeaders(extra) {
  const headers = { ...(extra || {}) };
  if (TOKEN) headers["X-WUKONG-Token"] = TOKEN;
  return headers;
}

export async function json(url, options) {
  const request = { ...(options || {}) };
  request.headers = authHeaders(request.headers);
  const response = await fetch(url, request);
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`${response.status}: ${detail.slice(0, 300)}`);
  }
  return response.json();
}

export function post(url, body) {
  return json(url, {
    method: "POST",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

/** Read an SSE body and hand each JSON payload to `onEvent`.
 *
 * `fetch` + ReadableStream rather than EventSource: EventSource can only do
 * GET, and an AG-UI run carries a request body.
 */
export async function consumeStream(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // SSE frames are separated by a blank line.
    let index;
    while ((index = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, index);
      buffer = buffer.slice(index + 2);
      for (const line of frame.split("\n")) {
        if (!line.startsWith("data:")) continue;
        const payload = line.slice(5).trim();
        if (!payload) continue;
        try {
          onEvent(JSON.parse(payload));
        } catch (error) {
          console.warn("bad SSE payload", payload, error);
        }
      }
    }
  }
}
