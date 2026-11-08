// Saves the job-jarvis address and token, and asks Chrome for access to that one address only.
const $ = (id) => document.getElementById(id);
chrome.storage.local.get(["appUrl", "token"]).then(({ appUrl = "", token = "" }) => {
  $("appUrl").value = appUrl;
  $("token").value = token;
});
$("save").onclick = async () => {
  const status = $("status");
  try {
    const origin = new URL($("appUrl").value.trim()).origin;
    if (!origin.startsWith("https://")) throw new Error("use the https:// address of your job-jarvis app");
    const granted = await chrome.permissions.request({ origins: [`${origin}/*`] });
    if (!granted) throw new Error("Chrome access to that address was not granted");
    await chrome.storage.local.set({ appUrl: origin, token: $("token").value.trim() });
    status.textContent = "Saved.";
  } catch (e) {
    status.textContent = e.message;
  }
};
