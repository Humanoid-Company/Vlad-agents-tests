// Browser side: microphone + speaker go straight to OpenAI GPT-Live over WebRTC. The backend only
// turns our SDP offer into a session (the keys stay there) and runs the tools.
(() => {
  const params = new URLSearchParams(location.search);
  const BACKEND = (params.get("backend") || (window.APP_CONFIG || {}).BACKEND_URL || "").replace(/\/$/, "");
  const $ = (id) => document.getElementById(id);

  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
  };
  // Random id that keeps this browser's conversation and settings separate from other testers'.
  let clientId = store.get("va-client-id");
  if (!clientId) {
    clientId = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2)).replace(/[^A-Za-z0-9-]/g, "");
    store.set("va-client-id", clientId);
  }
  let assistantName = ""; // set when renamed by voice (/api/me); «Єва» otherwise
  let callName = "Єва"; // the applied character's name: what wakes and pauses it (showVoice)
  // Voice picker: the choice lives here (the server forgets it on restart) and is sent with
  // every new call. Voice is fixed for a Live session, so a change applies from the next call.
  let voices = [];
  let voice = store.get("va-voice") || "";
  let speed = store.get("va-speed") || "normal";
  let style = store.get("va-style") || "normal";
  // iPhone / iPad (any browser there is Safari's engine; an iPad may report itself as a Mac).
  const IOS = /iP(hone|ad|od)/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const ANDROID = /Android/.test(navigator.userAgent);
  // Browsers inside other apps (Telegram, Instagram, Facebook, Viber…) may give pages no mic at all:
  // navigator.mediaDevices is missing (prod, iPhone: «undefined is not an object (evaluating
  // 'navigator.mediaDevices.getUserMedia')»). The page says what to do before anyone presses a button.
  const UA = navigator.userAgent;
  const BROWSER = /Instagram/.test(UA) ? "instagram" : /FBAN|FBAV/.test(UA) ? "facebook" : /Telegram/i.test(UA) ? "telegram"
    : /Viber/i.test(UA) ? "viber" : /Line\//.test(UA) ? "line" : /CriOS/.test(UA) ? "chrome-ios" : /FxiOS/.test(UA) ? "firefox-ios"
    : /EdgiOS|Edg\//.test(UA) ? "edge" : /Chrome\//.test(UA) ? "chrome" : /Safari\//.test(UA) ? "safari" : "other";
  // Some in-app WebKit browsers keep only the old prefixed API: wrapped, the browser's own permission
  // prompt can still come up there.
  const LEGACY_GUM = navigator.getUserMedia || navigator.webkitGetUserMedia || navigator.mozGetUserMedia;
  if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia) && LEGACY_GUM) {
    try {
      if (!navigator.mediaDevices) Object.defineProperty(navigator, "mediaDevices", { value: {}, configurable: true });
      navigator.mediaDevices.getUserMedia = (c) => new Promise((ok, fail) => LEGACY_GUM.call(navigator, c, ok, fail));
    } catch { /* read-only here: no mic */ }
  }
  const HAS_MIC_API = Boolean(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  // For /api/diag: the iOS version and the browser's own («17.4», «141.0»).
  const OS_VERSION = ((UA.match(/OS (\d+[_.]\d+)/) || [])[1] || "").replace("_", ".");
  const BROWSER_VERSION = (UA.match(/(?:CriOS|FxiOS|EdgiOS|Edg|Chrome|Version)\/(\d+(?:\.\d+)?)/) || [])[1] || "";
  // No mic in this browser (Chrome 155 on iOS 26.5, prod /api/diag: micApi false over https): a page can't
  // bring up the browser's permission prompt without the API, so this explains it and offers the way
  // that works — the same page in Safari (iOS 17+ opens «x-safari-https://…» there) or its link. Shown
  // once the page opens and again on «Почати розмову».
  let noMicReady = false;
  function showNoMic() {
    const dlg = $("noMic");
    if (!noMicReady) {
      noMicReady = true;
      const chromeLike = IOS && ["chrome-ios", "firefox-ios", "edge"].includes(BROWSER);
      $("noMicTitle").textContent = chromeLike ? "Цей браузер на iPhone не дає сайтам мікрофон" : "Мікрофон у цьому браузері недоступний";
      $("noMicText").textContent = chromeLike ? "Відкрийте цю сторінку в Safari — там усе працює." : noMicReason();
      $("noMicIos").hidden = !chromeLike;
      $("openSafari").hidden = !(IOS && window.isSecureContext);
      $("openSafari").href = "x-safari-" + location.href;
      $("copyLink").addEventListener("click", async () => {
        let ok = true;
        try { await navigator.clipboard.writeText(location.href); } catch { ok = false; }
        flashButton($("copyLink"), ok ? "Скопійовано ✓" : location.href);
      });
      $("noMicClose").addEventListener("click", () => dlg.close ? dlg.close() : (dlg.hidden = true));
    }
    if (dlg.showModal) { if (!dlg.open) dlg.showModal(); } else dlg.hidden = false;
  }

  function noMicReason() {
    if (HAS_MIC_API) return "";
    if (!window.isSecureContext) return "Мікрофон працює лише на захищеній сторінці — відкрийте її за адресою https://…";
    // Chrome (and Firefox, Edge) on an iPhone is WebKit inside another app, and it may give pages no mic
    // (prod: Chrome on an iPhone); Safari does.
    if (BROWSER === "chrome-ios" || BROWSER === "firefox-ios" || (IOS && BROWSER === "edge")) {
      return "На iPhone цей браузер не дає сторінці мікрофон. Відкрийте її в Safari — там усе працює. " +
        "(Якщо потрібен саме цей браузер: оновіть iOS і браузер та перевірте «Параметри» → браузер → «Мікрофон».)";
    }
    return IOS
      ? "Цей браузер не дає сайтам мікрофон — так буває у вбудованому браузері Telegram, Instagram чи Viber. Відкрийте посилання в Safari: «⋯» або «Поділитися» → «Відкрити в Safari»."
      : "Цей браузер не дає сайтам мікрофон (часто це вбудований браузер іншого застосунку). Відкрийте сторінку в Chrome, Safari або Edge.";
  }
  let volume = parseFloat(store.get("va-volume") || "1");
  // Keep what is said nearby during a pause (voice/background.py); the user can turn it off.
  let bgListen = store.get("va-bg-listen") !== "0";
  // A voice picked while Єва answers: switch at once (she goes on in the new voice) or once she is done.
  let voiceNow = store.get("va-voice-now") !== "0";
  if (!(volume >= 0 && volume <= 1)) volume = 1;
  if (IOS) volume = 1; // the slider is hidden there (see showVolume): a level saved earlier must not stay
  // Devices picked in «Пристрої» ("" = the system default); kept in this browser only.
  let micId = store.get("va-mic") || "";
  let speakerId = store.get("va-speaker") || "";
  let cameraId = store.get("va-camera") || "";
  // Phones: front («user») or back («environment») camera, unless a camera is picked in «Пристрої».
  let cameraFacing = store.get("va-camera-facing") === "environment" ? "environment" : "user";

  const DEBUG = params.get("debug") === "1";
  // Delivery prompt (prompts/live_prompt.py): v5 is the usual one (v4 condensed, the same rules said
  // once), v4 the long one, v3 behaves like a person. v1 and v2 are gone from the web (the desktop keeps them).
  const WEB_PROMPTS = ["v3", "v4", "v5"];
  // No picker on the page any more: v5 for everyone (?prompt=v4 still works for a test).
  let promptVariant = params.get("prompt") || "v5";
  // The top switch: «Помічниця» (the usual talk) or «Консьєрж» — she welcomes people, asks their name and
  // checks it against «Знання» with one check question (server/knowledge.py CONCIERGE_NOTE).
  let mode = "assistant"; // «Консьєрж» is off in the UI for now (its code stays)
  // «Консьєрж» → «Перевірочне питання»: off — the name on the list is enough (kept on this device).
  let checkQuestion = store.get("va-check-question") !== "0";
  if (!WEB_PROMPTS.includes(promptVariant)) promptVariant = "v5";
  let pc = null;
  let channel = null;
  let sessionVoice = ""; // the voice the current call was opened with
  let mic = null;
  // «Вимкнути мікрофон»: nothing reaches her — not the call, not the wake phrase, not background listening.
  let micMuted = false;
  let gate = null;
  let gateStats = null; // ?debug=1: the gate's totals (evaGate())
  let micSender = null; // the current call's mic sender: another mic replaces its track in place
  // One open bubble per speaker: user and assistant transcripts arrive interleaved when they
  // overlap, and a single "current" bubble shredded both into word fragments.
  const openMsg = { user: null, assistant: null };
  const lastDeltaAt = { user: 0, assistant: 0 };
  const TURN_PAUSE_MS = 3000; // the transcript of one utterance can pause this long mid-sentence
  // Her reply after the user spoke is a new bubble below their line once her text has paused this long
  // (a stream that goes on over them stays one bubble). With only TURN_PAUSE_MS her answer to «А зараз»
  // was added to her previous bubble, above the question — the log read as if she ignored it.
  const TURN_RESUME_MS = 1000;

  const headers = () => ({
    "Content-Type": "application/json",
    "X-Client-Id": clientId,
  });

  function setStatus(text, state) {
    $("status").textContent = text;
    $("dot").className = "dot" + (state ? " " + state : "");
  }

  function addLine(role, text) {
    const empty = $("log").querySelector(".empty");
    if (empty) empty.remove();
    const p = document.createElement("p");
    p.className = "msg " + role;
    p.textContent = text;
    $("log").appendChild(p);
    $("log").scrollTop = $("log").scrollHeight;
    return p;
  }

  function appendTranscript(role, delta) {
    if (!delta) return;
    const now = Date.now();
    const other = role === "user" ? "assistant" : "user";
    // A new turn starts after a pause, or once the other side has spoken since this bubble.
    const otherSpoke = lastDeltaAt[other] > lastDeltaAt[role];
    if (!openMsg[role] || now - lastDeltaAt[role] > TURN_PAUSE_MS || lastDeltaAt[other] > lastDeltaAt[role] + TURN_PAUSE_MS
        || (otherSpoke && now - lastDeltaAt[role] > TURN_RESUME_MS)) {
      openMsg[role] = addLine(role, "");
    }
    lastDeltaAt[role] = now;
    const text = openMsg[role].textContent;
    // Sentences can arrive glued («недочула.О, та добре»): keep a space between them.
    openMsg[role].textContent += /[.!?…]$/.test(text) && /^[^\s.,!?…»)]/.test(delta) ? " " + delta : delta;
    $("log").scrollTop = $("log").scrollHeight;
  }

  function closeBubbles() {
    openMsg.user = openMsg.assistant = null;
    lastDeltaAt.user = lastDeltaAt.assistant = 0;
  }

  function clearLog() {
    closeBubbles();
    $("log").replaceChildren();
    const empty = document.createElement("p");
    empty.className = "muted empty";
    empty.textContent = "Тут з'являтиметься текст розмови.";
    $("log").appendChild(empty);
  }

  // Noise gate between the mic and WebRTC: background sounds (typing, TV, people nearby)
  // never reach the model, only speech louder than the room's noise floor does. While the
  // assistant is audible the bar also sits above its echo in the mic, learned from how loud
  // that echo really is: high on laptop speakers, near zero in headphones — so a quiet voice
  // in a headset still gets through.
  // It also hears the assistant's own audio (2nd input) and reports to the page when the
  // user starts/stops talking and when the assistant is audible — that drives barge-in.
  // ?gate=off turns it off. It runs on the audio thread (AudioWorklet), so a background tab,
  // where timers are throttled to once a second, doesn't chop the user's speech.
  const GATE_WORKLET = `
    class NoiseGate extends AudioWorkletProcessor {
      constructor(options) {
        super();
        this.frame = Math.round(sampleRate * 0.02);       // judge loudness per 20 ms
        // 250 ms look-ahead: the gate opens on voiced sound, and a soft start before it («с», «ф»,
        // «х», «п» — «Скажи», «Привіт») was cut at 60 ms and misheard; at 150 ms quiet words still lost
        // their start (measured, see the gain below).
        this.ring = new Float32Array(Math.round(sampleRate * 0.25));
        this.pos = 0; this.sum = 0; this.outSum = 0; this.count = 0;
        this.floor = 0.003; this.open = false; this.loud = 0; this.quietFor = 1;
        this.gain = 0;
        // Barge-in only listens to "near" voice: the person at the mic is far louder than
        // people talking in the background, a TV or a door bang (those are short).
        this.near = (options.processorOptions || {}).near || 0.06;
        // How loud this user speaks, learned while Єва is silent: the «near voice» bar follows it, so a
        // quiet user can still interrupt her (a fixed 0.06 missed them even in headphones), while
        // background voices and a TV, quieter than the user, still don't count.
        this.voice = 0;
        this.speaking = false; this.nearFrames = 0; this.nearQuietFor = 1;
        this.audible = false; this.outQuietFor = 1;       // assistant playback
        // Mic level per unit of assistant output level while only the assistant talks. Starts
        // as "loud speakers" and drops within a second or two when there is no echo (headset).
        this.echo = 0.5;
        // ?sens=1 (a test): while she talks on speakers, pass a quieter voice — 1.5× her expected echo
        // instead of 2.5×, and the user's own level instead of the fixed 0.04. A quiet voice over her gets
        // through, but so does more of her echo (she may cut herself off): a Python port of this gate, a
        // voice at 0.02–0.03 over echo 0.1 → passed 0–28% → 98%, her echo alone open 0% → ~60% of the time.
        this.sensitive = Boolean((options.processorOptions || {}).sensitive);
        // ?debug=1: totals posted every second for evaGate() — how much of the user the gate cut while
        // she talked, measured on the real device (echo and voice levels differ per laptop).
        this.stats = (options.processorOptions || {}).stats
          ? { her: 0, herOpen: 0, cut: 0, quiet: 0, quietOpen: 0, frames: 0 } : null;
      }
      judge(level, outLevel) {
        if (outLevel > 0.01) this.outQuietFor = 0; else this.outQuietFor += 0.02;
        const audible = this.outQuietFor < 0.3;
        if (audible !== this.audible) { this.audible = audible; this.port.postMessage({ audible }); }
        // 0.006 ≈ quiet speech into a headset mic; the room's noise floor raises the bar.
        const echoLevel = audible ? outLevel * this.echo * (this.sensitive ? 1.5 : 2.5) : 0;
        // Speakers leave bursts of echo that the browser's echo canceller misses; averaged into
        // this.echo they look small, and letting them through can make the model hear itself
        // and break off mid-sentence. So while the assistant talks keep the old 0.04 bar unless the mic plainly
        // hears no echo at all (headphones). this.echo starts high, so "speakers" is the default.
        const headset = this.echo < 0.05;
        // 3× the room's noise (was 4×): very quiet speech by a laptop fan lost whole words at 4×.
        const bar = this.sensitive && this.voice ? Math.min(0.04, this.voice * 0.5) : 0.04;
        const threshold = Math.max(audible && !headset ? bar : 0.006, this.floor * 3, echoLevel);
        // Learn the echo while the gate is shut (the user isn't talking over the assistant).
        if (audible && !this.open && outLevel > 0.01) {
          const ratio = level / outLevel;
          this.echo += (ratio - this.echo) * (ratio < this.echo ? 0.1 : 0.03);
        }
        if (level > threshold) { this.loud += 1; this.quietFor = 0; }
        else { this.quietFor += 0.02; if (level < threshold * 0.6) this.loud = 0; }
        if (!this.open && this.loud >= 2) this.open = true;
        // 1 s: a quiet word ending or a pause mid-sentence is not cut off (0.6 s cut phrase ends).
        else if (this.open && this.quietFor > 1.0) this.open = false;
        if (this.open && !audible && level > threshold * 1.5) this.voice += (level - this.voice) * (this.voice ? 0.02 : 1);
        const near = this.voice ? Math.min(this.near, Math.max(0.01, this.voice * 0.5)) : this.near;
        const nearThreshold = Math.max(audible ? near * 1.3 : near, this.floor * 8, echoLevel * 1.3);
        if (level > nearThreshold) { this.nearFrames += 1; this.nearQuietFor = 0; }
        else { this.nearQuietFor += 0.02; if (this.nearQuietFor > 0.25) this.nearFrames = 0; }
        // Near voice: ~120 ms of loud frames to start (a click/bang is shorter), 250 ms gap to end.
        const speaking = this.speaking ? this.nearQuietFor <= 0.25 : this.nearFrames >= 6;
        if (speaking !== this.speaking) { this.speaking = speaking; this.port.postMessage({ speaking }); }
        if (this.stats) {
          const st = this.stats;
          // «cut»: while she talks the gate stays shut on a sound at the user's own level, well above her
          // expected echo — most likely their words lost (an estimate: a loud echo burst counts too).
          if (audible) {
            st.her += 1;
            if (this.open) st.herOpen += 1;
            else if (this.voice && level > Math.max(this.voice * 0.4, this.floor * 4, outLevel * this.echo * 1.5)) st.cut += 1;
          } else { st.quiet += 1; if (this.open) st.quietOpen += 1; }
          if (++st.frames % 50 === 0) {
            this.port.postMessage({ stats: { ...st, echo: this.echo, voice: this.voice, floor: this.floor, threshold } });
          }
        }
        // Learn the room's noise only while nobody speaks, so a long sentence can't raise it.
        if (!this.open && !audible) {
          this.floor = level < this.floor ? this.floor * 0.7 + level * 0.3 : this.floor + (level - this.floor) * 0.01;
        }
      }
      process(inputs, outputs) {
        const input = inputs[0][0];
        const remote = inputs[1] && inputs[1][0];
        const output = outputs[0][0];
        if (!input || !output) return true;
        for (let i = 0; i < input.length; i++) {
          const x = input[i];
          this.sum += x * x;
          if (remote) this.outSum += remote[i] * remote[i];
          if (++this.count >= this.frame) {
            this.judge(Math.sqrt(this.sum / this.count), Math.sqrt(this.outSum / this.count));
            this.sum = 0; this.outSum = 0; this.count = 0;
          }
          const delayed = this.ring[this.pos];
          this.ring[this.pos] = x;
          this.pos = (this.pos + 1) % this.ring.length;
          // Closed while she is silent = −14 dB, not silence: a quiet user below the bar is still heard.
          // Cutting to zero made GPT-Live mishear a quiet voice by a fan (feedback: «говорю одне, а
          // виходить інше»): measured with this gate ported to Python on real GPT-Live, 8 phrases, a
          // quiet voice and room noise — word errors 51% / 24% → 6%, a TV-level voice alone still 0 of
          // 70 words heard. While she talks it still closes fully: her echo must not reach the model.
          const closed = this.audible ? 0 : 0.2;
          this.gain += ((this.open ? 1 : closed) - this.gain) * (this.open ? 0.004 : 0.0005);
          output[i] = delayed * this.gain;
        }
        for (let c = 1; c < outputs[0].length; c++) outputs[0][c].set(output);
        return true;
      }
    }
    registerProcessor("noise-gate", NoiseGate);
  `;

  // WebKit (Safari, every iPhone browser) starts an AudioContext only inside a tap: one made after
  // awaiting the mic may stay «suspended», and awaiting its resume() never ends. So a context is started
  // in the tap (primeAudio), the gate takes it, and no audio step is awaited for long: past the limit the
  // call goes on without that step. The mic must be asked for first in the same tap: an AudioContext
  // started before getUserMedia used up the tap, and WebKit refused the mic (NotAllowedError) or never
  // answered — measured in WebKit: context first, 1 of 3 refused; mic first, 5 of 5 fine (~2.5 s).
  let primedCtx = null;
  function primeAudio() {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx || (primedCtx && primedCtx.state !== "closed")) return;
    let ctx;
    try { ctx = new AudioCtx(); ctx.resume().catch(() => {}); } catch { return; }
    primedCtx = ctx;
    // Unused (the call kept its gate, or the mic prompt took long): closed — iOS allows only a few.
    setTimeout(() => { if (primedCtx === ctx) { primedCtx = null; ctx.close().catch(() => {}); } }, 30000);
  }
  function takeAudioContext(AudioCtx) {
    const ctx = primedCtx && primedCtx.state !== "closed" ? primedCtx : new AudioCtx();
    primedCtx = null;
    return ctx;
  }
  const within = (promise, ms) => Promise.race([promise, new Promise((resolve) => setTimeout(() => resolve("late"), ms))]);

  async function createNoiseGate(stream, onSignal) {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx || params.get("gate") === "off") return null;
    const ctx = takeAudioContext(AudioCtx);
    try {
      if (!ctx.audioWorklet) throw new Error("no AudioWorklet");
      const url = URL.createObjectURL(new Blob([GATE_WORKLET], { type: "application/javascript" }));
      try {
        if (await within(ctx.audioWorklet.addModule(url), 4000) === "late") throw new Error("worklet load timed out");
      } finally { URL.revokeObjectURL(url); }
      await within(ctx.resume(), 1500);
      // Not running: the gate would pass only silence — the call goes on with the mic as it is.
      if (ctx.state !== "running") throw new Error("audio not started: " + ctx.state);
      const node = new AudioWorkletNode(ctx, "noise-gate", {
        numberOfInputs: 2, channelCount: 1, channelCountMode: "explicit",
        // ?near=0.04 makes barge-in more sensitive (quiet voice / far mic), 0.09 less.
        processorOptions: { near: parseFloat(params.get("near")) || 0.06, stats: true, sensitive: params.get("sens") === "1" },
      });
      node.port.onmessage = (e) => onSignal(e.data);
      ctx.onstatechange = () => {
        if (ctx.state !== "suspended" && ctx.state !== "interrupted") return;
        diag.ctxInterrupts += 1;
        ctx.resume().catch(() => {}); // fails while a phone call holds the audio; retried when the page is back
      };
      const out = ctx.createMediaStreamDestination();
      ctx.createMediaStreamSource(stream).connect(node, 0, 0).connect(out);
      let remoteSource = null;
      return {
        stream,
        ctx, // already running (started in a tap): Шазам records its clip through it without a tap of its own
        track: out.stream.getAudioTracks()[0],
        // The assistant's audio, measured only (never played from here). A gate kept for the next
        // call (voice switch) drops the old call's audio first.
        listenTo(remote) {
          if (remoteSource) remoteSource.disconnect();
          remoteSource = remote ? ctx.createMediaStreamSource(remote) : null;
          if (remoteSource) remoteSource.connect(node, 0, 1);
        },
        // Still usable for the next call: not closed by the browser, and running again. iOS leaves it
        // «interrupted» after a phone call, Siri or a locked screen: the gate then passes only silence.
        alive() { if (ctx.state === "suspended" || ctx.state === "interrupted") ctx.resume().catch(() => {}); return ctx.state !== "closed"; },
        close() { ctx.close().catch(() => {}); },
      };
    } catch {
      ctx.close().catch(() => {});
      return null; // old browser: send the mic as is
    }
  }

  // ── Єва: «Єва, скажи» wakes her (or «Привіт, Єва», «Гей, Єва», …), «Дякую, Єва» pauses ───────
  // Same rules as voice/wake_phrases.py — keep the two in sync (tests/test_eva_phrases.py has the
  // cases). The name is matched against a list (fuzzy matching a 3-letter word would accept
  // «два»), the verbs fuzzily, like Python's difflib ratio ≥ 0.75.
  const EVA_NAME_FORMS = new Set(["єва", "єво", "ева", "эва", "эво", "ево", "єфа", "ефа", "їва", "іва", "йева", "йєва", "єєва",
    "eva", "evo", "eve", "yeva", "yevo", "jeva"]);
  const EVA_WAKE_VERBS = [
    "скажи", "кажи", "скажіть", "скажи-но",
    "привіт", "привітик", "вітаю", "гей", "хей", "агов", "алло",
    "слухай", "послухай", "допоможи", "підкажи", "прокидайся",
    "hello", "hey",
  ];
  const EVA_STOP_VERBS = ["дякую", "дякуємо"]; // one pause phrase: «Дякую, Єва»
  const EVA_MAX_GAP = 2;
  // The names she answers to: the picked character's («Степан, скажи»; /api/voices «wake»), Єва's until known.
  let wakeNames = EVA_NAME_FORMS;
  let wakeSizes = [4, 3]; // the name lengths unglue tries
  function setWakeNames(list) {
    wakeNames = list && list.length ? new Set(list) : EVA_NAME_FORMS;
    wakeSizes = [...new Set([...wakeNames].map((n) => n.length))].filter((n) => n >= 3).sort((a, b) => b - a);
  }

  function evaTokens(text) {
    const lowered = String(text || "").toLowerCase().replace(/’/g, "'").replace(/ё/g, "е");
    const words = (lowered.match(/[a-zа-яіїєґё'-]+/g) || []).map((w) => w.replace(/^['-]+|['-]+$/g, "")).filter(Boolean);
    const merged = [];
    for (const word of words) {
      const last = merged[merged.length - 1];
      if ((last === "є" || last === "е" || last === "э") && word === "ва") merged[merged.length - 1] = last + word;
      else merged.push(...unglue(word));
    }
    return merged;
  }

  // «привітєва» / «гейєва» / «євоскажи»: the recogniser glued the name to the verb — split them.
  function unglue(word) {
    if (word.length <= 4 || wakeNames.has(word)) return [word];
    const verbs = EVA_WAKE_VERBS.concat(EVA_STOP_VERBS);
    for (const size of wakeSizes) {
      if (wakeNames.has(word.slice(-size)) && like(word.slice(0, -size), verbs)) return [word.slice(0, -size), word.slice(-size)];
      if (wakeNames.has(word.slice(0, size)) && like(word.slice(size), verbs)) return [word.slice(0, size), word.slice(size)];
    }
    return [word];
  }

  // difflib.SequenceMatcher.ratio(): 2·matches / total length, matches = longest common blocks.
  function matchingChars(a, b) {
    let best = 0, ai = 0, bj = 0;
    for (let i = 0; i < a.length; i++) {
      for (let j = 0; j < b.length; j++) {
        let k = 0;
        while (i + k < a.length && j + k < b.length && a[i + k] === b[j + k]) k++;
        if (k > best) { best = k; ai = i; bj = j; }
      }
    }
    if (!best) return 0;
    return best + matchingChars(a.slice(0, ai), b.slice(0, bj)) + matchingChars(a.slice(ai + best), b.slice(bj + best));
  }
  const like = (word, verbs) => verbs.some((v) => word === v || (2 * matchingChars(word, v)) / (word.length + v.length) >= 0.75);

  function evaPair(words, verbs) {
    for (let i = 0; i < words.length; i++) {
      if (!wakeNames.has(words[i])) continue;
      const lo = Math.max(0, i - EVA_MAX_GAP - 1), hi = Math.min(words.length, i + EVA_MAX_GAP + 2);
      for (let j = lo; j < hi; j++) if (j !== i && like(words[j], verbs)) return [i, j];
    }
    return null;
  }

  // «Єва, скажи …» → what was said after the phrase ("" if nothing); null if no wake phrase.
  function matchWake(text) {
    const words = evaTokens(text);
    const pair = evaPair(words, EVA_WAKE_VERBS);
    return pair ? words.slice(Math.max(pair[0], pair[1]) + 1).join(" ") : null;
  }
  const isStop = (text) => evaPair(evaTokens(text), EVA_STOP_VERBS) !== null;

  // mode: off → connecting → waiting («Єва, скажи»; the model is muted) ⇄ active (listening /
  // thinking / speaking); switching = reconnecting with a new voice.
  const eva = {
    lastActiveAt: 0, // the user's near voice or her answer last (touchActivity)
    mode: "off",
    pending: null,
    recentUser: "",   // rolling user transcript, for «Дякую, Єва»
    lastUserAt: 0,
    lastAudibleAt: 0,
    thinking: false,
    thinkTimer: null,
    pauseTimer: null,
    recognizer: null,
    wakeSupported: Boolean(window.SpeechRecognition || window.webkitSpeechRecognition) && params.get("wake") !== "off",
  };

  const songs = { list: [], current: store.get("va-song") || "", audio: new Audio(), loadedId: "",
    playing: false, primed: false, pending: false, pendingSince: 0, pendingTimer: null, mutedMic: false, left: null,
    external: null }; // external: {url, name} — a recognised song's preview playing in the same player
  songs.audio.setAttribute("playsinline", "");
  // The «Музика» panel (header button): which tab, what Шазам found, the songs being written (see initMusicPanel).
  const musicUi = { open: false, tab: store.get("va-music-tab") || "songs",
    status: { recognize: true, generate: true }, listening: false, track: null, history: [], jobs: [],
    jobsTimer: null, jobsTick: null, unseen: false, calls: new Set() };
  try { musicUi.history = JSON.parse(store.get("va-shazam") || "[]").slice(0, 8); } catch { musicUi.history = []; }

  function showState() {
    const m = eva.mode;
    if (m === "off") return; // stop() sets its own text
    let state;
    if (songs.playing && m !== "connecting" && m !== "switching") state = ["Грає пісня — " + callName + " мовчить", "wait"];
    else if (songs.pending && m === "active") state = [songs.left === null ? "Пісня почнеться, коли розмова затихне"
      : "Пісня почнеться за " + songs.left + " с — скажіть щось, щоб відкласти", "wait"];
    else if (micMuted && m !== "connecting" && m !== "switching") state = ["Мікрофон вимкнено — " + callName + " нічого не чує", "wait"];
    else if (m === "connecting") state = ["Підключаюсь…", "busy"];
    else if (m === "switching") state = ["Перемикаю голос…", "busy"];
    else if (m === "waiting" && eva.ready) state = ["На зв'язку — чекаю «Привіт, " + callName + "»", "wait"];
    else if (m === "waiting") state = eva.wakeSupported
      ? [bgListen ? "Пауза — слухаю фоном, чекаю «" + callName + ", скажи»" : "Пауза — чекаю «" + callName + ", скажи»", "wait"]
      : ["Пауза — натисніть «Продовжити»", "wait"];
    else if (barge.audible) state = ["Говорю", "speak"];
    else if (barge.speaking) state = ["Слухаю", "live"];
    else if (eva.thinking) state = ["Думаю…", "think"];
    else state = ["Слухаю — говоріть", "live"];
    setStatus(state[0], state[1]);
  }

  // ── «Помічниця» / «Консьєрж» ──
  function showMode() {
    $("modeAssistant").setAttribute("aria-selected", String(mode !== "concierge"));
    $("modeConcierge").setAttribute("aria-selected", String(mode === "concierge"));
    $("concierge").hidden = mode !== "concierge";
    $("wakeHint").hidden = mode === "concierge";
    if (mode === "concierge") checkGuestList();
  }

  function setMode(next) {
    if (next === mode) return;
    mode = next;
    store.set("va-mode", mode);
    // A call keeps the instructions it opened with: the other mode needs a new one.
    if (eva.mode !== "off") {
      stop();
      addLine("system", "Режим змінено — натисніть «Почати розмову».");
    }
    showGuest(null);
    showMode();
    updateButtons();
  }

  function showCheckQuestion() {
    $("checkQuestion").checked = checkQuestion;
    $("checkQuestionInfo").textContent = checkQuestion
      ? "Після імені " + callName + " питає щось із запису гостя (посаду, хобі…)."
      : "Вимкнено: " + callName + " пускає, щойно ім'я знайдено в списку.";
  }

  function setCheckQuestion() {
    checkQuestion = $("checkQuestion").checked;
    store.set("va-check-question", checkQuestion ? "1" : "0");
    showCheckQuestion();
    checkGuestList();
    // A call keeps the instructions it opened with: «Наступний гість» opens a new one.
    if (eva.mode !== "off") addLine("system", (checkQuestion ? "Перевірочне питання увімкнено" : "Перевірочне питання вимкнено") +
      " — з наступного гостя.");
  }

  // g: the server's {name, verified, duplicate?, time?} (/api/me); null — nobody yet.
  let shownGuest = "";
  function showGuest(g, announce = false) {
    const name = g && g.name;
    $("guestName").textContent = name || "ще не назвався";
    $("guestBadge").hidden = !name;
    $("guestBadge").textContent = !g ? "" : g.duplicate ? "вже відмічений о " + g.time : g.verified ? "✓ підтверджено" : "не підтверджено";
    $("guestBadge").className = "guest-badge " + (g && g.verified ? "ok" : "err");
    const key = name ? name + (g.verified ? "+" : g.duplicate ? "=" : "-") : "";
    if (announce && name && key !== shownGuest) {
      addLine("system", g.duplicate ? name + " уже проходив(-ла) о " + g.time + " — повторно не пускаємо."
        : (g.verified ? "Гостя підтверджено й відмічено: " : "Не вдалося підтвердити: ") + name + ".");
      if (g.verified || g.duplicate) checkGuestList();
    }
    shownGuest = key;
  }

  async function refreshGuest() {
    const me = await fetchMe();
    if (me && me.guest) showGuest(me.guest, true);
  }

  // A new event: everyone may come in again.
  async function resetCheckins() {
    if (!confirm("Скинути всі відмітки гостей? Кожен зможе пройти знову (нова подія).")) return;
    try {
      const res = await fetch(BACKEND + "/api/concierge/checkins", { method: "DELETE", headers: headers() });
      if (!res.ok) throw new Error(res.status);
      addLine("system", "Відмітки гостей скинуто.");
    } catch { addLine("system", "Не вдалося скинути відмітки."); }
    checkGuestList();
  }

  // The guest list is what «Знання» holds: without an enabled set there is nobody to check against.
  async function checkGuestList() {
    fetch(BACKEND + "/api/concierge/checkins", { headers: headers() })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (d) $("checkinCount").textContent = "Відмічено гостей: " + d.checkins.length; })
      .catch(() => {});
    try {
      const res = await fetch(BACKEND + "/api/knowledge", { headers: headers() });
      if (!res.ok) return;
      const data = await res.json();
      const on = data.sets.filter((s) => s.enabled);
      const people = on.reduce((n, s) => n + s.count, 0);
      $("conciergeInfo").classList.toggle("warn-text", !on.length);
      $("conciergeInfo").textContent = on.length
        ? "Список: " + on.map((s) => "«" + s.name + "»").join(", ") + " — " + people + " записів. " + callName + " вітає людину, питає ім'я, " +
          "звіряє його зі списком (однакові імена — питає прізвище)" +
          (checkQuestion ? " і ставить одне перевірочне питання." : " і пускає, щойно знайшла в списку.")
        : "Список людей порожній — завантажте його у «Знання» (кнопка вгорі), інакше нема з ким звіряти.";
    } catch { /* asleep: the hint stays */ }
  }

  function updateButtons() {
    const on = eva.mode !== "off";
    $("nextGuest").hidden = !(on && mode === "concierge");
    $("talk").textContent = on ? "Завершити" : "Почати розмову";
    $("talk").classList.toggle("primary", !on);
    $("talk").classList.toggle("danger", on);
    $("talk").disabled = false;
    $("resume").hidden = eva.mode !== "waiting";
    $("pause").hidden = eva.mode !== "active";
    $("micMute").hidden = !on;
    $("micMute").textContent = micMuted ? "Увімкнути мікрофон" : "Вимкнути мікрофон";
    $("micMute").setAttribute("aria-pressed", String(micMuted));
    $("micMute").classList.toggle("danger", micMuted);
    showWakeHint();
    showMeter(); // «Останній дзвінок» once she is off
  }

  // Barge-in, like the desktop app: the Live model alone reacts slowly and keeps talking over
  // the user. Only the person at the mic counts ("near" voice — background talk, a TV, bangs
  // don't): their voice over the assistant ducks it a little; a stop word or real words from
  // them mute it and tell the model to stop. A sound alone never stops the assistant.
  // Whole words only; no «секунду»/«хвилинку» — they are everyday words, not a stop request.
  const STOP_WORDS = new Set(["стоп", "зачекай", "почекай", "стривай", "досить", "stop", "wait"]);
  const splitWords = (text) => text.toLowerCase().replace(/[ʼ’`]/g, "'").split(/[^а-яіїєґa-z'-]+/i).filter(Boolean);
  const BACKCHANNEL = new Set(["угу", "ага", "так", "ммм", "мм", "м", "ок", "окей", "добре", "ну", "ого", "ага-ага", "мгм", "ясно", "зрозуміло", "да",
    "так-так", "угу-угу", "хм", "о", "ой", "ох", "слухаю", "нічого", "собі", "та", "ха", "ха-ха", "хаха", "ахах", "вау"]);
  const barge = {
    audible: false,     // assistant audio is playing right now
    audibleEndedAt: 0,  // when her audio last went quiet
    speaking: false,    // user voice right now
    stage: "idle",      // idle → ducked → muted
    heard: "",          // user words since this barge-in candidate started
    burst: "",          // what she has said since her last pause
    burstAt: 0,
    timers: [],
    releaseTimer: null,
    speechEndedAt: 0,   // when the near voice last stopped (transcripts lag behind it)
    recentAssistant: "", // for the echo check
    ducks: 0, mutes: 0, // how often she was ducked / cut off (?debug=1: evaDebug())
  };

  function bargeClear() {
    barge.timers.forEach(clearTimeout);
    barge.timers = [];
  }

  // level: barge-in ducking (1 / 0.5 / 0) × the user's volume; silent while Єва is paused.
  let remoteLevel = 1;
  function setRemoteVolume(level) {
    remoteLevel = level;
    const el = $("remote");
    const v = eva.mode === "waiting" ? 0 : level * volume;
    rampVolume(el, v);
  }

  // Her volume moves over ~60 ms instead of jumping: an instant step in a playing voice is a click, and
  // on speakers — where her own echo makes barge-in ducks (1 → 0.5 → 1) and mutes frequent — the clicks
  // came as crackle, louder the higher the volume (feedback: «хрипи … повʼязане з самоперебиванням»).
  let volRamp = null;
  function rampVolume(el, target) {
    clearInterval(volRamp);
    // iOS: an <audio> element's volume is read-only (always 1) — a duck to 0.5 can't be done, a mute can.
    if (IOS) { el.muted = target === 0; return; }
    if (el.muted) { el.volume = 0; el.muted = false; }
    const from = el.volume;
    let step = 0;
    volRamp = setInterval(() => {
      step += 1;
      el.volume = Math.min(1, Math.max(0, from + (target - from) * (step / 8)));
      if (step >= 8) { clearInterval(volRamp); volRamp = null; el.muted = target === 0; }
    }, 8);
  }

  // Her voice into the speakers and into the gate's echo meter.
  function playRemote(stream) {
    stopPreview(); // a sample left playing would talk over her
    $("remote").srcObject = stream;
    if (gate) gate.listenTo(stream);
  }

  // Her voice for the current call.
  function attachVoice(stream) {
    if (stream) playRemote(stream);
  }

  const HER_PAUSE_MS = 700; // a pause between her sentences: she goes on after it
  // At the door (concierge) people talk around her all the time: ducking her at the first sound and muting
  // her on any 1.5 s of voice cut her words and phrases off («пропускає слова», «обриває фразу»). There she
  // gives way later and only to clearer speech; the stop word still works at once.
  const bargeTiming = () => (mode === "concierge"
    ? { duckAfterMs: 1000, duckLevel: 0.85, stopBySoundMs: 3000, meaningfulWords: 4 }
    : { duckAfterMs: 400, duckLevel: 0.5, stopBySoundMs: 1500, meaningfulWords: 3 });
  const bargeLevel = () => (barge.stage === "muted" ? 0 : barge.stage === "ducked" ? bargeTiming().duckLevel : 1);

  function bargeReset() {
    bargeClear();
    clearTimeout(barge.releaseTimer);
    barge.stage = "idle";
    barge.heard = "";
    barge.speaking = false;
    barge.audible = false;
    setRemoteVolume(1);
  }

  function steer(kind) {
    const content = {
      // A stop word is often followed by what they want instead («Stop, stop. Explain it to a kid»):
      // «say only Добре and wait» made her answer «Добре.» and nothing else.
      ack: "Stop your previous answer immediately. Do not continue or resume it. If the user goes on with a question or request, answer that. If they only told you to stop, say at most one short «Добре.» and wait silently.",
      // Prod: she once said this aloud — «Стоп, слухаю тебе.»
      stop: "Stop speaking immediately. The user is talking. Do not finish or resume your previous sentence, and do not say that you stop or listen — just go silent. Listen and answer what they say.",
      pause: "The user said «Дякую» with your name: stop speaking immediately and stay completely silent. Do not reply to it. You will be woken again later — then continue with full memory of this conversation.",
    }[kind];
    try {
      if (channel && channel.readyState === "open") {
        channel.send(JSON.stringify({
          type: "session.instructions.append",
          content,
          delegation_id: null,
          event_id: "barge_" + Math.random().toString(16).slice(2, 10),
        }));
      }
    } catch { /* channel closing */ }
  }

  // She is still talking: audible now, or quiet for less than a breath between sentences.
  const herStillTalking = () => barge.audible || Date.now() - barge.audibleEndedAt < HER_PAUSE_MS;

  function bargeConfirm(kind) {
    if (barge.stage === "muted") return;
    // The user's words come 1–2 s after their sound: by then she has often finished. A «stop, the user is
    // talking» then reached a model that was already listening — it went silent for good or said «Ага, чую.
    // Говори.» and waited, and what they said was lost (measured on GPT-Live, a name said over the end of
    // her answer: 3 of 5). She is quiet: nothing to stop, she answers them as usual.
    if (kind === "stop" && !herStillTalking()) {
      bargeClear();
      barge.stage = "idle";
      barge.heard = "";
      setRemoteVolume(1);
      return;
    }
    bargeClear();
    barge.stage = "muted";
    barge.mutes += 1;
    diagBargeMuted();
    setRemoteVolume(0);
    steer(kind);
    scheduleRelease();
  }

  // Unmute once the interrupted answer has stopped (assistant quiet) and the user is done, or
  // at the latest 1.5 s after the user stopped — the model may go straight into a new reply.
  function scheduleRelease() {
    clearTimeout(barge.releaseTimer);
    if (barge.stage !== "muted") return;
    if (barge.speaking) return; // re-checked when the user stops
    const sinceSpeech = Date.now() - barge.speechEndedAt;
    if (!barge.audible || sinceSpeech > 1500) {
      barge.stage = "idle";
      barge.heard = "";
      setRemoteVolume(1);
      return;
    }
    barge.releaseTimer = setTimeout(scheduleRelease, 100);
  }

  function onGateSignal(signal) {
    if ("stats" in signal) { gateStats = signal.stats; return; } // for evaGate() and /api/diag
    bargeOnGate(signal);
    if ("audible" in signal) eva.lastAudibleAt = Date.now(); // when she started / stopped sounding
    if (barge.audible) eva.thinking = false;
    showState();
  }

  function bargeOnGate(signal) {
    if ("audible" in signal) {
      if (barge.audible && !signal.audible) barge.audibleEndedAt = Date.now();
      barge.audible = signal.audible;
      if (barge.stage === "muted") scheduleRelease();
      return;
    }
    barge.speaking = signal.speaking;
    if (signal.speaking && eva.mode === "active") touchActivity(); // the user talks: not idle
    if (signal.speaking) {
      clearTimeout(barge.releaseTimer);
      if (barge.stage !== "idle" || !barge.audible || herBackchannel()) return;
      diagBargeStart();
      if (Date.now() - barge.speechEndedAt > 1500) barge.heard = "";
      const timing = bargeTiming();
      barge.timers.push(setTimeout(() => {
        if (barge.speaking && barge.stage === "idle") { barge.stage = "ducked"; barge.ducks += 1; setRemoteVolume(timing.duckLevel); }
      }, timing.duckAfterMs));
      // A quiet voice over her on speakers: the gate rightly keeps it below her echo, so its words never
      // reach the model and nothing confirms the barge-in — she went on and the user felt ignored.
      // Someone who keeps talking to her for 1.5 s with none of their own words heard (only her echo,
      // or nothing) wants the floor; once she stops, the rest of what they say gets through. Their
      // own words, an «угу» included, are judged by bargeOnWords as before.
      barge.timers.push(setTimeout(() => {
        if (barge.speaking && barge.stage === "ducked" && !ownWords().length) bargeConfirm("stop");
      }, timing.stopBySoundMs));
    } else {
      barge.speechEndedAt = Date.now();
      diagBargeEnded();
      if (barge.stage === "muted") { scheduleRelease(); return; }
      bargeClear();
      if (barge.stage === "ducked") {
        // A short sound: wait briefly for its transcript, else it was a cough / «угу».
        barge.timers.push(setTimeout(() => {
          if (barge.stage === "ducked" && !barge.speaking) { barge.stage = "idle"; barge.heard = ""; setRemoteVolume(1); }
        }, 400));
      }
    }
  }

  // User transcript while the assistant talks: decide by the words, not just the sound.
  // The user's words since this barge-in started; words she herself just said are her echo, not the
  // user — stop words included.
  function ownWords() {
    const echo = new Set(splitWords(barge.recentAssistant));
    return splitWords(barge.heard).filter((w) => !echo.has(w));
  }

  function bargeOnWords(delta) {
    if (barge.stage === "muted" || !(barge.audible || barge.stage === "ducked")) return;
    if (herBackchannel() && !splitWords(delta).some((w) => STOP_WORDS.has(w))) return;
    // Words count only while the near voice is on (or just ended — transcripts lag); words
    // heard with no one at the mic are background talk.
    if (!barge.speaking && Date.now() - barge.speechEndedAt > 1200) return;
    barge.heard += delta;
    const own = ownWords();
    if (own.some((w) => STOP_WORDS.has(w))) { bargeConfirm("ack"); return; }
    const meaningful = own.filter((w) => !BACKCHANNEL.has(w));
    if (meaningful.length >= bargeTiming().meaningfulWords) bargeConfirm("stop");
  }

  // A stop word the user said — not one in her own recent words (the recogniser hears her too).
  function stopWordOverHer(text) {
    const echo = new Set(splitWords(barge.recentAssistant));
    return splitWords(text).some((w) => STOP_WORDS.has(w) && !echo.has(w));
  }

  function bargeOnAssistantText(delta) {
    barge.recentAssistant = (barge.recentAssistant + delta).slice(-300);
    if (Date.now() - barge.burstAt > 800) barge.burst = "";
    barge.burst += delta;
    barge.burstAt = Date.now();
  }

  // She is only saying «угу» / «так-так» / «ммм» while the user talks (voice/interrupt_intent.py
  // is_backchannel_utterance): that overlap is the point, not an answer to barge into.
  function herBackchannel() {
    const words = splitWords(barge.burst);
    return words.length > 0 && words.length <= 3 && words.every((w) => BACKCHANNEL.has(w));
  }

  function onServerEvent(raw) {
    let event;
    try { event = JSON.parse(raw); } catch { return; }
    switch (event.type) {
      case "session.started":
        applyStart();
        break;
      case "session.input_transcript.delta":
        if (eva.mode === "waiting") break;
        appendTranscript("user", event.delta);
        eva.lastUserAt = Date.now();
        eva.recentUser = (eva.recentUser + event.delta).slice(-80);
        // «Дякую, Єва» — also while she is talking: cut her off and pause.
        if (isStop(eva.recentUser)) { pauseEva(); break; }
        eva.thinking = true;
        clearTimeout(eva.thinkTimer);
        eva.thinkTimer = setTimeout(() => { eva.thinking = false; showState(); }, 15000);
        bargeOnWords(event.delta);
        // Only words of the person at the mic keep the call on: colleagues or a TV in the room, also
        // transcribed, kept a forgotten tab's call open up to the server's cap (billed per minute).
        if (nearVoiceLately()) touchActivity();
        showState();
        break;
      case "session.output_transcript.delta":
        if (eva.mode === "waiting") break; // whatever she says to «Дякую, Єва» is not played
        appendTranscript("assistant", event.delta);
        eva.lastOutputAt = Date.now();
        bargeOnAssistantText(event.delta);
        eva.thinking = false;
        touchActivity();
        break;
      case "session.closed":
        onSessionClosed();
        break;
      case "session.delegation.created":
        // A request went to the backend: if it is «що ти бачиш», the frame is on the server before
        // the backend has even decided to look (the tool takes any frame since the work started).
        snapForLook("");
        break;
      case "response.event": {
        // The backend is about to look through the camera: snap what is in front of it right now.
        const inner = event.event || {};
        const item = inner.item || {};
        if ((inner.type === "response.output_item.added" || inner.type === "response.output_item.done")
            && item.type === "function_call" && item.name === "look_at_camera") snapForLook(item.call_id || item.id);
        // «Музика»: recognize_song needs this page's mic, play_song a song started here, generate_song a job to watch.
        if ((inner.type === "response.output_item.added" || inner.type === "response.output_item.done")
            && item.type === "function_call" && MUSIC_CALLS.has(item.name)) {
          onMusicCall(item.name, item.call_id || item.id || item.name, inner.type === "response.output_item.done");
        }
        // «Консьєрж»: the backend says who the guest is once the check question is answered.
        // What the server decided (confirmed, refused without an answer, already let in) is in /api/me.
        if (inner.type === "response.output_item.done" && item.type === "function_call" && item.name === "confirm_guest") {
          setTimeout(refreshGuest, 600);
        }
        break;
      }
      case "error": {
        const message = (event.error && event.error.message) || "невідома";
        // A voice switch closes the call while the page may still be sending: not a real error.
        if (!/session is closing/i.test(message)) addLine("system", "Помилка: " + message);
        break;
      }
      default:
        break;
    }
  }

  // Every start() gets a number; a teardown (or a newer start) makes the old one stale, so an
  // attempt that is still awaiting the mic / backend never touches the new call's state.
  let attempt = 0;
  let dropTimer = null;
  let idleTimer = null;
  // GPT-Live bills every second a session is open, muted or not ($0.05/min), so a forgotten call
  // pauses after this long with nobody speaking… (config.js: IDLE_PAUSE_SECONDS)
  const CFG = window.APP_CONFIG || {};
  const IDLE_LIMIT_MS = (CFG.IDLE_PAUSE_SECONDS ?? 90) * 1000;
  // …and a pause this long closes the Live session; «Єва, скажи» reopens it with the history.
  // (config.js: PAUSE_CLOSE_SECONDS)
  const PAUSE_CLOSE_MS = (CFG.PAUSE_CLOSE_SECONDS ?? 15) * 1000;
  // A pause never keeps a call open longer than this, even while the server asks to.
  const PAUSE_HOLD_MAX_MS = 5 * 60 * 1000;

  // ── call meter: minutes and approximate cost from «Почати розмову» to «Завершити» ──
  // GPT-Live bills every second a session is open, so this adds up every open session: the call,
  // a pause's first PAUSE_CLOSE_MS, the standby call of a voice switch. A session counts from the
  // backend's answer (it exists then) until the page closes it. (config.js: PRICE_PER_MINUTE_USD)
  const PRICE_PER_MIN = Number(CFG.PRICE_PER_MINUTE_USD ?? 0.05);
  const meter = { doneMs: 0, open: new Map(), timer: null };

  function meterReset() {
    meter.doneMs = 0;
    meter.open.clear();
    showMeter();
  }

  function meterOpen(peer) {
    if (peer.signalingState === "closed" || meter.open.has(peer)) return;
    meter.open.set(peer, Date.now());
    if (!meter.timer) meter.timer = setInterval(showMeter, 1000);
    showMeter();
  }

  // Every call is closed here, so the meter stops counting it.
  function closePeer(peer) {
    const since = meter.open.get(peer);
    if (since !== undefined) {
      meter.doneMs += Date.now() - since;
      meter.open.delete(peer);
    }
    peer.close();
    if (!meter.open.size) { clearInterval(meter.timer); meter.timer = null; }
    showMeter();
  }

  function showMeter() {
    const now = Date.now();
    let ms = meter.doneMs;
    meter.open.forEach((since) => { ms += now - since; });
    $("meter").hidden = ms <= 0;
    if (ms <= 0) return;
    const sec = Math.floor(ms / 1000);
    const used = Math.floor(sec / 60) + ":" + String(sec % 60).padStart(2, "0") +
      " · ≈ $" + ((ms / 60000) * PRICE_PER_MIN).toFixed(2);
    $("meter").textContent = meter.open.size ? "Дзвінок: " + used
      : eva.mode === "off" ? "Останній дзвінок: " + used
      : "Дзвінок: " + used + " · на паузі не рахується";
  }

  // The person at the mic spoke just now (the gate's near voice; transcripts come 1–2 s after it). Without
  // the gate (an old browser) any words count, as before.
  const NEAR_VOICE_RECENT_MS = 3000;
  const nearVoiceLately = () => !gate || barge.speaking || Date.now() - barge.speechEndedAt < NEAR_VOICE_RECENT_MS;

  function touchActivity() {
    eva.lastActiveAt = Date.now();
    clearTimeout(idleTimer);
    if (!pc || eva.mode !== "active") return;
    idleTimer = setTimeout(() => {
      if (eva.mode !== "active") return;
      addLine("system", "Тиша вже " + Math.round(IDLE_LIMIT_MS / 1000) + " с — ставлю на паузу, щоб не витрачати хвилини. " + callName + " все пам'ятає.");
      pauseEva();
    }, IDLE_LIMIT_MS);
  }

  function sendEvent(event) {
    try {
      if (channel && channel.readyState === "open") { channel.send(JSON.stringify(event)); return true; }
    } catch { /* channel closing */ }
    return false;
  }

  const eventId = (prefix) => prefix + "_" + Math.random().toString(16).slice(2, 10);
  // Context the model acts on right away (it answers it out loud).
  const commentary = (content) =>
    sendEvent({ type: "session.commentary.append", content, delegation_id: null, event_id: eventId("say") });

  // «Почати розмову»: the call opens paused and she waits for «Привіт, Єва» — she spoke first on her
  // own, and the user wants to start the talk. Testers could not tell whether she was on at all
  // (feedback: «при запуску … мовчить»), so the status and the log say she is on and waiting. Without
  // the phrase listener (Firefox) nothing could wake her: she greets at once.
  // A concierge greets whoever comes up at once: guests don't know to say «Привіт, Єва».
  const defaultStart = () => (eva.wakeSupported && mode !== "concierge" ? { paused: true, ready: true } : { greet: true, hello: true });

  // opts: paused — connect and wait for «Єва, скажи»; ask — a request said with the wake phrase;
  // greet — a short «Слухаю» unless the user starts talking; switched — first words in a new voice.
  async function start(opts = {}) {
    if (!BACKEND) {
      addLine("system", "Не вказано адресу бекенду (web/config.js або ?backend=…).");
      return;
    }
    const my = ++attempt;
    eva.pending = opts;
    if (eva.mode !== "switching") eva.mode = "connecting";
    showState();
    updateButtons();
    try {
      // A voice switch / new conversation keeps the microphone open: no new permission, faster.
      const live = mic && mic.getAudioTracks().some((t) => t.readyState === "live");
      const stream = live ? mic : await openMic();
      if (my !== attempt) { if (stream !== mic) stream.getTracks().forEach((t) => t.stop()); return; }
      if (stream !== mic) listDevices(); // device names are known once the mic is allowed
      if (stream !== mic) watchMic(stream);
      mic = stream;
      const peer = new RTCPeerConnection();
      pc = peer;
      peer.ontrack = (e) => attachVoice(e.streams[0]);
      // A voice switch keeps the gate too (it is built on the same mic): no new AudioContext.
      if (gate && (gate.stream !== mic || !gate.alive())) { gate.close(); gate = null; }
      if (!gate) {
        const newGate = await createNoiseGate(mic, onGateSignal);
        if (my !== attempt) { if (newGate) newGate.close(); return; }
        gate = newGate;
      }
      const track = gate ? gate.track : mic.getAudioTracks()[0];
      track.enabled = !micMuted;
      if (micMuted) mic.getAudioTracks().forEach((t) => { t.enabled = false; }); // muted: the gate hears nothing either
      micSender = peer.addTrack(track, mic);
      channel = peer.createDataChannel("oai-events");
      wireCall(peer, channel);

      const offer = await peer.createOffer();
      await peer.setLocalDescription(offer);
      sessionVoice = voice;
      const res = await fetch(BACKEND + "/api/session", {
        method: "POST",
        headers: headers(),
        body: JSON.stringify(sessionBody({ sdp: withClearOpus(offer.sdp), user_text: opts.userText || undefined })),
      });
      if (my !== attempt) return;
      if (!res.ok) throw new Error("Сервер відповів " + res.status);
      const { sdp } = await res.json();
      if (my !== attempt) return;
      meterOpen(peer);
      await peer.setRemoteDescription({ type: "answer", sdp });
    } catch (err) {
      if (my !== attempt) return;
      const msg = err && err.name === "NotAllowedError" ? "Немає доступу до мікрофона" : (err.message || String(err));
      addLine("system", msg);
      stop("Не вдалося підключитись", "err");
    }
  }

  // What a new call is opened with: voice and delivery.
  const sessionBody = (extra) => Object.assign({
    voice: voice || undefined, voice_variant: voice ? variantFor(voice) : undefined, speed, style, prompt: promptVariant,
    mode: mode === "concierge" ? "concierge" : undefined,
    check_question: mode === "concierge" ? checkQuestion : undefined,
  }, extra);

  // The offer's Opus line says how we want to receive her voice. Browser defaults leave the bitrate
  // to the sender, and a low one smears «с», «ш», «ц» (they live at 4–12 kHz) — she sounded lisped.
  // Only the copy sent to the server is changed; the local description stays as the browser made it.
  function withClearOpus(sdp) {
    const pt = (sdp.match(/a=rtpmap:(\d+) opus\/48000/i) || [])[1];
    if (!pt) return sdp;
    const want = { maxplaybackrate: "48000", maxaveragebitrate: "64000", useinbandfec: "1", stereo: "0" };
    return sdp.replace(new RegExp("a=fmtp:" + pt + " ([^\\r\\n]*)"), (line, params) => {
      const kept = params.split(";").filter((p) => p && !(p.split("=")[0].trim() in want));
      return "a=fmtp:" + pt + " " + kept.concat(Object.entries(want).map(([k, v]) => k + "=" + v)).join(";");
    });
  }

  // The current call's connection and server events (a standby call gets them when swapped in).
  function wireCall(peer, ch) {
    peer.onconnectionstatechange = () => {
      if (pc !== peer) return;
      const state = peer.connectionState;
      clearTimeout(dropTimer);
      // The server ends the call itself to restart it in a new voice: check before giving up.
      if (state === "failed" || state === "closed") onSessionClosed("З'єднання втрачено", "err");
      // "disconnected" is often a network blip that recovers by itself (Wi-Fi switch etc.).
      else if (state === "disconnected") {
        setStatus("Зв'язок перервався — відновлюю…", "busy");
        dropTimer = setTimeout(() => { if (pc === peer) onSessionClosed("З'єднання втрачено", "err"); }, 8000);
      } else if (state === "connected") showState();
    };
    ch.onmessage = (e) => { if (pc === peer) onServerEvent(e.data); };
    ch.onclose = () => { if (pc === peer && eva.mode !== "connecting" && eva.mode !== "switching") onSessionClosed(); };
  }

  // Sent as silent context (session.thinking.append): as a commentary, which is speakable, she once said
  // part of it aloud («користувач змінив ваш голос»). Measured on GPT-Live: from a silent note she carries
  // on the interrupted sentence just as well (9/9 runs, no note read out). Kept short: notes are ≤500 tokens.
  const continueInNewVoice = (said) => "Your voice was just changed while you were saying: «" + said.slice(-300) +
    "». Continue exactly that answer in the new voice from where it stopped (its last few words may not have been heard) — do not start it over, do not mention the voice change.";

  // session.started: the call is up — pause, or talk.
  function applyStart() {
    const opts = eva.pending || {};
    eva.pending = null;
    if (opts.paused) { holdScreen(true); announceDevices(); enterPause(false, opts.ready); return; }
    eva.mode = "active";
    setRemoteVolume(1);
    if (IOS) stopPhraseListener(); else startPhraseListener();
    announceDevices();
    holdScreen(true);
    sendFrame(); // frames stop on a pause: the server gets a fresh one at once
    if (opts.switched) {
      // A new voice is a new session: carry on as if nothing happened — no «тепер я іншим голосом».
      // Asked by voice: nothing to say — she just listens on in the new voice.
      if (opts.interrupted) thinking(continueInNewVoice(opts.interrupted));
    } else if (opts.ask) {
      askWoken(opts.ask, opts.note);
    } else if (opts.greet) {
      // A fresh call after a wake: the user finished the phrase seconds ago — greet at once.
      greetIfQuiet(opts.heard, opts.note, opts.fresh ? 0 : 700, opts.hello ? START_GREETING : "");
    }
    showState();
    updateButtons();
    touchActivity();
  }

  // Close the call but keep Єва's mode (pause, voice switch); stop() also turns her off.
  // keepMic: a new session follows right away (voice switch, new conversation).
  function teardown({ keepMic = false } = {}) {
    attempt += 1;
    clearTimeout(dropTimer);
    clearTimeout(idleTimer);
    sendEvent({ type: "session.close" });
    if (pc) { closePeer(pc); pc = null; }
    micSender = null;
    if (gate && keepMic) gate.listenTo(null);
    else if (gate) { gate.close(); gate = null; }
    if (mic && !keepMic) { mic.getTracks().forEach((t) => t.stop()); mic = null; }
    channel = null;
    closeBubbles();
    bargeReset();
    barge.recentAssistant = "";
  }

  // Once per call, in the log: where her voice is heard from and played to.
  let devicesAnnounced = false;
  function announceDevices() {
    showDevicesNow();
    if (devicesAnnounced) return;
    devicesAnnounced = true;
    const now = devicesNow();
    addLine("system", "Мікрофон: " + now.input + ". Звук: " + now.output + ".");
    if (btMicTip(now.input)) addLine("system", btMicTip(now.input));
  }

  // A Bluetooth headset's mic on a computer: the headset drops to its call mode (narrow, telephone-like
  // sound) and GPT-Live hears words worse (feedback in headphones: «вона ужасно слухає мене»). The
  // built-in mic keeps the headphones in full quality for her voice.
  const BT_MIC = /airpods|buds|bluetooth|hands-?free|headset|гарнітур|jabra|bose|beats|wh-1000|wf-1000/i;
  let otherMics = 0; // mics besides the one in use (listDevices)
  function btMicTip(input) {
    if (IOS || ANDROID || !BT_MIC.test(input || "") || !otherMics) return "";
    return "Мікрофон Bluetooth-навушників передає звук гірше, і " + callName + " може погано вас чути. Оберіть у «Пристроях» " +
      "вбудований мікрофон — її голос лишиться в навушниках.";
  }

  function stop(text = "Не підключено", state = "") {
    devicesAnnounced = false;
    micMuted = false; // the button hides with the call: the next one starts with the mic on
    if (eva.mode !== "off") postDiag(true);
    holdScreen(false);
    dropStandby();
    teardown();
    stopPhraseListener();
    if (cam) { $("camera").checked = false; stopCamera(); } // the call is over: so is the camera
    clearTimeout(eva.pauseTimer);
    eva.mode = "off";
    eva.ready = false;
    setTimeout(showDevicesNow, 0); // the mic is closed now: «Буде: …»
    eva.pending = null;
    setStatus(text, state);
    updateButtons();
    refreshMe(); // picks up a voice the agent changed by voice during the call
  }

  // ── pause («Дякую, Єва») / wake («Єва, скажи») ──
  // ready: the call was just started — she is on and waits to be greeted, not paused.
  function enterPause(announce, ready = false) {
    eva.mode = "waiting";
    if (ready && !eva.ready) addLine("system", callName + " на зв'язку. Скажіть «Привіт, " + callName + "» — і говоріть.");
    eva.ready = ready; // kept across a reconnect during the wait (voice switch): no second line
    eva.recentUser = "";
    eva.thinking = false;
    clearTimeout(idleTimer);
    sendEvent({ type: "session.input_audio.mute", event_id: eventId("mute") }); // the model hears nothing
    bargeReset(); // and nothing it says is played while waiting (see setRemoteVolume)
    closeBubbles();
    if (announce) {
      addLine("system", eva.wakeSupported
        ? (bgListen
          ? "Пауза. " + callName + " слухає фоном і запам'ятовує, про що говорять поруч, — зможе нагадати. Скажіть «" + callName + ", скажи», щоб продовжити."
          : "Пауза. Скажіть «" + callName + ", скажи», щоб продовжити — " + callName + " все пам'ятає.")
        : "Пауза. Натисніть «Продовжити» — " + callName + " все пам'ятає.");
    }
    clearTimeout(eva.pauseTimer);
    schedulePauseClose(Date.now());
    startPhraseListener();
    showState();
    updateButtons();
  }

  // The paused call is closed after PAUSE_CLOSE_MS — unless a tool is still running or Єва waits
  // for «так/ні» (that confirmation belongs to this session): then ask again a bit later.
  function schedulePauseClose(pausedAt) {
    clearTimeout(eva.pauseTimer);
    eva.pauseTimer = setTimeout(async () => {
      if (eva.mode !== "waiting" || !pc) return;
      if (Date.now() - pausedAt < PAUSE_HOLD_MAX_MS) {
        const me = await fetchMe(); // offline: close as usual
        if (eva.mode !== "waiting" || !pc) return;
        if (me && me.keep_session) { schedulePauseClose(pausedAt); return; }
      }
      teardown({ keepMic: true }); // the wake phrase listener needs it anyway; the next wake skips getUserMedia
    }, PAUSE_CLOSE_MS);
  }

  function pauseEva() {
    if (eva.mode !== "active") return; // both recognisers may report the same «Дякую, Єва»
    steer("pause");
    enterPause(true);
  }

  function sendOverheard(text) {
    return fetch(BACKEND + "/api/overheard", { method: "POST", headers: headers(), body: JSON.stringify({ text }) })
      .catch(() => {});
  }

  // Held a moment before it is kept: the recogniser splits speech into several results, so the user's
  // own request can come apart from the «Єва, скажи» that names her — prod: «…розкажи мені про
  // Монте-Крісто» was kept as background and she said «хтось поруч теж згадав Монте-Крісто».
  const OVERHEARD_HOLD_MS = 4000;
  const overheardPending = [];
  function postOverheard(text) {
    const entry = { text };
    entry.timer = setTimeout(() => {
      overheardPending.splice(overheardPending.indexOf(entry), 1);
      sendOverheard(text);
    }, OVERHEARD_HOLD_MS);
    overheardPending.push(entry);
  }

  // At a wake: a held piece that shares the request's words is the user's own lead-in — dropped; the
  // rest is kept now, before the digest is read.
  const contentWords = (text) => evaTokens(text).filter((w) => w.length >= 4 && !wakeNames.has(w));
  function flushOverheard(request) {
    const asked = new Set(contentWords(request));
    const sent = overheardPending.splice(0).map((entry) => {
      clearTimeout(entry.timer);
      const words = contentWords(entry.text);
      const shared = words.filter((w) => asked.has(w)).length;
      if (shared >= 2 || (shared && shared === words.length)) return null;
      return sendOverheard(entry.text);
    });
    return Promise.all(sent);
  }

  // What she heard in the background during the pause, as a commentary ("" if nothing).
  async function overheardNote(flushed) {
    if (!bgListen) return "";
    try {
      await flushed;
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 4000);
      const res = await fetch(BACKEND + "/api/overheard/digest", { method: "POST", headers: headers(), signal: ctrl.signal });
      clearTimeout(timer);
      return res.ok ? (await res.json()).commentary || "" : "";
    } catch { return ""; }
  }

  function showBgListen() {
    $("bgListen").checked = bgListen;
    $("bgListenInfo").textContent = bgListen
      ? "Увімкнено: " + callName + " запам'ятовує, про що говорять поруч, і може нагадати."
      : "Вимкнено: на паузі " + callName + " нічого не запам'ятовує.";
  }

  function toggleBgListen() {
    bgListen = $("bgListen").checked;
    store.set("va-bg-listen", bgListen ? "1" : "0");
    showBgListen();
    showState();
    // Turned off: forget what was already heard, it must not reach her.
    if (!bgListen) fetch(BACKEND + "/api/overheard", { method: "DELETE", headers: headers() }).catch(() => {});
    addLine("system", bgListen
      ? "На паузі " + callName + " слухатиме фоном і запам'ятовуватиме, про що говорять поруч."
      : "Фонове слухання вимкнено: на паузі " + callName + " нічого не запам'ятовує.");
  }

  // The model was muted and never heard the phrase: hand it what followed «Єва, скажи», together
  // with what she overheard during the pause (fetched while the call connects, not before it).
  const withNote = (note, text) => (note ? note + "\n\n" + text : text);
  const thinking = (content) =>
    sendEvent({ type: "session.thinking.append", content, delegation_id: null, event_id: eventId("think") });

  // What followed «Єва, скажи» was never heard by the model (it was muted), and GPT-Live never delegates
  // a request handed over as a commentary (it made up a room with a laptop and a cup for «що ти бачиш»).
  // So it goes to the backend as the user's message, with a silent note — woken_request_note in
  // prompts/live_prompt.py says why; keep the wording the same (tests/test_wake_request.py checks).
  // «Зміни голос …» is the page's own.
  function askBackend(text) {
    thinking("The user woke you with: «" + text + "». It has gone to the backend as their message, and the " +
      "backend handles it the usual way, with its tools (camera, web) — nothing has been looked at or searched " +
      "yet. You, the voice: do not delegate it again; when the backend's answer comes, tell it in your own " +
      "words. Never voice their words or invent what they said.");
    sendEvent({ type: "response.item.create", event_id: eventId("ask"),
      item: { type: "message", role: "user", content: [{ type: "input_text", text }] } });
    sendEvent({ type: "response.create", event_id: eventId("go") });
    eva.thinking = true;
    clearTimeout(eva.thinkTimer);
    eva.thinkTimer = setTimeout(() => { eva.thinking = false; showState(); }, 15000);
  }

  // note: what she overheard during the pause (a promise) — silent context, never waited for.
  function askWoken(text, note) {
    askBackend(text);
    Promise.resolve(note).then((n) => { if (n) thinking(n); });
  }

  function wakeEva(rest, heard = "") {
    if (eva.mode !== "waiting") return;
    if (songs.playing || songs.pending) stopSong(); // she speaks again: the song ends
    clearTimeout(eva.pauseTimer);
    const note = overheardNote(flushOverheard((heard || "") + " " + (rest || ""))); // a promise: never delays the wake
    if (rest) addLine("user", callName + ", скажи, " + rest);
    if (pc && channel && channel.readyState === "open") {
      eva.mode = "active";
      if (IOS) { stopPhraseListener(); reviveMic(); } // the recogniser had the mic during the pause
      sendEvent({ type: "session.input_audio.unmute", event_id: eventId("unmute") });
      setRemoteVolume(1);
      sendFrame();
      if (rest) askWoken(rest, note);
      else greetIfQuiet(heard, note);
      showState();
      updateButtons();
      touchActivity();
    } else {
      // The pause closed the call: reopen it right away. What they asked goes into the new call's
      // history as their own message — quoted in a commentary, GPT-Live sometimes read it back.
      start({ userText: rest ? callName + ", скажи, " + rest : "", ask: rest, greet: !rest, heard, note, fresh: true });
    }
  }

  // Same wording as WAKE_GREETING in voice/live_driver.py.
  const wakeGreeting = (phrase) =>
    "The user just called you: «" + phrase + "». You are back and listening — let them hear it. " +
    "Reply right away in two to four words that match it: to «привіт», «вітаю», «гей» greet back " +
    "warmly («Привіт! Що робимо?», «О, привіт! Слухаю»), otherwise a short «Так, слухаю» or " +
    "«Слухаю тебе». Vary it, then wait.";

  // A greeting tells the user Єва is on. Skipped only when they already went on talking (the model
  // answers that) or she is already speaking; a noise at the wrong moment just delays it a little.
  // note: what she overheard during the pause — delivered with the greeting, or alone if skipped.
  // note: a string or a promise of one (overheardNote) — never waited for before the greeting.
  const START_GREETING = "The user just pressed «Почати розмову»: the call is open. Greet them in a few " +
    "words so they hear you are on and listening — a short hello and that they can just talk; vary it, " +
    "no question about how you can help. Then wait.";

  // «Консьєрж»: every start and «Наступний гість» — a polite hello and their name.
  const CONCIERGE_GREETING = "A person has just come up to you (concierge). Greet them politely in one short " +
    "sentence and ask their name — «Добрий день! Ласкаво просимо. Як вас звати?»; vary it. Then wait.";

  // text: what to say instead of the wake greeting (START_GREETING when the call was just started).
  function greetIfQuiet(heard = "", note = "", delayMs = 700, text = "") {
    const wokeAt = Date.now();
    const phrase = heard || callName + ", скажи";
    const giveUpAt = wokeAt + 4000;
    const tryGreet = async () => {
      if (eva.mode !== "active") return;
      const n = await Promise.resolve(note);
      if (eva.mode !== "active") return;
      if (eva.lastUserAt > wokeAt || barge.audible) { if (n) commentary(n); return; }
      if (barge.speaking && Date.now() < giveUpAt) { setTimeout(tryGreet, 300); return; }
      commentary(withNote(n, mode === "concierge" ? CONCIERGE_GREETING : text || wakeGreeting(phrase)));
    };
    setTimeout(tryGreet, delayMs);
  }

  // The browser's own recogniser (free; Chrome, Edge, Safari) runs the whole time Єва is on: it
  // hears «Єва, скажи» while the model is muted, and «Дякую, Єва» during the conversation too —
  // a second pair of ears, as GPT-Live's transcript sometimes drops the name («Дякую»). It stops
  // by itself after a while of silence, so it is restarted.
  function startPhraseListener() {
    if (micMuted) return; // nothing is heard, the wake phrase neither
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR || !eva.wakeSupported || eva.recognizer) return;
    // iOS: the recogniser takes the mic from the call (and restarts every few seconds), so it runs only
    // during a pause — for the wake phrase. In a call GPT-Live's own transcript catches «Дякую, Єва» and
    // voice requests, and the iPhone's echo cancelling lets her hear «стоп» over herself.
    if (IOS && eva.mode === "active") return;
    let rec;
    try { rec = new SR(); } catch { eva.wakeSupported = false; showState(); updateButtons(); return; }
    rec.lang = "uk-UA";
    rec.continuous = true;
    rec.interimResults = true; // «Дякую, Єва» cuts her off as soon as it is heard
    rec.maxAlternatives = 3;
    let retryMs = 300;
    let stopResult = -1; // the result a stop word already cut her off for (interim results repeat)
    rec.onresult = (e) => {
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const result = e.results[i];
        for (let k = 0; k < result.length; k++) {
          const text = result[k].transcript;
          if (eva.mode === "active" && isStop(text)) { pauseEva(); return; }
          // «Стоп» / «зачекай» over her: on speakers the gate keeps a normal voice below her echo, so
          // neither the model nor the page heard it and she talked on through long answers. This
          // recogniser hears it anyway; GPT-Live stops on the steer (measured: ~0.8 s, also mid-way
          // through a delegated answer). Her own words are echo, not a stop.
          if (eva.mode === "active" && k === 0 && i !== stopResult && (barge.audible || barge.stage === "ducked")
              && stopWordOverHer(text)) {
            stopResult = i;
            barge.speechEndedAt = Date.now(); // keep her muted until she has actually stopped
            bargeConfirm("ack");
            return;
          }
          // ?debug=1: what the browser heard during the pause — shows why a wake phrase was missed.
          if (DEBUG && eva.mode === "waiting" && result.isFinal) addLine("system", "браузер почув: «" + text.trim() + "»");
          // Wake on the final text only: it carries the whole request after «Єва, скажи».
          if (eva.mode === "waiting" && result.isFinal) {
            const rest = matchWake(text);
            if (rest !== null) { wakeEva(rest, text.trim()); return; }
          }
        }
        // Paused, not switched off: what is said nearby is kept for her (voice/background.py).
        // Not a phrase that names her: a wake phrase the recogniser mangled is the user, not background.
        if (bgListen && eva.mode === "waiting" && result.isFinal && !isStop(result[0].transcript)
            && !evaTokens(result[0].transcript).some((w) => wakeNames.has(w))) {
          postOverheard(result[0].transcript);
        }
      }
    };
    rec.onerror = (e) => {
      if (["not-allowed", "service-not-allowed", "language-not-supported"].includes(e.error)) {
        eva.wakeSupported = false;
        addLine("system", "Браузер не дає слухати «" + callName + ", скажи» — натискайте «Продовжити».");
        showState();
        updateButtons();
      } else if (e.error === "network") {
        retryMs = 3000;
      }
    };
    rec.onend = () => {
      if (eva.recognizer !== rec) return;
      eva.recognizer = null;
      if (eva.mode !== "off" && eva.wakeSupported) setTimeout(startPhraseListener, retryMs);
    };
    eva.recognizer = rec;
    // A mic picked in «Пристрої»: browsers that take a track (newer Chrome) listen to it, older ones
    // ignore the argument and keep the system default.
    const track = micId && mic ? mic.getAudioTracks().find((t) => t.readyState === "live") : null;
    try { rec.start(...(track ? [track] : [])); } catch {
      try { if (track) rec.start(); else eva.recognizer = null; } catch { eva.recognizer = null; }
    }
  }

  // The mic tracks (and the gate's, which the call sends) on or off; the wake phrase listener with them.
  function setMicMuted(muted) {
    micMuted = muted;
    const tracks = [...(mic ? mic.getAudioTracks() : []), ...(gate && gate.track ? [gate.track] : [])];
    tracks.forEach((t) => { t.enabled = !muted; });
    if (muted) stopPhraseListener();
    else if (eva.mode !== "off") startPhraseListener();
    addLine("system", muted ? "Мікрофон вимкнено — " + callName + " нічого не чує, навіть «" + callName + ", скажи»."
      : "Мікрофон знову увімкнено.");
    showState();
    updateButtons();
  }

  function stopPhraseListener() {
    const rec = eva.recognizer;
    eva.recognizer = null;
    if (rec) { rec.onend = null; try { rec.abort(); } catch { /* already stopped */ } }
  }

  // GPT-Live voices are fixed per session: a new voice (or a fresh start) is a new session that
  // the server seeds with the conversation so far, so Єва remembers everything.
  async function restartSession(mode, opts) {
    const paused = eva.mode === "waiting";
    if (eva.mode === "off" || eva.mode === "connecting") return;
    if (paused && !pc) return; // closed during a pause: the next «Єва, скажи» opens it fresh
    dropStandby();
    clearTimeout(eva.pauseTimer);
    teardown({ keepMic: true });
    eva.mode = mode;
    await start(paused ? { paused: true, ready: eva.ready } : opts);
  }

  // The server closed the call: to restart it in a new voice — otherwise it really ended.
  async function onSessionClosed(text = "Розмову завершено", state = "") {
    if (eva.mode === "switching" || eva.mode === "off" || eva.mode === "connecting") return;
    const wasPaused = eva.mode === "waiting";
    dropStandby();
    teardown({ keepMic: true });
    eva.mode = "connecting";
    showState();
    const me = await fetchMe();
    if (eva.mode !== "connecting") return; // the user did something meanwhile
    // The server's cap (10 min a session) closed it: a talk still going on carries on in a new session with
    // the whole conversation, silently; a call nobody talks in ends here.
    if (me && me.expired && !wasPaused && Date.now() - eva.lastActiveAt < IDLE_LIMIT_MS) {
      addLine("system", "Нова сесія (обмеження — 10 хв на сесію). " + callName + " все пам'ятає, говоріть далі.");
      eva.mode = "switching";
      await start({ switched: true });
      return;
    }
    if (me && me.reconnect && voices.some((x) => x.id === me.voice)) {
      voice = me.voice;
      store.set("va-voice", voice);
      showVoice();
      addLine("system", "Голос змінено на «" + voiceLabel(voice) + "»" + (me.voice_note ? " (" + me.voice_note + ")" : "") + ".");
      eva.mode = "switching";
      await start(wasPaused ? { paused: true, ready: eva.ready } : { switched: true });
      return;
    }
    if (gate) { gate.close(); gate = null; }
    if (mic) { mic.getTracks().forEach((t) => t.stop()); mic = null; }
    if (wasPaused) { eva.mode = "waiting"; startPhraseListener(); showState(); updateButtons(); return; }
    stop(text, state);
  }

  // ── voice settings ──
  const voiceLabel = (id) => { const v = voices.find((x) => x.id === id) || { label: id }; return groupMembers(v).length > 1 && v.variant_label ? v.variant_label : v.label; };

  // How the applied character is called: its name wakes and pauses it («Степан, скажи», «Дякую, Степан»).
  function showWakeHint() {
    const n = callName;
    $("wakeHint").textContent = eva.wakeSupported
      ? "Покличте — «" + n + ", скажи», «Привіт, " + n + "», «Гей, " + n + "», «" + n + ", слухай». «Дякую, " + n + "» — пауза. Браузер попросить доступ до мікрофона."
      : "Цей браузер не вміє слухати «" + n + ", скажи» (потрібен Chrome, Edge або Safari) — користуйтеся кнопками «Пауза» / «Продовжити». «Дякую, " + n + "» працює.";
  }

  // The picker may show a voice that isn't applied yet (to listen to it first).
  function showVoice(keepSelection = false) {
    const applied = voices.find((x) => x.id === voice);
    setWakeNames(applied && applied.wake);
    callName = applied ? applied.label : "Єва";
    store.set("va-call-name", callName);
    showWakeHint();
    if (voices.length) { // loaded: the hints that name the character follow it
      showBgListen(); showVoiceNow(); showCamera(); showCheckQuestion(); showState();
    }
    if (!keepSelection) $("voice").value = voice;
    const picked = voices.find((x) => x.id === $("voice").value);
    $("voiceInfo").textContent = picked ? picked.description : "";
    $("voiceApply").hidden = !picked || picked.id === voice;
    for (const b of $("characters").children) b.setAttribute("aria-selected", String(picked ? b.dataset.group === groupOf(picked) : false));
    showVoiceVariant(picked);
  }

  // «Голос»: one tab may hold several characters (Степан: безпечний / звичайний / небезпечний), each with a new (own) and an old (preset)
  // voice when the server has an own one for it (own_voice). Options are "id|own" or "id|preset".
  // The old preset voice is not offered any more: every character with an own voice speaks it.
  const variantOf = () => "own";
  const groupOf = (x) => x.group || x.id;
  const groupMembers = (x) => voices.filter((m) => groupOf(m) === groupOf(x));
  function voiceChoices(picked) {
    const members = groupMembers(picked);
    const out = [];
    for (const m of members) {
      const name = members.length > 1 ? (m.variant_label || m.label) : "";
      if (m.own_voice) {
        out.push([m.id + "|own", (name || "Створений голос") + " (новий голос)"]);
      } else if (members.length > 1) {
        out.push([m.id + "|preset", name]);
      }
    }
    return out;
  }
  function showVoiceVariant(picked) {
    const choices = picked ? voiceChoices(picked) : [];
    $("voiceVariantRow").hidden = choices.length < 2;
    if (choices.length >= 2) {
      $("voiceVariant").replaceChildren(...choices.map(([v, t]) => new Option(t, v)));
      $("voiceVariant").value = picked.id + "|" + (picked.own_voice ? variantOf(picked.id) : "preset");
    }
    const has = Boolean(picked && picked.own_voice);
    const own = !has || variantOf(picked.id) === "own";
    // A prompt-generated voice is not in the speech endpoint, so it has no sample: only the old one does.
    $("voicePreview").disabled = !voices.length || (has && own);
    $("voiceVariantInfo").textContent = has && own
      ? "Створений голос звучить лише в розмові — зразка для нього немає."
      : "";
  }

  // The variant to send for a character: only one with an own voice has the choice.
  const variantFor = (id) => ((voices.find((x) => x.id === id) || {}).own_voice ? variantOf(id) : undefined);

  // The voice of the current character changed (a new character, or its new / old voice): the call goes on in it.
  function reconnectForVoice(text) {
    if (eva.mode === "active") {
      const later = !voiceNow && answering();
      addLine("system", later ? text.later : text.now);
      switchVoiceFast(later);
    } else if (eva.mode === "waiting") {
      addLine("system", text.now);
      restartSession("switching", { switched: true }); // paused: she is silent, nothing to continue
    }
  }

  async function applyVoiceVariant() {
    const [id, kind] = $("voiceVariant").value.split("|");
    const target = voices.find((x) => x.id === id);
    if (!target) return;
    const variant = kind === "preset" ? "preset" : "own";
    if (id !== voice) { // another character of the same tab (Степан: безпечний / звичайний / небезпечний)
      if (target.own_voice) store.set("va-voice-variant-" + id, variant);
      $("voice").value = id;
      stopPreview();
      applyVoice();
      return;
    }
    if (!target.own_voice || variant === variantOf(voice)) return;
    store.set("va-voice-variant-" + voice, variant);
    showVoiceVariant(target);
    stopPreview();
    fetch(BACKEND + "/api/voice", { method: "POST", headers: headers(), body: JSON.stringify({ voice, voice_variant: variant }) }).catch(() => {});
    const name = variant === "own" ? "новий голос" : "старий голос";
    reconnectForVoice({
      now: callName + ": " + name + ".",
      later: callName + ": " + name + " — щойно договорить поточну відповідь.",
    });
  }

  const CHARACTERS = ["meridian", "blunt", "blunt_rough", "blunt_unsafe", "lesia", "kit"]; // Тарас, Степан (безпечний, звичайний, небезпечний), Леся, Кішка

  async function loadVoices() {
    try {
      const res = await fetch(BACKEND + "/api/voices");
      if (!res.ok) return;
      const data = await res.json();
      // Only these characters, in this order (the rest stay on the server, off the page for now).
      voices = (data.voices || []).filter((x) => CHARACTERS.includes(x.id))
        .sort((a, b) => CHARACTERS.indexOf(a.id) - CHARACTERS.indexOf(b.id));
      voice = store.get("va-voice") || "";
      if (!voices.some((x) => x.id === voice)) voice = voices.some((x) => x.id === data.default) ? data.default : "";
      if (!voices.some((x) => x.id === voice) && voices.length) voice = voices[0].id;
      $("voice").replaceChildren(...voices.map((x) => new Option(x.label, x.id)));
      // One tab per group: both Степани sit under «Степан», the «Голос» list picks between them.
      $("characters").replaceChildren(...voices.filter((x, i) => voices.findIndex((m) => groupOf(m) === groupOf(x)) === i).map((x) => {
        const b = document.createElement("button");
        b.type = "button";
        b.setAttribute("role", "tab");
        b.dataset.id = x.id;
        b.textContent = x.label;
        b.title = x.description;
        b.dataset.group = groupOf(x);
        b.addEventListener("click", () => {
          const now = voices.find((m) => m.id === voice);
          if (now && groupOf(now) === groupOf(x)) return; // already on this tab
          $("voice").value = x.id; stopPreview(); applyVoice();
        });
        return b;
      }));
      $("voice").disabled = !voices.length;
      $("voicePreview").disabled = !voices.length;
      showVoice();
    } catch { /* backend offline: picker stays disabled */ }
  }

  // What Єва is saying right now (the transcript runs slightly ahead of the audio).
  const speakingNow = () => (barge.audible && openMsg.assistant ? openMsg.assistant.textContent.trim() : "");

  async function applyVoice() {
    const id = $("voice").value;
    if (!voices.some((x) => x.id === id) || id === voice) return;
    // Each character has its own memory: another tab shows its own conversation (the three Степани are one).
    const before = voices.find((x) => x.id === voice), after = voices.find((x) => x.id === id);
    const otherCharacter = !before || groupOf(before) !== groupOf(after);
    voice = id;
    store.set("va-voice", id);
    showVoice();
    if (otherCharacter) clearLog();
    // Not awaited: the new call carries the voice itself (/api/session), this only keeps the server
    // in step (it remembers the voice and swaps to this character's conversation).
    fetch(BACKEND + "/api/voice", { method: "POST", headers: headers(), body: JSON.stringify({ voice: id, voice_variant: variantFor(id) }) })
      .then(() => { if (otherCharacter) loadConversation(); }).catch(() => {});
    reconnectForVoice({
      now: "Голос змінено на «" + voiceLabel(id) + "».",
      later: "Персонаж зміниться на «" + voiceLabel(id) + "», щойно договорить поточну відповідь.",
    });
  }

  // ── fast voice switch ──
  // Opening a GPT-Live session takes seconds and its voice is fixed, so the call in the new voice
  // opens beside the current one — it hears nothing and plays nothing — while Єва goes on talking.
  // Once it is up it takes over the mic and the speaker («Одразу»), or waits until she has finished
  // («Після відповіді»). What the old call said meanwhile is handed over, so nothing is lost.
  let standby = null;
  let voiceTimer = null;
  const STANDBY_WAIT_MS = 60 * 1000; // a ready call waiting for her to finish — longer: start over
  // A swap doesn't derail her (unlike speed/style), so a short silence is enough: her answer is over.
  const SWAP_QUIET_MS = 800;
  const stillAnswering = () => barge.audible || eva.thinking || Date.now() - eva.lastAudibleAt < SWAP_QUIET_MS;

  function dropStandby() {
    clearTimeout(voiceTimer);
    voiceTimer = null;
    const s = standby;
    standby = null;
    if (!s) return;
    clearTimeout(s.timer);
    try { if (s.channel.readyState === "open") s.channel.send(JSON.stringify({ type: "session.close" })); } catch { /* closing */ }
    if (s.peer) closePeer(s.peer);
  }

  // The log from `mark` on: what the old call said / heard after the new one got the history.
  function logMark() {
    const last = $("log").lastElementChild;
    return { el: last, len: last ? last.textContent.length : 0 };
  }
  function logSince(mark, skip) {
    const lines = [];
    // The bubble open at the mark goes in whole: the history had only its start, and the model
    // took that for an unfinished answer and told it again.
    const whole = mark.el && mark.len < mark.el.textContent.length;
    let el = mark.el ? (whole ? mark.el : mark.el.nextElementSibling) : $("log").firstElementChild;
    for (; el; el = el.nextElementSibling) {
      if (el === skip || el.classList.contains("system") || el.classList.contains("empty")) continue;
      const text = el.textContent.trim();
      if (text) lines.push((el.classList.contains("user") ? "User: " : "You: ") + text);
    }
    return lines.join("\n");
  }

  // announce: say «Голос змінено» when it actually switches (the user was told «зміниться»).
  // command: asked by voice — swap as soon as the new call is up, with nothing to continue.
  async function switchVoiceFast(announce = false, command = false) {
    dropStandby();
    if (!pc || !mic) { restartSession("switching", { switched: true }); return; }
    const s = { voice, mark: logMark(), ready: false, announce, command };
    standby = s;
    const fail = () => {
      if (standby !== s) return;
      dropStandby();
      switchVoiceWhenQuiet(s.announce); // the old way: reconnect (once she is quiet, if asked to wait)
    };
    s.timer = setTimeout(fail, 15000);
    try {
      const peer = new RTCPeerConnection();
      s.peer = peer;
      peer.ontrack = (e) => { s.stream = e.streams[0]; };
      // No mic until the swap: replaceTrack later needs no renegotiation.
      s.sender = peer.addTransceiver("audio", { direction: "sendrecv" }).sender;
      s.channel = peer.createDataChannel("oai-events");
      s.channel.onmessage = (e) => {
        if (standby !== s) return;
        let event;
        try { event = JSON.parse(e.data); } catch { return; }
        if (event.type === "session.started" || event.type === "session.created") { s.ready = true; s.readyAt = Date.now(); clearTimeout(s.timer); swapWhenQuiet(s); }
        else if (event.type === "session.closed") fail();
      };
      s.channel.onclose = fail;
      const offer = await peer.createOffer();
      await peer.setLocalDescription(offer);
      const res = await fetch(BACKEND + "/api/session", {
        method: "POST",
        headers: headers(),
        // keep_old: the current call goes on until the page swaps (the server would end it).
        body: JSON.stringify(sessionBody({ sdp: withClearOpus(offer.sdp), voice: s.voice, keep_old: true })),
      });
      if (standby !== s) return;
      if (!res.ok) throw new Error("session " + res.status);
      const { sdp } = await res.json();
      if (standby !== s) return;
      meterOpen(peer);
      await peer.setRemoteDescription({ type: "answer", sdp });
    } catch {
      fail();
    }
  }

  function swapWhenQuiet(s) {
    clearTimeout(voiceTimer);
    voiceTimer = null;
    if (standby !== s) return;
    if (eva.mode !== "active" || !pc) { dropStandby(); if (eva.mode === "waiting") restartSession("switching", {}); return; }
    // Never mid-sentence of the user (half of it would go to each call — prod: the new call heard
    // only «…чи сьогодні день»). The gate notices speech a moment late and the transcript lags
    // more, so a user who spoke in the last second still counts. «Після відповіді» also waits for her.
    const userTalking = barge.speaking || Date.now() - barge.speechEndedAt < 1000 || Date.now() - eva.lastUserAt < 1000;
    if (userTalking || (!voiceNow && !s.command && stillAnswering())) {
      if (Date.now() - s.readyAt > STANDBY_WAIT_MS) { dropStandby(); switchVoiceWhenQuiet(s.announce); return; }
      voiceTimer = setTimeout(() => swapWhenQuiet(s), 100);
      return;
    }
    swapIn(s);
  }

  async function swapIn(s) {
    standby = null;
    const open = openMsg.assistant;
    // Mid-answer also counts a short gap between her sentences (the bubble is still open).
    // «Після відповіді» swaps only once she has finished: nothing to continue then.
    const midAnswer = voiceNow && !s.command && (barge.audible || Date.now() - eva.lastAudibleAt < 1500);
    const interrupted = open && midAnswer ? open.textContent.trim() : "";
    const said = logSince(s.mark, interrupted ? open : null);
    teardown({ keepMic: true }); // the old call: closed, its bubbles done
    pc = s.peer;
    channel = s.channel;
    sessionVoice = s.voice;
    wireCall(s.peer, s.channel);
    attachVoice(s.stream);
    s.peer.ontrack = (e) => attachVoice(e.streams[0]);
    try {
      const track = gate ? gate.track : mic.getAudioTracks()[0];
      track.enabled = !micMuted;
      if (micMuted) mic.getAudioTracks().forEach((t) => { t.enabled = false; }); // muted: the gate hears nothing either
      micSender = s.sender;
      await s.sender.replaceTrack(track);
    } catch { /* closed meanwhile: its close handler takes over */ }
    if (pc !== s.peer) return;
    setRemoteVolume(1);
    if (s.announce) addLine("system", "Голос змінено на «" + voiceLabel(s.voice) + "».");
    // The history ended when this call opened; what came after is handed over here. The model
    // answers a context message out loud, so it is told plainly when to stay silent.
    const context = said ? "After your history ended, the conversation went on (the user heard all of it; " +
      "these replies of yours are finished — never repeat or retell them):\n" + said.slice(-800) + "\n\n" : "";
    if (interrupted) {
      thinking(context + continueInNewVoice(interrupted));
    } else if (context) {
      sendEvent({ type: "session.instructions.append", delegation_id: null, event_id: eventId("ctx"),
        content: context + "Say nothing now — no acknowledgement. Wait silently for the user's next words." });
    }
    showState();
    updateButtons();
    touchActivity();
  }

  // Without a standby call: reconnect in the new voice, once she is quiet if asked to wait.
  function switchVoiceWhenQuiet(announce = false) {
    clearTimeout(voiceTimer);
    voiceTimer = null;
    if (sessionVoice === voice || eva.mode === "off" || eva.mode === "connecting" || eva.mode === "switching") return; // a new call already has it
    if (eva.mode === "active" && !voiceNow && answering()) { voiceTimer = setTimeout(() => switchVoiceWhenQuiet(announce), 300); return; }
    if (announce) addLine("system", "Голос змінено на «" + voiceLabel(voice) + "».");
    restartSession("switching", { switched: true, interrupted: eva.mode === "active" ? speakingNow() : "" });
  }

  function showVoiceNow() {
    $("voiceNow").checked = voiceNow;
    $("voiceNowInfo").textContent = voiceNow
      ? "Одразу: відповідь договорить уже новий персонаж."
      : "Після відповіді: новий персонаж — коли договорить поточна відповідь.";
  }

  function toggleVoiceNow() {
    voiceNow = $("voiceNow").checked;
    store.set("va-voice-now", voiceNow ? "1" : "0");
    showVoiceNow();
    // A waiting switch happens now.
    if (voiceNow && standby && standby.ready) swapWhenQuiet(standby);
    else if (voiceNow && voiceTimer && !standby) switchVoiceWhenQuiet(true);
  }

  // Speed and manner are instructions to the model. One that arrives while she talks derails the
  // answer (instructions.append is also how a barge-in stops her), so it waits until she has been
  // quiet for a moment — the gate hears her real audio. A newer setting replaces a waiting one.
  let pendingDelivery = "";
  let deliveryTimer = null;
  const DELIVERY_QUIET_MS = 2500;
  // She is talking, thinking, or stopped only a moment ago (between sentences).
  const answering = () => barge.audible || barge.speaking || eva.thinking || Date.now() - eva.lastAudibleAt < DELIVERY_QUIET_MS;

  async function applyDelivery() {
    speed = $("speed").value;
    style = $("style").value;
    store.set("va-speed", speed);
    store.set("va-style", style);
    try {
      const res = await fetch(BACKEND + "/api/voice", { method: "POST", headers: headers(), body: JSON.stringify({ speed, style }) });
      const data = res.ok ? await res.json() : null;
      if (data && data.instruction) { pendingDelivery = data.instruction; sendDeliveryWhenQuiet(); }
    } catch { /* offline: the next session starts with these settings anyway */ }
  }

  // ?debug=1: evaDebug() in the console shows the page's state.
  // The mic gate on this device — echo of her voice in the mic (speakers or headphones), how loud the
  // user speaks, and how much of what they said over her was cut. Counts since the page opened.
  function gateSummary() {
    const g = gateStats;
    if (!g) return null;
    const pct = (a, b) => (b ? Math.round((100 * a) / b) : 0);
    return {
      mic: g.echo < 0.05 ? "headphones" : "speakers",
      echo: +g.echo.toFixed(3), voice: +g.voice.toFixed(3), floor: +g.floor.toFixed(4),
      herTalkS: +(g.her * 0.02).toFixed(1), openWhileHerPct: pct(g.herOpen, g.her),
      cutWhileHerS: +(g.cut * 0.02).toFixed(1), quietS: +(g.quiet * 0.02).toFixed(1),
      openWhileQuietPct: pct(g.quietOpen, g.quiet),
    };
  }

  // Numbers only, posted to /api/diag (server/app.py) every 20 s of a call and when it ends: how the call
  // sounds on this device, which tests with clean audio can't show — feedback «довго перебивається»,
  // «замовкає»: the user talked over her (how long until she was cut off, or never), or finished a turn
  // and she said nothing for 8 s (and what state the page was in then). No words are sent.
  const diag = { bargeAt: 0, bargeStarts: 0, bargeMuteMs: [], bargeMissed: 0, silences: [], silenceFor: 0, changed: false,
    ctxInterrupts: 0, micRevives: 0, micMutes: 0 };
  function diagBargeStart() { diag.bargeAt = Date.now(); diag.bargeStarts += 1; diag.changed = true; }
  function diagBargeMuted() {
    if (!diag.bargeAt) return;
    diag.bargeMuteMs = diag.bargeMuteMs.concat(Date.now() - diag.bargeAt).slice(-30);
    diag.bargeAt = 0;
  }
  function diagBargeEnded() {
    // A second or more of talk over her that never cut her off (an «угу» is shorter).
    if (diag.bargeAt && barge.stage !== "muted" && Date.now() - diag.bargeAt > 1000) diag.bargeMissed += 1;
    diag.bargeAt = 0;
  }
  function diagCheckSilence() {
    const now = Date.now();
    if (eva.mode !== "active" || !eva.lastUserAt || eva.lastUserAt <= (eva.lastOutputAt || 0)) return;
    if (now - eva.lastUserAt < 8000 || diag.silenceFor === eva.lastUserAt) return;
    diag.silenceFor = eva.lastUserAt;
    diag.silences = diag.silences.concat({
      stage: barge.stage, audible: barge.audible, speaking: barge.speaking, thinking: eva.thinking,
      level: remoteLevel, channel: channel ? channel.readyState : "none",
    }).slice(-20);
    diag.changed = true;
  }
  function postDiag(final = false) {
    if (!BACKEND || (!diag.changed && !final)) return;
    diag.changed = false;
    const body = {
      final, prompt: promptVariant, sens: params.get("sens") === "1", ios: IOS,
      browser: BROWSER, browserV: BROWSER_VERSION, osV: OS_VERSION, micApi: HAS_MIC_API, legacyMic: Boolean(LEGACY_GUM),
      secure: window.isSecureContext,
      audio: { ctxInterrupts: diag.ctxInterrupts, micRevives: diag.micRevives, micMutes: diag.micMutes, wakeLock: Boolean(wakeLock) },
      gate: gateSummary(),
      barge: { starts: diag.bargeStarts, muteMs: diag.bargeMuteMs, missed: diag.bargeMissed, ducks: barge.ducks, mutes: barge.mutes },
      silences: diag.silences,
    };
    fetch(BACKEND + "/api/diag", { method: "POST", headers: headers(), body: JSON.stringify(body), keepalive: true }).catch(() => {});
  }
  setInterval(diagCheckSilence, 1000);
  setInterval(() => { if (eva.mode === "active") { diag.changed = true; postDiag(); } }, 20000);

  if (DEBUG) {
    // evaGate(): see gateSummary — talk over her a few times first.
    window.evaGate = gateSummary;
    window.evaDebug = () => ({
      mode: eva.mode, pendingDelivery, audible: barge.audible, speaking: barge.speaking,
      thinking: eva.thinking, quietMs: Date.now() - eva.lastAudibleAt, channel: channel && channel.readyState,
      // Many ducks while only she talks = her echo is taken for the user (crackle, self-interruption).
      ducks: barge.ducks, mutes: barge.mutes,
    });
    // await evaAudio(): what her voice actually arrives as — codec line, kbit/s over 3 s, lost and
    // concealed audio. A low bitrate or a lot of concealment = the lisp is the transport, not the model.
    window.evaAudio = async () => {
      if (!pc) return null;
      const read = async () => {
        let inbound = null, codecs = {};
        (await pc.getStats()).forEach((r) => {
          if (r.type === "inbound-rtp" && r.kind === "audio") inbound = r;
          if (r.type === "codec") codecs[r.id] = r;
        });
        return { inbound, codec: inbound && codecs[inbound.codecId] };
      };
      const a = await read();
      await new Promise((r) => setTimeout(r, 3000));
      const b = await read();
      if (!a.inbound || !b.inbound) return null;
      const s = b.inbound;
      return {
        codec: b.codec && b.codec.mimeType + " " + b.codec.clockRate + " " + (b.codec.sdpFmtpLine || ""),
        kbps: Math.round(((s.bytesReceived - a.inbound.bytesReceived) * 8) / 3000),
        packetsLost: s.packetsLost,
        concealedPct: s.totalSamplesReceived ? +((100 * s.concealedSamples) / s.totalSamplesReceived).toFixed(1) : 0,
        jitterMs: Math.round((s.jitter || 0) * 1000),
      };
    };
  }

  function sendDeliveryWhenQuiet() {
    clearTimeout(deliveryTimer);
    if (!pendingDelivery) return;
    if (!channel || eva.mode !== "active") { pendingDelivery = ""; return; } // a new session gets them at start
    if (answering()) {
      deliveryTimer = setTimeout(sendDeliveryWhenQuiet, 300);
      return;
    }
    sendEvent({ type: "session.instructions.append", content: pendingDelivery, delegation_id: null, event_id: eventId("style") });
    pendingDelivery = "";
  }

  function showVolume() {
    // iOS: the page can't set the volume of her voice (read-only there) — the phone's buttons do.
    if (IOS) { $("volume").style.display = "none"; $("volumeValue").parentElement.style.display = "none"; }
    $("volume").value = String(Math.round(volume * 100));
    $("volumeValue").textContent = Math.round(volume * 100) + "%";
  }

  let preview = null;
  function stopPreview() {
    if (preview) {
      // Unloaded, not only paused: a paused sample came back on a speaker change (WebKit, setSinkId).
      preview.onended = preview.onerror = null;
      preview.pause();
      preview.removeAttribute("src");
      preview.load();
    }
    preview = null;
    $("voicePreview").textContent = "▶ Прослухати";
  }
  function togglePreview() {
    if (preview) { stopPreview(); return; }
    const picked = voices.find((x) => x.id === $("voice").value);
    const audio = new Audio("samples/" + encodeURIComponent((picked && picked.sample) || $("voice").value) + ".m4a");
    audio.volume = volume;
    if (speakerId && audio.setSinkId) audio.setSinkId(speakerId).catch(() => {});
    audio.onended = stopPreview;
    audio.onerror = () => { stopPreview(); addLine("system", "Зразок цього голосу недоступний."); };
    preview = audio;
    $("voicePreview").textContent = "■ Стоп";
    audio.play().catch(stopPreview);
  }

  // ── Музика ──
  // Songs are shared (the server keeps them): one added on a laptop is there on every phone. They play by the button or by
  // voice (play_song: the server picks the song by name, the page fetches it). She must not speak over a song: it
  // pauses her and turns the mic off — so she hears nothing of it — and both come back when it stops.
  // The list is the server's (server/songs.py): what one device uploads, every device and everyone sees.
  const SONG_MIME = { mp3: "audio/mpeg", m4a: "audio/mp4", aac: "audio/aac", wav: "audio/wav", ogg: "audio/ogg",
    opus: "audio/ogg", flac: "audio/flac" };
  const SONG_MAX_MB = 25;
  const songInfoText = $("songInfo").textContent;
  const songNote = (text) => { $("songInfo").textContent = text || songInfoText; };

  const clock = (sec) => {
    const t = Math.max(0, Math.floor(sec || 0));
    const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), r = String(t % 60).padStart(2, "0");
    return h ? h + ":" + String(m).padStart(2, "0") + ":" + r : m + ":" + r;
  };
  let seeking = false; // the slider is being dragged: the song's own position must not pull it back

  // The slider and the times: the song's length, where it is now; dragging it rewinds.
  function showSeek() {
    const a = songs.audio;
    const length = Number.isFinite(a.duration) ? a.duration : 0;
    $("songSeek").max = String(Math.floor(length));
    $("songSeek").disabled = !length;
    if (!seeking) $("songSeek").value = String(Math.floor(length ? a.currentTime : 0));
    $("songTime").textContent = clock(seeking ? Number($("songSeek").value) : a.currentTime);
    $("songDuration").textContent = clock(length);
  }

  // The picked song is loaded (not played) so its length shows and it can be rewound before it starts.
  function attachSong() {
    if (songs.external) return; // a preview is loaded instead: the picked song comes back after it
    const song = songs.list.find((x) => x.id === songs.current);
    if (!song || songs.loadedId === song.id) return;
    songs.audio.preload = "metadata";
    songs.audio.src = BACKEND + "/api/songs/" + song.id + "/audio";
    songs.loadedId = song.id;
    showSeek();
  }

  async function loadSongs() {
    try {
      const res = await fetch(BACKEND + "/api/songs");
      if (!res.ok) throw new Error(String(res.status));
      songs.list = (await res.json()).songs;
    } catch {
      if (songs.list.length) return; // offline for a moment: the list stays
      songNote("Музика зараз недоступна — сервер не відповідає.");
      showSongs();
      return;
    }
    if (!songs.list.some((x) => x.id === songs.current)) songs.current = songs.list.length ? songs.list[0].id : "";
    if (songs.playing && !songs.external && !songs.list.some((x) => x.id === songs.loadedId)) stopSong(); // deleted from another device
    if (!songs.playing) attachSong();
    showSongs();
  }

  function showSongs() {
    const has = songs.list.length > 0;
    $("songSelect").replaceChildren(...(has ? songs.list.map((x) => new Option(x.name, x.id)) : [new Option("Пісень ще немає", "")]));
    $("songSelect").value = songs.current;
    $("songSelect").disabled = !has;
    $("songPlay").disabled = !has && !songs.playing && !songs.pending;
    $("songDelete").disabled = !has;
    $("songPlay").textContent = songs.playing ? "■ Зупинити" : songs.pending ? "✕ Скасувати" : "▶ Увімкнути";
    showMusicBtn();
  }

  async function addSongs(files) {
    let last = "";
    for (const file of files) {
      const ext = (file.name.split(".").pop() || "").toLowerCase();
      const type = file.type.startsWith("audio/") ? file.type : SONG_MIME[ext];
      if (!type) { addLine("system", "«" + file.name + "» — не аудіофайл, пропущено."); continue; }
      if (file.size > SONG_MAX_MB * 1024 * 1024) { addLine("system", "«" + file.name + "» завелика: найбільше " + SONG_MAX_MB + " МБ."); continue; }
      songNote("Завантажую «" + file.name + "»…");
      try {
        const res = await fetch(BACKEND + "/api/songs?name=" + encodeURIComponent(file.name.replace(/\.[^.]+$/, "")),
          { method: "POST", headers: { "Content-Type": type }, body: file });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || "помилка " + res.status);
        }
        last = (await res.json()).id;
      } catch (e) { addLine("system", "Не вдалося завантажити «" + file.name + "»: " + e.message); }
    }
    songNote();
    if (last) songs.current = last;
    store.set("va-song", songs.current);
    await loadSongs();
  }

  async function deleteSong() {
    const song = songs.list.find((x) => x.id === songs.current);
    if (!song || !confirm("Видалити «" + song.name + "»? Вона зникне для всіх.")) return;
    if (songs.playing && songs.loadedId === song.id) stopSong();
    if (songs.loadedId === song.id) { songs.audio.removeAttribute("src"); songs.audio.load(); songs.loadedId = ""; showSeek(); }
    try { await fetch(BACKEND + "/api/songs/" + song.id, { method: "DELETE" }); } catch { /* stays in the list */ }
    songs.current = "";
    await loadSongs();
    store.set("va-song", songs.current);
  }

  function playSong() {
    const song = songs.list.find((x) => x.id === songs.current);
    if (!song) { addLine("system", "Спершу додайте пісню в панелі «Музика»."); return; }
    if (songs.playing || songs.pending) return;
    if (eva.mode === "connecting" || eva.mode === "switching") { addLine("system", "Зачекайте, поки розмова підключиться, і вмикайте пісню."); return; }
    if (eva.mode === "active" && quietFor() < SONG_NOW_QUIET_MS) { queueSong(); return; } // the talk goes on first
    startSong();
  }

  // A queued song waits for the conversation to go quiet — she and the user both — this long, so a question
  // asked after pressing the button is answered first and the song does not cut into the talk.
  const SONG_QUIET_MS = 5000;
  const SONG_NOW_QUIET_MS = 1200; // she has just finished: the button starts the song at once
  const talkingNow = () => barge.audible || barge.speaking || eva.thinking;
  const quietFor = () => (talkingNow() ? 0 : Date.now() - Math.max(barge.audibleEndedAt, eva.lastAudibleAt, eva.lastUserAt));

  // iOS lets a page start audio only from a tap: a short silent play inside one unlocks this element for the
  // song that starts later, after she has finished (no tap then).
  function primeSong() {
    const a = songs.audio;
    if (songs.primed || !songs.loadedId || songs.playing) return;
    songs.primed = true;
    const at = a.currentTime;
    a.muted = true;
    a.play().then(() => {
      if (!songs.playing) { a.pause(); a.currentTime = at; }
      a.muted = false;
    }).catch(() => { a.muted = false; songs.primed = false; });
  }

  function queueSong() {
    songs.pending = true;
    songs.left = null;
    songs.pendingTimer = setInterval(() => {
      if (!songs.pending) { clearInterval(songs.pendingTimer); return; }
      // A call that is no longer going (a pause, the end) has nothing to wait for.
      if (eva.mode === "active" && quietFor() < SONG_QUIET_MS) {
        const left = talkingNow() ? null : Math.ceil((SONG_QUIET_MS - quietFor()) / 1000);
        if (left !== songs.left) { songs.left = left; showState(); }
        return;
      }
      const song = songs.list.find((x) => x.id === songs.current);
      cancelPending();
      if ((song || songs.external) && !songs.playing) startSong();
    }, 250);
    showSongs();
    showState();
  }

  function cancelPending() {
    if (!songs.pending) return;
    songs.pending = false;
    clearInterval(songs.pendingTimer);
    showSongs();
    if (eva.mode !== "off") showState();
  }

  function startSong() {
    if (eva.mode === "active") pauseEva(); // she goes silent now
    if (eva.mode !== "off" && !micMuted) { songs.mutedMic = true; setMicMuted(true); }
    const a = songs.audio;
    attachSong();
    a.volume = IOS ? 1 : volume;
    if (speakerId && a.setSinkId) a.setSinkId(speakerId).catch(() => {});
    songs.playing = true;
    showSongs();
    if (eva.mode !== "off") showState();
    a.play().catch(() => {
      endSong();
      addLine("system", "Браузер не дозволив увімкнути пісню — натисніть «Увімкнути» ще раз.");
    });
  }

  function stopSong() {
    cancelPending();
    songs.audio.pause();
    endSong();
    clearExternal();
  }

  // A preview is over: the player gets the picked song back.
  function clearExternal() {
    if (!songs.external || songs.playing) return;
    songs.external = null;
    songs.loadedId = "";
    songNote();
    attachSong();
    showSeek();
  }

  // The song is over (or stopped): the mic comes back, she stays paused until «Продовжити» / her wake phrase.
  function endSong() {
    if (!songs.playing) return;
    songs.playing = false;
    if (songs.mutedMic) {
      songs.mutedMic = false;
      if (micMuted && eva.mode !== "off") setMicMuted(false);
    }
    clearExternal();
    showSongs();
    if (eva.mode !== "off") showState();
  }

  function initMusic() {
    songs.audio.addEventListener("ended", () => { songs.audio.currentTime = 0; endSong(); showSeek(); });
    songs.audio.addEventListener("error", () => {
      if (!songs.playing) return;
      const preview = Boolean(songs.external);
      endSong();
      addLine("system", preview ? "Уривок не вдалося відтворити." : "Цю пісню не вдалося відтворити — формат не підтримується браузером.");
    });
    $("songFile").addEventListener("change", (e) => { addSongs([...e.target.files]); e.target.value = ""; });
    $("songSelect").addEventListener("change", () => {
      if (songs.playing || songs.pending) stopSong();
      songs.current = $("songSelect").value;
      store.set("va-song", songs.current);
      attachSong();
    });
    for (const ev of ["loadedmetadata", "durationchange", "timeupdate", "seeked"]) songs.audio.addEventListener(ev, showSeek);
    $("songSeek").addEventListener("input", () => { seeking = true; showSeek(); });
    $("songSeek").addEventListener("change", () => {
      songs.audio.currentTime = Number($("songSeek").value);
      seeking = false;
      showSeek();
    });
    $("songPlay").addEventListener("click", () => {
      if (songs.playing || songs.pending) { stopSong(); return; }
      primeSong();
      playSong();
    });
    $("songDelete").addEventListener("click", deleteSong);
    loadSongs();
    // Another device may have added or removed songs: look again when the page comes back and every half minute.
    document.addEventListener("visibilitychange", () => { if (!document.hidden) loadSongs(); });
    setInterval(() => { if (!document.hidden) loadSongs(); }, 30000);
    initMusicPanel();
  }

  // ── «Музика» panel: the header button opens it — the songs, Шазам (what plays nearby) and «Створити» (a new song) ──
  // All three work by voice too: recognize_song makes this page record the clip, generate_song starts the same job as
  // «Створити пісню», play_song asks the page to play a song (fetched from /api/music/action).
  const MUSIC_CALLS = new Set(["recognize_song", "generate_song", "play_song"]);
  const MUSIC_TABS = ["songs", "shazam", "create"];
  const CLIP_MS = 10000; // AudD needs a few seconds; ten is safe in a noisy room
  const JOB_POLL_MS = 4000;
  const CLIP_TYPES = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus", "audio/webm"];
  const LINK_NAMES = [["spotify", "Spotify"], ["apple_music", "Apple Music"], ["youtube", "YouTube"], ["deezer", "Deezer"]];
  const shazamInfoText = $("shazamStatus").textContent;
  const genInfoText = $("genInfo").textContent;
  const shazamNote = (text) => { $("shazamStatus").textContent = text || shazamInfoText; };
  const genNote = (text) => { $("genInfo").textContent = text || genInfoText; };
  const mk = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };
  const isWebUrl = (url) => /^https:\/\//.test(url || "");
  const trackName = (t) => "«" + (t.title || "без назви") + "» — " + (t.artist || "невідомий виконавець");

  function setMusicTab(tab) {
    if (!MUSIC_TABS.includes(tab)) tab = "songs";
    musicUi.tab = tab;
    store.set("va-music-tab", tab);
    document.querySelectorAll("[data-music-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.musicTab === tab)));
    document.querySelectorAll("[data-music-panel]").forEach((panel) => { panel.hidden = panel.dataset.musicPanel !== tab; });
    if (tab === "create") musicUi.unseen = false;
    showMusicBtn();
  }

  function setMusicOpen(open, tab) {
    musicUi.open = open;
    if (tab) setMusicTab(tab);
    $("musicCard").hidden = !open;
    $("musicBtn").setAttribute("aria-expanded", String(open));
    store.set("va-music-open", open ? "1" : "0");
    if (open && musicUi.tab === "create") musicUi.unseen = false;
    showMusicBtn();
  }

  // The header button tells what music is doing even with the panel closed.
  function showMusicBtn() {
    const btn = $("musicBtn");
    if (!btn) return;
    btn.classList.toggle("listening", musicUi.listening);
    btn.classList.toggle("playing", songs.playing && !musicUi.listening);
    btn.classList.toggle("busy", !songs.playing && !musicUi.listening && musicUi.jobs.some((j) => j.status === "running"));
    $("musicBadge").hidden = !musicUi.unseen;
  }

  // ── Шазам ──
  function clipType() {
    if (!window.MediaRecorder || !MediaRecorder.isTypeSupported) return "";
    return CLIP_TYPES.find((t) => MediaRecorder.isTypeSupported(t)) || "";
  }

  // Raw sound: echo cancelling and noise suppression (what the call's mic uses) wipe music out. On iPhone a second
  // capture with other settings can silence the call's own mic, so a copy of that one is used there.
  async function openClipMic() {
    const callTrack = mic && mic.getAudioTracks().find((t) => t.readyState === "live");
    const copy = () => { const t = callTrack.clone(); t.enabled = true; return new MediaStream([t]); };
    if (IOS && callTrack) return copy();
    const raw = { echoCancellation: false, noiseSuppression: false, autoGainControl: false };
    if (micId) raw.deviceId = { ideal: micId };
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: raw });
      // Chrome can hand back the call's processed capture: ask the track again, and tell what it really is.
      const track = stream.getAudioTracks()[0];
      const got = (track && track.getSettings && track.getSettings()) || {};
      if (track && (got.echoCancellation || got.noiseSuppression || got.autoGainControl) && track.applyConstraints) {
        await track.applyConstraints(raw).catch(() => {});
      }
      return stream;
    } catch (err) {
      if (callTrack) return copy();
      throw err;
    }
  }

  // While the clip records the call hears nothing: no barge-in on the music, no transcript of it. Quietly — not
  // «Вимкнути мікрофон», which the user set and which stays as it was.
  function holdCallMic(hold) {
    const tracks = [...(mic ? mic.getAudioTracks() : []), ...(gate && gate.track ? [gate.track] : [])];
    tracks.forEach((t) => { t.enabled = !hold && !micMuted; });
  }

  // Her «Слухаю…» must not be in the clip: recording starts once her audio has stopped (or after maxMs).
  const waitHerQuiet = (maxMs) => new Promise((done) => {
    const until = Date.now() + maxMs;
    const tick = () => (!barge.audible || Date.now() > until ? done() : setTimeout(tick, 100));
    tick();
  });

  function showListening(left) {
    const btn = $("shazamBtn");
    btn.classList.toggle("listening", left !== 0);
    btn.disabled = left !== 0 || !musicUi.status.recognize;
    btn.textContent = left > 0 ? "Слухаю… " + left + " с" : left < 0 ? "Шукаю, що це…" : "🎧 Слухати";
    if (left > 0) shazamNote("Тримайте телефон ближче до звуку.");
    showMusicBtn();
  }

  // The countdown on the button while a clip records.
  function countdown(ms) {
    return new Promise((done) => {
      const started = Date.now();
      const timer = setInterval(() => {
        const left = Math.ceil((ms - (Date.now() - started)) / 1000);
        if (left > 0) { showListening(left); return; }
        clearInterval(timer);
        done();
      }, 250);
    });
  }

  // The clip as WAV (16-bit mono, ~16 kHz) built here from the raw samples: AudD fingerprints that reliably — the
  // containers MediaRecorder writes failed (prod: «problem with creating an audio fingerprint»). Its level (RMS)
  // tells silence apart. The call's running AudioContext is used when there is one (WebKit starts a new one only in
  // a tap); null when audio can't start here — then MediaRecorder records instead.
  async function recordWav(stream) {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return null;
    const shared = gate && gate.ctx && gate.ctx.state === "running" ? gate.ctx : null;
    let ctx = shared;
    if (!ctx) {
      try { ctx = new AudioCtx(); } catch { return null; }
      await within(ctx.resume(), 1000);
      if (ctx.state !== "running") { ctx.close().catch(() => {}); return null; }
    }
    const source = ctx.createMediaStreamSource(stream);
    const proc = ctx.createScriptProcessor(4096, 1, 1);
    const parts = [];
    let sumSq = 0;
    let count = 0;
    let peak = 0;
    proc.onaudioprocess = (e) => {
      const data = e.inputBuffer.getChannelData(0);
      parts.push(new Float32Array(data));
      for (let i = 0; i < data.length; i++) { sumSq += data[i] * data[i]; const a = Math.abs(data[i]); if (a > peak) peak = a; }
      count += data.length;
    }; // the output stays silent: nothing of the clip is played
    source.connect(proc);
    proc.connect(ctx.destination);
    await countdown(CLIP_MS);
    proc.onaudioprocess = null;
    source.disconnect();
    proc.disconnect();
    const rate = ctx.sampleRate;
    if (!shared) ctx.close().catch(() => {});
    if (!count) return null;
    const step = Math.max(1, Math.floor(rate / 16000));
    const samples = new Float32Array(count);
    let at = 0;
    for (const part of parts) { samples.set(part, at); at += part.length; }
    // A laptop mic hearing speakers is faint (raw: no auto gain): lift it so the loudest peak is ~0.8, at most 25×.
    const gain = peak > 0.0005 ? Math.min(25, Math.max(1, 0.8 / peak)) : 1;
    const pcm = new Int16Array(Math.floor(count / step));
    for (let i = 0; i < pcm.length; i++) {
      let sum = 0;
      for (let k = 0; k < step; k++) sum += samples[i * step + k];
      const v = Math.max(-1, Math.min(1, (sum / step) * gain));
      pcm[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
    }
    return { blob: wavBlob(pcm, Math.round(rate / step)), level: Math.sqrt(sumSq / count), peak, gain };
  }

  function wavBlob(pcm, rate) {
    const view = new DataView(new ArrayBuffer(44));
    const text = (at, str) => { for (let i = 0; i < str.length; i++) view.setUint8(at + i, str.charCodeAt(i)); };
    text(0, "RIFF"); view.setUint32(4, 36 + pcm.byteLength, true); text(8, "WAVE");
    text(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
    view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
    text(36, "data"); view.setUint32(40, pcm.byteLength, true);
    return new Blob([view, pcm], { type: "audio/wav" });
  }

  function recordClip(stream, type) {
    return new Promise((resolve, reject) => {
      const chunks = [];
      let rec;
      try { rec = new MediaRecorder(stream, { mimeType: type }); } catch (err) { reject(err); return; }
      rec.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
      rec.onerror = (e) => reject((e && e.error) || new Error("запис перервався"));
      rec.onstop = () => (chunks.length ? resolve(new Blob(chunks, { type: rec.mimeType || type })) : reject(new Error("порожній запис")));
      rec.start(1000);
      const started = Date.now();
      const timer = setInterval(() => {
        const left = Math.ceil((CLIP_MS - (Date.now() - started)) / 1000);
        if (left > 0) { showListening(left); return; }
        clearInterval(timer);
        if (rec.state !== "inactive") rec.stop();
      }, 250);
    });
  }

  // via: "button" (the panel) or "voice" (recognize_song is waiting on the server for this clip).
  async function listenForSong(via) {
    if (musicUi.listening) return;
    setMusicOpen(true, "shazam");
    if (!musicUi.status.recognize) { shazamNote("Розпізнавання музики ще не налаштоване на сервері (потрібен ключ AudD)."); return; }
    const type = clipType();
    const canWav = Boolean(window.AudioContext || window.webkitAudioContext);
    if (!HAS_MIC_API || (!type && !canWav)) { shazamNote("Цей браузер не вміє записувати звук для розпізнавання."); return; }
    musicUi.listening = true;
    showListening(CLIP_MS / 1000);
    fetch(BACKEND + "/api/music/listening", { method: "POST", headers: headers() }).catch(() => {});
    if (via === "voice") await waitHerQuiet(2500);
    const inCall = eva.mode !== "off";
    if (inCall) holdCallMic(true);
    let stream = null;
    try {
      stream = await openClipMic();
      const wav = canWav ? await recordWav(stream) : null;
      if (!wav && !type) throw new Error("цей браузер не вміє записувати звук");
      const blob = wav ? wav.blob : await recordClip(stream, type);
      showClipBack(blob, wav, stream);
      showListening(-1);
      shazamNote("Шукаю, що це…");
      const sendHeaders = { "Content-Type": blob.type || type, "X-Client-Id": clientId };
      if (wav) sendHeaders["X-Clip-Level"] = wav.level.toFixed(5);
      const res = await fetch(BACKEND + "/api/music/recognize?via=" + via, { method: "POST", headers: sendHeaders, body: blob });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || "помилка " + res.status);
      showTrackResult(body);
    } catch (err) {
      shazamNote(err && err.name === "NotAllowedError" ? "Браузер не дав мікрофон — дозвольте його й спробуйте ще раз."
        : "Не вдалося: " + ((err && err.message) || err));
    } finally {
      if (stream) stream.getTracks().forEach((t) => t.stop());
      if (inCall) holdCallMic(false);
      musicUi.listening = false;
      showListening(0);
    }
  }

  // What was recorded, to hear for yourself when a song is not found: the clip, its level and what the mic did to it.
  let clipUrl = "";
  function showClipBack(blob, wav, stream) {
    try {
      const track = stream.getAudioTracks()[0];
      const st = (track && track.getSettings && track.getSettings()) || {};
      if (clipUrl) URL.revokeObjectURL(clipUrl);
      clipUrl = URL.createObjectURL(blob);
      $("shazamClip").src = clipUrl;
      const level = wav ? "рівень " + wav.level.toFixed(4) + ", пік " + wav.peak.toFixed(3) + ", підсилення ×" + wav.gain.toFixed(1) : "";
      const mic = "ехоподавлення " + (st.echoCancellation ? "увімкнене" : "вимкнене") + ", шумодав " + (st.noiseSuppression ? "увімкнений" : "вимкнений");
      $("shazamClipInfo").textContent = [level, mic, Math.round(blob.size / 1024) + " КБ"].filter(Boolean).join(" · ");
      $("shazamDebug").hidden = false;
    } catch { /* the clip is only for a look */ }
  }

  function showTrackResult(result) {
    if (!result || !result.found) {
      $("shazamResult").hidden = true;
      shazamNote(result && result.quiet ? "Мікрофон майже нічого не почув — увімкніть музику голосніше або піднесіть телефон ближче."
        : "Не вдалося впізнати — піднесіть телефон ближче до звуку й спробуйте ще раз.");
      return;
    }
    shazamNote();
    renderTrack(result);
    musicUi.history = [{ ...result, at: Date.now() },
      ...musicUi.history.filter((h) => !(h.title === result.title && h.artist === result.artist))].slice(0, 8);
    store.set("va-shazam", JSON.stringify(musicUi.history));
    renderHistory();
  }

  function renderTrack(t) {
    musicUi.track = t;
    const box = $("shazamResult");
    let cover;
    if (isWebUrl(t.cover_url)) {
      cover = mk("img", "track-cover");
      cover.src = t.cover_url;
      cover.alt = "";
      cover.referrerPolicy = "no-referrer";
    } else cover = mk("div", "track-cover", "🎵");
    const info = mk("div");
    const meta = [t.album, (t.release_date || "").slice(0, 4)].filter(Boolean).join(" · ");
    info.append(mk("div", "track-title", t.title || "Без назви"), mk("div", "track-artist", t.artist || ""));
    if (meta) info.append(mk("div", "track-meta", meta));
    const actions = mk("div", "track-actions");
    if (isWebUrl(t.preview_url)) {
      const play = mk("button", "btn primary small-btn", "▶ Уривок 30 с");
      play.type = "button";
      play.addEventListener("click", () => playPreview(t.preview_url, trackName(t)));
      actions.append(play);
    }
    const links = t.links || {};
    for (const [key, label] of LINK_NAMES) {
      if (!isWebUrl(links[key])) continue;
      const a = mk("a", "btn ghost small-btn", label);
      a.href = links[key];
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      actions.append(a);
    }
    box.replaceChildren(cover, info, actions);
    box.hidden = false;
  }

  function renderHistory() {
    const list = $("shazamHistory");
    list.replaceChildren(...musicUi.history.map((h) => {
      const li = mk("li");
      const btn = mk("button");
      btn.type = "button";
      const when = new Date(h.at || 0);
      const today = when.toDateString() === new Date().toDateString();
      btn.append(mk("span", "", trackName(h)), mk("span", "when", today
        ? when.toLocaleTimeString("uk-UA", { hour: "2-digit", minute: "2-digit" }) : when.toLocaleDateString("uk-UA")));
      btn.addEventListener("click", () => renderTrack(h));
      li.append(btn);
      return li;
    }));
    $("shazamHistoryBlock").hidden = musicUi.history.length === 0;
  }

  // A recognised song's 30 s preview, in the songs' player: she pauses for it the same way.
  function playPreview(url, name) {
    if (songs.playing || songs.pending) stopSong();
    songs.external = { url, name };
    songs.audio.src = url;
    songs.loadedId = "ext";
    songNote("Уривок: " + name);
    showSeek();
    if (eva.mode === "connecting" || eva.mode === "switching") { addLine("system", "Зачекайте, поки розмова підключиться."); clearExternal(); return; }
    if (eva.mode === "active" && quietFor() < SONG_NOW_QUIET_MS) { queueSong(); return; }
    startSong();
  }

  // ── a song from «Музика» by its id (play_song, «▶ Увімкнути» of a new song) ──
  async function playSongById(id, tapped) {
    if (!songs.list.some((x) => x.id === id)) await loadSongs();
    if (!songs.list.some((x) => x.id === id)) { addLine("system", "Цієї пісні вже немає в «Музиці»."); return; }
    if (songs.playing || songs.pending) stopSong();
    songs.current = id;
    store.set("va-song", id);
    attachSong();
    showSongs();
    setMusicOpen(true, "songs");
    if (tapped) primeSong();
    playSong();
  }

  // ── Створити ──
  async function createSong() {
    const prompt = $("genPrompt").value.trim();
    const lyrics = $("genLyrics").value.trim();
    if (!prompt && !lyrics) { genNote("Опишіть, яку пісню створити."); $("genPrompt").focus(); return; }
    $("genBtn").disabled = true;
    try {
      const res = await fetch(BACKEND + "/api/music/generate", {
        method: "POST", headers: headers(),
        body: JSON.stringify({ prompt, lyrics, title: $("genTitle").value.trim(), instrumental: $("genInstrumental").checked }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || "помилка " + res.status);
      genNote("Пишу «" + body.title + "» — це хвилина-дві, розмова тим часом іде як завжди.");
      $("genPrompt").value = "";
      $("genLyrics").value = "";
      $("genTitle").value = "";
      refreshJobs();
    } catch (err) {
      genNote("Не вдалося: " + err.message);
    } finally {
      $("genBtn").disabled = !musicUi.status.generate;
    }
  }

  async function refreshJobs() {
    let list;
    try {
      const res = await fetch(BACKEND + "/api/music/jobs", { headers: headers() });
      if (!res.ok) return;
      list = (await res.json()).jobs || [];
    } catch { return; }
    const before = new Map(musicUi.jobs.map((j) => [j.id, j.status]));
    const now = Date.now();
    list.forEach((j) => { j.localStart = now - (j.elapsed_s || 0) * 1000; });
    musicUi.jobs = list;
    let ready = false;
    for (const j of list) {
      if (before.get(j.id) !== "running" || j.status === "running") continue;
      if (j.status === "done") { ready = true; addLine("system", "Пісня «" + j.song_name + "» готова — вона в «Музиці»."); }
      else addLine("system", "Пісню «" + j.title + "» написати не вдалося: " + j.error);
    }
    if (ready) {
      await loadSongs();
      if (!musicUi.open || musicUi.tab !== "create") musicUi.unseen = true;
    }
    renderJobs();
    clearTimeout(musicUi.jobsTimer);
    if (list.some((j) => j.status === "running")) musicUi.jobsTimer = setTimeout(refreshJobs, JOB_POLL_MS);
  }

  function jobState(j) {
    if (j.status === "running") return "Пишеться… " + clock((Date.now() - j.localStart) / 1000);
    if (j.status === "done") return "Готово — у «Піснях»" + (j.model ? " · " + j.model : "");
    return j.error || "Не вдалося";
  }

  function renderJobs() {
    $("genJobs").replaceChildren(...musicUi.jobs.slice(0, 6).map((j) => {
      const li = mk("li");
      const text = mk("div");
      text.append(mk("div", "job-title", j.title), mk("div", "job-state " + j.status, jobState(j)));
      text.lastChild.dataset.job = j.id;
      li.append(text);
      if (j.status === "done" && j.song_id) {
        const play = mk("button", "btn small-btn", "▶ Увімкнути");
        play.type = "button";
        play.addEventListener("click", () => playSongById(j.song_id, true));
        li.append(play);
      }
      return li;
    }));
    clearInterval(musicUi.jobsTick);
    musicUi.jobsTick = null;
    if (musicUi.jobs.some((j) => j.status === "running")) {
      musicUi.jobsTick = setInterval(() => {
        for (const j of musicUi.jobs) {
          const node = j.status === "running" && $("genJobs").querySelector('[data-job="' + j.id + '"]');
          if (node) node.textContent = jobState(j);
        }
      }, 1000);
    }
    showMusicBtn();
  }

  async function loadMusicStatus() {
    try {
      const res = await fetch(BACKEND + "/api/music/status");
      if (res.ok) musicUi.status = await res.json();
    } catch { /* the buttons stay on; a failure says why */ }
    $("shazamBtn").disabled = !musicUi.status.recognize;
    $("genBtn").disabled = !musicUi.status.generate;
    if (!musicUi.status.recognize) shazamNote("Розпізнавання музики ще не налаштоване на сервері (потрібен ключ AudD).");
    if (!musicUi.status.generate) genNote("Створення пісень ще не налаштоване на сервері (потрібен ключ OpenRouter, Gemini або ElevenLabs).");
  }

  // ── her music tools (the backend's calls, seen on the call's data channel) ──
  function onMusicCall(name, callId, done) {
    if (name === "recognize_song") {
      if (musicUi.calls.has(callId)) return;
      musicUi.calls.add(callId);
      listenForSong("voice");
      return;
    }
    if (!done || musicUi.calls.has(callId + ":" + name)) return;
    musicUi.calls.add(callId + ":" + name);
    if (name === "play_song") fetchMusicAction(0);
    else if (name === "generate_song") { setMusicOpen(true, "create"); setTimeout(refreshJobs, 800); }
  }

  // The call is seen here before the server has run it: ask a few times until the song it picked is there.
  async function fetchMusicAction(tries) {
    try {
      const res = await fetch(BACKEND + "/api/music/action", { headers: headers() });
      const action = res.ok ? (await res.json()).action : null;
      if (action) {
        if (action.type === "play" && action.song_id) playSongById(action.song_id, false);
        else if (action.type === "preview" && isWebUrl(action.url)) { setMusicOpen(true, "shazam"); playPreview(action.url, action.name || "уривок"); }
        return;
      }
    } catch { /* tried again below */ }
    if (tries < 10) setTimeout(() => fetchMusicAction(tries + 1), 400);
  }

  function initMusicPanel() {
    setMusicTab(musicUi.tab);
    setMusicOpen(musicUi.open);
    $("musicBtn").addEventListener("click", () => setMusicOpen(!musicUi.open));
    $("musicClose").addEventListener("click", () => setMusicOpen(false));
    // A mini panel: a click outside it or Esc closes it (a song keeps playing).
    document.addEventListener("click", (e) => {
      if (musicUi.open && !$("musicCard").contains(e.target) && !$("musicBtn").contains(e.target)) setMusicOpen(false);
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && musicUi.open) setMusicOpen(false); });
    document.querySelectorAll("[data-music-tab]").forEach((b) => b.addEventListener("click", () => setMusicTab(b.dataset.musicTab)));
    $("shazamBtn").addEventListener("click", () => listenForSong("button"));
    $("genBtn").addEventListener("click", createSong);
    renderHistory();
    loadMusicStatus();
    refreshJobs();
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshJobs(); });
  }

  async function newConversation() {
    await fetch(BACKEND + "/api/conversation", { method: "DELETE", headers: headers() }).catch(() => {});
    clearLog();
    showGuest(null);
    shownGuest = "";
    addLine("system", mode === "concierge"
      ? "Наступний гість — " + callName + " привітається і спитає ім'я. Попередня розмова в «Історії»."
      : "Нова розмова — з чистого аркуша. Попередня збережена в «Історії».");
    restartSession("connecting", { greet: true });
  }

  // After a refresh (or a deploy) the conversation Єва remembers is shown again: the server keeps it
  // (server/conversations.py). Not over lines a call already wrote.
  async function loadConversation() {
    let data;
    try {
      const res = await fetch(BACKEND + "/api/conversation", { headers: headers() });
      if (!res.ok) return;
      data = await res.json();
    } catch { return; }
    if (!data.turns.length || $("log").querySelector(".msg.user, .msg.assistant")) return;
    const notes = [...$("log").querySelectorAll(".msg.system")];
    clearLog();
    $("log").querySelector(".empty").remove();
    data.turns.forEach((t) => addLine(t.role, t.text));
    const when = new Date(data.started_at * 1000).toLocaleString("uk-UA", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });
    addLine("system", "Розмова від " + when + " — " + callName + " її пам'ятає. «Нова розмова» почне з чистого аркуша.");
    notes.forEach((n) => $("log").appendChild(n));
    $("log").scrollTop = $("log").scrollHeight;
  }

  // «Скопіювати»: the dialogue as plain text, without the page's own notes.
  function conversationText() {
    const name = assistantName || callName;
    return [...$("log").querySelectorAll(".msg.user, .msg.assistant")]
      .map((el) => [el.classList.contains("user") ? "Ви" : name, el.textContent.trim()])
      .filter(([, text]) => text)
      .map(([who, text]) => who + ": " + text)
      .join("\n\n");
  }

  // The async clipboard needs a secure page with focus; the old way works where it doesn't.
  function copyFallback(text) {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    try { return document.execCommand("copy"); } finally { area.remove(); }
  }

  // The button says what happened for a moment, then goes back to its label.
  function flashButton(btn, text) {
    btn.dataset.label = btn.dataset.label || btn.textContent;
    clearTimeout(btn.flashTimer);
    btn.textContent = text;
    btn.flashTimer = setTimeout(() => { btn.textContent = btn.dataset.label; }, 1800);
  }

  async function copyConversation() {
    const text = conversationText();
    const btn = $("copyChat");
    if (!text) { flashButton(btn, "Поки порожньо"); return; }
    let ok = false;
    try { await navigator.clipboard.writeText(text); ok = true; } catch { ok = copyFallback(text); }
    flashButton(btn, ok ? "Скопійовано ✓" : "Не вдалося");
  }

  // The server's view of this browser (voice settings, call state); null if unreachable.
  async function fetchMe() {
    try {
      const res = await fetch(BACKEND + "/api/me", { headers: headers() });
      return res.ok ? await res.json() : null;
    } catch { return null; } // backend asleep / offline
  }

  // Voice settings changed on the server (asked by voice).
  async function refreshMe() {
    if (!BACKEND) return;
    try {
      const me = await fetchMe();
      if (!me) return;
      assistantName = me.assistant_name || ""; // renamed by voice: the copied conversation uses it
      if (mode === "concierge" && me.guest) showGuest(me.guest);
      const { voice: serverVoice, speed: serverSpeed, style: serverStyle } = me;
      // Set on the server only when chosen there (e.g. asked by voice) — adopt it then.
      if (serverVoice && serverVoice !== voice && voices.some((x) => x.id === serverVoice)) {
        voice = serverVoice;
        store.set("va-voice", voice);
        showVoice();
      }
      // «Говори повільніше» by voice: show it (a restarted server reports "normal" — keep ours then).
      if (serverSpeed && serverSpeed !== "normal" && serverSpeed !== speed) { speed = serverSpeed; store.set("va-speed", speed); $("speed").value = speed; }
      if (serverStyle && serverStyle !== "normal" && serverStyle !== style) { style = serverStyle; store.set("va-style", style); $("style").value = style; }
    } catch { /* backend asleep / offline: keep the old state */ }
  }

  // ── devices: microphone (in), speaker (out), camera ──
  // Browser echo cancellation keeps the agent from hearing (and interrupting) itself.
  const micConstraints = () => Object.assign(
    { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    micId ? { deviceId: { exact: micId } } : {},
  );
  // Chrome, Edge, Firefox 116+, Safari 18.4+ on a computer. Not on a phone: there the system routes a
  // call's sound with its mic (headphones in → headphones), and a pick did nothing (feedback, in headphones:
  // «вивід не міняється на динамік телефону»); WebKit also takes setSinkId only in a tap and replayed
  // the voice sample on every pick. Whether there is anything to pick shows only in the list.
  const canPickSpeaker = !IOS && !ANDROID && "setSinkId" in HTMLMediaElement.prototype;
  if (!canPickSpeaker && speakerId) { speakerId = ""; store.set("va-speaker", ""); } // picked before this
  // False once the names are known and the browser lists no output to pick: the system chooses, and
  // the page says where the sound goes instead of a dropdown that opens on one «За замовчуванням».
  let speakerPickable = canPickSpeaker;
  let devicesNamed = false; // the browser gave the names (the mic was allowed)
  const deviceKeys = { mic: "va-mic", speaker: "va-speaker", camera: "va-camera" };

  function setDevice(kind, id) {
    if (kind === "mic") micId = id;
    else if (kind === "speaker") speakerId = id;
    else cameraId = id;
    store.set(deviceKeys[kind], id);
  }

  // The picked mic, or the default one when it is gone (unplugged headset).
  async function openMic() {
    try {
      return await navigator.mediaDevices.getUserMedia({ audio: micConstraints() });
    } catch (err) {
      if (!micId || !["OverconstrainedError", "NotFoundError"].includes(err && err.name)) throw err;
      addLine("system", "Обраного мікрофона немає — беру той, що за замовчуванням.");
      setDevice("mic", "");
      $("micSelect").value = "";
      return navigator.mediaDevices.getUserMedia({ audio: micConstraints() });
    }
  }

  // Fill the three pickers. Before the mic is allowed the browser hides device names (and ids),
  // so only «За замовчуванням» is offered until then.
  async function listDevices() {
    const md = navigator.mediaDevices;
    if (!md || !md.enumerateDevices) { $("devicesInfo").textContent = "Цей браузер не дає вибрати пристрої."; return; }
    let devices;
    try { devices = await md.enumerateDevices(); } catch { return; }
    const fill = (select, kind, current, noun) => {
      // Chrome's «default» / «communications» entries are the system default again: "" covers them —
      // and its label names the device behind it («MacBook Pro Microphone (за замовчуванням)»).
      const list = devices.filter((d) => d.kind === kind && d.deviceId && d.deviceId !== "default" && d.deviceId !== "communications");
      const def = devices.find((d) => d.kind === kind && d.deviceId === "default" && d.label);
      const defName = def ? def.label.replace(/^(Default|За замовчуванням|По умолчанию)\s*[-–—:]\s*/i, "") : "";
      // The name first: a phone-width picker cuts the end off.
      $(select).replaceChildren(new Option(defName ? defName + " (за замовчуванням)" : "За замовчуванням", ""),
        ...list.map((d, i) => new Option(d.label || noun + " " + (i + 1), d.deviceId)));
      $(select).value = list.some((d) => d.deviceId === current) ? current : "";
      return list.length;
    };
    otherMics = Math.max(0, fill("micSelect", "audioinput", micId, "Мікрофон") - 1);
    const outputs = fill("speakerSelect", "audiooutput", speakerId, "Динамік");
    const cameras = fill("cameraSelect", "videoinput", cameraId, "Камера");
    const named = devices.some((d) => d.label);
    devicesNamed = named;
    speakerPickable = canPickSpeaker && (outputs > 0 || !named);
    // Nothing to pick: words, not a dead dropdown (feedback: «кнопка вибору динаміка не працює»).
    $("speakerSelect").hidden = !speakerPickable;
    $("speakerFixed").hidden = speakerPickable;
    // Names come only once the browser has given the mic: one tap asks for it without starting a call.
    $("devicesAllow").hidden = named || !md.getUserMedia;
    showDevicesNow();
    $("devicesInfo").textContent = [
      named ? "" : "Назви пристроїв з'являться, коли браузер дасть доступ до мікрофона.",
      speakerPickable ? "" : IOS
        ? "На iPhone звук розмови спрямовує система, сторінка його не перемикає: з навушниками він іде в навушники. " +
          "Щоб на динамік телефону — вийміть навушники або вимкніть Bluetooth (чи оберіть iPhone у Пункті керування, значок AirPlay)."
        : ANDROID
          ? "На Android звук розмови спрямовує система, сторінка його не перемикає: з навушниками він іде в навушники. " +
            "Щоб на динамік телефону — вийміть навушники або вимкніть Bluetooth."
          : "Динамік у цьому браузері не вибирається (потрібен Chrome, Edge або Safari 18.4+) — звук іде в системний.",
      named && !cameras ? "Камери не знайдено." : "",
      btMicTip(devicesNow().input),
    ].filter(Boolean).join(" ") || "Зміна діє одразу, навіть посеред розмови.";
  }

  // A device's name without the picker's « (за замовчуванням)».
  const deviceName = (text) => text.replace(/ \(за замовчуванням\)$/, "");
  // Where the sound goes when the page can't pick it. On an iPhone a headset's mic (AirPods) means its
  // speakers too: iOS routes both ends of a call to it.
  function systemOutput(input) {
    if (!IOS) return ANDROID ? "Динамік телефону або навушники" : "Системний вихід";
    if (input && !/iPhone|iPad|вбудован|built-in|^За замовчуванням$/i.test(input)) return input + " (через iPhone)";
    return "Динамік iPhone або навушники";
  }

  // What the call hears from and plays to, in words — the pickers alone said «За замовчуванням».
  // In a call the mic is the track's own device; otherwise what the next call will open.
  function devicesNow() {
    const picked = (id) => ($(id).selectedOptions[0] || {}).textContent || "За замовчуванням";
    const track = mic && mic.getAudioTracks().find((t) => t.readyState === "live");
    const input = deviceName(track && track.label ? track.label : picked("micSelect"));
    const output = speakerPickable ? deviceName(picked("speakerSelect")) : systemOutput(input);
    return { input, output, live: Boolean(track) };
  }
  function showDevicesNow() {
    const now = devicesNow();
    $("devicesNow").textContent = (now.live ? "Зараз: " : "Буде: ") + "мікрофон — " + now.input + "; звук — " + now.output + ".";
    // The same in the call card: on a phone «Пристрої» is a long scroll away.
    $("callDevicesText").textContent = "Мікрофон: " + now.input + " · Звук: " + now.output;
    $("speakerFixed").textContent = now.output;
  }

  // A picker touched before the names are known offers only «За замовчуванням»: the touch asks for the
  // mic instead (the names come with it), then the list opens where the browser can open it.
  function namesFirst(e) {
    if (devicesNamed || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return;
    e.preventDefault();
    const select = e.currentTarget;
    allowDevices().then(() => {
      if (!devicesNamed || select.hidden) return;
      if (document.activeElement === select && e.type === "focus") return; // already open (iPhone)
      try { select.showPicker(); } catch { /* not here or not now: one more tap */ }
    });
  }

  // One ask at a time: a tap on a picker brings both mousedown and focus.
  let allowing = null;
  function allowDevices() {
    if (!allowing) allowing = askForNames().finally(() => { allowing = null; });
    return allowing;
  }
  async function askForNames() {
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      $("devicesInfo").textContent = err && err.name === "NotAllowedError"
        ? "Браузер не дав доступ до мікрофона — дозвольте його в налаштуваннях сайту." : "Не вдалося відкрити мікрофон.";
      return;
    }
    // Listed while the mic is open: Safari may hide the names again once nothing captures.
    try { await listDevices(); } finally { stream.getTracks().forEach((t) => t.stop()); } // the call opens its own
  }

  // Another mic mid-call: a new stream and gate, the call's track replaced in place — no reconnect.
  async function changeMic() {
    setDevice("mic", $("micSelect").value);
    listDevices(); // the hints follow the pick (a Bluetooth headset's mic)
    if (await swapMic()) addLine("system", "Мікрофон: " + devicesNow().input + ".");
    showDevicesNow();
  }

  // The call's mic went dead (iOS: the recogniser or a phone call took it; the track ended or is muted):
  // open it again in place. Nothing to do while it is live.
  async function reviveMic() {
    const track = mic && mic.getAudioTracks()[0];
    if (!track || (track.readyState === "live" && !track.muted)) return;
    diag.micRevives += 1;
    await swapMic();
  }

  // The screen stays on during a call: a phone that locks mid-sentence puts the tab to sleep (iOS 16.4+,
  // Chrome, Edge). Taken again when the page is back — the browser drops it when hidden.
  let wakeLock = null;
  async function holdScreen(on) {
    try {
      if (on && !wakeLock && navigator.wakeLock && document.visibilityState === "visible") {
        wakeLock = await navigator.wakeLock.request("screen");
        wakeLock.addEventListener("release", () => { wakeLock = null; });
      } else if (!on && wakeLock) {
        const lock = wakeLock;
        wakeLock = null;
        await lock.release();
      }
    } catch { /* not allowed now (battery saver, hidden): the call goes on without it */ }
  }

  // Back to the page (unlocked, another app closed, a phone call over): audio running again, the mic alive.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible" || eva.mode === "off") return;
    holdScreen(true);
    if (gate) gate.alive();
    if (eva.mode === "active") reviveMic();
  });

  // iOS mutes the call's mic when something else takes it (a phone call, Siri, another app): counted, and
  // in a call opened again once the page is in front.
  function watchMic(stream) {
    const track = stream.getAudioTracks()[0];
    if (!track) return;
    track.addEventListener("mute", () => {
      diag.micMutes += 1;
      diag.changed = true;
      if (eva.mode === "active" && document.visibilityState === "visible") setTimeout(reviveMic, 500);
    });
  }

  // A new mic stream and gate in place of the old ones; true once swapped.
  async function swapMic() {
    if (!mic) return false; // the next call opens it
    const old = mic;
    let stream;
    try { stream = await openMic(); } catch (err) {
      addLine("system", "Не вдалося відкрити мікрофон: " + ((err && err.message) || err));
      return false;
    }
    const newGate = await createNoiseGate(stream, onGateSignal);
    if (mic !== old) { // the call ended or restarted meanwhile: it opens its own mic
      stream.getTracks().forEach((t) => t.stop());
      if (newGate) newGate.close();
      return false;
    }
    const oldGate = gate;
    watchMic(stream);
    mic = stream;
    gate = newGate;
    if (gate) gate.listenTo($("remote").srcObject || null);
    const track = gate ? gate.track : mic.getAudioTracks()[0];
    track.enabled = !micMuted;
    if (micMuted) mic.getAudioTracks().forEach((t) => { t.enabled = false; }); // muted: the gate hears nothing either
    try { if (micSender) await micSender.replaceTrack(track); } catch { /* call closing */ }
    if (oldGate) oldGate.close();
    old.getTracks().forEach((t) => t.stop());
    if (eva.recognizer) { stopPhraseListener(); startPhraseListener(); } // it follows the mic
    return true;
  }

  async function changeSpeaker() {
    const id = $("speakerSelect").value;
    try {
      await $("remote").setSinkId(id);
    } catch (err) {
      addLine("system", "Браузер не переключив звук на цей динамік (" + ((err && err.name) || err) + ") — лишаю попередній.");
      $("speakerSelect").value = speakerId;
      return;
    }
    setDevice("speaker", id);
    showDevicesNow();
    if (eva.mode !== "off") addLine("system", "Звук: " + devicesNow().output + ".");
    stopPreview(); // the next «Прослухати» plays to the new one
  }

  // Camera: Єва can't see it herself (GPT-Live takes no images). The backend tool look_at_camera hands
  // a frame to the model that runs her tools. The page snaps that frame the moment it sees the tool
  // called (the data channel carries the session's events), so a look shows what is held up now; the
  // tool waits for it. A frame every FRAME_EVERY_MS is the fallback (an upload lost) and feeds the scene
  // line: each carries X-Frame-Change, how much the view changed (0–100), so the server describes it
  // only when it did (server/vision.py). Nothing is sent on a pause or with the camera off.
  const FRAME_EVERY_MS = 1500;
  const FRAME_MAX_SIDE = 768;  // fallback frames: ~400 image tokens
  const LOOK_MAX_SIDE = 1024;  // the look itself: small print stays legible (~600 tokens, ≈$0.0001)
  const FRAME_TIMEOUT_MS = 4000; // a hung upload must not stop the next ones
  let cam = null;
  let frameTimer = null;
  let frameBusy = false;
  let frameCanvas = null;
  let thumbCanvas = null;
  let lastThumb = null; // grey 32×18 of the last frame sent: the change score compares against it
  const snapped = new Set(); // calls already snapped for (output_item.added and .done both come)

  function showCamera() {
    $("cameraInfo").textContent = cam
      ? "Увімкнено: коли попросите «" + callName + ", подивись», кадр буде перевірено. Кадри ніде не зберігаються."
      : "Попросіть «" + callName + ", подивись» — погляд через камеру.";
  }

  async function startCamera() {
    // The side picked under «Камера» (front by default: «подивись на мене»), or a camera from «Пристрої».
    const size = { width: { ideal: 1280 }, height: { ideal: 720 } };
    const video = Object.assign({}, size,
      cameraId ? { deviceId: { exact: cameraId } } : { facingMode: { exact: cameraFacing } });
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ video });
    } catch (err) {
      if (!["OverconstrainedError", "NotFoundError"].includes(err && err.name)) throw err;
      // The picked camera is gone, or there is no such side (a laptop has one camera): whatever there is.
      if (cameraId) setDevice("camera", "");
      stream = await navigator.mediaDevices.getUserMedia({ video: Object.assign({}, size, { facingMode: cameraFacing }) });
    }
    if (!$("camera").checked) { stream.getTracks().forEach((t) => t.stop()); return; } // turned off meanwhile
    if (cam) cam.getTracks().forEach((t) => t.stop());
    cam = stream;
    const track = cam.getVideoTracks()[0];
    const view = $("cameraView");
    view.srcObject = cam;
    view.hidden = false;
    // The preview is mirrored for a front camera, like a mirror; frames go out as the camera sees.
    const facing = (track.getSettings && track.getSettings().facingMode) || (cameraId ? "user" : cameraFacing);
    view.classList.toggle("mirror", facing === "user");
    track.onended = () => { if (cam === stream) { $("camera").checked = false; stopCamera(); addLine("system", "Камеру вимкнено."); } };
    clearInterval(frameTimer);
    frameTimer = setInterval(() => sendFrame(), FRAME_EVERY_MS);
    showCamera();
    listDevices();
    view.onloadeddata = () => sendFrame();
  }

  function stopCamera() {
    clearInterval(frameTimer);
    frameTimer = null;
    if (cam) cam.getTracks().forEach((t) => t.stop());
    cam = null;
    $("cameraView").srcObject = null;
    $("cameraView").hidden = true;
    lastThumb = null;
    showCamera();
    // The server forgets the last frame: Єва says the camera is off instead of describing an old one.
    if (BACKEND) fetch(BACKEND + "/api/frame", { method: "DELETE", headers: headers() }).catch(() => {});
  }

  async function toggleCamera() {
    if (!$("camera").checked) { stopCamera(); return; }
    try {
      await startCamera();
    } catch (err) {
      $("camera").checked = false;
      stopCamera();
      addLine("system", err && err.name === "NotAllowedError" ? "Немає доступу до камери." : "Не вдалося увімкнути камеру.");
    }
  }

  async function changeCamera() {
    setDevice("camera", $("cameraSelect").value);
    showFacing();
    if (cam) await toggleCamera();
  }

  // Фронтальна / Задня: on phones and tablets (a laptop has one camera). Picking a side drops a camera
  // picked in «Пристрої», and a camera that is on switches at once — mid-call too.
  function showFacing() {
    $("cameraFacing").hidden = !(IOS || ANDROID);
    for (const b of $("cameraFacing").querySelectorAll("button")) {
      b.setAttribute("aria-pressed", String(!cameraId && b.dataset.facing === cameraFacing));
    }
  }

  async function pickFacing(facing) {
    const same = facing === cameraFacing && !cameraId;
    cameraFacing = facing;
    store.set("va-camera-facing", facing);
    if (cameraId) { setDevice("camera", ""); $("cameraSelect").value = ""; }
    showFacing();
    if (!cam || same) return;
    // A phone opens one camera at a time: the old one stops first. The server keeps its last frame
    // meanwhile (no «камера вимкнена» for a switch); the next frame is a new view.
    cam.getTracks().forEach((t) => t.stop());
    cam = null; // no frames from the stopped one while the new one opens
    lastThumb = null;
    try {
      await startCamera();
      addLine("system", facing === "environment" ? "Камера: задня." : "Камера: фронтальна.");
    } catch (err) {
      $("camera").checked = false;
      stopCamera();
      addLine("system", err && err.name === "NotAllowedError" ? "Немає доступу до камери." : "Не вдалося перемкнути камеру.");
    }
  }

  let lastSnapAt = 0;
  // callId "" = a request just went to the backend; a call id = the backend calls look_at_camera — then
  // only if nothing was snapped in the last 2 s (the frame from the start of the work is fresh enough).
  function snapForLook(callId) {
    if (!cam || (callId && (snapped.has(callId) || Date.now() - lastSnapAt < 2000))) return;
    if (callId) snapped.add(callId);
    lastSnapAt = Date.now();
    if (snapped.size > 50) snapped.clear();
    // The next fallback frame (smaller) comes a full interval later: it must not replace this one
    // before the tool has taken it.
    clearInterval(frameTimer);
    frameTimer = setInterval(() => sendFrame(), FRAME_EVERY_MS);
    sendFrame(true);
  }

  // A frame to the backend, only while she is in an active call. look: snapped for look_at_camera —
  // sharper, and sent even while a fallback frame is still uploading.
  async function sendFrame(look = false) {
    const view = $("cameraView");
    if (!cam || (frameBusy && !look) || eva.mode !== "active" || !view.videoWidth) return;
    const side = look ? LOOK_MAX_SIDE : FRAME_MAX_SIDE;
    const scale = Math.min(1, side / Math.max(view.videoWidth, view.videoHeight));
    const canvas = look ? document.createElement("canvas") : (frameCanvas = frameCanvas || document.createElement("canvas"));
    canvas.width = Math.round(view.videoWidth * scale);
    canvas.height = Math.round(view.videoHeight * scale);
    canvas.getContext("2d").drawImage(view, 0, 0, canvas.width, canvas.height);
    const change = frameChange(canvas);
    if (!look) frameBusy = true;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), FRAME_TIMEOUT_MS);
    try {
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", look ? 0.85 : 0.7));
      if (blob && cam) {
        await fetch(BACKEND + "/api/frame", {
          method: "POST", body: blob, signal: ctrl.signal,
          headers: Object.assign(headers(), { "Content-Type": "image/jpeg" }, change === null ? {} : { "X-Frame-Change": String(change) }),
        });
      }
    } catch { /* offline or timed out: the next frame tries again */ } finally {
      clearTimeout(timer);
      if (!look) frameBusy = false;
    }
  }

  // How much the view changed since the last frame sent: mean grey difference on a 32×18 copy, 0–100.
  // Cheap (576 pixels), and blind to JPEG noise; null for the first frame.
  function frameChange(source) {
    try {
      thumbCanvas = thumbCanvas || Object.assign(document.createElement("canvas"), { width: 32, height: 18 });
      const ctx = thumbCanvas.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(source, 0, 0, 32, 18);
      const px = ctx.getImageData(0, 0, 32, 18).data;
      const grey = new Uint8Array(32 * 18);
      for (let i = 0; i < grey.length; i++) grey[i] = (px[i * 4] * 3 + px[i * 4 + 1] * 6 + px[i * 4 + 2]) / 10;
      const prev = lastThumb;
      lastThumb = grey;
      if (!prev) return null;
      let sum = 0;
      for (let i = 0; i < grey.length; i++) sum += Math.abs(grey[i] - prev[i]);
      return Math.round((sum / grey.length / 255) * 1000) / 10;
    } catch {
      return null;
    }
  }

  async function init() {
    $("talk").addEventListener("click", () => {
      if (eva.mode !== "off") { stop(); return; }
      if (noMicReason()) { showNoMic(); setStatus("Немає доступу до мікрофона", "err"); postDiag(true); return; }
      if (songs.playing || songs.pending) stopSong(); // a new call is not started over a song
      primeSong(); // so that a song queued behind the conversation can start later without a tap
      meterReset(); // a new call counts from zero
      start(defaultStart()); // asks for the mic synchronously, while the tap still counts
      primeAudio(); // then, in the same tap, the audio the gate will use
    });
    $("pause").addEventListener("click", pauseEva);
    $("micMute").addEventListener("click", () => setMicMuted(!micMuted));
    $("resume").addEventListener("click", () => { wakeEva(""); primeAudio(); }); // a closed call reopens here
    $("bgListen").addEventListener("change", toggleBgListen);
    showBgListen();
    initMusic();
    $("voiceNow").addEventListener("change", toggleVoiceNow);
    showVoiceNow();
    $("newChat").addEventListener("click", newConversation);
    $("nextGuest").addEventListener("click", newConversation);
    $("checkinReset").addEventListener("click", resetCheckins);
    $("modeAssistant").addEventListener("click", () => setMode("assistant"));
    $("modeConcierge").addEventListener("click", () => setMode("concierge"));
    $("checkQuestion").addEventListener("change", setCheckQuestion);
    showCheckQuestion();
    showMode();
    $("copyChat").addEventListener("click", copyConversation);
    $("voice").addEventListener("change", () => { stopPreview(); showVoice(true); });
    $("voiceVariant").addEventListener("change", applyVoiceVariant);
    $("voicePreview").addEventListener("click", togglePreview);
    $("voiceApply").addEventListener("click", applyVoice);
    $("speed").value = speed;
    $("style").value = style;
    $("speed").addEventListener("change", applyDelivery);
    $("style").addEventListener("change", applyDelivery);
    showVolume();
    $("volume").addEventListener("input", (e) => {
      volume = Math.min(1, Math.max(0, Number(e.target.value) / 100));
      store.set("va-volume", String(volume));
      if (!IOS) songs.audio.volume = volume;
      showVolume();
      setRemoteVolume(bargeLevel());
      if (preview) preview.volume = volume;
    });
    $("micSelect").addEventListener("change", changeMic);
    $("speakerSelect").addEventListener("change", changeSpeaker);
    $("cameraSelect").addEventListener("change", changeCamera);
    $("knowledgeLink").href = "knowledge.html" + location.search; // keep ?backend=… for a test backend
    $("historyLink").href = "history.html" + location.search;
    $("settingsLink").href = "settings.html" + location.search;
    $("devicesAllow").addEventListener("click", allowDevices);
    for (const id of ["micSelect", "speakerSelect", "cameraSelect"]) {
      $(id).addEventListener("mousedown", namesFirst); // the empty list doesn't open
      $(id).addEventListener("focus", namesFirst); // iPhone: its own picker opens anyway; asked at once
    }
    $("camera").addEventListener("change", toggleCamera);
    for (const b of $("cameraFacing").querySelectorAll("button")) {
      b.addEventListener("click", () => pickFacing(b.dataset.facing));
    }
    showFacing();
    if (navigator.mediaDevices && typeof navigator.mediaDevices.addEventListener === "function") {
      // A headset plugged in or out: the pickers follow.
      navigator.mediaDevices.addEventListener("devicechange", listDevices);
      listDevices();
    } else {
      $("camera").disabled = true;
    }
    // The picked speaker from the last visit (gone → the system default).
    if (canPickSpeaker && speakerId) {
      $("remote").setSinkId(speakerId).catch((err) => {
        if (err && err.name === "NotAllowedError") return; // Safari: only in a tap — the choice stays
        setDevice("speaker", ""); listDevices(); // gone
      });
    }
    updateButtons();
    // Closing or leaving the tab ends the paid Live session instead of leaving it open.
    window.addEventListener("pagehide", () => { if (eva.mode !== "off") stop(); });
    if (!BACKEND) { addLine("system", "Не вказано адресу бекенду."); return; }
    const micMissing = noMicReason();
    if (micMissing) addLine("system", micMissing); // before the backend wakes: that can take a minute
    if (micMissing) showNoMic();
    const cfg = await wakeBackend();
    if (micMissing) setStatus("Немає доступу до мікрофона", "err");
    if (cfg) {
      await loadVoices();
      refreshMe();
      loadConversation();
      if (mode === "concierge") checkGuestList(); // the backend is awake now
    }
    // Free Render sleeps after ~15 min without requests and forgets every conversation;
    // while the page is open and visible, keep it awake.
    setInterval(() => {
      if (document.visibilityState === "visible") fetch(BACKEND + "/healthz").catch(() => {});
    }, 10 * 60 * 1000);
  }

  // A sleeping free-tier backend takes up to a minute to start: wait for it instead of failing.
  async function wakeBackend() {
    const deadline = Date.now() + 90 * 1000;
    let notified = false;
    while (Date.now() < deadline) {
      try {
        const res = await fetch(BACKEND + "/healthz");
        if (res.ok) {
          if (notified) setStatus("Не підключено");
          $("talk").disabled = false;
          return true;
        }
      } catch { /* still waking up */ }
      if (!notified) {
        notified = true;
        $("talk").disabled = true;
        setStatus("Сервер прокидається — до хвилини…", "busy");
      }
      await new Promise((resolve) => setTimeout(resolve, 3000));
    }
    $("talk").disabled = false;
    setStatus("Сервер не відповідає", "err");
    addLine("system", "Бекенд не відповідає. Оновіть сторінку трохи згодом.");
    return null;
  }

  init();
})();
