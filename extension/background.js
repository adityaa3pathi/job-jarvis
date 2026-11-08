// Talks to job-jarvis with the token from the options page. Never calls LinkedIn itself:
// the page script reports a Send the owner pressed, and this records it.
async function settings() {
  const { appUrl = "", token = "" } = await chrome.storage.local.get(["appUrl", "token"]);
  return { appUrl: appUrl.replace(/\/+$/, ""), token };
}

async function api(path, init = {}) {
  const { appUrl, token } = await settings();
  if (!appUrl || !token) throw new Error("Set the job-jarvis address and token in the extension options.");
  const res = await fetch(`${appUrl}/api${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}`, ...(init.headers || {}) },
  });
  const text = await res.text();
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!res.ok) throw new Error((body && body.detail) || `job-jarvis answered ${res.status}`);
  return body;
}

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg?.type === "api") {
    api(msg.path, msg.init).then((data) => reply({ ok: true, data }), (e) => reply({ ok: false, error: e.message }));
    return true; // the reply comes later
  }
  if (msg?.type === "sent") {
    api(`/ext/outreach/${msg.outreachId}/sent`, { method: "POST" }).then(
      () => chrome.action.setBadgeText({ text: "✓" }),
      (e) => {
        chrome.action.setBadgeText({ text: "!" });
        chrome.storage.local.set({ lastError: `Not recorded in job-jarvis: ${e.message}` });
      },
    );
  }
  return false;
});
