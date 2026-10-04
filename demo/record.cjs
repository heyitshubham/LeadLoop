// Records the technical LeadLoop demo: architecture slides, a live run of the app with a step bar
// and a label naming the component at work, then economics and time-saved slides filled with
// numbers measured during this run.
//
// Capture: pages render at 1.5x zoom in a 1920x1200 window and frames are grabbed as JPEGs over
// CDP (Playwright's built-in video is too compressed to read small text). Writes marks.json:
// frame times, when each narration line starts, and silent moments the editor must keep.
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const DIR = process.env.WORK || path.join(__dirname, "work");
const DUR = JSON.parse(fs.readFileSync(path.join(DIR, "durations.json")));
const BASE = process.env.BASE || "http://localhost:8002";
const MAILPIT = "http://localhost:8025";
const SLIDES = "file://" + path.join(__dirname, "slides.html");
const FRAMES = path.join(DIR, "frames");
const STEPS = ["Discover", "Qualify + sample", "Approve pitch", "Negotiate", "Close", "Analytics"];

// Runs in every page: zoom the content, and add a cursor ring (overlays sit on <html>, outside the zoom).
const INIT = `(() => { const go = () => {
  document.body.style.zoom = "1.5";
  // vh units are zoomed too, so full-height side panels would be 1.5x the window: correct them.
  const fix = document.createElement("style");
  fix.textContent = "aside { height: calc(100vh / 1.5) !important; padding-bottom: 90px !important; }";
  document.head.appendChild(fix);
  const d = document.createElement("div"); d.id = "__cur";
  Object.assign(d.style, { position: "fixed", left: "-60px", top: "-60px", width: "30px", height: "30px",
    borderRadius: "50%", background: "rgba(47,91,234,.30)", border: "3px solid #2f5bea", zIndex: 2147483647,
    pointerEvents: "none", transform: "translate(-50%,-50%)", transition: "width .12s, height .12s" });
  document.documentElement.appendChild(d);
  addEventListener("mousemove", (e) => { d.style.left = e.clientX + "px"; d.style.top = e.clientY + "px"; }, true);
  addEventListener("mousedown", () => { d.style.width = d.style.height = "44px"; }, true);
  addEventListener("mouseup", () => { d.style.width = d.style.height = "30px"; }, true);
}; document.readyState === "loading" ? addEventListener("DOMContentLoaded", go) : go(); })();`;

(async () => {
  fs.rmSync(FRAMES, { recursive: true, force: true });
  fs.mkdirSync(FRAMES, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1920, height: 1200 }, colorScheme: "light" });
  await context.addInitScript(INIT);
  const page = await context.newPage();
  const cdp = await context.newCDPSession(page);
  const t0 = Date.now();
  const frames = [], marks = [], shows = [], measured = {};
  cdp.on("Page.screencastFrame", (f) => {
    const file = String(frames.length).padStart(6, "0") + ".jpg";
    fs.writeFileSync(path.join(FRAMES, file), Buffer.from(f.data, "base64"));
    frames.push({ f: file, t: f.metadata.timestamp - t0 / 1000 });
    cdp.send("Page.screencastFrameAck", { sessionId: f.sessionId }).catch(() => {});
  });
  // Re-armed after every navigation; "already active" just means it survived the navigation.
  const cast = () => cdp.send("Page.startScreencast", { format: "jpeg", quality: 88, maxWidth: 1920, maxHeight: 1200, everyNthFrame: 2 }).catch(() => {});
  await cast();

  const now = () => (Date.now() - t0) / 1000;
  const hold = (ms) => page.waitForTimeout(ms);
  const log = (msg) => console.log(`${now().toFixed(1).padStart(6)}s  ${msg}  [${frames.length} frames]`);
  const moveTo = (x, y) => page.mouse.move(x, y, { steps: 20 });
  const point = async (loc) => {
    await loc.scrollIntoViewIfNeeded();
    const b = await loc.boundingBox();
    if (b) await moveTo(b.x + b.width / 2, b.y + b.height / 2);
  };
  const click = async (loc) => { await point(loc); await hold(160); await loc.click(); };
  const show = async (ms) => { shows.push({ t: now(), d: ms / 1000 }); await hold(ms); };
  const say = async (id, during) => {
    marks.push({ id, t: now() });
    log(id);
    const end = Date.now() + DUR[id] * 1000 + 400;
    while (Date.now() < end) { if (during) await during(); await hold(250); }
  };

  // Bottom bar on the live demo: the six steps (current one highlighted) and what's running behind it.
  let bar = null;
  const drawBar = () => bar && page.evaluate(({ steps, step, tag }) => {
    let el = document.getElementById("__bar");
    if (!el) {
      el = document.createElement("div"); el.id = "__bar";
      Object.assign(el.style, { position: "fixed", left: "0", right: "0", bottom: "0", zIndex: 2147483646,
        background: "rgba(13,17,23,.95)", color: "#e8eaee", padding: "16px 28px 18px", borderTop: "1px solid #2b3240",
        fontFamily: "ui-sans-serif, system-ui, -apple-system, sans-serif" });
      document.documentElement.appendChild(el);
    }
    el.innerHTML = `<div style="display:flex; gap:10px; align-items:center; margin-bottom:10px">` + steps.map((s, i) => {
      const n = i + 1, on = n === step, done = n < step;
      return `<div style="display:flex; align-items:center; gap:9px; padding:7px 15px 7px 9px; border-radius:999px; font-size:19px; font-weight:${on ? 700 : 500};
        background:${on ? "#2f5bea" : "transparent"}; color:${on ? "#fff" : done ? "#8fd3ac" : "#7d8794"}; border:1.5px solid ${on ? "#2f5bea" : done ? "#2f6b4c" : "#2b3240"}">
        <span style="display:grid; place-items:center; width:28px; height:28px; border-radius:50%; font-size:15px; font-weight:700;
          background:${on ? "#fff" : done ? "#1f8a4c" : "#222a36"}; color:${on ? "#2f5bea" : "#fff"}">${done ? "✓" : n}</span>${s}</div>` +
        (n < steps.length ? `<span style="color:#465063; font-size:18px">›</span>` : "");
    }).join("") + `</div><div style="font:17px ui-monospace, Menlo, monospace; color:#aab4c2">${tag}</div>`;
  }, { steps: STEPS, ...bar });
  const setBar = async (step, tag) => { bar = { step, tag }; await drawBar(); };
  const nav = async (url) => { await page.goto(url); await cast(); await drawBar(); };
  const slide = async (name, data = {}) => {
    bar = null;
    await page.goto(`${SLIDES}?s=${name}&d=${encodeURIComponent(JSON.stringify(data))}`);
    await cast();
    await page.evaluate(() => { const c = document.getElementById("__cur"); if (c) c.style.display = "none"; }); // no cursor on slides
  };
  const api = (p) => fetch(`${BASE}${p}`).then((r) => r.json());
  const runToBottom = () => page.evaluate(() => { const r = document.getElementById("run"); if (r) r.scrollTop = r.scrollHeight; });

  // 1. Title, architecture, deal graph
  await slide("title"); await hold(700);
  await say("title");
  await slide("arch"); await hold(300);
  await say("arch");
  await slide("graph"); await hold(300);
  await say("graph");

  // 2. Step 1: live discovery over SSE
  bar = { step: 1, tag: "GET /api/discover/stream · Server-Sent Events · SerpClient: disk cache → live SerpApi search" };
  await nav(BASE); await hold(700);
  let t = Date.now();
  await click(page.locator("#discover"));
  await say("discover", runToBottom);
  await page.waitForSelector("#run-view", { timeout: 600000 });
  measured.discover_s = (Date.now() - t) / 1000;
  const vetStep = page.locator("#run .step", { hasText: "Checking who would actually buy" });
  await vetStep.scrollIntoViewIfNeeded(); await point(vetStep.locator(".sum"));
  await setBar(1, "Groq gpt-oss-120b · strict json_schema · batches of 15 · re-asks anything skipped");
  await show(2400);
  await click(page.locator("#run-view")); await hold(400);
  await setBar(1, "score_lead(): deterministic, a reason for every point · market → currency (INR / USD)");
  await say("board");

  // 3. Step 2 -> 3: qualify, google_maps sample, draft, interrupt()
  const card = page.locator("#c-found .card", { hasText: "INR" }).first();
  await click(card); await hold(400);
  await setBar(2, "LangGraph thread per deal · qualify (Groq) → sample (SerpApi google_maps) → draft_pitch");
  t = Date.now();
  await click(page.locator("#start"));
  await say("deal", async () => {
    if (!measured.deal_s && await page.locator("text=Waiting for your decision").count()) {
      measured.deal_s = (Date.now() - t) / 1000;
      await setBar(3, "interrupt() · state checkpointed by SqliteSaver · waiting for Command(resume=decision)");
      await point(page.locator("#drawer .gate"));
    }
  });
  await page.waitForSelector("text=Waiting for your decision", { timeout: 180000 });
  if (!measured.deal_s) measured.deal_s = (Date.now() - t) / 1000;
  await setBar(3, "interrupt() · state checkpointed by SqliteSaver · waiting for Command(resume=decision)");
  await point(page.locator("#drawer h4", { hasText: "Free sample" })); await show(2000);
  await point(page.locator("#drawer .gate")); await show(1600);
  await click(page.locator("#approve"));
  await page.waitForSelector("#simulate", { timeout: 60000 });

  // 4. The email in the sandbox inbox (skipped, not fatal, if it never arrives)
  let mails = 0;
  for (let i = 0; i < 30 && !mails; i++) {
    // Ask Mailpit from Node, not from the page: the browser blocks that cross-origin request.
    mails = await fetch(`${MAILPIT}/api/v1/messages`).then((r) => r.json()).then((j) => j.total).catch(() => 0);
    if (!mails) await hold(500);
  }
  bar = { step: 3, tag: "send_pitch · SMTP with Message-ID / In-Reply-To threading · CSV sample attached" };
  if (mails) {
    await nav(MAILPIT); await hold(900);
    await click(page.locator(".message, a[href*='/view/']").first()); await hold(600);
  } else {
    console.log("WARNING: no email reached Mailpit; check the server log. Narrating over the board instead.");
  }
  await say("smtp");

  // 5. Step 4 -> 5: ask price, object twice, accept
  bar = { step: 4, tag: "negotiate node · Groq read_reply() → pricing.quote(): discount ladder + hard floor" };
  await nav(BASE); await hold(700);
  await click(page.locator("#c-waiting .card").first()); await hold(400);
  const reply = async (preset) => {
    await click(page.locator(`[data-preset="${preset}"]`)); await hold(200);
    const t1 = Date.now();
    await click(page.locator("#simulate"));
    await page.waitForSelector("#drawer .gate >> text=Waiting for your decision", { timeout: 120000 });
    if (!measured.reply_s) measured.reply_s = (Date.now() - t1) / 1000;
    await hold(300);
  };
  const approve = async () => { await click(page.locator("#approve")); await page.waitForSelector("#simulate", { timeout: 60000 }); await hold(250); };
  await reply(0);
  await point(page.locator("#drawer .quote").first());
  await say("negotiate");
  await approve();
  await reply(1); await point(page.locator("#drawer .quote").first()); await show(1700); await approve();
  await reply(1); await point(page.locator("#drawer .quote").first()); await show(1700); await approve();
  await reply(3); await click(page.locator("#approve"));
  await page.waitForFunction(() => document.querySelector("#drawer .stage")?.textContent.trim() === "won", null, { timeout: 60000 });
  await setBar(5, "send_reply → END · stage = won · every agent, human and lead step in the audit trail");
  await point(page.locator("#drawer .stage")); await show(1800);

  // 6. Step 6: analytics
  await page.keyboard.press("Escape"); await hold(200);
  await setBar(6, "GET /api/stats · aggregated from LangGraph state · totals per currency, never mixed");
  await click(page.locator('button[data-view="insights"]'));
  await page.waitForSelector("text=Agent flow", { timeout: 30000 }); await hold(400);
  let ticks = 0;
  await say("insights", async () => { if (++ticks > 5) await page.mouse.wheel(0, 60); });

  // 7. Numbers measured in this run -> economics + time slides
  const deals = await api("/api/deals");
  const won = deals.find((d) => d.stage === "won");
  const health = await api("/api/health");
  const leads = await api("/api/leads");
  const data = {
    ...measured, leads: leads.length, deal_price: won?.quote?.price, deal_rows: won?.quote?.rows,
    discount: won?.quote?.discount_pct, rounds: won?.quote_history?.length, per_row: health.pricing?.INR?.per_row,
    tests: Number(process.env.TESTS) || undefined,
  };
  await slide("econ", data); await hold(300);
  await say("econ");
  await slide("time", data); await hold(300);
  await say("time");
  await slide("outro", data); await hold(300);
  await say("outro");
  await hold(900);

  const total = now();
  await cdp.send("Page.stopScreencast").catch(() => {});
  await context.close(); await browser.close();
  fs.writeFileSync(path.join(DIR, "marks.json"), JSON.stringify({ total, marks, shows, measured: data, frames }, null, 1));
  log(`done · raw ${total.toFixed(1)}s · ${frames.length} frames · measured ${JSON.stringify(data)}`);
  // The narration states these facts; flag the take if the run didn't produce them.
  if (data.deal_price !== 3200) console.log(`WARNING: deal closed at ${data.deal_price}, narration says ₹3,200`);
  const perDeal = (data.discover_s / data.leads) * 3 + data.deal_s + data.reply_s * 3;
  const manual = 10 * 60 * 3 + 30 * 60 + 8 * 60 * 3;
  if (1 - perDeal / manual < 0.99) console.log(`WARNING: time saved ${(100 * (1 - perDeal / manual)).toFixed(1)}%, narration says over 99%`);
})().catch((e) => { console.error("RECORDING FAILED:", e.message); process.exit(1); });
