// «Знання» — the team's knowledge sets (server/knowledge.py via /api/knowledge). Two kinds: records from a file
// (read on the server by /api/knowledge/parse and shown here first; only what the user keeps is saved) and a
// text under its own heading, written here. Єва looks the enabled sets up with knowledge_lookup; a pinned one
// («Завжди пам'ятати») goes into the instructions of every call instead.
(() => {
  const params = new URLSearchParams(location.search);
  const BACKEND = (params.get("backend") || (window.APP_CONFIG || {}).BACKEND_URL || "").replace(/\/$/, "");
  const $ = (id) => document.getElementById(id);
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
  };
  let draft = null; // { items, file } — the parsed file before it is saved
  const PREVIEW_MAX = 200; // records drawn in the preview; all of them are saved
  const opened = new Map(); // set id → its records, once «Переглянути» loaded them

  // Text always goes in as text, never as HTML: these are people's details.
  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat(Infinity)) if (c !== null && c !== undefined && c !== false) node.append(c.nodeType ? c : String(c));
    return node;
  }

  const records = (n) => {
    const m10 = n % 10, m100 = n % 100;
    const word = m10 === 1 && m100 !== 11 ? "запис" : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? "записи" : "записів";
    return n + " " + word;
  };
  const fmtDate = (s) => new Date(s * 1000).toLocaleDateString("uk-UA", { day: "2-digit", month: "2-digit", year: "numeric" });

  class ApiError extends Error {
    constructor(status, detail) { super(detail); this.status = status; }
  }

  async function api(path, opts = {}) {
    const res = await fetch(BACKEND + path, {
      ...opts,
      headers: { ...(opts.body && !(opts.body instanceof Blob) ? { "Content-Type": "application/json" } : {}), ...(opts.headers || {}) },
    });
    if (!res.ok) {
      let detail = "Помилка " + res.status;
      try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
      throw new ApiError(res.status, typeof detail === "string" ? detail : "Помилка " + res.status);
    }
    return res.json();
  }

  function status(text, kind = "") {
    $("parseStatus").hidden = !text;
    $("parseStatus").textContent = text || "";
    $("parseStatus").className = "status-msg" + (kind ? " " + kind : "");
  }

  // One record: the name, then its facts.
  function recordCard(item, onRemove, checkedAt) {
    return el("article", { class: "record" + (checkedAt ? " checked" : "") },
      el("div", { class: "record-head" },
        el("strong", {}, item.title),
        checkedAt && el("span", { class: "check-badge", title: "Консьєрж уже пропустив цю людину" }, "✓ відмічено о " + checkedAt),
        onRemove && el("button", { class: "icon-btn", type: "button", title: "Прибрати", "aria-label": "Прибрати " + item.title, onclick: onRemove }, "×")),
      Object.keys(item.fields).length
        ? el("dl", {}, Object.entries(item.fields).map(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]))
        : el("p", { class: "muted small" }, "без подробиць"));
  }

  // ── a new set: file → preview → save ──
  async function readFile(file) {
    if (!file) return;
    if (!/\.(csv|tsv|xlsx|json|docx|txt|md)$/i.test(file.name)) {
      status(/\.(doc|xls)$/i.test(file.name)
        ? "Старий формат: збережіть файл як .docx / .xlsx і завантажте ще раз."
        : "Підтримуються CSV, Excel (.xlsx), JSON, DOCX і текст (.txt, .md).", "err");
      return;
    }
    if (file.size > 5_000_000) { status("Файл завеликий: до 5 МБ.", "err"); return; }
    $("preview").hidden = true;
    draft = null;
    const textual = /\.(docx|txt|md)$/i.test(file.name);
    status(textual ? "Розбираю текст — модель шукає людей і факти, це до хвилини…" : "Читаю файл…");
    $("drop").classList.add("busy");
    try {
      const parsed = await api("/api/knowledge/parse", {
        method: "POST", body: file,
        headers: { "Content-Type": "application/octet-stream", "X-File-Name": encodeURIComponent(file.name) },
      });
      draft = { items: parsed.items, file: file.name, warnings: parsed.warnings || [] };
      if (!$("setName").value.trim()) $("setName").value = file.name.replace(/\.[^.]+$/, "").replace(/[_]+/g, " ");
      status("");
      showPreview();
    } catch (err) {
      status(err instanceof ApiError ? err.message : "Сервер не відповідає — спробуйте ще раз за хвилину.", "err");
    } finally {
      $("drop").classList.remove("busy");
      $("file").value = "";
    }
  }

  function showPreview() {
    const { items, file, warnings } = draft;
    $("preview").hidden = false;
    $("previewTitle").textContent = "Знайдено " + records(items.length);
    $("previewFile").textContent = file;
    $("previewWarnings").replaceChildren(...warnings.map((w) => el("li", {}, w)));
    $("previewItems").replaceChildren(...items.slice(0, PREVIEW_MAX).map((item, i) => recordCard(item, () => {
      draft.items.splice(i, 1);
      if (!draft.items.length) { cancel(); status("Усі записи прибрано.", ""); return; }
      showPreview();
    })));
    $("previewMore").hidden = items.length <= PREVIEW_MAX;
    $("previewMore").textContent = "Показано перші " + PREVIEW_MAX + " — збережуться всі " + records(items.length) + ".";
    $("saveSet").disabled = false;
  }

  function cancel() {
    draft = null;
    $("preview").hidden = true;
    status("");
  }

  async function save() {
    if (!draft) return;
    const name = $("setName").value.trim();
    if (!name) { $("setName").focus(); status("Дайте набору назву — напр. «Корпоратив 20.10».", "err"); return; }
    $("saveSet").disabled = true;
    try {
      const saved = await api("/api/knowledge", {
        method: "POST",
        body: JSON.stringify({ name, note: $("setNote").value.trim(), source: draft.file, items: draft.items }),
      });
      cancel();
      $("setName").value = "";
      $("setNote").value = "";
      status("Збережено «" + saved.name + "»: " + records(saved.count) + ". Єва знатиме їх з наступної розмови.", "ok");
      await loadSets();
    } catch (err) {
      $("saveSet").disabled = false;
      status(err instanceof ApiError ? err.message : "Не вдалося зберегти — сервер не відповідає.", "err");
    }
  }

  // ── a new text section: heading + text, optionally pinned ──
  const chars = (n) => n.toLocaleString("uk-UA") + " симв.";
  let pinnedMax = 12000;

  function showTab(which) {
    const text = which === "text";
    $("tabFile").setAttribute("aria-selected", String(!text));
    $("tabText").setAttribute("aria-selected", String(text));
    $("paneFile").hidden = text;
    $("paneText").hidden = !text;
    if (text) cancel();
    status("");
    store.set("kb.tab", which);
  }

  function countText() {
    const n = $("textBody").value.trim().length;
    $("textCount").textContent = n ? chars(n) : "";
  }

  async function saveText() {
    const name = $("setName").value.trim();
    const body = $("textBody").value.trim();
    if (!name) { $("setName").focus(); status("Дайте розділу заголовок — напр. «Правила свята».", "err"); return; }
    if (!body) { $("textBody").focus(); status("Напишіть текст розділу.", "err"); return; }
    $("saveText").disabled = true;
    try {
      const saved = await api("/api/knowledge", {
        method: "POST",
        body: JSON.stringify({ name, note: $("setNote").value.trim(), kind: "text", body, pinned: $("textPinned").checked }),
      });
      $("setName").value = "";
      $("setNote").value = "";
      $("textBody").value = "";
      $("textPinned").checked = false;
      countText();
      status("Збережено «" + saved.name + "»" + (saved.pinned ? " — Єва пам'ятатиме його в кожній розмові." : ". Єва знатиме його з наступної розмови."), "ok");
      await loadSets();
    } catch (err) {
      status(err instanceof ApiError ? err.message : "Не вдалося зберегти — сервер не відповідає.", "err");
    } finally {
      $("saveText").disabled = false;
    }
  }

  // ── the sets ──
  async function loadSets(attempt = 0) {
    let data;
    try {
      data = await api("/api/knowledge");
    } catch (err) {
      if (err instanceof ApiError) {
        $("sets").replaceChildren(el("p", { class: "muted empty" }, err.status === 401 ? "Потрібен вхід." : err.message));
        return;
      }
      // Render's free plan sleeps: the first request wakes it (up to a minute).
      $("sets").replaceChildren(el("p", { class: "muted empty" }, "Сервер прокидається — до хвилини…"));
      if (attempt < 30) setTimeout(() => loadSets(attempt + 1), 3000);
      return;
    }
    $("storageNote").hidden = data.persistent;
    $("storageNote").textContent = "Сховище тимчасове: бази даних ще не під'єднано (DATABASE_URL на Render), і наступний деплой зітре все завантажене.";
    pinnedMax = data.pinned_max || pinnedMax;
    showPinned(data.pinned_chars || 0);
    renderSets(data.sets);
  }

  function showPinned(n) {
    $("pinnedMeter").hidden = !n;
    $("pinnedMeter").className = "meter" + (n > pinnedMax ? " over" : "");
    $("pinnedMeter").textContent = "«Завжди пам'ятати» в кожній розмові: " + chars(n) + " з " + chars(pinnedMax)
      + (n > pinnedMax ? " — забагато, кінець обріжеться. Відкріпіть або скоротіть щось." : "");
  }

  const MEMORY_SET = "Єва запам'ятала"; // server/knowledge.py MEMORY_SET / MEMORY_PREFIX: never pinned or renamed
  const isMemory = (name) => name === MEMORY_SET || name.startsWith("Пам'ять: ");

  function renderSets(sets) {
    const on = sets.filter((s) => s.enabled).length;
    $("setsCount").textContent = sets.length ? on + " з " + sets.length + " увімкнено" : "";
    if (!sets.length) {
      $("sets").replaceChildren(el("p", { class: "muted empty" }, "Ще нічого немає. Додайте файл із записами або текстовий розділ вище."));
      return;
    }
    $("sets").replaceChildren(...sets.map((s) => {
      const text = s.kind === "text";
      const own = isMemory(s.name);
      const body = el("div", { class: text ? "text-view" : "records", hidden: !opened.has(s.id) });
      if (opened.has(s.id)) (text ? fillText : fillRecords)(body, opened.get(s.id), s);
      const view = el("button", { class: "btn small-btn", type: "button", "aria-expanded": String(opened.has(s.id)) },
        opened.has(s.id) ? "Згорнути" : text ? "Переглянути й змінити" : "Переглянути");
      view.addEventListener("click", () => toggleView(s, body, view));
      const sw = el("input", { type: "checkbox", class: "switch", role: "switch", checked: s.enabled, "aria-label": "Єва знає «" + s.name + "»" });
      sw.addEventListener("change", () => patch(s, { enabled: sw.checked }, sw));
      const pin = !own && el("input", { type: "checkbox", class: "switch", role: "switch", checked: s.pinned, "aria-label": "Завжди пам'ятати «" + s.name + "»" });
      if (pin) pin.addEventListener("change", () => patch(s, { pinned: pin.checked }, pin));
      const meta = [text ? chars(s.chars || 0) : records(s.count), s.source, fmtDate(s.created_at)].filter(Boolean).join(" · ");
      return el("article", { class: "set" + (s.enabled ? "" : " off") },
        el("div", { class: "set-head" },
          el("div", { class: "set-text" },
            el("strong", {}, s.name),
            el("span", { class: "muted small" },
              el("span", { class: "set-kind" + (s.pinned ? " pinned" : "") }, s.pinned ? "Завжди пам'ятати" : text ? "Текст" : "Записи"),
              meta)),
          el("div", { class: "set-switches" },
            el("label", { class: "set-switch" }, el("span", { class: "small" }, s.enabled ? "Єва знає" : "Вимкнено"), sw),
            pin && el("label", { class: "set-switch" }, el("span", { class: "small" }, "Завжди пам'ятати"), pin))),
        s.note && el("p", { class: "hint" }, s.note),
        el("div", { class: "row set-actions" },
          view,
          !own && el("button", { class: "btn small-btn ghost", type: "button", onclick: () => rename(s) }, "Перейменувати"),
          el("button", { class: "btn small-btn ghost danger-text", type: "button", onclick: () => remove(s) }, "Видалити")),
        body);
    }));
  }

  // A text section opened: its text in an editor, saved in place.
  function fillText(body, text, s) {
    const area = el("textarea", { class: "field", rows: String(Math.min(24, Math.max(8, text.split("\n").length + 1))), maxlength: "20000", "aria-label": "Текст розділу «" + s.name + "»" });
    area.value = text;
    const count = el("span", { class: "meter" }, chars(text.length));
    const save = el("button", { class: "btn primary small-btn", type: "button", disabled: true }, "Зберегти зміни");
    area.addEventListener("input", () => {
      count.textContent = chars(area.value.trim().length);
      save.disabled = area.value.trim() === text.trim();
    });
    save.addEventListener("click", async () => {
      save.disabled = true;
      try {
        const saved = await api("/api/knowledge/" + s.id, { method: "POST", body: JSON.stringify({ body: area.value }) });
        opened.set(s.id, saved.body);
        status("Зміни в «" + s.name + "» збережено. Єва знатиме їх з наступної розмови.", "ok");
        await loadSets();
      } catch (err) {
        save.disabled = false;
        status(err.message, "err");
      }
    });
    body.replaceChildren(area, el("div", { class: "row actions" }, save, count));
  }

  // A big set draws its first PREVIEW_MAX records: a phone would choke on two thousand cards.
  // «Консьєрж» marks who has been let in (server/checkins.py): the same name, any word order.
  const nameKey = (t) => t.toLowerCase().replace(/[’ʼ]/g, "'").split(/[^\p{L}'-]+/u).filter((w) => w.length >= 3).sort().join(" ");
  let checked = new Map(); // name key → «18:46»
  async function loadCheckins() {
    try {
      const data = await api("/api/concierge/checkins");
      checked = new Map(data.checkins.map((c) => [nameKey(c.full_name), c.time]));
    } catch { checked = new Map(); }
  }

  function fillRecords(body, items) {
    const more = items.length > PREVIEW_MAX ? [el("p", { class: "hint" }, "…і ще " + records(items.length - PREVIEW_MAX) + ".")] : [];
    body.replaceChildren(...items.slice(0, PREVIEW_MAX).map((item) => recordCard(item, null, checked.get(nameKey(item.title)))), ...more);
  }

  async function toggleView(s, body, button) {
    if (opened.has(s.id)) {
      opened.delete(s.id);
      body.hidden = true;
      button.textContent = "Переглянути";
      button.setAttribute("aria-expanded", "false");
      return;
    }
    button.disabled = true;
    try {
      const [full] = await Promise.all([api("/api/knowledge/" + s.id), s.kind === "text" ? null : loadCheckins()]);
      if (s.kind === "text") {
        opened.set(s.id, full.body);
        fillText(body, full.body, s);
      } else {
        opened.set(s.id, full.items);
        fillRecords(body, full.items);
      }
      body.hidden = false;
      button.textContent = "Згорнути";
      button.setAttribute("aria-expanded", "true");
    } catch (err) {
      status(err.message, "err");
    } finally {
      button.disabled = false;
    }
  }

  // A switch on a set («Єва знає», «Завжди пам'ятати»): saved at once, put back if the server refused.
  async function patch(s, change, sw) {
    try {
      await api("/api/knowledge/" + s.id, { method: "POST", body: JSON.stringify(change) });
      await loadSets();
    } catch (err) {
      sw.checked = !sw.checked;
      status(err.message, "err");
    }
  }

  async function rename(s) {
    const name = (prompt("Новий заголовок розділу:", s.name) || "").trim();
    if (!name || name === s.name) return;
    try {
      await api("/api/knowledge/" + s.id, { method: "POST", body: JSON.stringify({ name }) });
      status("Перейменовано на «" + name + "».", "ok");
      await loadSets();
    } catch (err) {
      status(err.message, "err");
    }
  }

  async function remove(s) {
    const size = s.kind === "text" ? chars(s.chars || 0) : records(s.count);
    if (!confirm("Видалити «" + s.name + "» (" + size + ")? Єва забуде ці дані, відновити не можна.")) return;
    try {
      await api("/api/knowledge/" + s.id, { method: "DELETE" });
      opened.delete(s.id);
      status("Видалено «" + s.name + "».", "");
      await loadSets();
    } catch (err) {
      status(err.message, "err");
    }
  }

  // ── start ──
  $("backLink").href = "index.html" + location.search; // keep ?backend=… for a test backend
  $("file").addEventListener("change", (e) => readFile(e.target.files[0]));
  const drop = $("drop");
  ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, () => drop.classList.remove("over")));
  drop.addEventListener("drop", (e) => { e.preventDefault(); readFile(e.dataTransfer.files[0]); });
  $("saveSet").addEventListener("click", save);
  $("tabFile").addEventListener("click", () => showTab("file"));
  $("tabText").addEventListener("click", () => showTab("text"));
  $("textBody").addEventListener("input", countText);
  $("saveText").addEventListener("click", saveText);
  if (store.get("kb.tab") === "text") showTab("text");
  $("cancelSet").addEventListener("click", cancel);
  if (!BACKEND) { $("sets").replaceChildren(el("p", { class: "muted empty" }, "Не вказано адресу бекенду.")); return; }
  loadSets();
})();
