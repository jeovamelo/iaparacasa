/* ==============================================================================
   iaparacasa - Frontend Client Logic (Vanilla JS, Zero Dependencies)
   Layout: Tiles Quadrados por Canal (eWeLink Web) + Voz / Áudio (Speech API)
   ============================================================================== */

let currentAcTemp = 23;
let cachedDevices = [];
let toastTimeout = null;
let currentSearchFilter = "";

// Controle de Voz / Áudio
let speechRecognition = null;
let isRecordingVoice = false;
let voiceOutputEnabled = true;

document.addEventListener("DOMContentLoaded", () => {
  updateDashboard();
  loadPreferences();
  setInterval(updateDashboard, 5000);
});

// Toast Notificação Flutuante Discreta
function toast(message) {
  const t = document.getElementById("toast-popup");
  if (!t) return;
  t.innerText = message;
  t.style.display = "block";
  if (toastTimeout) clearTimeout(toastTimeout);
  toastTimeout = setTimeout(() => {
    t.style.display = "none";
  }, 2400);
}

// Filtro de pesquisa rápida de interruptores
function filterDevices(term) {
  currentSearchFilter = (term || "").toLowerCase().trim();
  renderSwitchTiles(cachedDevices);
}

// ==============================================================================
// 1. Atualização e Renderização dos Interruptores em Quadrados (Tiles)
// ==============================================================================
async function updateDashboard() {
  try {
    // 1. Health & Status
    const healthRes = await fetch("/health");
    if (healthRes.ok) {
      const h = await healthRes.json();
      const eng = document.getElementById("badge-engine");
      const ram = document.getElementById("badge-ram");
      if (eng) eng.innerText = `🧠 ${h.mock_mode ? 'Mock Heurístico' : 'Qwen 2.5 1.5B'}`;
      if (ram) ram.innerText = `💾 RAM: ${h.ram_usage_mb} MB`;
    }

    // 2. Status MQTT
    const mqttRes = await fetch("/mqtt/status");
    if (mqttRes.ok) {
      const m = await mqttRes.json();
      const mqttBadge = document.getElementById("badge-mqtt");
      if (mqttBadge) {
        mqttBadge.innerText = m.connected ? "📡 MQTT: Online" : "📡 MQTT: Conectando...";
      }
    }

    // 3. Carregar e Renderizar Dispositivos
    const devsRes = await fetch("/ewelink/devices");
    if (devsRes.ok) {
      const devices = await devsRes.json();
      cachedDevices = devices;
      renderSwitchTiles(devices);
    }
  } catch (err) {
    console.warn("Falha ao atualizar dashboard:", err);
  }
}

function renderSwitchTiles(devices) {
  const container = document.getElementById("switches-container");
  if (!container) return;

  const visibleDevices = devices.filter(d => d.device_type !== "sensor");

  // Nomes amigáveis para canais específicos
  const channelNames = {
    "chaves_de_seguranca": {
      0: "Chaves Seg. (Canal 1)",
      1: "Chaves Seg. (Canal 2)",
      2: "Portão Garagem (Canal 3)",
      3: "Chaves Seg. (Canal 4)"
    },
    "casa_de_maquinas": {
      0: "Casa Máquinas (Canal 1)",
      1: "Casa Máquinas (Canal 2)",
      2: "Casa Máquinas (Canal 3)",
      3: "Casa Máquinas (Canal 4)"
    }
  };

  // Contadores para o topo
  let onCount = 0;
  let offCount = 0;

  visibleDevices.forEach(d => {
    const st = d.last_state || {};
    if (d.is_multichannel && Array.isArray(st.switches)) {
      st.switches.forEach(s => {
        if (s.switch === "on") onCount++;
        else offCount++;
      });
    } else {
      if (st.switch === "on") onCount++;
      else offCount++;
    }
  });

  const onPill = document.getElementById("txt-on-count");
  const offPill = document.getElementById("txt-off-count");
  if (onPill) onPill.innerText = `${onCount} LIGADO`;
  if (offPill) offPill.innerText = `${offCount} Desligados`;

  // Mapeamento em Grupos (Externa, Controle & Segurança, Casa)
  const groups = {
    "Externa": [
      "jardim_deck", "corredor_externo", "jardim_varanda",
      "jardim_garagem", "jardim_principal", "refletor_da_entrada", "luz_da_frente"
    ],
    "Controle": [
      "torneira", "fonte", "chaves_de_seguranca", "casa_de_maquinas", "portao_garagem"
    ],
    "Casa": [
      "corredor_cima", "varanda_superior", "luz_sala", "ar_condicionado_sala"
    ]
  };

  const devMap = {};
  visibleDevices.forEach(d => { devMap[d.device_id] = d; });

  // Adicionar grupo "Outros" se houver algum interruptor não mapeado nos grupos fixos
  const mappedIds = new Set(Object.values(groups).flat());
  const unmappedDevs = visibleDevices.filter(d => !mappedIds.has(d.device_id));
  if (unmappedDevs.length > 0) {
    groups["Outros"] = unmappedDevs.map(d => d.device_id);
  }

  let html = "";

  for (const [groupTitle, devIds] of Object.entries(groups)) {
    const groupItems = [];
    for (const id of devIds) {
      if (devMap[id]) groupItems.push(devMap[id]);
    }

    if (groupItems.length === 0) continue;

    let groupHtml = "";

    for (const dev of groupItems) {
      const state = dev.last_state || {};
      const isMulti = Boolean(dev.is_multichannel || (Array.isArray(state.switches) && state.switches.length > 1));

      // Se for dispositivo de 4 canais, renderiza 4 quadrados independentes!
      if (isMulti && Array.isArray(state.switches) && state.switches.length > 0) {
        state.switches.forEach(sw => {
          const outlet = sw.outlet;
          const isOutletOn = sw.switch === "on";
          const customName = (channelNames[dev.device_id] && channelNames[dev.device_id][outlet])
            ? channelNames[dev.device_id][outlet]
            : `${dev.name} (Canal ${outlet + 1})`;

          const isGateChannel = (dev.device_id === "chaves_de_seguranca" && outlet === 2);

          // Filtro de busca
          if (currentSearchFilter &&
              !customName.toLowerCase().includes(currentSearchFilter) &&
              !dev.device_id.toLowerCase().includes(currentSearchFilter)) {
            return;
          }

          let tileClass = `switch-tile ${isOutletOn ? "is-on" : "is-off"} ${isGateChannel ? "is-gate" : ""} ${dev.online ? "is-online" : "is-offline"}`;
          let wifiSvg = `<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M12 4C7.31 4 3.07 5.9 0 8.98L12 21 24 8.98C20.93 5.9 16.69 4 12 4zm0 3.3c3.81 0 7.27 1.48 9.87 3.93L12 18.9 2.13 11.23C4.73 8.78 8.19 7.3 12 7.3z"/></svg>`;

          let iconSvg = isGateChannel
            ? `<svg viewBox="0 0 24 24" width="26" height="26" fill="currentColor"><path d="M19 19V5c0-1.1-.9-2-2-2H7c-1.1 0-2 .9-2 2v14H3v2h18v-2h-2zm-8-6H9v-2h2v2z"/></svg>`
            : `<svg viewBox="0 0 24 24" width="26" height="26" fill="currentColor"><path d="M16 7V3h-2v4h-4V3H8v4c-1.1 0-2 .9-2 2v6c0 3.31 2.69 6 6 6s6-2.69 6-6V9c0-1.1-.9-2-2-2zm0 8c0 2.21-1.79 4-4 4s-4-1.79-4-4V9h8v6z"/></svg>`;

          let stateText = isGateChannel
            ? `<button class="btn-gate-pulse" onclick="event.stopPropagation(); triggerGatePulse('${dev.device_id}', this)">⚡ Pulso 1s</button>`
            : `<span class="switch-tile-state-label">${isOutletOn ? "Ligar" : "Desligar"}</span>`;

          let clickHandler = isGateChannel
            ? `triggerGatePulse('${dev.device_id}', this)`
            : `toggleSwitchChannel('${dev.device_id}', ${outlet}, ${isOutletOn})`;

          groupHtml += `
            <div class="${tileClass}" onclick="${clickHandler}" title="Clique para alternar ${escapeHtml(customName)}">
              <div class="switch-tile-wifi">${wifiSvg}</div>
              <div class="switch-tile-icon-bubble">${iconSvg}</div>
              <div class="switch-tile-name">${escapeHtml(customName)}</div>
              <div>${stateText}</div>
            </div>
          `;
        });
        continue;
      }

      // Dispositivos de 1 canal padrão
      const isOn = state.switch === "on";
      const isGate = dev.device_type === "gate_pulse" || dev.device_id === "portao_garagem";

      // Filtro de busca
      if (currentSearchFilter &&
          !(dev.name || "").toLowerCase().includes(currentSearchFilter) &&
          !dev.device_id.toLowerCase().includes(currentSearchFilter)) {
        return;
      }

      let tileClass = `switch-tile ${isOn ? "is-on" : "is-off"} ${isGate ? "is-gate" : ""} ${dev.online ? "is-online" : "is-offline"}`;
      let wifiSvg = `<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M12 4C7.31 4 3.07 5.9 0 8.98L12 21 24 8.98C20.93 5.9 16.69 4 12 4zm0 3.3c3.81 0 7.27 1.48 9.87 3.93L12 18.9 2.13 11.23C4.73 8.78 8.19 7.3 12 7.3z"/></svg>`;

      let iconSvg = isGate
        ? `<svg viewBox="0 0 24 24" width="26" height="26" fill="currentColor"><path d="M19 19V5c0-1.1-.9-2-2-2H7c-1.1 0-2 .9-2 2v14H3v2h18v-2h-2zm-8-6H9v-2h2v2z"/></svg>`
        : `<svg viewBox="0 0 24 24" width="26" height="26" fill="currentColor"><path d="M16 7V3h-2v4h-4V3H8v4c-1.1 0-2 .9-2 2v6c0 3.31 2.69 6 6 6s6-2.69 6-6V9c0-1.1-.9-2-2-2zm0 8c0 2.21-1.79 4-4 4s-4-1.79-4-4V9h8v6z"/></svg>`;

      let stateText = isGate
        ? `<button class="btn-gate-pulse" onclick="event.stopPropagation(); triggerGatePulse('${dev.device_id}', this)">⚡ Pulso 1s</button>`
        : `<span class="switch-tile-state-label">${isOn ? "Ligar" : "Desligar"}</span>`;

      let clickHandler = isGate
        ? `triggerGatePulse('${dev.device_id}', this)`
        : `toggleSwitch('${dev.device_id}', ${isOn})`;

      groupHtml += `
        <div class="${tileClass}" onclick="${clickHandler}" title="Clique para alternar ${escapeHtml(dev.name)}">
          <div class="switch-tile-wifi">${wifiSvg}</div>
          <div class="switch-tile-icon-bubble">${iconSvg}</div>
          <div class="switch-tile-name">${escapeHtml(dev.name)}</div>
          <div>${stateText}</div>
        </div>
      `;
    }

    if (groupHtml) {
      html += `<section class="section-group">`;
      html += `<h2 class="section-group-title">${escapeHtml(groupTitle)}</h2>`;
      html += `<div class="switch-tiles-grid">${groupHtml}</div>`;
      html += `</section>`;
    }
  }

  container.innerHTML = html || `<div class="empty-state">Nenhum interruptor encontrado com o filtro atual.</div>`;
}

function bool(val) {
  return Boolean(val);
}

// Alternar liga/desliga de interruptor simples de 1 canal
async function toggleSwitch(deviceId, currentIsOn) {
  const nextCommand = currentIsOn ? "power_off" : "power_on";
  const dev = cachedDevices.find(d => d.device_id === deviceId);
  const devName = dev ? dev.name : deviceId;

  toast(`⏳ Enviando comando para ${devName}...`);

  try {
    const response = await fetch(`/ewelink/control?target_device=${encodeURIComponent(deviceId)}&command=${encodeURIComponent(nextCommand)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({})
    });

    const result = await response.json();

    if (response.ok && result.success !== false) {
      toast(`✔ ${devName} ${nextCommand === "power_on" ? "ligado" : "desligado"} com sucesso!`);
      if (dev && dev.last_state) {
        dev.last_state.switch = nextCommand === "power_on" ? "on" : "off";
        renderSwitchTiles(cachedDevices);
      }
    } else {
      const errDetail = result.details || result.detail || "Erro no controlador";
      toast(`Aviso: ${typeof errDetail === "object" ? JSON.stringify(errDetail) : errDetail}`);
    }
    updateDashboard();
  } catch (error) {
    toast(`Falha de conexão com ${devName}`);
  }
}

// Alternar canal específico de um dispositivo de múltiplos canais (4CH)
async function toggleSwitchChannel(deviceId, outlet, currentIsOn) {
  const nextCommand = currentIsOn ? "power_off" : "power_on";
  const dev = cachedDevices.find(d => d.device_id === deviceId);
  const devName = dev ? dev.name : deviceId;

  toast(`⏳ Acionando ${devName} (Canal ${outlet + 1})...`);

  try {
    const response = await fetch(`/ewelink/control?target_device=${encodeURIComponent(deviceId)}&command=${encodeURIComponent(nextCommand)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ outlet: outlet })
    });

    const result = await response.json();

    if (response.ok && result.success !== false) {
      toast(`✔ ${devName} Canal ${outlet + 1} ${nextCommand === "power_on" ? "ligado" : "desligado"}!`);
      if (dev && dev.last_state && Array.isArray(dev.last_state.switches)) {
        const sw = dev.last_state.switches.find(s => s.outlet === outlet);
        if (sw) sw.switch = nextCommand === "power_on" ? "on" : "off";
        renderSwitchTiles(cachedDevices);
      }
    } else {
      const errDetail = result.details || result.detail || "Erro no controlador";
      toast(`Aviso: ${typeof errDetail === "object" ? JSON.stringify(errDetail) : errDetail}`);
    }
    updateDashboard();
  } catch (error) {
    toast(`Falha de conexão com ${devName}`);
  }
}

// Acionamento com pulso estrito de 1000ms para o Portão
async function triggerGatePulse(deviceId = "portao_garagem", btnEl = null) {
  const orig = btnEl ? btnEl.innerText : "⚡ Pulso 1s";
  if (btnEl) btnEl.innerText = "⏳...";
  toast("🚪 Acionando portão da garagem (pulso de 1s)...");

  try {
    const response = await fetch(`/ewelink/control?target_device=${encodeURIComponent(deviceId)}&command=pulse`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pulse_duration_ms: 1000, outlet: 2 })
    });
    const result = await response.json();
    if (response.ok && result.success !== false) {
      toast("✔ Portão acionado com segurança!");
    } else {
      toast("Aviso no acionamento do portão.");
    }
    updateDashboard();
  } catch (err) {
    toast("Falha de conexão com o portão.");
  } finally {
    if (btnEl) setTimeout(() => { btnEl.innerText = orig; }, 1000);
  }
}

async function syncEwelinkDevices() {
  const btn = document.getElementById("btn-sync");
  if (btn) btn.style.opacity = "0.5";
  toast("🔄 Sincronizando dispositivos...");
  try {
    const res = await fetch("/ewelink/sync", { method: "POST" });
    if (res.ok) {
      const data = await res.json();
      if (data.devices) {
        cachedDevices = data.devices;
        renderSwitchTiles(cachedDevices);
      }
      toast(`✔ ${data.total_devices || 11} dispositivos sincronizados!`);
    }
  } catch (err) {
    toast("Falha na sincronização.");
  } finally {
    if (btn) btn.style.opacity = "1";
  }
}

// ==============================================================================
// 2. Chat com Edge AI (Qwen 2.5 1.5B) - 100% Linguagem Natural e Síntese de Voz
// ==============================================================================
function sendPrompt(text) {
  const input = document.getElementById("chat-input");
  if (input) input.value = text;
  submitChat();
}

function clearChat() {
  const box = document.getElementById("chat-messages");
  if (!box) return;
  box.innerHTML = `
    <div class="chat-bubble bubble-assistant">
      <div class="bubble-header">
        <strong>iaparacasa</strong>
        <span class="bubble-time">Agora</span>
      </div>
      <div class="bubble-content">
        Conversa reiniciada. Em que posso ajudar você agora?
      </div>
    </div>
  `;
}

async function submitChat() {
  const input = document.getElementById("chat-input");
  if (!input) return;
  const prompt = input.value.trim();
  if (!prompt) return;

  input.value = "";
  appendUserMessage(prompt);

  const sendBtn = document.getElementById("btn-send");
  if (sendBtn) sendBtn.disabled = true;

  try {
    const res = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prompt: prompt,
        user_id: "morador_web"
      })
    });

    if (!res.ok) throw new Error("Erro na resposta do assistente");
    const data = await res.json();
    appendAssistantMessage(data);

    if (data.decision && data.decision.learned_preference) {
      setTimeout(loadPreferences, 500);
    }
    updateDashboard();
  } catch (err) {
    appendAssistantMessage({
      decision: {
        response_text: "Desculpe, tive uma instabilidade temporária ao processar a instrução."
      },
      interaction_id: null
    });
  } finally {
    if (sendBtn) sendBtn.disabled = false;
  }
}

function appendUserMessage(text) {
  const box = document.getElementById("chat-messages");
  if (!box) return;
  const div = document.createElement("div");
  div.className = "chat-bubble bubble-user";
  div.innerHTML = `
    <div class="bubble-header"><span class="bubble-time">Você</span></div>
    <div class="bubble-content">${escapeHtml(text)}</div>
  `;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

// Renderiza a resposta do assistente APENAS com texto em linguagem natural
function appendAssistantMessage(data) {
  const box = document.getElementById("chat-messages");
  if (!box) return;
  const div = document.createElement("div");
  div.className = "chat-bubble bubble-assistant";

  const dec = data.decision || {};
  const interactionId = data.interaction_id;
  const responseText = dec.response_text || "Comando executado com sucesso.";

  // Síntese de Voz (Lê em voz alta se ativado)
  speakText(responseText);

  // Feedback sutil para aprendizado
  let feedbackHtml = "";
  if (interactionId) {
    feedbackHtml = `
      <div style="display:flex;gap:10px;margin-top:6px;font-size:11px;" id="fb-bar-${interactionId}">
        <span style="color:#64748b;">Foi útil?</span>
        <button style="border:none;background:none;color:#10b981;cursor:pointer;font-weight:600;" onclick="sendFeedback(${interactionId}, true)">👍 Sim</button>
        <button style="border:none;background:none;color:#f59e0b;cursor:pointer;font-weight:600;" onclick="openCorrectionModal(${interactionId})">👎 Ensinar</button>
      </div>
    `;
  }

  // Apenas linguagem natural humana; SEM BLOCO JSON DE COMANDO
  div.innerHTML = `
    <div class="bubble-header">
      <strong>iaparacasa</strong>
      <div style="display:flex;align-items:center;gap:8px;">
        <button class="btn-bubble-audio" onclick="speakText(decodeURIComponent('${encodeURIComponent(responseText)}'))" title="Ouvir resposta em voz alta">🔊</button>
        <span class="bubble-time">Agora</span>
      </div>
    </div>
    <div class="bubble-content">${escapeHtml(responseText)}</div>
    ${feedbackHtml}
  `;

  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

// ==============================================================================
// 3. Web Speech API (Reconhecimento de Voz e Síntese de Fala)
// ==============================================================================
function initSpeechRecognition() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    console.warn("Reconhecimento de fala não suportado neste navegador.");
    return null;
  }
  const recog = new SpeechRecognition();
  recog.lang = "pt-BR";
  recog.continuous = false;
  recog.interimResults = false;

  recog.onstart = () => {
    isRecordingVoice = true;
    const btn = document.getElementById("btn-mic");
    if (btn) btn.classList.add("is-recording");
    toast("🎙️ Ouvindo... Fale agora");
  };

  recog.onresult = (event) => {
    const transcript = event.results[0][0].transcript;
    const input = document.getElementById("chat-input");
    if (input) input.value = transcript;
    toast(`🗣️ Reconhecido: "${transcript}"`);
    submitChat();
  };

  recog.onerror = (event) => {
    console.warn("Erro no microfone:", event.error);
    isRecordingVoice = false;
    const btn = document.getElementById("btn-mic");
    if (btn) btn.classList.remove("is-recording");

    if (event.error === "not-allowed") {
      const isLocal = window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1";
      if (!isLocal && window.location.protocol !== "https:") {
        toast("⚠️ Microfone bloqueado por HTTP de rede. Acesse por http://localhost:8001 para liberar!");
      } else {
        toast("⚠️ Permissão de microfone negada. Clique no ícone ao lado do endereço e permita o Microfone.");
      }
    } else if (event.error !== "no-speech") {
      toast(`Microfone: ${event.error}`);
    }
  };

  recog.onend = () => {
    isRecordingVoice = false;
    const btn = document.getElementById("btn-mic");
    if (btn) btn.classList.remove("is-recording");
  };

  return recog;
}

async function toggleVoiceRecording() {
  const isLocal = window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1";

  if (!isLocal && window.location.protocol !== "https:") {
    console.warn("Navegador acessando via IP de rede sem HTTPS; microfone pode requerer localhost ou permissão explícita.");
  }

  if (!speechRecognition) {
    speechRecognition = initSpeechRecognition();
  }
  if (!speechRecognition) {
    toast("Reconhecimento de voz não suportado pelo navegador atual. Use Google Chrome ou Edge.");
    return;
  }

  if (isRecordingVoice) {
    speechRecognition.stop();
  } else {
    // Tenta solicitar permissão explicitamente via getUserMedia caso o navegador suporte
    if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        stream.getTracks().forEach(track => track.stop());
      } catch (err) {
        if (!isLocal && window.location.protocol !== "https:") {
          toast("⚠️ Acesse via http://localhost:8001 para o Chrome liberar o microfone!");
        } else {
          toast("⚠️ Permissão de microfone negada. Permita o microfone no ícone ao lado do endereço.");
        }
        return;
      }
    }

    try {
      speechRecognition.start();
    } catch (e) {
      try { speechRecognition.stop(); } catch(_) {}
    }
  }
}

// Cache e seleção de vozes pt-BR
let cachedVoices = [];
if (typeof window !== "undefined" && window.speechSynthesis) {
  const loadVoices = () => {
    cachedVoices = window.speechSynthesis.getVoices() || [];
  };
  loadVoices();
  window.speechSynthesis.onvoiceschanged = loadVoices;
}

function getPtBrVoice() {
  if (!window.speechSynthesis) return null;
  const voices = cachedVoices.length > 0 ? cachedVoices : window.speechSynthesis.getVoices();
  if (!voices || voices.length === 0) return null;

  const ptVoices = voices.filter(v => (v.lang || "").toLowerCase().includes("pt"));
  if (ptVoices.length === 0) return null;

  // Prioriza vozes naturais brasileiras
  const naturalBr = ptVoices.find(v => 
    (v.lang === "pt-BR" || v.lang === "pt_BR") &&
    (v.name.includes("Google") || v.name.includes("Natural") || v.name.includes("Francisca") || v.name.includes("Antonio") || v.name.includes("Maria"))
  );
  if (naturalBr) return naturalBr;

  const anyBr = ptVoices.find(v => (v.lang === "pt-BR" || v.lang === "pt_BR"));
  return anyBr || ptVoices[0];
}

function toggleVoiceOutput() {
  voiceOutputEnabled = !voiceOutputEnabled;
  const icon = document.getElementById("voice-icon");
  if (icon) icon.innerText = voiceOutputEnabled ? "🔊" : "🔇";
  toast(`Leitura em voz alta ${voiceOutputEnabled ? "ativada" : "desativada"}`);
  if (!voiceOutputEnabled && window.speechSynthesis) {
    window.speechSynthesis.cancel();
  } else if (voiceOutputEnabled) {
    speakText("Voz ativada. Agora posso responder em áudio.");
  }
}

function speakText(text) {
  if (!voiceOutputEnabled || !window.speechSynthesis) return;
  try {
    window.speechSynthesis.cancel();
    const clean = String(text || "").replace(/[*#`_]/g, "").trim();
    if (!clean) return;

    const utterance = new SpeechSynthesisUtterance(clean);
    utterance.lang = "pt-BR";
    utterance.rate = 1.05;
    utterance.pitch = 1.0;

    const voice = getPtBrVoice();
    if (voice) {
      utterance.voice = voice;
    }

    window.speechSynthesis.speak(utterance);
  } catch (err) {
    console.warn("Erro ao reproduzir áudio:", err);
  }
}

// ==============================================================================
// 4. Modais e Preferências
// ==============================================================================
async function sendFeedback(interactionId, approved) {
  try {
    const res = await fetch("/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        interaction_id: interactionId,
        approved: approved
      })
    });
    if (res.ok) {
      const bar = document.getElementById(`fb-bar-${interactionId}`);
      if (bar) bar.innerHTML = `<span style="color:#10b981;font-size:11px;">✔ Feedback registrado!</span>`;
      loadPreferences();
    }
  } catch (err) {
    console.error(err);
  }
}

function openCorrectionModal(interactionId) {
  const idEl = document.getElementById("modal-interaction-id");
  const corrEl = document.getElementById("modal-correction");
  const modalEl = document.getElementById("feedback-modal");
  if (idEl) idEl.value = interactionId;
  if (corrEl) corrEl.value = "";
  if (modalEl) modalEl.style.display = "flex";
}

function closeFeedbackModal() {
  const modalEl = document.getElementById("feedback-modal");
  if (modalEl) modalEl.style.display = "none";
}

async function submitCorrection() {
  const interactionId = parseInt(document.getElementById("modal-interaction-id").value);
  const text = document.getElementById("modal-correction").value.trim();
  if (!text) return;

  try {
    const res = await fetch("/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        interaction_id: interactionId,
        approved: false,
        correction: text,
        preference_to_learn: text
      })
    });

    if (res.ok) {
      closeFeedbackModal();
      toast("Regra ensinada com sucesso à inteligência!");
      loadPreferences();
    }
  } catch (err) {
    alert("Erro: " + err);
  }
}

async function loadPreferences() {
  try {
    await fetch("/preferences");
  } catch (e) {}
}

function openMqttModal() {
  const selectEl = document.getElementById("mqtt-device-select");
  if (!selectEl) return;

  selectEl.innerHTML = cachedDevices
    .map(d => `<option value="${escapeHtml(d.device_id)}">${escapeHtml(d.name)} (${escapeHtml(d.device_id)})</option>`)
    .join("");
  onMqttDeviceSelectChange();
  document.getElementById("mqtt-modal").style.display = "flex";
}

function closeMqttModal() {
  document.getElementById("mqtt-modal").style.display = "none";
}

function onMqttDeviceSelectChange() {
  const deviceId = document.getElementById("mqtt-device-select").value;
  const dev = cachedDevices.find(d => d.device_id === deviceId);
  if (!dev) return;

  const fw = dev.firmware_type && dev.firmware_type !== "ewelink" ? dev.firmware_type : "tasmota";
  document.getElementById("mqtt-firmware-type").value = fw;
  document.getElementById("mqtt-topic-input").value = dev.mqtt_topic || dev.device_id;
  document.getElementById("mqtt-ip-input").value = dev.ip_address || "";
  document.getElementById("mqtt-channel-input").value = dev.channel_outlet || 0;
  document.getElementById("mqtt-domain-input").value = dev.entity_domain || "switch";
}

async function saveMqttDeviceConfig() {
  const targetDevice = document.getElementById("mqtt-device-select").value;
  const firmwareType = document.getElementById("mqtt-firmware-type").value;
  const mqttTopic = document.getElementById("mqtt-topic-input").value.trim();
  const ipAddress = document.getElementById("mqtt-ip-input").value.trim() || null;
  const channelOutlet = parseInt(document.getElementById("mqtt-channel-input").value || "0", 10);
  const entityDomain = document.getElementById("mqtt-domain-input").value;

  try {
    const res = await fetch("/mqtt/devices/pair", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        target_device: targetDevice,
        firmware_type: firmwareType,
        mqtt_topic: mqttTopic || targetDevice,
        ip_address: ipAddress,
        port: firmwareType === "ewelink" ? 8081 : 80,
        is_multichannel: channelOutlet > 0,
        channel_outlet: channelOutlet,
        entity_domain: entityDomain,
        entity_id: targetDevice
      })
    });
    if (res.ok) {
      closeMqttModal();
      toast(`✔ ${targetDevice} configurado!`);
      updateDashboard();
    }
  } catch (err) {
    alert("Erro: " + err);
  }
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
