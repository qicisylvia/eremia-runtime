/* Eremia-specific presentation only. Relay/API contracts stay untouched. */
(function () {
  "use strict";

  const SYSTEM_PREFIX = /^\[系统\]\s*/;
  const WAKE_PREFIX = /^\[论坛唤醒(?:\s+[^\]]+)?\]\s*/;
  const TIME_WAKE_PREFIX = /^\[时间唤醒\s+(check-in|night)\s+[^\]]+\]\s*/;
  const HUMAN_AVATAR_KEY = "companion_human_avatar";
  const DEFAULT_HUMAN_AVATAR = "icon-192.webp";

  /* ---- Eremia 的声音（ElevenLabs 边车在 /tts，key 只待在服务端） ---- */
  const TTS_ENDPOINT = "/tts/speak";
  const TTS_AUTOPLAY_KEY = "eremia_tts_autoplay";
  const SECRET_STORAGE_KEY = "companion_secret";
  // 文本停止变动多久才算"这条说完了"，用于自动朗读（防流式更新时念半句）。
  const TTS_SETTLE_MS = 1200;
  // 页面刚打开时灌进来的历史消息不该突然开口。
  const TTS_HISTORY_GRACE_MS = 2500;
  const TTS_URL_CACHE_MAX = 20;

  function toast(message) {
    if (typeof window.showToast === "function") window.showToast(message);
  }

  function applyHumanAvatar(url) {
    const value = url ? `url("${url}")` : `url("${DEFAULT_HUMAN_AVATAR}")`;
    document.documentElement.style.setProperty("--human-avatar-default", value);
  }

  function prepareAvatar(file) {
    return new Promise((resolve, reject) => {
      if (!file || !String(file.type || "").startsWith("image/")) {
        reject(new Error("请选择图片文件"));
        return;
      }
      const reader = new FileReader();
      reader.onerror = () => reject(new Error("图片读取失败"));
      reader.onload = () => {
        const image = new Image();
        image.onerror = () => reject(new Error("图片无法打开"));
        image.onload = () => {
          const edge = Math.min(image.naturalWidth || image.width, image.naturalHeight || image.height);
          if (!edge) {
            reject(new Error("图片尺寸无效"));
            return;
          }
          const size = Math.min(512, edge);
          const canvas = document.createElement("canvas");
          canvas.width = size;
          canvas.height = size;
          const context = canvas.getContext("2d");
          const sx = ((image.naturalWidth || image.width) - edge) / 2;
          const sy = ((image.naturalHeight || image.height) - edge) / 2;
          context.drawImage(image, sx, sy, edge, edge, 0, 0, size, size);
          resolve(canvas.toDataURL("image/webp", .88));
        };
        image.src = String(reader.result || "");
      };
      reader.readAsDataURL(file);
    });
  }

  function installHumanAvatarEditor() {
    const identity = document.querySelector(".profile-identity");
    if (!identity || document.getElementById("humanAvatarCard")) return;

    const card = document.createElement("section");
    card.className = "settings-card human-avatar-card";
    card.id = "humanAvatarCard";
    card.innerHTML = `
      <button class="human-avatar-preview" id="humanAvatarPreview" type="button" aria-label="更换栖瓷的头像"></button>
      <div class="human-avatar-copy">
        <div class="human-avatar-name">栖瓷的头像</div>
        <div class="human-avatar-note">只保存在这台设备；Eremia 看不到。</div>
      </div>
      <div class="human-avatar-actions">
        <button class="human-avatar-change" id="humanAvatarChange" type="button">更换</button>
        <button class="human-avatar-reset" id="humanAvatarReset" type="button">恢复默认</button>
      </div>`;
    identity.insertAdjacentElement("afterend", card);

    const input = document.createElement("input");
    input.type = "file";
    input.id = "humanAvatarInput";
    input.accept = "image/*";
    input.className = "hidden";
    card.appendChild(input);

    const openPicker = () => input.click();
    card.querySelector("#humanAvatarPreview")?.addEventListener("click", openPicker);
    card.querySelector("#humanAvatarChange")?.addEventListener("click", openPicker);
    card.querySelector("#humanAvatarReset")?.addEventListener("click", () => {
      try { localStorage.removeItem(HUMAN_AVATAR_KEY); } catch (_) {}
      applyHumanAvatar("");
      toast("已恢复栖瓷的默认头像");
    });
    input.addEventListener("change", async () => {
      const file = input.files && input.files[0];
      input.value = "";
      if (!file) return;
      try {
        const dataUrl = await prepareAvatar(file);
        localStorage.setItem(HUMAN_AVATAR_KEY, dataUrl);
        applyHumanAvatar(dataUrl);
        toast("栖瓷的头像已换好");
      } catch (error) {
        toast(error && error.message ? error.message : "头像更换失败");
      }
    });
  }

  let savedHumanAvatar = "";
  try { savedHumanAvatar = localStorage.getItem(HUMAN_AVATAR_KEY) || ""; } catch (_) {}
  applyHumanAvatar(savedHumanAvatar);
  installHumanAvatarEditor();

  const sessionButton = document.getElementById("sessionBtn");
  const peerPill = document.querySelector(".peerpill");
  if (sessionButton && peerPill && sessionButton.parentElement?.classList.contains("header-actions")) {
    peerPill.insertAdjacentElement("beforebegin", sessionButton);
    sessionButton.classList.add("session-title-left");
  }

  function decorateSystemRow(row) {
    if (!row || row.dataset.eremiaDecorated === "1") return;
    const textNode = row.querySelector(".bubble .txt");
    if (!textNode) return;

    const raw = (textNode.textContent || "").trim();
    let kind = "";
    let visible = raw;
    const timeWake = raw.match(TIME_WAKE_PREFIX);
    if (timeWake) {
      kind = "time";
      row.dataset.timeWakeKind = timeWake[1];
      visible = timeWake[1] === "night"
        ? "凌晨巡夜：Eremia 醒来看看小窝、记忆或论坛。"
        : "离席心跳：Eremia 醒来看看瓷瓷有没有回来。";
    } else if (WAKE_PREFIX.test(raw)) {
      kind = "wake";
      visible = raw.replace(WAKE_PREFIX, "");
    } else if (SYSTEM_PREFIX.test(raw)) {
      kind = "system";
      visible = raw.replace(SYSTEM_PREFIX, "");
    }
    if (!kind) return;

    row.dataset.eremiaDecorated = "1";
    row.dataset.eventKind = kind;
    row.classList.remove("human", "ai", "grouped", "tail", "has-reactions");
    row.classList.add(
      "system-event",
      kind === "wake" ? "system-wake" : (kind === "time" ? "system-time" : "system-notice"),
    );
    textNode.textContent = visible || (kind === "wake" ? "论坛传来新的唤醒信号。" : "通道状态发生变化。");
  }

  const startedAt = Date.now();
  let audioUnlocked = false;
  let unlockHintShown = false;
  let currentRow = null;
  const audio = new Audio();
  const urlCache = new Map();

  function autoplayEnabled() {
    try {
      return localStorage.getItem(TTS_AUTOPLAY_KEY) === "1";
    } catch (error) {
      return false;
    }
  }

  function setAutoplay(on) {
    try {
      localStorage.setItem(TTS_AUTOPLAY_KEY, on ? "1" : "0");
    } catch (error) {
      toast("这台设备不让我记住这个开关");
    }
  }

  // 网页端本来就握着一个凭证在调 relay；朗读接口复用它，这样 /tts 不是裸着挂在公网上。
  // 取不到就不发这个头：边车没配 TTS_SHARED_SECRET 时照样能用（只受日额度约束）。
  // 上游自己就是 `localStorage.getItem(LS_KEY)`，而 LS_KEY === "companion_secret"。
  // 只读这一个键：模糊匹配有可能捞到别的 token 发出去，换来一串看不懂的 401。
  // customize.py 在构建时会断言上游没改这个名字，所以这里可以写死。
  // 读不到就不发头（没登录，或隐私模式）；边车那边还有 cookie 一路可走。
  function readSecret() {
    try {
      return localStorage.getItem(SECRET_STORAGE_KEY) || "";
    } catch (error) {
      return "";   // 隐私模式下访问 storage 本身就会抛
    }
  }

  function speakableText(row) {
    const textNode = row.querySelector(".bubble .txt");
    const raw = (textNode && textNode.textContent ? textNode.textContent : "").trim();
    return raw
      .replace(TIME_WAKE_PREFIX, "")
      .replace(WAKE_PREFIX, "")
      .replace(SYSTEM_PREFIX, "")
      .trim();
  }

  function forgetUrl(key) {
    const stale = urlCache.get(key);
    if (!stale) return;
    urlCache.delete(key);
    try { URL.revokeObjectURL(stale); } catch (error) { /* 已经失效了 */ }
  }

  function rememberUrl(key, url) {
    urlCache.set(key, url);
    while (urlCache.size > TTS_URL_CACHE_MAX) {
      const oldest = urlCache.keys().next().value;
      const stale = urlCache.get(oldest);
      urlCache.delete(oldest);
      try { URL.revokeObjectURL(stale); } catch (error) { /* 已经失效了 */ }
    }
  }

  async function fetchVoice(text) {
    const headers = { "Content-Type": "application/json" };
    const secret = readSecret();
    // 和网页端调 relay 用的是同一种方言（Authorization: Bearer），边车两种都认。
    if (secret) headers["Authorization"] = "Bearer " + secret;
    // POST：文本不进 URL，也不会被 service worker 当静态资源缓存。
    const response = await fetch(TTS_ENDPOINT, {
      method: "POST",
      headers: headers,
      body: JSON.stringify({ text: text }),
    });
    if (!response.ok) {
      let message = "朗读失败了";
      try {
        const payload = await response.json();
        if (payload && payload.message) message = payload.message;
      } catch (error) { /* 不是 JSON 就用默认文案 */ }
      const failure = new Error(message);
      failure.friendly = true;   // 我们自己的文案，可以直接给瓷瓷看
      throw failure;
    }
    if (response.headers.get("X-TTS-Truncated") === "1") {
      toast("这条太长，只念了前面一段");
    }
    return URL.createObjectURL(await response.blob());
  }

  function resetRow(row) {
    if (!row) return;
    row.classList.remove("voice-loading", "voice-playing");
  }

  audio.addEventListener("ended", () => { resetRow(currentRow); currentRow = null; });
  audio.addEventListener("error", () => { resetRow(currentRow); currentRow = null; });

  async function speakRow(row, byHand) {
    const text = speakableText(row);
    if (!text) {
      if (byHand) toast("这条没有可朗读的内容");
      return;
    }
    if (currentRow === row && !audio.paused) {
      audio.pause();
      resetRow(row);
      currentRow = null;
      return;
    }
    audio.pause();
    resetRow(currentRow);
    currentRow = row;
    row.classList.add("voice-loading");
    try {
      let url = urlCache.get(text);
      if (!url) {
        url = await fetchVoice(text);
        rememberUrl(text, url);
      }
      audio.src = url;
      await audio.play();
      row.classList.remove("voice-loading");
      row.classList.add("voice-playing");
      if (byHand) audioUnlocked = true;
    } catch (error) {
      resetRow(row);
      currentRow = null;
      const blocked = error && (error.name === "NotAllowedError" || error.name === "AbortError");
      // 播不出来的 URL 不能留在缓存里，否则再点只会一直失败到刷新为止。
      // （被浏览器拦下的那种例外：音频本身是好的，手点一下就能播。）
      if (!blocked) forgetUrl(text);
      if (blocked && !byHand) {
        // iOS Safari 不允许非用户手势触发播放：第一声必须手点。
        if (!unlockHintShown) {
          unlockHintShown = true;
          toast("手机上先手动点一次喇叭，之后这次打开就能自动念了");
        }
      } else if (byHand || !blocked) {
        // 浏览器自己的报错是英文的内部文案（解码失败、来源不支持之类），
        // 不该原样糊到屏幕上。
        toast(error && error.friendly ? error.message : "这段音频没能播出来");
      }
    }
  }

  function attachVoice(row) {
    if (!row || row.dataset.voiceReady === "1") return;
    if (row.dataset.eventKind) return;               // 系统/唤醒消息不念
    if (!row.classList.contains("ai")) return;       // 只念 Eremia 说的话
    const bubble = row.querySelector(".bubble");
    if (!bubble) return;

    row.dataset.voiceReady = "1";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "voice-btn";
    button.setAttribute("aria-label", "朗读这条");
    button.textContent = "🔊";
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      speakRow(row, true);
    });
    bubble.appendChild(button);

    if (!autoplayEnabled()) return;
    if (Date.now() - startedAt < TTS_HISTORY_GRACE_MS) return;   // 这是历史回填
    if (!audioUnlocked && unlockHintShown) return;

    // 等文本静默下来再念，省得流式更新时念半句；只念容器里最后那条。
    let timer = null;
    const watcher = new MutationObserver(() => { settle(); });
    function settle() {
      clearTimeout(timer);
      timer = setTimeout(() => {
        watcher.disconnect();
        if (row.parentElement && row === row.parentElement.lastElementChild) {
          speakRow(row, false);
        }
      }, TTS_SETTLE_MS);
    }
    watcher.observe(row, { childList: true, subtree: true, characterData: true });
    settle();
  }

  function installVoiceSettings() {
    const identity = document.querySelector(".profile-identity");
    if (!identity || document.getElementById("voiceCard")) return;

    const card = document.createElement("section");
    card.className = "settings-card voice-card";
    card.id = "voiceCard";
    card.innerHTML = [
      '<div class="voice-copy">',
      '  <div class="voice-name">自动朗读 Eremia 的话</div>',
      '  <div class="voice-note">只保存在这台设备。关掉也随时能点气泡上的喇叭。</div>',
      '</div>',
      '<button class="voice-toggle" id="voiceToggle" type="button" role="switch"></button>',
    ].join("");
    identity.insertAdjacentElement("afterend", card);

    const toggle = card.querySelector("#voiceToggle");
    const paint = () => {
      const on = autoplayEnabled();
      toggle.setAttribute("aria-checked", on ? "true" : "false");
      toggle.classList.toggle("on", on);
      toggle.textContent = on ? "已开启" : "已关闭";
    };
    toggle.addEventListener("click", () => {
      setAutoplay(!autoplayEnabled());
      paint();
    });
    paint();
  }

  installVoiceSettings();

  function decorateVisibleRows(root) {
    if (!root) return;
    const handle = (row) => { decorateSystemRow(row); attachVoice(row); };
    if (root.matches && root.matches(".row")) handle(root);
    root.querySelectorAll && root.querySelectorAll(".row").forEach(handle);
  }

  const scroll = document.getElementById("scroll");
  if (scroll) {
    decorateVisibleRows(scroll);
    new MutationObserver((records) => {
      for (const record of records) {
        record.addedNodes.forEach((node) => {
          if (node.nodeType === Node.ELEMENT_NODE) decorateVisibleRows(node);
        });
      }
    }).observe(scroll, { childList: true, subtree: true });
  }

  const menu = document.getElementById("menuPanel");
  if (menu) {
    menu.addEventListener("click", (event) => {
      const item = event.target.closest(".menu-item");
      if (!item || item.dataset.menu === "chat" || item.dataset.menu === "album") return;
      event.stopImmediatePropagation();
      const name = item.querySelector(".menu-name")?.textContent?.trim() || "这扇门";
      toast(`${name} 的入口先留在这里，等以后接通。`);
    }, true);
  }

  function syncThemeColor() {
    const dark = document.documentElement.getAttribute("data-theme") === "harbor";
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", dark ? "#151A13" : "#E6E5DA");
  }
  syncThemeColor();
  new MutationObserver(syncThemeColor).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
})();
