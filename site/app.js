const $ = (sel) => document.querySelector(sel);
const eur = new Intl.NumberFormat("it-IT", { style: "currency", currency: "EUR", maximumFractionDigits: 0 });
const int = new Intl.NumberFormat("it-IT");
const DOW = ["dom", "lun", "mar", "mer", "gio", "ven", "sab"];
const MON = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"];
const MONTH = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"];
const KIND = { weekend: "Weekend", ponte: "Ponte", festivo: "Festa" };
const PREVIEW = 8;
const STORE = "voli-filtri";

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const fold = (s) => s.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();
const day = (iso) => { const [y, m, d] = iso.slice(0, 10).split("-").map(Number); return new Date(y, m - 1, d); };
const fmtDay = (iso) => { const d = day(iso); return `${DOW[d.getDay()]} ${d.getDate()} ${MON[d.getMonth()]}`; };
const fmtWhen = (iso) => (iso.length > 10 ? `${fmtDay(iso)} ${iso.slice(11, 16)}` : fmtDay(iso));
const daysBetween = (a, b) => Math.round((day(b) - day(a)) / 864e5);

function rangeTitle(w) {
  const a = day(w.start), b = day(w.end);
  if (a.getMonth() === b.getMonth()) return `${a.getDate()}–${b.getDate()} ${MONTH[a.getMonth()]}`;
  return `${a.getDate()} ${MON[a.getMonth()]} – ${b.getDate()} ${MON[b.getMonth()]}`;
}

let data;
let state = { view: "periodi", trip: "ar", q: "", onlyDeals: false, onlyLong: false, onlyDirect: false, budget: null };
const expanded = new Set();
const TRIPS = { ar: "fares", andata: "andata", ritorno: "ritorno" };
const tripFares = () => data[TRIPS[state.trip] ?? "fares"] ?? [];

const isDeal = (f) => f.r != null && f.p <= f.r * (1 - data.rules.discount) && f.r - f.p >= data.rules.min_saving_eur;
const off = (f) => Math.round((1 - f.p / f.r) * 100);
const rot = (f) => ((f.w * 7 + f.c.charCodeAt(0) * 13 + f.c.charCodeAt(1)) % 11) - 6;

function matches(f) {
  if (state.onlyDirect && f.s !== 0) return false;
  if (state.budget != null && f.p > state.budget) return false;
  if (state.onlyLong && data.windows[f.w].kind === "weekend") return false;
  if (state.onlyDeals && !f.deal) return false;
  if (state.q) {
    const [name, country] = data.places[f.c] || [f.c, ""];
    const hay = fold(`${name} ${country} ${f.c} ${f.a}`);
    if (!fold(state.q).split(/\s+/).every((t) => hay.includes(t))) return false;
  }
  return true;
}

/* ---------- render: periods ---------- */

function hotelLink(f, name, country) {
  if (state.trip === "ritorno") return "";
  const checkin = f.d.slice(0, 10);
  const checkout = (f.b || data.windows[f.w].end).slice(0, 10);
  if (checkout <= checkin) return "";
  const url = `https://www.booking.com/searchresults.it.html?ss=${encodeURIComponent(country ? `${name}, ${country}` : name)}`
    + `&checkin=${checkin}&checkout=${checkout}&group_adults=1&group_children=0&no_rooms=1`;
  return `<a class="hotel" href="${esc(url)}" target="_blank" rel="noopener sponsored">Hotel a ${esc(name)}</a>`;
}

function fareRow(f, i) {
  const [name, country] = data.places[f.c] || [f.c, ""];
  const stops = f.s < 0 ? "scali da verificare" : f.s === 0 ? "diretto" : f.s === 1 ? "1 scalo" : `${f.s} scali`;
  const unknown = f.d.length > 10 ? "" : " · orario da verificare";
  const cls = ["fare", f.deal && "is-deal", f.deal && f.n && "is-new"].filter(Boolean).join(" ");
  const typical = f.r != null ? `<small>tipico ${eur.format(f.r)}</small>` : "";
  const stamp = f.deal
    ? `<span class="stamp" style="--rot:${rot(f)}deg;--i:${i}" title="${off(f)}% sotto il prezzo tipico${f.n ? ", trovato nell'ultima ricerca" : ""}">
         <b>−${off(f)}%</b><small>${f.n ? "nuovo" : "anomalo"}</small></span>`
    : "";
  const book = f.l
    ? `<a class="book" href="${esc(f.l)}" target="_blank" rel="noopener sponsored" aria-label="Prenota ${esc(name)}, ${eur.format(f.p)}">Prenota<svg aria-hidden="true"><use href="#i-arrow"/></svg></a>`
    : "";
  return `<li class="${cls}">
    <div class="place"><span class="place-name">${esc(name)}</span><span class="place-country">${esc(country)}</span>${hotelLink(f, name, country)}</div>
    <div class="trip"><span class="route">${esc(f.o)}–${esc(f.a)}</span> · ${stops} · ${esc(f.k)}<br>
      <span class="when">${f.b ? `${fmtWhen(f.d)} → ${fmtWhen(f.b)}` : fmtWhen(f.d)}${unknown}</span></div>
    <div class="price"><b>${eur.format(f.p)}</b>${typical}</div>
    <div class="stamp-cell">${stamp}</div>
    ${book}
  </li>`;
}

function renderPeriods(fares) {
  const byWindow = new Map();
  for (const f of fares) (byWindow.get(f.w) || byWindow.set(f.w, []).get(f.w)).push(f);
  let stampIndex = 0;
  const html = [...byWindow.entries()].map(([wi, list]) => {
    const w = data.windows[wi];
    const deals = list.filter((f) => f.deal);
    const open = expanded.has(wi);
    const shown = open ? list : list.filter((f, i) => i < PREVIEW || f.deal);
    const hidden = list.length - shown.length;
    const days = daysBetween(w.start, w.end) + 1;
    const label = w.kind === "weekend" ? "" : ` · <span class="kind-${w.kind}">${KIND[w.kind]} · ${esc(w.label)}</span>`;
    return `<section class="page" aria-labelledby="w${wi}">
      <header class="page-head">
        <h2 class="page-title" id="w${wi}">${rangeTitle(w)}</h2>
        <p class="page-sub">${fmtDay(w.start)} → ${fmtDay(w.end)} · ${days} giorni liberi${label}</p>
        <p class="page-best">da<b>${eur.format(list[0].p)}</b>${deals.length ? `${deals.length} ${deals.length === 1 ? "anomalo" : "anomali"}` : `${list.length} mete`}</p>
      </header>
      <ol class="fares">${shown.map((f) => fareRow(f, f.deal ? stampIndex++ : 0)).join("")}</ol>
      ${hidden > 0 ? `<button class="more" type="button" data-expand="${wi}">Mostra altre ${hidden} mete</button>` : ""}
      ${open && list.length > PREVIEW ? `<button class="more" type="button" data-collapse="${wi}">Mostra meno</button>` : ""}
    </section>`;
  });
  return html.join("");
}

/* ---------- render: destinations ---------- */

function monthHeader() {
  const spans = [];
  data.windows.forEach((w) => {
    const m = day(w.start).getMonth();
    const last = spans.at(-1);
    if (last && last.m === m) last.n++;
    else spans.push({ m, n: 1 });
  });
  return spans.map((s) => `<span style="--span:${s.n}">${s.n >= 3 ? MON[s.m] : ""}</span>`).join("");
}

function renderDestinations(fares) {
  const byCity = new Map();
  for (const f of fares) (byCity.get(f.c) || byCity.set(f.c, []).get(f.c)).push(f);
  const rows = [...byCity.entries()].map(([c, list]) => {
    const best = list.reduce((a, b) => (b.p < a.p ? b : a));
    const bestOff = Math.max(0, ...list.filter((f) => f.deal).map(off));
    return { c, list, best, bestOff };
  });
  rows.sort((a, b) => b.bestOff - a.bestOff || a.best.p - b.best.p);

  const n = data.windows.length;
  const body = rows.map(({ c, list, best }) => {
    const [name, country] = data.places[c] || [c, ""];
    const byW = new Map(list.map((f) => [f.w, f]));
    const max = Math.max(...list.map((f) => f.p));
    const cells = data.windows.map((w, wi) => {
      const f = byW.get(wi);
      if (!f) return `<span class="cell is-empty"></span>`;
      const cls = ["cell", f.deal && "is-deal", f.deal && f.n && "is-new", w.kind !== "weekend" && "is-kind"].filter(Boolean).join(" ");
      const tip = `<b>${rangeTitle(w)}</b>${w.kind !== "weekend" ? ` · ${esc(w.label)}` : ""}<br>${eur.format(f.p)}${f.deal ? ` · −${off(f)}%` : ""}`;
      const label = `${name}, ${rangeTitle(w)}: ${eur.format(f.p)}`;
      const attrs = `class="${cls}" style="--h:${(0.15 + 0.85 * f.p / max).toFixed(3)}" data-tip="${esc(tip)}" aria-label="${esc(label)}"`;
      return f.l ? `<a ${attrs} href="${esc(f.l)}" target="_blank" rel="noopener"></a>` : `<span ${attrs}></span>`;
    });
    return `<li class="dest">
      <div class="place"><span class="place-name">${esc(name)}</span><span class="place-country">${esc(country)}</span></div>
      <div class="price"><b class="${best.deal ? (best.n ? "is-new" : "is-deal") : ""}">${eur.format(best.p)}</b>${best.r != null ? `<small>tipico ${eur.format(best.r)}</small>` : ""}</div>
      <div class="strip" style="--n:${n}">${cells.join("")}</div>
    </li>`;
  });
  return `<p class="legend"><span><i></i>prezzo per periodo, più alta = più cara</span>
      <span><i class="l-deal"></i>anomalo</span><span><i class="l-new"></i>anomalo, nuovo</span>
      <span><i class="l-kind"></i>ponte o festa</span><span>Tocca una barra per prenotare</span></p>
    <div class="dest-head"><span>Meta</span><span>Minimo</span><div class="months" style="--n:${n}">${monthHeader()}</div></div>
    <ol class="fares dests">${body.join("")}</ol>`;
}

/* ---------- shell ---------- */

function render() {
  const main = $("#main");
  const fares = tripFares().filter(matches);
  if (!fares.length) {
    main.innerHTML = `<div class="state"><h2>Nessuna tariffa con questi filtri</h2>
      <p>Prova ad alzare il budget o a togliere qualche filtro.</p>
      <button type="button" data-reset>Azzera i filtri</button></div>`;
  } else {
    main.innerHTML = state.view === "mete" ? renderDestinations(fares) : renderPeriods(fares);
  }
  main.setAttribute("aria-busy", "false");
}

function renderMeta() {
  const g = new Date(data.generated);
  const age = Math.floor((Date.now() - g) / 864e5);
  const list = tripFares();
  const deals = list.filter((f) => f.deal).length;
  const fresh = new Set(list.filter((f) => f.n).map((f) => `${f.w}|${f.c}`)).size;
  const when = `${DOW[g.getDay()]} ${g.getDate()} ${MON[g.getMonth()]}, ${g.toTimeString().slice(0, 5)}`;
  $("#meta").innerHTML = [
    age >= 2 ? `<span class="stale">Dati di ${age} giorni fa</span>` : `Aggiornato ${when}`,
    `<b>${data.windows.length}</b> periodi`,
    `<b>${int.format(list.length)}</b> tariffe`,
    `<b>${deals}</b> anomali${fresh ? `, <b>${fresh}</b> nuovi` : ""}`,
  ].join(" · ");

  $("#colophon").innerHTML = `
    <p>Prezzi per 1 adulto da ${esc(data.origin)}, il più basso per ogni meta e periodo. <b>Andata e ritorno</b>: si parte la sera prima o il primo giorno libero e si rientra l'ultimo giorno. <b>Solo andata</b> e <b>solo ritorno</b> mostrano il singolo volo, con gli stessi orari. Per alcune compagnie si conosce solo il giorno, non l'orario: controllalo prima di prenotare. I prezzi possono cambiare al momento della prenotazione.</p>
    <p>Un prezzo è <b>anomalo</b> se costa almeno il ${Math.round(data.rules.discount * 100)}% in meno del prezzo tipico di quella meta e fa risparmiare almeno ${eur.format(data.rules.min_saving_eur)}. Il prezzo tipico è la mediana delle tariffe degli ultimi 90 giorni, separata per andata e ritorno, solo andata e solo ritorno. <b>Nuovo</b> vuol dire segnalato su Telegram nell'ultima ricerca (solo per andata e ritorno).</p>
    <p>Alcuni link sono di affiliazione (Aviasales, Booking.com e i codici sconto): se prenoti da lì il sito riceve una piccola commissione, senza costi in più per te.</p>`;
}

function syncControls() {
  for (const b of document.querySelectorAll("[data-view]")) b.setAttribute("aria-selected", String(b.dataset.view === state.view));
  for (const r of document.querySelectorAll('input[name="trip"]')) r.checked = r.value === state.trip;
  $("#q").value = state.q;
  for (const id of ["onlyDeals", "onlyLong", "onlyDirect"]) $(`#${id}`).checked = state[id];
  const range = $("#budget");
  range.value = state.budget ?? range.max;
  $("#budgetOut").textContent = state.budget == null ? "qualsiasi prezzo" : eur.format(state.budget);
}

function update(patch) {
  Object.assign(state, patch);
  localStorage.setItem(STORE, JSON.stringify(state));
  syncControls();
  if ("trip" in patch) { expanded.clear(); renderMeta(); }
  render();
}

function bind() {
  const controls = $("#controls");
  const setControlsHeight = () => document.documentElement.style.setProperty("--controls-h", `${controls.offsetHeight}px`);
  new ResizeObserver(setControlsHeight).observe(controls);

  for (const b of document.querySelectorAll("[data-view]")) b.addEventListener("click", () => update({ view: b.dataset.view }));
  for (const r of document.querySelectorAll('input[name="trip"]')) r.addEventListener("change", () => update({ trip: r.value }));
  let t;
  $("#q").addEventListener("input", (e) => { clearTimeout(t); t = setTimeout(() => update({ q: e.target.value.trim() }), 120); });
  for (const id of ["onlyDeals", "onlyLong", "onlyDirect"]) $(`#${id}`).addEventListener("change", (e) => update({ [id]: e.target.checked }));
  const range = $("#budget");
  range.addEventListener("input", () => {
    const v = Number(range.value);
    $("#budgetOut").textContent = v >= Number(range.max) ? "qualsiasi prezzo" : eur.format(v);
  });
  range.addEventListener("change", () => { const v = Number(range.value); update({ budget: v >= Number(range.max) ? null : v }); });

  $("#main").addEventListener("click", (e) => {
    const t = e.target.closest("[data-expand],[data-collapse],[data-reset]");
    if (!t) return;
    if (t.dataset.reset != null) return update({ q: "", onlyDeals: false, onlyLong: false, onlyDirect: false, budget: null });
    const wi = Number(t.dataset.expand ?? t.dataset.collapse);
    t.dataset.expand != null ? expanded.add(wi) : expanded.delete(wi);
    render();
    if (t.dataset.collapse != null) document.getElementById(`w${wi}`)?.scrollIntoView({ block: "start" });
  });

  const tip = $("#tip");
  const show = (el) => {
    tip.innerHTML = el.dataset.tip;
    tip.hidden = false;
    const r = el.getBoundingClientRect();
    const tr = tip.getBoundingClientRect();
    tip.style.left = `${Math.min(Math.max(8, r.left + r.width / 2 - tr.width / 2), innerWidth - tr.width - 8)}px`;
    tip.style.top = `${r.top - tr.height - 8 < 8 ? r.bottom + 8 : r.top - tr.height - 8}px`;
  };
  const hide = () => { tip.hidden = true; };
  for (const type of ["pointerover", "focusin"]) document.addEventListener(type, (e) => {
    const el = e.target.closest?.("[data-tip]");
    el ? show(el) : hide();
  });
  document.addEventListener("scroll", hide, { passive: true });
}

function drawGuilloche() {
  const svg = $("#guilloche");
  const paths = [];
  for (const [R, a, k, n] of [[250, 18, 24, 14], [190, 14, 18, 12], [130, 10, 14, 10]]) {
    for (let i = 0; i < n; i++) {
      const ph = (2 * Math.PI * i) / n / k;
      let d = "";
      for (let s = 0; s <= 360; s++) {
        const t = (2 * Math.PI * s) / 360;
        const r = R + a * Math.sin(k * (t + ph)) + 6 * Math.sin(3 * t);
        d += `${s ? "L" : "M"}${(300 + r * Math.cos(t)).toFixed(1)},${(300 + r * Math.sin(t)).toFixed(1)}`;
      }
      paths.push(`<path d="${d}Z"/>`);
    }
  }
  svg.innerHTML = `<g fill="none" stroke="#E2C48A" stroke-width=".6" stroke-opacity=".35">${paths.join("")}</g>`;
}

function bindPromos() {
  const section = $("#promos");
  const today = new Date().toLocaleDateString("sv");
  for (const p of section.querySelectorAll(".promo")) if (p.dataset.until < today) p.remove();
  if (!section.querySelector(".promo")) section.querySelector(".promo-list").remove();

  section.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-copy]");
    if (!b) return;
    try {
      await navigator.clipboard.writeText(b.dataset.copy);
      b.textContent = "Copiato";
    } catch {
      b.textContent = "Non copiato";
    }
    clearTimeout(b.t);
    b.t = setTimeout(() => { b.textContent = "Copia link"; }, 1600);
  });

  if (matchMedia("(max-width: 52rem)").matches) {
    const s = document.createElement("script");
    s.async = true;
    s.charset = "utf-8";
    s.src = "https://tpwgts.com/content?trs=584480&shmarker=788519&locale=en&powered_by=true&border_radius=5&plain=true&color_background=%23ffffff&color_button=%235080c5&promo_id=5472&campaign_id=57";
    $("#promoWidget").append(s);
  }
}

async function start() {
  drawGuilloche();
  bindPromos();
  try {
    Object.assign(state, JSON.parse(localStorage.getItem(STORE) || "{}"));
  } catch { /* corrupted preferences: keep defaults */ }

  try {
    const res = await fetch("data.json", { cache: "no-cache" });
    if (!res.ok) throw new Error(res.status === 404 ? "missing" : `HTTP ${res.status}`);
    data = await res.json();
  } catch (err) {
    const missing = err.message === "missing";
    $("#meta").textContent = missing ? "Nessun dato pubblicato" : "Dati non disponibili";
    $("#main").innerHTML = `<div class="state ${missing ? "" : "is-error"}">
      <h2>${missing ? "Ancora nessuna ricerca" : "Non riesco a caricare le tariffe"}</h2>
      <p>${missing
        ? "I dati compaiono dopo la prima esecuzione del workflow su GitHub (Actions → Voli economici → Run workflow)."
        : "Ricarica la pagina tra poco. Se il problema resta, controlla l'ultima esecuzione del workflow su GitHub."}</p></div>`;
    $("#main").setAttribute("aria-busy", "false");
    return;
  }

  if (!(state.trip in TRIPS)) state.trip = "ar";
  const all = Object.values(TRIPS).flatMap((k) => data[k] ?? []);
  for (const f of all) f.deal = isDeal(f);
  const maxPrice = Math.ceil(Math.max(...all.map((f) => f.p)) / 10) * 10;
  $("#budget").max = String(Math.max(maxPrice, 30));
  if (state.budget != null && state.budget >= maxPrice) state.budget = null;

  renderMeta();
  bind();
  syncControls();
  document.body.classList.add("first-paint");
  render();
  setTimeout(() => document.body.classList.remove("first-paint"), 2500);
}

start();
