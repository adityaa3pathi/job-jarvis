// Starts the form helper on any page, shows what job-jarvis has for a LinkedIn page, and fills
// LinkedIn's own boxes when asked. The owner always presses LinkedIn's Send; the page script then records it.
const out = document.getElementById("out");
document.getElementById("options").onclick = () => chrome.runtime.openOptionsPage();

const call = (path, init) =>
  new Promise((resolve, reject) =>
    chrome.runtime.sendMessage({ type: "api", path, init }, (r) => (r?.ok ? resolve(r.data) : reject(new Error(r?.error || "no answer")))),
  );

function el(tag, attrs = {}, ...kids) {
  const node = Object.assign(document.createElement(tag), attrs);
  node.append(...kids.filter((k) => k !== null && k !== undefined));
  return node;
}

async function inTab(tabId, name, args = []) {
  await chrome.scripting.executeScript({ target: { tabId }, files: ["linkedin.js"] });
  const [res] = await chrome.scripting.executeScript({
    target: { tabId },
    func: (fn, a) => globalThis[fn](...a),
    args: [name, args],
  });
  return res?.result;
}

function show(...nodes) {
  out.replaceChildren(...nodes);
}

async function appLink(path, text) {
  const { appUrl = "" } = await chrome.storage.local.get("appUrl");
  return el("a", { href: `${appUrl.replace(/\/+$/, "")}${path}`, target: "_blank", textContent: text });
}

async function threadCard(tab, t) {
  const status = el("div", { className: "muted" });
  const card = el("div", { className: "card" }, el("strong", { textContent: `${t.name} · ${t.company}` }), status);
  const fillInvite = t.step === 0;
  const fillMessage = (t.step === 1 && t.state === "accepted") || (t.step === 2 && t.state === "waiting");
  if (["replied", "bounced", "no_reply", "stopped"].includes(t.state) || !t.can_send) {
    status.textContent = `Conversation ${t.state.replace("_", " ")}.`;
    return card;
  }
  if (t.step === 1 && t.state === "waiting") {
    status.textContent = "Invite sent. Once they accept, mark it here.";
    const btn = el("button", { textContent: "They accepted" });
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        await call(`/ext/outreach/${t.id}/accepted`, { method: "POST" });
        render();
      } catch (e) {
        status.textContent = e.message;
        status.className = "error";
      }
    };
    card.append(btn);
    return card;
  }
  card.append(el("pre", { textContent: t.body }));
  if (!t.edited) {
    status.textContent = "Rewrite this in your own words in job-jarvis first.";
    status.className = "note";
    card.append(await appLink(`/outreach/${t.job_id}`, "Open in job-jarvis"));
    return card;
  }
  status.textContent = fillInvite ? "Next: connection note" : "Next: LinkedIn message";
  const btn = el("button", { textContent: fillInvite ? "Fill invite note" : "Fill message" });
  btn.disabled = !(fillInvite || fillMessage);
  btn.onclick = async () => {
    btn.disabled = true;
    await chrome.storage.local.remove("lastError");
    chrome.action.setBadgeText({ text: "" });
    const said = await inTab(tab.id, fillInvite ? "jjFillInvite" : "jjFillMessage", [t.body, t.id]);
    status.textContent = said || "Done.";
  };
  card.append(btn);
  return card;
}

async function saveCard(tab, data) {
  const person = await inTab(tab.id, "jjReadProfile");
  const select = el("select", {}, ...data.jobs.map((j) => el("option", { value: j.id, textContent: `${j.company}: ${j.title}` })));
  const status = el("div", { className: "muted" });
  const btn = el("button", { textContent: "Save as contact" });
  btn.disabled = !data.jobs.length || !person?.name;
  btn.onclick = async () => {
    btn.disabled = true;
    try {
      await call("/ext/contact", {
        method: "POST",
        body: JSON.stringify({ job_id: Number(select.value), name: person.name, title: person.title, linkedin_url: person.url }),
      });
      status.replaceChildren("Saved. Write the note in job-jarvis, then come back here. ", await appLink(`/outreach/${select.value}`, "Open"));
    } catch (e) {
      status.textContent = e.message;
      status.className = "error";
      btn.disabled = false;
    }
  };
  return el(
    "div",
    { className: "card" },
    el("strong", { textContent: person?.name || "Unknown name" }),
    el("div", { className: "muted", textContent: person?.title || "" }),
    data.jobs.length ? el("label", {}, "For which job?", select) : el("div", { className: "note", textContent: "No jobs yet: send a LinkedIn job here first, or reach out on one in job-jarvis." }),
    btn,
    status,
  );
}

async function jobCard(tab) {
  const status = el("div", { className: "muted" });
  const btn = el("button", { textContent: "Send this job to job-jarvis" });
  btn.onclick = async () => {
    btn.disabled = true;
    try {
      const job = await inTab(tab.id, "jjReadJob");
      if (!job?.title || !job?.company) throw new Error("Could not read this job. Open the job on its own page and try again.");
      const { job_id } = await call("/ext/job", { method: "POST", body: JSON.stringify(job) });
      status.replaceChildren("Saved. ", await appLink(`/outreach/${job_id}`, "Reach out in job-jarvis"));
    } catch (e) {
      status.textContent = e.message;
      status.className = "error";
      btn.disabled = false;
    }
  };
  return el("div", { className: "card" }, el("strong", { textContent: "LinkedIn job" }), btn, status);
}

function formCard(tab) {
  const status = el("div", { className: "muted", textContent: "Reads the questions on this page and shows an answer under each one." });
  const btn = el("button", { textContent: "Help with this page's form" });
  btn.onclick = async () => {
    btn.disabled = true;
    try {
      await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, files: ["forms.js"] });
      await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, func: () => globalThis.jjForms?.start() });
      window.close();
    } catch (e) {
      status.textContent = `Could not read this page: ${e.message}`;
      status.className = "error";
      btn.disabled = false;
    }
  };
  return el("div", { className: "card" }, el("strong", { textContent: "Job form" }), btn, status);
}

async function render() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const url = tab?.url || "";
  const { lastError } = await chrome.storage.local.get("lastError");
  const warn = lastError ? el("div", { className: "error", textContent: lastError }) : null;
  const form = /^https?:/.test(url) ? formCard(tab) : null;
  try {
    if (/linkedin\.com\/in\//.test(url)) {
      const data = await call(`/ext/profile?url=${encodeURIComponent(url)}`);
      const cards = await Promise.all(data.threads.map((t) => threadCard(tab, t)));
      if (!cards.length && data.contacts.length) {
        cards.push(el("div", { className: "card" }, "Saved in job-jarvis. Write a LinkedIn note there, then come back.", await appLink(`/outreach`, "Open Outreach")));
      }
      if (!cards.length) cards.push(await saveCard(tab, data));
      show(...[warn, form, ...cards].filter(Boolean));
    } else if (/linkedin\.com\/jobs\//.test(url)) {
      show(...[warn, form, await jobCard(tab)].filter(Boolean));
    } else {
      show(...[warn, form || el("div", { className: "muted", textContent: "Open a job site to use this." })].filter(Boolean));
    }
  } catch (e) {
    show(el("div", { className: "error", textContent: e.message }));
  }
}

render();
