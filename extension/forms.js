// Reads the questions of the form open on a job site, asks job-jarvis for answers and shows each
// one under its question. It fills a box only when the owner clicks, and never submits anything.
(() => {
  if (globalThis.jjForms) return;

  const SKIP_TYPES = new Set(["hidden", "submit", "button", "reset", "image", "file", "password", "search", "range", "color"]);
  const MAX_FIELDS = 40;
  const MAX_CONTEXT = 4000;
  const FILL_ALL = new Set(["fact", "saved"]);
  const CONTACT = [
    [/first\s*name|given name/i, "firstName"],
    [/last\s*name|surname|family name/i, "lastName"],
    [/full\s*name|^name\b|your name|candidate name/i, "name"],
    [/e-?mail/i, "email"],
    [/phone|mobile|contact number|whatsapp/i, "phone"],
    [/linkedin/i, "linkedin"],
    [/github/i, "github"],
    [/portfolio|website|personal site|blog/i, "website"],
  ];
  // Ticking these is the owner's own promise, so they are never read or filled.
  const CONSENT = /\b(agree|consent|terms|privacy|policy|acknowledge|certify|declare|subscribe|newsletter|remember me)\b/i;
  const CONTROLS = "input:not([type='hidden']), select, textarea, [contenteditable='true']";
  // Currency marks and counters like "Rs." or "0 / 200" sit beside boxes but are not questions.
  const weak = (t) => t.replace(/[^A-Za-z]/g, "").length < 3 || /^(rs\.?|inr|lpa|usd|ctc)$/i.test(t);
  // Tag and dropdown widgets show the current choice beside their box; the question is outside.
  const WIDGET = ".selectize-control, .select2-container, .chosen-container, [class*='select__control'], [class*='tag-input'], [class*='chips']";
  const TAG_BOX = ".selectize-control.multi, .select2-container--multiple, [class*='tag-input'], [class*='chips']";
  const TAG_ITEM = ".item, [class*='chip'], [class*='tag-item'], .select2-selection__choice";
  const NOISE = /search|captcha|coupon|promo code|referral code|otp|one.time/i;

  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const editable = (el) => el.isContentEditable || el.getAttribute("contenteditable") === "true" || el.getAttribute("role") === "textbox";
  const textOf = (el) => clean(el?.innerText || el?.textContent || "");

  function hidden(el) {
    if (!el.isConnected || el.disabled || el.readOnly) return true;
    if (el.closest("[hidden], [aria-hidden='true'], [inert], #jj-forms-root")) return true;
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (style.display === "none" || style.visibility === "hidden") return true;
    }
    return false;
  }

  function hiddenChoice(el) {
    if (!hidden(el)) return false;
    const label = el.labels?.[0];
    return !label || el.disabled || hidden(label);
  }

  function ownText(container, control) {
    const copy = container.cloneNode(true);
    copy.querySelectorAll("input, select, textarea, option, button, [contenteditable]").forEach((n) => n.remove());
    const kept = textOf(copy);
    return kept && kept !== textOf(control) ? kept : "";
  }

  function byIds(ids) {
    return clean((ids || "").split(/\s+/).map((id) => textOf(document.getElementById(id))).join(" "));
  }

  // Tags already chosen ("Python ×") and suggestion lists sit between a tag box and its question.
  function pickList(n) {
    if (n.matches(".selectized, selectize") || /×/.test(textOf(n))) return true;
    return (n.matches("ul, ol") || !!n.querySelector("ul, ol")) && !n.querySelector("label, h1, h2, h3, h4, h5, h6");
  }

  // Closest text before the control, a few levels up. Text past another control is that
  // control's question, so the search stops there.
  function nearbyText(el) {
    let node = el;
    for (let depth = 0; depth < 5 && node && node !== document.body; depth++) {
      for (let prev = node.previousElementSibling; prev; prev = prev.previousElementSibling) {
        if (prev.matches("script, style, .jj-forms-card, #jj-forms-root") || pickList(prev)) continue;
        if (prev.matches(CONTROLS) || prev.querySelector(CONTROLS)) return "";
        const t = textOf(prev);
        if (!t || weak(t)) continue;
        return t.length <= 300 ? t : "";
      }
      node = node.parentElement;
    }
    return "";
  }

  function labelFor(el) {
    const shadow = el.id?.endsWith("-selectized") && document.getElementById(el.id.slice(0, -"-selectized".length));
    if (shadow) return labelFor(shadow);
    const widget = el.closest(WIDGET);
    const tries = [
      () => byIds(el.getAttribute("aria-labelledby")),
      () => clean([...(el.labels || [])].map((l) => ownText(l, el)).join(" ")),
      () => clean(el.getAttribute("aria-label")),
      () => nearbyText(widget || el),
      () => clean(el.getAttribute("placeholder")),
      () => clean(el.getAttribute("title")),
      () => clean((el.getAttribute("name") || "").replace(/[_\-[\].]+/g, " ")),
    ];
    for (const t of tries) {
      const got = t();
      if (got) return got.slice(0, 600);
    }
    return "";
  }

  function groupLabel(inputs) {
    const first = inputs[0];
    const holder = first.closest("fieldset, [role='radiogroup'], [role='group']");
    if (holder) {
      const legend = holder.querySelector(":scope > legend");
      const named = byIds(holder.getAttribute("aria-labelledby")) || clean(holder.getAttribute("aria-label"));
      const got = textOf(legend) || named;
      if (got) return got.slice(0, 600);
    }
    let box = first.parentElement;
    while (box && !inputs.every((i) => box.contains(i))) box = box.parentElement;
    return (box && nearbyText(box)) || nearbyText(first);
  }

  function optionLabel(input) {
    return clean([...(input.labels || [])].map((l) => ownText(l, input) || textOf(l)).join(" ")) || clean(input.getAttribute("aria-label")) || clean(input.value);
  }

  // Inside an open dialog when there is one (LinkedIn Easy Apply, edit popups), else the page.
  function roots(doc) {
    const dialogs = [...doc.querySelectorAll("dialog[open], [role='dialog'], [aria-modal='true']")].filter((d) => !hidden(d) && d.querySelector("input, select, textarea, [contenteditable='true']"));
    if (dialogs.length) return dialogs;
    if (/(^|\.)linkedin\.com$/.test(location.hostname)) return [];
    return [doc.body];
  }

  function groupKey(el, type) {
    const same = [...el.ownerDocument.querySelectorAll(`input[type='${type}']`)].filter((i) => i.name === el.name);
    const named = el.name && same.length > 1;
    if (named) return `n:${el.name}`;
    let box = el.closest("fieldset, [role='radiogroup'], [role='group']") || el.parentElement;
    for (let i = 0; box && i < 3 && box.querySelectorAll(`input[type='${type}']`).length < 2; i++) box = box.parentElement;
    return `b:${idOf(box || el)}`;
  }

  let nextId = 1;
  function idOf(el) {
    if (!el.dataset.jjId) el.dataset.jjId = `f${nextId++}`;
    return el.dataset.jjId;
  }

  function contactKey(label, el) {
    const type = (el?.getAttribute?.("type") || "").toLowerCase();
    if (type === "email") return "email";
    if (type === "tel") return "phone";
    const hit = CONTACT.find(([re]) => re.test(label));
    return hit ? hit[1] : null;
  }

  // Every question in the open form: one entry per box, select or radio/checkbox group.
  function collect(doc = document) {
    const out = [];
    const groups = new Map();
    for (const root of roots(doc)) {
      for (const el of root.querySelectorAll("input, select, textarea, [contenteditable='true'], [role='textbox']")) {
        if (el.closest("[role='search'], nav, header, footer")) continue;
        const type = (el.getAttribute("type") || "").toLowerCase();
        if (el.tagName === "INPUT" && SKIP_TYPES.has(type)) continue;
        const choice = type === "radio" || type === "checkbox";
        if (choice ? hiddenChoice(el) : hidden(el)) continue;
        if (choice) {
          const key = `${type}:${groupKey(el, type)}`;
          if (!groups.has(key)) groups.set(key, { type, inputs: [] });
          groups.get(key).inputs.push(el);
          continue;
        }
        const label = labelFor(el);
        if (!label || NOISE.test(label)) continue;
        const kind = el.tagName === "TEXTAREA" || editable(el) ? "textarea"
          : el.tagName === "INPUT" && el.closest(TAG_BOX) ? "chips"
          : el.tagName === "SELECT" ? "select"
          : type === "number" ? "number"
          : type === "date" ? "date"
          : "text";
        const hint = clean(el.getAttribute("placeholder"));
        const field = { id: idOf(el), label: kind === "chips" && hint && !label.includes(hint) ? `${label} (${hint})` : label, kind, el };
        if (kind === "select") field.options = [...el.options].filter((o) => o.value !== "" && !/^(select|choose|--)/i.test(clean(o.text))).map((o) => clean(o.text));
        const max = Number(el.getAttribute("maxlength"));
        if (max > 0) field.max_len = max;
        field.contact = contactKey(label, el);
        out.push(field);
      }
    }
    for (const { type, inputs } of groups.values()) {
      const label = groupLabel(inputs);
      if (inputs.length === 1 && (type === "checkbox" || !label)) continue;
      if (!label || CONSENT.test(label) || NOISE.test(label)) continue;
      out.push({ id: idOf(inputs[0]), label, kind: type, options: inputs.map(optionLabel), el: inputs[0], inputs });
    }
    return out.slice(0, MAX_FIELDS);
  }

  function setValue(el, value) {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function press(el, type, code) {
    const e = new KeyboardEvent(type, { key: "Enter", bubbles: true, cancelable: true });
    Object.defineProperty(e, "keyCode", { get: () => code });
    Object.defineProperty(e, "which", { get: () => code });
    el.dispatchEvent(e);
  }

  // Types each tag and presses Enter, so the site's own tag box accepts it as from a person.
  async function fillTags(field, value) {
    const el = field.el;
    const box = el.closest(TAG_BOX) || el.parentElement;
    const tags = () => [...box.querySelectorAll(TAG_ITEM)].map((n) => textOf(n).replace(/×$/, "").trim().toLowerCase());
    const items = value.split(",").map(clean).filter((v) => v && !tags().includes(v.toLowerCase()));
    let added = 0;
    for (const item of items) {
      const before = tags().length;
      el.focus();
      setValue(el, item);
      el.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true }));
      await wait(700); // the site looks the tag up while it is typed
      press(el, "keydown", 13);
      press(el, "keyup", 13);
      await wait(200);
      if (tags().length > before) added++;
      else setValue(el, "");
    }
    if (!items.length) return { done: true, note: "Already there." };
    const note = added === items.length ? `Added all ${added}.` : `Added ${added} of ${items.length}. Type the rest and pick from the list.`;
    return { done: added > 0, note };
  }

  // Puts one value into the page's own control the way a typing person would.
  async function fill(field, value) {
    const el = field.el;
    if (!el?.isConnected) return { done: false, note: "The box is gone from the page." };
    if (field.kind === "radio" || field.kind === "checkbox") {
      const want = new Set(value.split("|").map((v) => clean(v).toLowerCase()));
      field.inputs.forEach((input, i) => {
        const on = want.has((field.options[i] || "").toLowerCase());
        if (input.checked !== on && (on || field.kind === "checkbox")) input.click();
      });
      return { done: true, note: "" };
    }
    if (field.kind === "chips") return fillTags(field, value);
    if (field.kind === "select") {
      const opt = [...el.options].find((o) => clean(o.text).toLowerCase() === value.toLowerCase());
      if (!opt) return { done: false, note: "That choice is not in the list." };
      setValue(el, opt.value);
      return { done: true, note: "" };
    }
    el.focus();
    if (editable(el)) {
      const done = document.execCommand?.("selectAll", false) && document.execCommand("insertText", false, value);
      if (!done) {
        el.textContent = value;
        el.dispatchEvent(new InputEvent("input", { bubbles: true }));
      }
      return { done: true, note: "" };
    }
    setValue(el, value);
    const list = el.getAttribute("role") === "combobox" || el.hasAttribute("aria-autocomplete");
    return { done: true, note: list ? "Pick the matching item from the site's list." : "" };
  }

  function pageContext() {
    const main = document.querySelector("main, [role='main'], article") || document.body;
    return clean(`${document.title}\n${textOf(document.querySelector("h1"))}\n${textOf(main)}`).slice(0, MAX_CONTEXT);
  }

  // --- the on-page part: a small bar plus a card under each question ---

  const STYLE = `
    :host { all: initial; }
    .card, .bar { font: 13px/1.4 system-ui, sans-serif; color: #12161c; background: #fff; border: 1px solid #c9d3e3; border-radius: 8px; }
    .card { margin: 6px 0 10px; padding: 8px; display: flex; flex-direction: column; gap: 6px; max-width: 640px; }
    .bar { position: fixed; right: 16px; bottom: 16px; z-index: 2147483647; padding: 8px 10px; display: flex; gap: 8px; align-items: center; box-shadow: 0 4px 16px rgba(18,22,28,.15); }
    .val { white-space: pre-wrap; background: #eef1f5; padding: 6px; border-radius: 6px; max-height: 160px; overflow: auto; }
    .row { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
    .tag { font-weight: 600; font-size: 12px; }
    .fact, .saved { color: #1d7a3e; } .check { color: #8f5500; } .you, .none { color: #485060; }
    .note { color: #485060; font-size: 12px; }
    button { font: inherit; font-weight: 600; padding: 4px 9px; border-radius: 6px; border: 1px solid #2a5fd6; background: #2a5fd6; color: #fff; cursor: pointer; }
    button.ghost { background: #fff; color: #2a5fd6; }
    button:disabled { opacity: .5; cursor: default; }
  `;
  const TAGS = { fact: "From your profile", saved: "Your saved answer", check: "Check this", you: "Answer yourself", none: "No answer" };

  const state = { on: false, fields: new Map(), answers: new Map(), cards: new Map(), asking: false, bar: null, msg: "" };

  function shadowHost(id) {
    const host = document.createElement("div");
    host.id = id;
    const root = host.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = STYLE;
    root.append(style);
    return { host, root };
  }

  function node(tag, attrs = {}, ...kids) {
    const n = Object.assign(document.createElement(tag), attrs);
    n.append(...kids.filter((k) => k !== null && k !== undefined && k !== ""));
    return n;
  }

  function anchorOf(field) {
    const last = field.inputs ? field.inputs[field.inputs.length - 1] : field.el;
    const box = last.closest("fieldset, [role='radiogroup'], [role='group']") || last.closest("label") || last;
    return box.parentElement && box.parentElement !== document.body ? box : last;
  }

  function sig(field) {
    return `${field.kind}|${field.label}|${(field.options || []).join("|")}`;
  }

  function drawCard(field) {
    const answer = state.answers.get(sig(field));
    if (!answer) return;
    let card = state.cards.get(field.id);
    if (!card) {
      card = shadowHost("jj-card");
      card.host.className = "jj-forms-card";
      anchorOf(field).insertAdjacentElement("afterend", card.host);
      state.cards.set(field.id, card);
    }
    const status = node("span", { className: "note" });
    const kids = [node("div", { className: `tag ${answer.status}`, textContent: TAGS[answer.status] || answer.status })];
    if (answer.value) {
      const fillBtn = node("button", { textContent: "Fill" });
      fillBtn.onclick = async () => {
        fillBtn.disabled = true;
        status.textContent = (await fill(field, answer.value)).note || "Filled. Check it.";
        fillBtn.disabled = false;
      };
      const copyBtn = node("button", { className: "ghost", textContent: "Copy" });
      copyBtn.onclick = async () => {
        await navigator.clipboard.writeText(answer.value);
        status.textContent = "Copied.";
      };
      kids.push(node("div", { className: "val", textContent: answer.value }), node("div", { className: "row" }, fillBtn, copyBtn, status));
    }
    if (answer.note) kids.push(node("div", { className: "note", textContent: answer.note }));
    card.root.querySelector(".card")?.remove();
    card.root.append(node("div", { className: "card" }, ...kids));
  }

  function drawBar() {
    if (!state.bar) {
      state.bar = shadowHost("jj-forms-root");
      document.documentElement.append(state.bar.host);
    }
    const count = state.fields.size;
    const ready = [...state.fields.values()].filter((f) => FILL_ALL.has(state.answers.get(sig(f))?.status)).length;
    const kids = [node("strong", { textContent: "job-jarvis" })];
    if (!state.on) {
      const start = node("button", { textContent: `Suggest answers (${count})` });
      start.onclick = () => activate();
      kids.push(start);
    } else {
      const all = node("button", { textContent: `Fill all (${ready})`, disabled: !ready || state.asking });
      all.onclick = () => fillAll();
      kids.push(all);
    }
    const close = node("button", { className: "ghost", textContent: "Hide" });
    close.onclick = () => stop();
    kids.push(close, node("span", { className: "note", textContent: state.asking ? "Thinking…" : state.msg }));
    state.bar.root.querySelector(".bar")?.remove();
    state.bar.root.append(node("div", { className: "bar" }, ...kids));
    state.bar.host.style.display = count || state.on ? "" : "none";
  }

  async function fillAll() {
    let filled = 0;
    const left = [];
    for (const field of state.fields.values()) {
      const a = state.answers.get(sig(field));
      if (a && FILL_ALL.has(a.status) && a.value) {
        if ((await fill(field, a.value)).done) filled++;
      } else if (a?.status === "check") {
        left.push(field);
      }
    }
    state.msg = `Filled ${filled}. ${left.length ? `${left.length} need your check.` : ""} You press Submit.`;
    drawBar();
  }

  async function settings() {
    const { details = {}, prefs = {} } = await chrome.storage.local.get(["details", "prefs"]);
    return { details, prefs };
  }

  function api(path, body) {
    return new Promise((resolve, reject) =>
      chrome.runtime.sendMessage({ type: "api", path, init: { method: "POST", body: JSON.stringify(body) } }, (r) => (r?.ok ? resolve(r.data) : reject(new Error(r?.error || "job-jarvis did not answer")))),
    );
  }

  async function ask() {
    const missing = [...state.fields.values()].filter((f) => !state.answers.has(sig(f)));
    if (!missing.length || state.asking) return;
    const { details, prefs } = await settings();
    const toServer = [];
    for (const f of missing) {
      const local = f.contact && details[f.contact];
      if (f.contact) state.answers.set(sig(f), local ? { value: local, status: "saved", note: "" } : { value: "", status: "none", note: "Add it in the extension settings." });
      else toServer.push(f);
    }
    let failed = false;
    if (toServer.length) {
      state.asking = true;
      drawBar();
      try {
        const fields = toServer.map(({ id, label, kind, options, max_len }) => ({ id, label, kind, options, max_len }));
        const { items } = await api("/ext/form", { url: location.href, title: document.title, context: pageContext(), fields, prefs });
        const byId = new Map(items.map((i) => [i.id, i]));
        for (const f of toServer) {
          state.answers.set(sig(f), byId.get(f.id) || { value: "", status: "none", note: "No answer came back." });
        }
        state.msg = "";
      } catch (e) {
        state.msg = e.message;
        failed = true;
      } finally {
        state.asking = false;
      }
    }
    if (failed) return drawBar();
    for (const f of state.fields.values()) drawCard(f);
    drawBar();
    if ([...state.fields.values()].some((f) => !state.answers.has(sig(f)))) ask();
  }

  function rescan() {
    const found = collect();
    const live = new Set(found.map((f) => f.id));
    for (const [id, card] of state.cards) {
      if (!live.has(id)) {
        card.host.remove();
        state.cards.delete(id);
      }
    }
    state.fields = new Map(found.map((f) => [f.id, f]));
    drawBar();
    if (state.on) ask();
  }

  let timer = null;
  const ours = (n) => n.nodeType === 1 && (n.id === "jj-forms-root" || n.classList.contains("jj-forms-card"));
  const observer = new MutationObserver((records) => {
    const only = (r) => r.target.closest?.("#jj-forms-root, .jj-forms-card") || [...r.addedNodes, ...r.removedNodes].every(ours);
    if (records.every(only)) return;
    clearTimeout(timer);
    timer = setTimeout(rescan, 600);
  });

  function activate() {
    state.on = true;
    rescan();
  }

  function stop() {
    state.on = false;
    observer.disconnect();
    state.cards.forEach((c) => c.host.remove());
    state.cards.clear();
    state.bar?.host.remove();
    state.bar = null;
  }

  function watch() {
    observer.observe(document.documentElement, { childList: true, subtree: true });
    rescan();
  }

  globalThis.jjForms = { collect, labelFor, fill, contactKey, start: () => (watch(), activate()), watch };
  if (globalThis.chrome?.runtime?.id) watch();
})();
