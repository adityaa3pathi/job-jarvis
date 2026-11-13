// Saves the job-jarvis address and token, the owner's contact details (kept in this browser) and
// their saved answers for pay, notice and location questions.
const $ = (id) => document.getElementById(id);
const DETAILS = [
  ["name", "Full name"], ["firstName", "First name"], ["lastName", "Last name"], ["email", "Email"],
  ["phone", "Phone"], ["linkedin", "LinkedIn URL"], ["github", "GitHub URL"], ["website", "Portfolio / website"],
];
const PREFS = [
  "Total experience", "Notice period", "Current CTC", "Expected CTC", "Current city",
  "Preferred locations", "Willing to relocate", "Work mode", "Earliest start date", "Work authorization",
  "Languages known",
];

function inputs(boxId, rows, values) {
  const box = $(boxId);
  for (const [key, text] of rows) {
    const input = Object.assign(document.createElement("input"), { value: values[key] || "" });
    input.dataset.key = key;
    const label = document.createElement("label");
    label.append(text, input);
    box.append(label);
  }
}

function read(boxId) {
  const out = {};
  for (const input of $(boxId).querySelectorAll("input")) if (input.value.trim()) out[input.dataset.key] = input.value.trim();
  return out;
}

chrome.storage.local.get(["appUrl", "token", "details", "prefs"]).then(({ appUrl = "", token = "", details = {}, prefs = {} }) => {
  $("appUrl").value = appUrl;
  $("token").value = token;
  inputs("details", DETAILS, details);
  inputs("prefs", PREFS.map((p) => [p, p]), prefs);
});

$("save").onclick = async () => {
  const status = $("status");
  try {
    const origin = new URL($("appUrl").value.trim()).origin;
    if (!origin.startsWith("https://")) throw new Error("use the https:// address of your job-jarvis app");
    const granted = await chrome.permissions.request({ origins: [`${origin}/*`] });
    if (!granted) throw new Error("Chrome access to that address was not granted");
    await chrome.storage.local.set({ appUrl: origin, token: $("token").value.trim(), details: read("details"), prefs: read("prefs") });
    status.textContent = "Saved.";
  } catch (e) {
    status.textContent = e.message;
  }
};
