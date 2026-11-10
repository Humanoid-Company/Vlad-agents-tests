// «Роботи»: керування роботами джойстиком (server/robots.py → bridge біля робота, robot_bridge/).
// На головній — картка під дзвінком, відкривається кнопкою «Роботи» в шапці: Єва говорить, джойстик рухає.
// robots.html — та сама панель на весь екран, для телефона. Один робот — один оператор; СТОП — будь-хто.
// Паролів немає: лише ім'я, щоб усі бачили, хто яким роботом керує.
(() => {
  const root = document.getElementById("robotsPanel");
  if (!root) return;
  const standalone = root.dataset.standalone === "1";
  const params = new URLSearchParams(location.search);
  const BACKEND = (params.get("backend") || (window.APP_CONFIG || {}).BACKEND_URL || "").replace(/\/$/, "");
  const WS_URL = BACKEND.replace(/^http/, "ws") + "/ws/operator";
  const SEND_MS = 50; // 20 команд на секунду, поки джойстик відхилено; bridge зупиняє робота за 0.35 с тиші
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
  };

  // Текст завжди вставляється як текст: назви роботів і імена операторів приходять ззовні.
  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) node.append(c.nodeType ? c : String(c));
    return node;
  }

  const LABELS = {
    stand: "Встати", sit: "Сісти", lie_down: "Лягти", recovery: "Піднятись", hello: "Привітатись",
    stretch: "Потягнутись", balance: "Баланс", wave: "Помахати", shake_hand: "Потиснути руку", damp: "Розслабити",
  };

  let name = store.get("va-robots-name") || "";
  let ws = null;
  let wanted = false; // панель відкрита — тримаємо з'єднання
  let retry = null;
  let robots = [];
  let selected = store.get("va-robots-selected") || null;
  let mine = null; // robot_id, яким керуємо зараз
  let wasMoving = false;
  const input = { pad: { vx: 0, vy: 0, yaw: 0 }, keys: { vx: 0, vy: 0, yaw: 0 }, gp: { vx: 0, vy: 0, yaw: 0 } };

  // ── розмітка ──
  const dot = el("span", { class: "dot", "aria-hidden": "true" });
  const connText = el("span", { class: "muted small" }, "Не підключено");
  const who = el("button", { class: "btn ghost small-btn", type: "button", hidden: true, title: "Змінити ім'я", onclick: showLogin }, "");
  const closeBtn = standalone ? null
    : el("button", { class: "btn ghost small-btn", type: "button", "aria-label": "Закрити «Роботи»", onclick: () => setOpen(false) }, "✕");

  const nameIn = el("input", { type: "text", maxlength: "32", placeholder: "Ваше ім'я", autocomplete: "name", "aria-label": "Ім'я оператора" });
  const login = el("form", { class: "rb-login", hidden: true, onsubmit: (e) => {
    e.preventDefault();
    name = nameIn.value.trim().replace(/\s+/g, " ").slice(0, 32);
    if (!name) { nameIn.focus(); return; }
    store.set("va-robots-name", name);
    login.hidden = true;
    reconnect();
  } },
    el("p", { class: "hint" }, "Ім'я бачитимуть колеги: хто яким роботом керує."),
    el("div", { class: "row" }, nameIn, el("button", { class: "btn primary", type: "submit" }, "Увійти")));

  const list = el("div", { class: "rb-robots", role: "listbox", "aria-label": "Роботи онлайн" });
  const status = el("span", { class: "rb-status" }, "Оберіть робота");
  const claimBtn = el("button", { class: "btn primary", type: "button", onclick: claim }, "Керувати");
  const releaseBtn = el("button", { class: "btn", type: "button", hidden: true, onclick: release }, "Відпустити");
  const padMove = el("div", { class: "rb-pad", "aria-label": "Джойстик руху" }, el("div", { class: "rb-knob" }), el("span", { class: "rb-pad-label" }, "рух"));
  const padTurn = el("div", { class: "rb-pad", "aria-label": "Джойстик повороту" }, el("div", { class: "rb-knob" }), el("span", { class: "rb-pad-label" }, "поворот"));
  const stopBtn = el("button", { class: "btn danger rb-stop", type: "button", onclick: emergencyStop }, "СТОП");
  const actions = el("div", { class: "rb-actions" });
  const ctrl = el("div", { class: "rb-ctrl rb-off" },
    el("div", { class: "row rb-ctrl-top" }, status, claimBtn, releaseBtn),
    el("div", { class: "rb-pads" }, padMove, stopBtn, padTurn),
    actions,
    el("p", { class: "hint" }, "Клавіатура: W/S — вперед/назад, A/D — вбік, Q/E або ←/→ — поворот, пробіл — стоп. " +
      "Геймпад підхоплюється сам (кнопка B — стоп). Відпустили джойстик чи зник зв'язок — робот зупиняється."));
  const log = el("div", { class: "rb-log", "aria-live": "polite" });

  root.append(
    el("div", { class: "card-head" },
      el("h2", {}, "Роботи"),
      el("div", { class: "head-actions" }, el("span", { class: "rb-conn" }, dot, connText), who, closeBtn)),
    login, list, ctrl, log);

  function note(text) {
    const line = el("div", {}, el("span", { class: "muted" }, new Date().toLocaleTimeString("uk-UA") + "  "), text);
    log.prepend(line);
    while (log.childElementCount > 30) log.lastElementChild.remove();
  }
  function send(msg) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(msg)); }
  function setConn(state, text) { dot.className = "dot " + state; connText.textContent = text; }

  // ── з'єднання ──
  async function connect() {
    if (!wanted || ws) return;
    if (!BACKEND) { setConn("err", "Немає адреси бекенду (config.js)"); return; }
    if (!name) { showLogin(); return; }
    who.textContent = name;
    who.hidden = false;
    setConn("wait", "Підключення…");
    const sock = new WebSocket(`${WS_URL}?name=${encodeURIComponent(name)}`);
    ws = sock;
    sock.onopen = () => setConn("live", "Онлайн");
    sock.onmessage = (ev) => onMessage(JSON.parse(ev.data));
    sock.onclose = (ev) => {
      if (ws === sock) ws = null;
      mine = null;
      render();
      if (ev.code === 4403) { setConn("err", "Сайт не в FRONTEND_ORIGINS"); return; }
      if (!wanted) { setConn("", "Не підключено"); return; }
      setConn("busy", "Зв'язок втрачено, перепідключення…");
      clearTimeout(retry);
      retry = setTimeout(connect, 2000);
    };
  }
  function disconnect() {
    wanted = false;
    clearTimeout(retry);
    if (mine) send({ type: "release", robot_id: mine });
    mine = null;
    if (ws) ws.close();
    ws = null;
    setConn("", "Не підключено");
  }
  function reconnect() {
    if (ws) { const s = ws; ws = null; s.onclose = null; s.close(); }
    wanted = true;
    connect();
  }
  function showLogin() {
    nameIn.value = name;
    login.hidden = false;
    nameIn.focus();
  }

  function onMessage(m) {
    if (m.type === "robots") {
      robots = m.robots || [];
      if (mine && !robots.some((r) => r.robot_id === mine && r.operator === name)) {
        note(`Керування ${mine} втрачено.`);
        mine = null;
      }
      if (!selected || !robots.some((r) => r.robot_id === selected)) selected = robots.length === 1 ? robots[0].robot_id : selected;
      render();
    } else if (m.type === "claim_result") {
      if (m.ok) {
        mine = m.robot_id;
        note(`Ви керуєте ${robotName(m.robot_id)}.`);
      } else if (m.reason === "busy") {
        if (confirm(`${robotName(m.robot_id)} зараз керує ${m.operator}. Перехопити керування?`)) send({ type: "takeover", robot_id: m.robot_id });
      } else {
        note(`Не вдалося: робот ${m.reason === "offline" ? "офлайн" : m.reason}.`);
      }
      render();
    } else if (m.type === "event") {
      note(`${robotName(m.robot_id)}: ${m.text}`);
    } else if (m.type === "error") {
      note(m.text);
    }
  }
  const robotName = (id) => (robots.find((r) => r.robot_id === id) || {}).name || id;

  // ── вигляд ──
  function render() {
    list.replaceChildren();
    if (!robots.length && ws) {
      list.append(el("p", { class: "muted small rb-empty" }, "Немає роботів онлайн. Запустіть bridge біля робота (robot_bridge/)."));
    }
    for (const r of robots) {
      const isMine = r.operator === name;
      const badge = isMine ? el("span", { class: "rb-badge mine" }, "ви керуєте")
        : r.operator ? el("span", { class: "rb-badge busy" }, "керує " + r.operator)
        : el("span", { class: "rb-badge free" }, "вільний");
      const battery = r.state && r.state.battery != null ? ` · 🔋 ${r.state.battery}%` : "";
      list.append(el("button", {
        class: "rb-robot" + (r.robot_id === selected ? " sel" : ""), type: "button", role: "option",
        "aria-selected": String(r.robot_id === selected),
        onclick: () => { selected = r.robot_id; store.set("va-robots-selected", selected); render(); },
      },
        el("span", { class: "rb-robot-name" }, (r.kind === "humanoid" ? "🤖 " : r.kind === "dog" ? "🐕 " : "⚙️ ") + r.name),
        el("span", { class: "muted small" }, r.robot_id + battery),
        badge));
    }

    const r = robots.find((x) => x.robot_id === selected);
    const isMine = !!r && mine === r.robot_id;
    ctrl.classList.toggle("rb-off", !r);
    padMove.classList.toggle("rb-off", !isMine);
    padTurn.classList.toggle("rb-off", !isMine);
    claimBtn.hidden = isMine || !r;
    releaseBtn.hidden = !isMine;
    status.textContent = !r ? (robots.length ? "Оберіть робота" : "")
      : isMine ? `Керуєте: ${r.name}` : r.operator ? `${r.name} — керує ${r.operator}` : `${r.name} — вільний`;
    claimBtn.textContent = r && r.operator && r.operator !== name ? "Перехопити" : "Керувати";
    actions.replaceChildren(...(r ? r.actions : []).map((a) =>
      el("button", { class: "btn", type: "button", disabled: !isMine, onclick: () => send({ type: "action", robot_id: mine, name: a }) }, LABELS[a] || a)));
  }

  function claim() {
    const r = robots.find((x) => x.robot_id === selected);
    if (!r) return;
    if (r.operator && r.operator !== name) {
      if (confirm(`${r.name} зараз керує ${r.operator}. Перехопити керування?`)) send({ type: "takeover", robot_id: r.robot_id });
    } else send({ type: "claim", robot_id: r.robot_id });
  }
  function release() {
    if (!mine) return;
    send({ type: "release", robot_id: mine });
    note(`Відпустили ${robotName(mine)}.`);
    mine = null;
    render();
  }
  function emergencyStop() {
    const target = mine || selected;
    if (target) send({ type: "stop", robot_id: target });
  }

  // ── сенсорні джойстики ──
  function joystick(pad, onMove) {
    const knob = pad.querySelector(".rb-knob");
    let pointer = null;
    const set = (x, y) => { knob.style.transform = `translate(${x * 90}%, ${y * 90}%)`; onMove(x, y); };
    const fromEvent = (e) => {
      const box = pad.getBoundingClientRect();
      const radius = box.width / 2;
      let x = (e.clientX - box.left - radius) / radius;
      let y = (e.clientY - box.top - radius) / radius;
      const d = Math.hypot(x, y);
      if (d > 1) { x /= d; y /= d; }
      if (Math.abs(x) < 0.08) x = 0;
      if (Math.abs(y) < 0.08) y = 0;
      set(x, y);
    };
    pad.addEventListener("pointerdown", (e) => { pointer = e.pointerId; pad.setPointerCapture(pointer); pad.classList.add("active"); fromEvent(e); });
    pad.addEventListener("pointermove", (e) => { if (e.pointerId === pointer) fromEvent(e); });
    const end = (e) => { if (e.pointerId !== pointer) return; pointer = null; pad.classList.remove("active"); set(0, 0); };
    pad.addEventListener("pointerup", end);
    pad.addEventListener("pointercancel", end);
  }
  joystick(padMove, (x, y) => { input.pad.vx = -y; input.pad.vy = -x; });
  joystick(padTurn, (x) => { input.pad.yaw = -x; });

  // ── клавіатура: лише коли панель відкрита й фокус не в полі вводу ──
  const down = new Set();
  const typing = (t) => t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
  const panelVisible = () => standalone || !root.hidden;
  function keysToSpeed() {
    const k = (code) => (down.has(code) ? 1 : 0);
    const clamp = (v) => Math.max(-1, Math.min(1, v));
    input.keys.vx = clamp(k("KeyW") + k("ArrowUp") - k("KeyS") - k("ArrowDown"));
    input.keys.vy = clamp(k("KeyA") - k("KeyD"));
    input.keys.yaw = clamp(k("KeyQ") + k("ArrowLeft") - k("KeyE") - k("ArrowRight"));
  }
  const DRIVE_KEYS = new Set(["KeyW", "KeyA", "KeyS", "KeyD", "KeyQ", "KeyE", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"]);
  document.addEventListener("keydown", (e) => {
    if (!panelVisible() || typing(e.target) || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.code === "Space" && (mine || selected) && e.target.tagName !== "BUTTON") { e.preventDefault(); emergencyStop(); return; }
    if (!mine || !DRIVE_KEYS.has(e.code)) return;
    e.preventDefault(); // стрілки не гортають сторінку, поки ви керуєте
    down.add(e.code);
    keysToSpeed();
  });
  document.addEventListener("keyup", (e) => { if (down.delete(e.code)) keysToSpeed(); });
  window.addEventListener("blur", () => { down.clear(); keysToSpeed(); });

  // ── геймпад ──
  let gpStopWas = false;
  function readGamepad() {
    const gp = [...(navigator.getGamepads ? navigator.getGamepads() : [])].find(Boolean);
    if (!gp) { input.gp = { vx: 0, vy: 0, yaw: 0 }; return; }
    const dz = (v) => (Math.abs(v || 0) < 0.12 ? 0 : v);
    input.gp = { vx: -dz(gp.axes[1]), vy: -dz(gp.axes[0]), yaw: -dz(gp.axes[2]) };
    const stopNow = !!(gp.buttons[1] && gp.buttons[1].pressed); // B / ○
    if (stopNow && !gpStopWas) emergencyStop();
    gpStopWas = stopNow;
  }

  // ── відправка ──
  setInterval(() => {
    if (!wanted) return;
    readGamepad();
    if (!mine || document.hidden) { wasMoving = false; return; } // вкладка у фоні — тиша, bridge зупинить робота
    const r = robots.find((x) => x.robot_id === mine);
    const lim = (r && r.max_speed) || { vx: 0.5, vy: 0.3, yaw: 0.8 };
    const strongest = (axis) => [input.pad[axis], input.keys[axis], input.gp[axis]].reduce((m, v) => (Math.abs(v) > Math.abs(m) ? v : m), 0);
    const vx = +(strongest("vx") * lim.vx).toFixed(3);
    const vy = +(strongest("vy") * lim.vy).toFixed(3);
    const yaw = +(strongest("yaw") * lim.yaw).toFixed(3);
    const moving = !!(vx || vy || yaw);
    if (moving || wasMoving) send({ type: "move", robot_id: mine, vx, vy, yaw }); // ще один нуль — плавна зупинка
    wasMoving = moving;
  }, SEND_MS);

  // ── відкрити / закрити (головна сторінка) ──
  const openBtn = document.getElementById("robotsBtn");
  function setOpen(open) {
    if (standalone) return;
    root.hidden = !open;
    if (openBtn) openBtn.setAttribute("aria-expanded", String(open));
    store.set("va-robots-open", open ? "1" : "0");
    if (open) { wanted = true; connect(); } else disconnect();
  }
  if (openBtn) openBtn.addEventListener("click", () => setOpen(root.hidden));
  window.addEventListener("pagehide", () => { if (mine) send({ type: "release", robot_id: mine }); });

  render();
  if (standalone) { wanted = true; connect(); } else setOpen(store.get("va-robots-open") === "1");
})();
