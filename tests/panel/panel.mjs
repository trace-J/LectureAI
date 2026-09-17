/**
 * Loads a rendered panel page into a real DOM and runs its own script.
 *
 * The pages are rendered by test_panel.py through Flask, so what is loaded
 * here is the same bytes a browser gets, Jinja resolved and all — including
 * `const BASE = {{ base|tojson }}`, which is the only templated value the
 * script reads and the reason a page cannot be tested as a static file.
 *
 * jsdom, rather than a hand-written stand-in, because two of the defects
 * these tests exist for turn on what a real <select> does with an option set
 * that does not contain its value. Measured against Chromium, jsdom agrees:
 * a value no option carries reads back "" with selectedIndex -1, and markup
 * in which nothing is marked selected falls back to the first option. That
 * second rule IS the 5am-becomes-6am bug, and a shim that returned "" for it
 * would have made the test pass over a shipped defect.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { JSDOM } from "jsdom";

/** Timers are recorded, never armed. */
function freezeTimers(window, timers) {
  window.setTimeout = (fn, ms) => {
    timers.push({ fn, ms, kind: "timeout" });
    return timers.length;
  };
  window.setInterval = (fn, ms) => {
    timers.push({ fn, ms, kind: "interval" });
    return timers.length;
  };
  window.clearTimeout = () => {};
  window.clearInterval = () => {};
}

/**
 * `routes` maps a path to the JSON the page should receive, or to a function
 * of (body, path) returning it. Matching is on the longest registered suffix,
 * so one map serves both "/api/setup" and the relayed
 * "/p/<device>/api/setup".
 */
function stubFetch(window, routes, calls, unrouted) {
  window.fetch = async (url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : undefined;
    calls.push({ url, method: options.method || "GET", body });
    const match = Object.keys(routes)
      .filter((route) => String(url).endsWith(route))
      .sort((a, b) => b.length - a.length)[0];
    if (!match) {
      // Every one of these pages wraps its calls in try/catch and turns a
      // failure into a flash message, so a request nobody answered would
      // otherwise let a test pass by never running the code it is about.
      unrouted.push(String(url));
      throw new Error(`no fixture for ${url}`);
    }
    const answer = routes[match];
    const payload = typeof answer === "function" ? answer(body, String(url)) : answer;
    return {
      ok: payload.__status === undefined || payload.__status < 400,
      status: payload.__status || 200,
      json: async () => payload,
    };
  };
}

export function loadPage(dir, file, { routes = {} } = {}) {
  const html = readFileSync(path.join(dir, file), "utf8");
  const calls = [];
  const timers = [];
  const unrouted = [];
  const dom = new JSDOM(html, {
    runScripts: "dangerously",
    url: "http://127.0.0.1:5173/",
    // The pages use light-dark() and color-mix(), which this version cannot
    // parse and would otherwise report on stderr for every single load.
    virtualConsole: undefined,
    beforeParse(window) {
      freezeTimers(window, timers);
      stubFetch(window, routes, calls, unrouted);
      window.open = () => null;
      window.confirm = () => true;
      window.scrollTo = () => {};
      if (!window.Element.prototype.scrollIntoView) {
        window.Element.prototype.scrollIntoView = () => {};
      }
    },
  });

  const { window } = dom;
  const page = {
    window,
    document: window.document,
    calls,
    timers,
    $: (id) => window.document.getElementById(id),
    /** Drain pending microtasks; nothing here waits on wall time. */
    async settle(rounds = 8) {
      for (let i = 0; i < rounds; i++) {
        await new Promise((resolve) => setImmediate(resolve));
      }
    },
    /** Run the callbacks the page asked to have scheduled. */
    async fire() {
      const pending = timers.splice(0, timers.length);
      for (const timer of pending) await timer.fn();
      await page.settle();
    },
    /** Drain what the page still has in flight, then tear it down.
     *
     * Not optional: these pages start requests on load, and closing the
     * window out from under a pending continuation takes node down with a
     * TypeError from inside the page rather than a test failure.
     */
    async close() {
      await page.settle();
      window.close();
      if (unrouted.length) {
        throw new Error(
          `the page called ${unrouted.length} route(s) with no fixture, so ` +
            `whatever they feed was never exercised: ${unrouted.join(", ")}`
        );
      }
    },
  };
  return page;
}

/** The same shape every other suite in this repo prints. */
export function runner() {
  const results = [];
  return {
    async run(label, fn) {
      try {
        await fn();
      } catch (exc) {
        const detail = exc && exc.stack ? exc.stack.split("\n").slice(0, 4).join("\n      ") : exc;
        console.log(`FAIL  ${label}\n      ${detail}`);
        results.push(false);
        return;
      }
      console.log(`ok    ${label}`);
      results.push(true);
    },
    finish() {
      const passed = results.filter(Boolean).length;
      console.log();
      console.log(`${passed}/${results.length} passed`);
      process.exit(passed === results.length ? 0 : 1);
    },
  };
}

export function assert(condition, message) {
  if (!condition) throw new Error(message);
}

export function equal(got, want, message) {
  const same = JSON.stringify(got) === JSON.stringify(want);
  if (!same) {
    throw new Error(`${message}\n      got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
  }
}
