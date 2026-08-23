/* Eremia-specific presentation only. Relay/API contracts stay untouched. */
(function () {
  "use strict";

  const SYSTEM_PREFIX = /^\[系统\]\s*/;
  const WAKE_PREFIX = /^\[论坛唤醒(?:\s+[^\]]+)?\]\s*/;
  const HUMAN_AVATAR_KEY = "companion_human_avatar";
  const DEFAULT_HUMAN_AVATAR = "icon-192.webp";

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

  function decorateSystemRow(row) {
    if (!row || row.dataset.eremiaDecorated === "1") return;
    const textNode = row.querySelector(".bubble .txt");
    if (!textNode) return;

    const raw = (textNode.textContent || "").trim();
    let kind = "";
    let visible = raw;
    if (WAKE_PREFIX.test(raw)) {
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
    row.classList.add("system-event", kind === "wake" ? "system-wake" : "system-notice");
    textNode.textContent = visible || (kind === "wake" ? "论坛传来新的唤醒信号。" : "通道状态发生变化。");
  }

  function decorateVisibleRows(root) {
    if (!root) return;
    if (root.matches && root.matches(".row")) decorateSystemRow(root);
    root.querySelectorAll && root.querySelectorAll(".row").forEach(decorateSystemRow);
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
