// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

const HERE = dirname(fileURLToPath(import.meta.url));
const SOURCE = readFileSync(join(HERE, "../../extension/forms.js"), "utf8");
let jj;

beforeAll(() => {
  new Function(SOURCE)();
  jj = globalThis.jjForms;
});

beforeEach(() => {
  document.body.innerHTML = "";
});

const page = (html) => {
  document.body.innerHTML = html;
  return jj.collect(document);
};
const view = (fields) => fields.map(({ label, kind, options }) => (options ? { label, kind, options } : { label, kind }));

describe("collect", () => {
  it("reads labels the common ways", () => {
    const got = page(`
      <form>
        <label for="a">Current company</label><input id="a">
        <label>Why this role? <textarea></textarea></label>
        <span id="q3">Years of Python</span><input type="number" aria-labelledby="q3">
        <input placeholder="Portfolio link">
        <div class="question"><div>What is your notice period?</div><div><input name="np"></div></div>
      </form>`);
    expect(view(got)).toEqual([
      { label: "Current company", kind: "text" },
      { label: "Why this role?", kind: "textarea" },
      { label: "Years of Python", kind: "number" },
      { label: "Portfolio link", kind: "text" },
      { label: "What is your notice period?", kind: "text" },
    ]);
  });

  it("reads selects and choice groups, skipping placeholders and consent boxes", () => {
    const got = page(`
      <label for="s">Highest degree</label>
      <select id="s"><option value="">Select an option</option><option value="1">B.Tech</option><option value="2">M.Tech</option></select>
      <fieldset><legend>Are you willing to relocate?</legend>
        <label><input type="radio" name="r" value="y"> Yes</label>
        <label><input type="radio" name="r" value="n"> No</label></fieldset>
      <div>Which languages do you use?</div>
      <div><label><input type="checkbox" name="l"> Python</label><label><input type="checkbox" name="l"> Go</label></div>
      <label><input type="checkbox" name="t"> I agree to the terms</label>`);
    expect(view(got)).toEqual([
      { label: "Highest degree", kind: "select", options: ["B.Tech", "M.Tech"] },
      { label: "Are you willing to relocate?", kind: "radio", options: ["Yes", "No"] },
      { label: "Which languages do you use?", kind: "checkbox", options: ["Python", "Go"] },
    ]);
  });

  it("keeps styled radios whose label shows, and drops hidden, search and nav boxes", () => {
    const got = page(`
      <header><input placeholder="Search jobs"></header>
      <input type="hidden" name="csrf"><input type="password" name="pw">
      <div style="display:none"><label>Old field <input></label></div>
      <input type="search" placeholder="Find">
      <p>Do you have a valid passport?</p>
      <div><input type="radio" id="p1" name="pp" style="display:none"><label for="p1">Yes</label>
           <input type="radio" id="p2" name="pp" style="display:none"><label for="p2">No</label></div>`);
    expect(view(got)).toEqual([{ label: "Do you have a valid passport?", kind: "radio", options: ["Yes", "No"] }]);
  });

  it("reads only the open dialog when one has fields", () => {
    const got = page(`
      <label>Page box <input></label>
      <div role="dialog"><label>Mobile phone number <input type="tel"></label></div>`);
    expect(got.map((f) => [f.label, f.contact])).toEqual([["Mobile phone number", "phone"]]);
  });

  it("reads editable boxes as long text", () => {
    const got = page(`<div id="h">Profile summary</div><div contenteditable="true" aria-labelledby="h"></div>`);
    expect(view(got)).toEqual([{ label: "Profile summary", kind: "textarea" }]);
  });
});

describe("contactKey", () => {
  it.each([
    ["First Name", "firstName"],
    ["Email address", "email"],
    ["LinkedIn Profile URL", "linkedin"],
    ["Github URL", "github"],
    ["Notice period", null],
  ])("%s", (label, key) => expect(jj.contactKey(label)).toBe(key));
});

describe("fill", () => {
  it("sets a text box so the site's own listeners see it", () => {
    const [field] = page(`<label>City <input></label>`);
    const seen = [];
    field.el.addEventListener("input", (e) => seen.push(e.target.value));
    expect(jj.fill(field, "Bengaluru")).toEqual({ done: true, note: "" });
    expect(seen).toEqual(["Bengaluru"]);
  });

  it("picks select options, radios and checkboxes by their text", () => {
    const [sel, radio, box] = page(`
      <label>Degree <select><option value="">Pick</option><option value="bt">B.Tech</option></select></label>
      <fieldset><legend>Relocate?</legend><label><input type="radio" name="r"> Yes</label><label><input type="radio" name="r"> No</label></fieldset>
      <fieldset><legend>Skills</legend><label><input type="checkbox" name="s"> Python</label><label><input type="checkbox" name="s" checked> Java</label></fieldset>`);
    jj.fill(sel, "b.tech");
    jj.fill(radio, "No");
    jj.fill(box, "Python");
    expect(sel.el.value).toBe("bt");
    expect(radio.inputs.map((i) => i.checked)).toEqual([false, true]);
    expect(box.inputs.map((i) => i.checked)).toEqual([true, false]);
    expect(jj.fill(sel, "PhD").done).toBe(false);
  });

  it("fills an editable box", () => {
    const [field] = page(`<div id="h">About</div><div contenteditable="true" aria-labelledby="h"></div>`);
    jj.fill(field, "I build AI systems.");
    expect(field.el.textContent).toBe("I build AI systems.");
  });
});

describe("on the page", () => {
  it("asks job-jarvis once, shows a card per question and fills on click", async () => {
    const sent = [];
    globalThis.chrome = {
      storage: { local: { get: async () => ({ details: { email: "me@example.com" }, prefs: { "Notice period": "15 days" } }) } },
      runtime: {
        sendMessage: (msg, reply) => {
          sent.push(JSON.parse(msg.init.body));
          const id = sent.at(-1).fields[0].id;
          reply({ ok: true, data: { items: [{ id, value: "15 days", status: "saved", note: "" }] } });
        },
      },
    };
    document.body.innerHTML = `<label>Email <input type="email"></label><label>Notice period <input id="np"></label>`;
    jj.start();
    await vi.waitFor(() => expect(document.querySelectorAll(".jj-forms-card")).toHaveLength(2));
    expect(sent).toHaveLength(1);
    expect(sent[0].fields.map((f) => f.label)).toEqual(["Notice period"]);
    expect(sent[0].prefs).toEqual({ "Notice period": "15 days" });

    const bar = document.getElementById("jj-forms-root").shadowRoot;
    bar.querySelector("button").click();
    expect(document.querySelector("input[type=email]").value).toBe("me@example.com");
    expect(document.getElementById("np").value).toBe("15 days");
    expect(bar.textContent).toContain("Filled 2.");
  });
});
