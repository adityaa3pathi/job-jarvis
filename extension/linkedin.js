// Run inside the LinkedIn tab only when the owner clicks a button in the popup. These read
// the page or fill LinkedIn's own boxes; they never press Send, connect, like or post.
function jjWait(find, ms = 6000) {
  return new Promise((resolve) => {
    const start = Date.now();
    const tick = () => {
      const found = find();
      if (found || Date.now() - start > ms) return resolve(found || null);
      setTimeout(tick, 150);
    };
    tick();
  });
}

function jjText(el) {
  return (el?.innerText || el?.textContent || "").replace(/\s+/g, " ").trim();
}

function jjReadProfile() {
  const top = document.querySelector("main section") || document.querySelector("main");
  const name = jjText(top?.querySelector("h1"));
  const headline = jjText(top?.querySelector(".text-body-medium"));
  return { name, title: headline.slice(0, 160), url: location.href.split("?")[0] };
}

function jjReadJob() {
  const pick = (sels) => sels.map((s) => document.querySelector(s)).find((el) => el && jjText(el));
  const title = jjText(pick([".job-details-jobs-unified-top-card__job-title", ".jobs-unified-top-card__job-title", "main h1"]));
  const company = jjText(
    pick([".job-details-jobs-unified-top-card__company-name a", ".job-details-jobs-unified-top-card__company-name", ".jobs-unified-top-card__company-name", "main a[href*='/company/']"]),
  );
  const place = jjText(pick([".job-details-jobs-unified-top-card__primary-description-container", ".job-details-jobs-unified-top-card__tertiary-description-container", ".jobs-unified-top-card__bullet"]));
  const description = jjText(pick(["#job-details", ".jobs-description__content", ".jobs-box__html-content"]));
  return { url: location.href, title, company, location: place.split("·")[0].trim(), description: description.slice(0, 50000) };
}

function jjOnceSent(outreachId, isSend) {
  let done = false;
  const handler = (event) => {
    if (done || !isSend(event)) return;
    done = true;
    document.removeEventListener("click", handler, true);
    document.removeEventListener("keydown", handler, true);
    chrome.runtime.sendMessage({ type: "sent", outreachId });
  };
  document.addEventListener("click", handler, true);
  document.addEventListener("keydown", handler, true);
}

async function jjFillInvite(text, outreachId) {
  const top = document.querySelector("main section") || document.querySelector("main");
  const buttons = () => [...document.querySelectorAll("main button, div[role='menu'] [role='button'], .artdeco-dropdown__content [role='button']")];
  if (buttons().some((b) => /^Pending\b/i.test(jjText(b)) || /withdraw invitation/i.test(b.getAttribute("aria-label") || ""))) {
    return "An invite is already pending for this person.";
  }
  let connect = buttons().find((b) => /^Invite .* to connect$/i.test(b.getAttribute("aria-label") || ""));
  if (!connect) {
    const more = top?.querySelector("button[aria-label='More actions'], button[aria-label='More']");
    more?.click();
    connect = await jjWait(() => buttons().find((b) => /to connect$/i.test(b.getAttribute("aria-label") || "") || jjText(b) === "Connect"));
  }
  if (!connect) return "No Connect button found. You may already be connected; use Fill message instead.";
  connect.click();
  const addNote = await jjWait(() => document.querySelector("div[role='dialog'] button[aria-label='Add a note']"));
  if (!addNote) return "LinkedIn's invite box did not open as expected. Add the note by hand.";
  addNote.click();
  const box = await jjWait(() => document.querySelector("div[role='dialog'] textarea[name='message'], textarea#custom-message"));
  if (!box) return "No note box found. Paste the note by hand.";
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
  setter.call(box, text);
  box.dispatchEvent(new Event("input", { bubbles: true }));
  box.focus();
  jjOnceSent(outreachId, (e) => {
    const btn = e.type === "click" && e.target.closest?.("div[role='dialog'] button");
    return !!btn && /send/i.test((btn.getAttribute("aria-label") || "") + jjText(btn));
  });
  return "Note filled in. Check it, then press Send in LinkedIn.";
}

async function jjFillMessage(text, outreachId) {
  const top = document.querySelector("main section") || document.querySelector("main");
  const message = [...(top?.querySelectorAll("button, a") || [])].find((b) => /^Message\b/i.test((b.getAttribute("aria-label") || "") + " " + jjText(b)) || jjText(b) === "Message");
  if (!message) return "No Message button found. They may not have accepted yet.";
  message.click();
  const box = await jjWait(() => [...document.querySelectorAll(".msg-form__contenteditable[contenteditable='true']")].pop());
  if (!box) return "LinkedIn's message box did not open. Paste the message by hand.";
  box.focus();
  document.execCommand("selectAll", false);
  document.execCommand("insertText", false, text);
  jjOnceSent(outreachId, (e) => {
    if (e.type === "keydown") return e.key === "Enter" && !e.shiftKey && !!e.target.closest?.(".msg-form");
    const btn = e.target.closest?.("button.msg-form__send-button, .msg-form button[type='submit']");
    return !!btn;
  });
  return "Message filled in. Check it, then press Send in LinkedIn.";
}
