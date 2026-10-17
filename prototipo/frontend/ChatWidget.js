/* Chat flutuante do assistente hídrico: bolha no canto inferior direito + janela que conversa com POST /api/chat. */
(function () {
  "use strict";

  const history = [];   // [{role, content}] enviado a cada pergunta
  let busy = false;

  const ICON_CHAT = '<svg viewBox="0 0 24 24" width="26" height="26" aria-hidden="true"><path fill="currentColor" d="M4 4h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-5 4v-4H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Zm3 6.5a1.5 1.5 0 1 0 0 .01Zm5 0a1.5 1.5 0 1 0 0 .01Zm5 0a1.5 1.5 0 1 0 0 .01Z"/></svg>';
  const ICON_CLOSE = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path fill="currentColor" d="M6.4 5 12 10.6 17.6 5 19 6.4 13.4 12l5.6 5.6-1.4 1.4-5.6-5.6L6.4 19 5 17.6l5.6-5.6L5 6.4Z"/></svg>';
  const ICON_SEND = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path fill="currentColor" d="M3 20.5V14l9-2-9-2V3.5L22 12Z"/></svg>';

  function el(tag, cls, attrs) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    for (const k in attrs || {}) n.setAttribute(k, attrs[k]);
    return n;
  }

  // texto com URLs clicáveis, sem innerHTML com conteúdo vindo do modelo
  function linkify(node, text) {
    const re = /https?:\/\/[^\s<>()"']+[^\s<>()"'.,;:!?]/g;
    let last = 0, m;
    while ((m = re.exec(text))) {
      node.appendChild(document.createTextNode(text.slice(last, m.index)));
      const a = el("a", null, { href: m[0], target: "_blank", rel: "noopener noreferrer" });
      a.textContent = m[0];
      node.appendChild(a);
      last = m.index + m[0].length;
    }
    node.appendChild(document.createTextNode(text.slice(last)));
  }

  const bubble = el("button", "cw-bubble", { type: "button", "aria-label": "Abrir assistente hídrico",
                                             "aria-expanded": "false", "aria-controls": "cw-panel" });
  bubble.innerHTML = ICON_CHAT;

  const panel = el("section", "cw-panel", { id: "cw-panel", role: "dialog", "aria-label": "Assistente hídrico" });
  panel.hidden = true;
  panel.innerHTML =
    '<header class="cw-head"><div><strong>Assistente hídrico</strong>' +
    '<span>Clima, hidrogeologia e legislação do Congo, com fontes da web</span></div>' +
    '<button type="button" class="cw-close" aria-label="Fechar">' + ICON_CLOSE + "</button></header>" +
    '<div class="cw-msgs" aria-live="polite"></div>' +
    '<form class="cw-form"><textarea rows="1" placeholder="Escreva sua pergunta…" aria-label="Mensagem" maxlength="4000"></textarea>' +
    '<button type="submit" class="cw-send" aria-label="Enviar">' + ICON_SEND + "</button></form>";

  const msgs = panel.querySelector(".cw-msgs");
  const form = panel.querySelector(".cw-form");
  const input = panel.querySelector("textarea");
  const send = panel.querySelector(".cw-send");

  function addMsg(role, text, sources) {
    const m = el("div", "cw-msg cw-" + role);
    const body = el("div", "cw-text");
    linkify(body, text);
    m.appendChild(body);
    if (sources && sources.length) {
      const list = el("ol", "cw-sources");
      for (const s of sources) {
        const li = el("li");
        const a = el("a", null, { href: s.url, target: "_blank", rel: "noopener noreferrer", title: s.url });
        a.textContent = s.title || s.url;
        li.appendChild(a);
        list.appendChild(li);
      }
      const cap = el("div", "cw-sources-cap");
      cap.textContent = "Fontes";
      m.append(cap, list);
    }
    msgs.appendChild(m);
    msgs.scrollTop = msgs.scrollHeight;
  }

  function typing(on) {
    let t = msgs.querySelector(".cw-typing");
    if (on && !t) {
      t = el("div", "cw-msg cw-assistant cw-typing", { "aria-label": "Pesquisando" });
      t.innerHTML = "<span></span><span></span><span></span>";
      msgs.appendChild(t);
      msgs.scrollTop = msgs.scrollHeight;
    } else if (!on && t) t.remove();
  }

  function setBusy(b) {
    busy = b;
    send.disabled = b;
    input.disabled = b;
    typing(b);
  }

  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 120) + "px";
  }

  async function ask(text) {
    addMsg("user", text);
    setBusy(true);
    try {
      const r = await fetch("api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, history }),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      history.push({ role: "user", content: text }, { role: "assistant", content: data.answer });
      addMsg("assistant", data.answer, data.sources);
    } catch (e) {
      addMsg("error", "Não foi possível falar com o assistente (" + e.message + "). Tente de novo.");
    } finally {
      setBusy(false);
      input.focus();
    }
  }

  function toggle(open) {
    panel.hidden = !open;
    bubble.setAttribute("aria-expanded", String(open));
    bubble.classList.toggle("cw-open", open);
    if (open) {
      if (!msgs.childElementCount)
        addMsg("assistant", "Olá! Pergunte sobre chuvas, aquíferos, poços ou legislação da água na República do Congo. Eu pesquiso na web e cito as fontes.");
      input.focus();
    } else bubble.focus();
  }

  bubble.addEventListener("click", () => toggle(panel.hidden));
  panel.querySelector(".cw-close").addEventListener("click", () => toggle(false));
  panel.addEventListener("keydown", (e) => { if (e.key === "Escape") toggle(false); });
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text || busy) return;
    input.value = "";
    autosize();
    ask(text);
  });

  document.body.append(panel, bubble);
})();
