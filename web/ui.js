// The main page's look on top of app.js, without touching its logic: it only watches what app.js writes.
//  - the status line → a big title and a line under it («Пауза — чекаю «…»» → «Пауза» / «Чекаю «…»»);
//  - .in-call on <body> while a call is open (app.js gives #talk the class «danger» then);
//  - the last line of the conversation as a caption under the orb;
//  - a round initial and a colour for every character chip;
//  - the drawer with devices, sound and behaviour.
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const watch = (el, fn, opts) => { if (el) new MutationObserver(fn).observe(el, opts); };
  const upFirst = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : "");

  // ── Status: one big line + a hint under it ──
  const status = $("status");
  function showStatus() {
    const t = status.textContent.trim();
    let title = t, sub = "";
    if (!t || t === "Не підключено") title = "Готові до розмови";
    else {
      const i = t.indexOf(" — ");
      if (i > 0) { title = t.slice(0, i); sub = upFirst(t.slice(i + 3)); }
    }
    $("statusTitle").textContent = title;
    $("statusSub").textContent = sub;
    $("statusSub").hidden = !sub;
  }
  watch(status, showStatus, { childList: true, characterData: true, subtree: true });
  showStatus();

  // ── In a call or not ──
  const talk = $("talk");
  function showCall() {
    document.body.classList.toggle("in-call", talk.classList.contains("danger"));
    showCaption();
  }
  watch(talk, showCall, { attributes: true, attributeFilter: ["class"] });

  // ── Caption: what was said last, under the orb ──
  const log = $("log");
  function showCaption() {
    const caption = $("caption");
    const all = log.querySelectorAll(".msg.user, .msg.assistant");
    const last = all[all.length - 1];
    const text = last ? last.textContent.trim() : "";
    const on = document.body.classList.contains("in-call") && !!text;
    caption.hidden = !on;
    if (!on) return;
    const user = last.classList.contains("user");
    caption.textContent = user ? "«" + text + "»" : text;
    caption.classList.toggle("from-user", user);
  }
  watch(log, showCaption, { childList: true, characterData: true, subtree: true });

  // ── Character chips: a round initial in the character's colour ──
  const COLORS = [["meridian", "#2E64C9"], ["blunt", "#A84A26"], ["lesia", "#23785A"], ["kit", "#7A47B8"]];
  const SPARE = ["#2E64C9", "#A84A26", "#23785A", "#7A47B8", "#8A5A00", "#B03A6E"];
  function decorateChars() {
    [...$("characters").children].forEach((b, i) => {
      const initial = b.textContent.trim().charAt(0).toUpperCase();
      if (b.dataset.initial !== initial) b.dataset.initial = initial;
      const hit = COLORS.find(([prefix]) => (b.dataset.id || "").startsWith(prefix));
      b.style.setProperty("--av", hit ? hit[1] : SPARE[i % SPARE.length]);
    });
  }
  watch($("characters"), decorateChars, { childList: true });
  decorateChars();

  // ── Drawer ──
  const drawer = $("drawer"), backdrop = $("drawerBackdrop"), opener = $("drawerOpen");
  let returnTo = null;
  function openDrawer(e) {
    if (e) e.preventDefault();
    returnTo = document.activeElement;
    drawer.hidden = false;
    backdrop.hidden = false;
    document.body.classList.add("drawer-open");
    opener.setAttribute("aria-expanded", "true");
    $("drawerClose").focus();
  }
  function closeDrawer() {
    if (drawer.hidden) return;
    drawer.hidden = true;
    backdrop.hidden = true;
    document.body.classList.remove("drawer-open");
    opener.setAttribute("aria-expanded", "false");
    if (returnTo && document.contains(returnTo)) returnTo.focus();
  }
  opener.addEventListener("click", openDrawer);
  $("drawerClose").addEventListener("click", closeDrawer);
  backdrop.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !drawer.hidden) closeDrawer(); });
  document.querySelectorAll('a[href="#devices"]').forEach((a) => a.addEventListener("click", openDrawer));
})();
