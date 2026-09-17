/**
 * What the panel's pages do, checked by running them.
 *
 * Driven by test_panel.py, which renders the pages through Flask and passes
 * the directory it wrote them to. Each test loads a page, feeds it the JSON
 * its own routes would return, calls the page's own functions, and asserts
 * against the resulting DOM.
 *
 * The fixtures start as what the live routes actually returned this run, and
 * a test overrides only what it is about. Nothing here hard-codes a payload
 * shape that could drift away from the route it stands for.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { assert, equal, loadPage, runner } from "./panel.mjs";

const DIR = process.argv[2];
const REAL = JSON.parse(readFileSync(path.join(DIR, "routes.json"), "utf8"));
const { run, finish } = runner();

/** A deep copy of a captured payload, with `changes` laid over the top. */
function like(route, changes = {}) {
  return { ...structuredClone(REAL[route]), ...changes };
}

/** The setup page calls these on load whatever a test is about. */
function setupRoutes(extra = {}) {
  return {
    "/api/setup": like("/api/setup"),
    "/api/doctor": like("/api/doctor"),
    "/api/account": like("/api/account"),
    "/api/login-item": like("/api/login-item"),
    ...extra,
  };
}

// --- The behavior two of the findings turn on ------------------------------

await run("a <select> whose value no option carries reads back empty", async () => {
  // Not a test of the app: a test of the harness, kept because the value of
  // everything below depends on it. Measured against Chromium, which gives
  // the same three answers.
  const page = loadPage(DIR, "setup.html", { routes: setupRoutes() });
  const { document } = page;
  const sel = document.createElement("select");
  sel.innerHTML = '<option value="6">6</option><option value="7">7</option>';
  equal([sel.value, sel.selectedIndex], ["6", 0], "markup with nothing selected takes the first");
  sel.value = "23";
  equal([sel.value, sel.selectedIndex], ["", -1], "a value no option carries reads back empty");
  await page.close();
});

// --- F9: an hour the backend accepts must survive a round trip -------------

await run("an early or late class keeps its hour through setup", async () => {
  for (const hour of [0, 5, 23]) {
    const settings = like("/api/setup", {
      schedule: [{ day: "Mon", start: hour, course: "ENTR-4306" }],
    });
    const page = loadPage(DIR, "setup.html", {
      routes: setupRoutes({ "/api/setup": settings }),
    });
    await page.window.load();
    await page.settle();
    const start = page.document.querySelector("#rows select.start");
    assert(start, "the schedule table rendered no row");
    equal(Number(start.value), hour,
      `a class at ${hour}:00 was moved by opening settings`);
    // And it survives being read back out the way Save reads it.
    equal(page.window.readRows()[0].start, hour, `reading row back changed ${hour}:00`);
    await page.close();
  }
});

// --- F13: the course picker has to follow the schedule ---------------------

await run("the course picker follows the schedule when it changes", async () => {
  const first = like("/api/status", { courses: ["ACCT-4321", "ENTR-3306"] });
  const page = loadPage(DIR, "index.html", { routes: { "/api/status": first } });
  await page.window.poll();
  await page.settle();
  const picker = page.$("courseSel");
  equal([...picker.options].map((o) => o.value).filter(Boolean),
    ["ACCT-4321", "ENTR-3306"], "the picker did not render the schedule");

  // The schedule changes in another window, or syncs from the account.
  page.window.fetch = async () => ({
    ok: true, status: 200,
    json: async () => like("/api/status", { courses: ["MATH-1100"] }),
  });
  await page.window.poll();
  await page.settle();
  equal([...picker.options].map((o) => o.value).filter(Boolean), ["MATH-1100"],
    "the picker still offers a course that left the schedule");
  await page.close();
});

await run("a course the user had picked is kept only while it exists", async () => {
  const page = loadPage(DIR, "index.html", {
    routes: { "/api/status": like("/api/status", { courses: ["ACCT-4321", "ENTR-3306"] }) },
  });
  await page.window.poll();
  await page.settle();
  const picker = page.$("courseSel");
  picker.value = "ENTR-3306";

  page.window.fetch = async () => ({
    ok: true, status: 200,
    json: async () => like("/api/status", { courses: ["ACCT-4321", "ENTR-3306"] }),
  });
  await page.window.poll();
  await page.settle();
  equal(picker.value, "ENTR-3306", "an unchanged schedule discarded the user's choice");

  page.window.fetch = async () => ({
    ok: true, status: 200,
    json: async () => like("/api/status", { courses: ["ACCT-4321"] }),
  });
  await page.window.poll();
  await page.settle();
  equal(picker.value, "", "a course that left the schedule stayed selected");
  await page.close();
});

// --- R1: course text reaches markup, and must not carry anything with it ---

await run("a course cannot smuggle an attribute into the picker", async () => {
  // The exploit is the quote, not the tag: inside a <select> the parser
  // drops element tags anyway, so an injected <img> was never the danger.
  // A quote closes value="..." and whatever follows becomes an attribute.
  const nasty = 'X" onmouseover="steal()" data-x="';
  const page = loadPage(DIR, "index.html", {
    routes: { "/api/status": like("/api/status", { courses: [nasty] }) },
  });
  await page.window.poll();
  await page.settle();
  const option = page.$("courseSel").options[1];
  equal([...option.attributes].map((a) => a.name).sort(), ["value"],
    "the course brought attributes of its own into the option");
  equal(option.value, nasty, "the course did not survive escaping intact");
  await page.close();
});

// --- F7: refreshing the microphone list is not a reload --------------------

await run("refreshing the microphone list keeps unsaved edits", async () => {
  const settings = like("/api/setup", {
    schedule: [{ day: "Mon", start: 9, course: "ENTR-4306" }],
  });
  const page = loadPage(DIR, "setup.html", {
    routes: setupRoutes({ "/api/setup": settings }),
  });
  await page.window.load();
  await page.settle();

  // Type, but do not save.
  page.document.querySelector("#rows input.course").value = "QA-UNSAVED-101";
  page.$("tolerance").value = "20";

  page.$("refreshDevices").click();
  await page.settle();

  equal(page.document.querySelector("#rows input.course").value, "QA-UNSAVED-101",
    "Refresh discarded an edited course");
  equal(page.$("tolerance").value, "20", "Refresh discarded an edited tolerance");
  // And it did refresh the thing it is named after.
  assert(page.$("device").options.length > 0, "Refresh left no microphones listed");
  await page.close();
});

// --- F16: an account change has to reach the parts it governs --------------

await run("signing out brings back this Mac's own key fields", async () => {
  const managed = like("/api/setup", { managed: true, managed_email: "me@example.com" });
  const signedIn = like("/api/account", { signed_in: true, email: "me@example.com" });
  let signedOut = false;
  const page = loadPage(DIR, "setup.html", {
    routes: setupRoutes({
      "/api/setup": () => (signedOut ? like("/api/setup", { managed: false }) : managed),
      "/api/account": () => (signedOut ? like("/api/account", { signed_in: false })
                                       : signedIn),
      "/api/account/signout": () => { signedOut = true; return { ok: true }; },
    }),
  });
  await page.window.load();
  await page.window.loadAccount();
  await page.settle();
  equal(page.$("ownKeys").hidden, true, "a managed Mac should not be asked for keys");

  page.$("accountOff").click();
  await page.settle();
  await page.fire();

  equal(page.$("ownKeys").hidden, false,
    "after signing out the key fields are still hidden");
  equal(page.$("managedKeys").hidden, true,
    "after signing out the account is still shown as providing keys");
  await page.close();
});

// --- The relay road --------------------------------------------------------

await run("a relayed page writes every request under its device path", async () => {
  const page = loadPage(DIR, "index.relayed.html", {
    routes: { "/api/status": like("/api/status") },
  });
  await page.window.poll();
  await page.settle();
  assert(page.calls.length > 0, "the relayed page made no requests at all");
  for (const call of page.calls) {
    assert(String(call.url).includes("/p/3XwJMPZ3_2RREYbt/api/"),
      `a relayed request skipped the device path: ${call.url}`);
  }
  await page.close();
});

finish();
