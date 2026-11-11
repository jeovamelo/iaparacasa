/* ==============================================================================
   iaparacasa - Frontend Client Logic (Vanilla JS, Zero Dependencies)
   ============================================================================== */

let currentAcTemp = 23;

// Inicialização
document.addEventListener("DOMContentLoaded", () => {
  updateDashboard();
  loadPreferences();
  setInterval(updateDashboard, 5000);
});

async function updateDashboard() {
  try {
    // 1. Atualiza Health & RAM
    const healthRes = await fetch("/health");
    if (healthRes.ok) {
      const h = await healthRes.json();
      document.getElementById("badge-engine").innerText = `🧠 Engine: ${h.mock_mode ? 'Mock Heurístico' : h.model_name}`;
      document.getElementById("badge-ram").innerText = `💾 RAM: ${h.ram_usage_mb} MB`;
      document.getElementById("badge-status").innerText = `● Sistema Operacional`;
    }

    // 2. Atualiza Telemetria
    const telemRes = await fetch("/telemetry/recent");
    if (telemRes.ok) {
      const data = await telemRes.json();
      if (data.sensor_temp_sala) {
        document.getElementById("val-temp").innerText = `${data.sensor_temp_sala.value.toFixed(1)} °C`;
      }
      if (data.sensor_umidade_sala) {
        document.getElementById("val-hum").innerText = `Umidade: ${data.sensor_umidade_sala.value.toFixed(0)}%`;
      }
      if (data.sensor_presenca_sala) {
        const isMotion = data.sensor_presenca_sala.value > 0;
        const motionElem = document.getElementById("val-motion");
        motionElem.innerText = isMotion ? "Detectada" : "Ausente";
        motionElem.style.color = isMotion ? "#3fb950" : "#8b949e";
        document.getElementById("val-motion-time").innerText = isMotion ? "Movimento recente" : "Nenhum movimento";
      }
    }
    // 3. Atualiza Dispositivos eWeLink (Minha Casa) agrupados
    const devsRes = await fetch("/ewelink/devices");
    if (devsRes.ok) {
      const devices = await devsRes.json();
      renderEwelinkDevices(devices);
    }
  } catch (err) {
    console.warn("Falha ao atualizar dashboard:", err);
  }
}

function getDeviceIcon(dev) {
  if (dev.device_type === "gate_pulse") return "🚪";
  if (dev.device_type === "climate") return "❄️";
  if ((dev.device_id || "").includes("jardim")) return "🌿";
  return "💡";
}

function renderEwelinkDevices(devices) {
  const container = document.getElementById("ewelink-device-groups");
  if (!container) return;

  const groupOrder = ["Atuadores Locais", "Grupo Casa", "Grupo Externa", "eWeLink LAN", "eWeLink Cloud"];
  const visibleDevices = devices.filter(d => d.device_type !== "sensor");

  const grouped = {};
  for (const dev of visibleDevices) {
    const grp = dev.group || "Atuadores Locais";
    if (!grouped[grp]) grouped[grp] = [];
    grouped[grp].push(dev);
  }

  let html = "";
  for (const grp of groupOrder) {
    const items = grouped[grp];
    if (!items || items.length === 0) continue;

    html += `<div class="device-group-header">${escapeHtml(grp)}</div>`;
    for (const dev of items) {
      const statusBadge = dev.online
        ? `<span class="status-pill status-online">Online</span>`
        : `<span class="status-pill status-offline">Offline</span>`;
      const aesBadge = dev.lan_encryption_ready
        ? `<span class="status-pill" style="background:rgba(56,139,253,0.16);color:#79c0ff;" title="Controle local LAN AES-128-CBC pronto">🔒 AES LAN</span>`
        : "";
      const state = dev.last_state || {};

      let subText = dev.model_hint || dev.device_id;
      let controlsHtml = "";

      if (dev.device_type === "gate_pulse") {
        subText = `${dev.model_hint || "Contato Seco"} • ID: ${dev.device_id}`;
        controlsHtml = `<button class="btn btn-pulse" onclick="triggerGatePulse('${dev.device_id}', this)">⚡ Pulso 1s</button>`;
      } else if (dev.device_type === "climate") {
        const temp = state.temperature || currentAcTemp;
        currentAcTemp = temp;
        const switchState = state.switch === "on" ? "Ligado" : "Desligado";
        subText = `Estado: ${switchState} • ${temp}°C (${state.mode || "cool"})`;
        controlsHtml = `
          <div class="switch-group">
            <button class="btn btn-temp" onclick="adjustTemp(-1)">-1°C</button>
            <button class="btn btn-temp" onclick="adjustTemp(1)">+1°C</button>
            <button class="btn btn-on" onclick="controlDevice('${dev.device_id}', 'set_state', {temperature: ${temp}, mode: 'cool'})">Ligar</button>
            <button class="btn btn-off" onclick="controlDevice('${dev.device_id}', 'power_off')">Off</button>
          </div>
        `;
      } else {
        const switchState = state.switch === "on" ? "Ligado" : "Desligado";
        subText = `${dev.model_hint || "Sonoff Switch"} • ${switchState}`;
        controlsHtml = `
          <div class="switch-group">
            <button class="btn btn-on" onclick="controlDevice('${dev.device_id}', 'power_on')">Ligar</button>
            <button class="btn btn-off" onclick="controlDevice('${dev.device_id}', 'power_off')">Desligar</button>
          </div>
        `;
      }

      html += `
        <div class="device-item">
          <div class="device-info">
            <span class="device-icon">${getDeviceIcon(dev)}</span>
            <div>
              <strong>${escapeHtml(dev.name)}</strong>${statusBadge}${aesBadge}
              <p class="device-sub">${escapeHtml(subText)}</p>
            </div>
          </div>
          ${controlsHtml}
        </div>
      `;
    }
  }

  container.innerHTML = html || `<div class="empty-state">Nenhum dispositivo cadastrado.</div>`;
}

async function syncEwelinkDevices() {
  const btn = document.getElementById("btn-sync");
  const statusEl = document.getElementById("sync-status-text");
  if (btn) btn.innerText = "⏳ Sincronizando...";
  try {
    const res = await fetch("/ewelink/sync", { method: "POST" });
    if (res.ok) {
      const data = await res.json();
      if (statusEl) {
        statusEl.innerText = data.cloud_configured
          ? `☁️ Nuvem sincronizada (${data.synced_count} dispositivos atualizados)`
          : `📡 LAN Local ativa (${data.total_devices} dispositivos) — configure AppID/Token no .env para nuvem`;
      }
      if (data.devices) renderEwelinkDevices(data.devices);
    }
  } catch (err) {
    console.error("Erro na sincronização eWeLink:", err);
  } finally {
    if (btn) btn.innerText = "↻ Sincronizar";
  }
}

async function loadPreferences() {
  try {
    const res = await fetch("/preferences");
    if (!res.ok) return;
    const prefs = await res.json();
    document.getElementById("count-prefs").innerText = `${prefs.length} Hábitos`;

    const list = document.getElementById("preferences-list");
    if (prefs.length === 0) {
      list.innerHTML = `<div class="empty-state">Nenhum hábito específico salvo ainda.</div>`;
      return;
    }

    list.innerHTML = prefs.map(p => `
      <div class="pref-item">
        <div>
          <div class="pref-text"><strong>[${p.category.toUpperCase()}]</strong> ${escapeHtml(p.preference_rule)}</div>
          <div class="pref-meta">
            <span>Dispositivo: <code>${p.target_device}</code></span>
            <span>Confiança: ${(p.confidence * 100).toFixed(0)}%</span>
            <span>Reforçado: ${p.times_reinforced}x</span>
          </div>
        </div>
        <button class="btn-del" onclick="deletePreference(${p.id})" title="Remover hábito">&times;</button>
      </div>
    `).join("");
  } catch (err) {
    console.error("Erro ao carregar preferências:", err);
  }
}

async function deletePreference(id) {
  if (!confirm("Deseja realmente remover este hábito aprendido?")) return;
  try {
    const res = await fetch(`/preferences/${id}`, { method: "DELETE" });
    if (res.ok) {
      loadPreferences();
    }
  } catch (e) {
    alert("Erro ao excluir preferência: " + e);
  }
}

function sendPrompt(text) {
  document.getElementById("chat-input").value = text;
  submitChat();
}

async function submitChat() {
  const input = document.getElementById("chat-input");
  const prompt = input.value.trim();
  if (!prompt) return;

  input.value = "";
  appendUserMessage(prompt);

  const sendBtn = document.getElementById("btn-send");
  sendBtn.disabled = true;
  sendBtn.innerText = "...";

  try {
    const res = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prompt: prompt,
        user_id: "morador_web"
      })
    });

    if (!res.ok) throw new Error("Erro na resposta da API");
    const data = await res.json();
    appendAssistantMessage(data);

    // Se o modelo aprendeu nova preferência, atualiza tabela
    if (data.decision.learned_preference) {
      setTimeout(loadPreferences, 500);
    }
    updateDashboard();
  } catch (err) {
    appendAssistantMessage({
      decision: {
        response_text: "Desculpe, ocorreu um erro de conexão com o motor de automação."
      },
      interaction_id: null,
      applied_preferences: [],
      latency_ms: 0
    });
  } finally {
    sendBtn.disabled = false;
    sendBtn.innerText = "Enviar";
  }
}

function appendUserMessage(text) {
  const box = document.getElementById("chat-messages");
  const div = document.createElement("div");
  div.className = "chat-bubble bubble-user";
  div.innerHTML = `
    <div class="bubble-header"><span class="bubble-time">Você</span></div>
    <div class="bubble-content">${escapeHtml(text)}</div>
  `;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function appendAssistantMessage(data) {
  const box = document.getElementById("chat-messages");
  const div = document.createElement("div");
  div.className = "chat-bubble bubble-assistant";

  const dec = data.decision || {};
  const interactionId = data.interaction_id;
  const latency = data.latency_ms ? `${data.latency_ms.toFixed(1)}ms` : "";

  let appliedHtml = "";
  if (data.applied_preferences && data.applied_preferences.length > 0) {
    appliedHtml = `<div style="font-size: 10px; color: #a371f7; margin-top: 4px;">🎯 Hábito Aplicado: ${escapeHtml(data.applied_preferences[0])}</div>`;
  }

  let feedbackHtml = "";
  if (interactionId) {
    feedbackHtml = `
      <div class="bubble-feedback-bar" id="fb-bar-${interactionId}">
        <span>A decisão foi correta?</span>
        <button class="btn-fb" onclick="sendFeedback(${interactionId}, true)">👍 Sim</button>
        <button class="btn-fb" onclick="openCorrectionModal(${interactionId})">👎 Corrigir</button>
      </div>
    `;
  }

  const ewelinkJson = data.ewelink_command
    ? JSON.stringify(data.ewelink_command, null, 2)
    : JSON.stringify({
        integration: "ewelink",
        action: dec.command === "pulse" ? "pulse_inching" : "set_device_status",
        target_device: dec.target_device,
        params: dec.params || {}
      }, null, 2);

  div.innerHTML = `
    <div class="bubble-header">
      <strong>🤖 iaparacasa</strong>
      <span class="bubble-time">${latency}</span>
    </div>
    <div class="bubble-content">${escapeHtml(dec.response_text || "")}</div>
    ${appliedHtml}
    <div class="bubble-decision-box">
      <strong>Bloco eWeLink JSON:</strong>
      <pre>${escapeHtml(ewelinkJson)}</pre>
    </div>
    ${feedbackHtml}
  `;

  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

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
      if (bar) bar.innerHTML = `<span style="color: #3fb950;">✔ Feedback gravado! Hábito reforçado na memória.</span>`;
      loadPreferences();
    }
  } catch (err) {
    alert("Erro ao enviar feedback: " + err);
  }
}

function openCorrectionModal(interactionId) {
  document.getElementById("modal-interaction-id").value = interactionId;
  document.getElementById("modal-correction").value = "";
  document.getElementById("feedback-modal").style.display = "flex";
}

function closeFeedbackModal() {
  document.getElementById("feedback-modal").style.display = "none";
}

async function submitCorrection() {
  const interactionId = parseInt(document.getElementById("modal-interaction-id").value);
  const text = document.getElementById("modal-correction").value.trim();
  if (!text) {
    alert("Por favor descreva a correção.");
    return;
  }

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
      const bar = document.getElementById(`fb-bar-${interactionId}`);
      if (bar) bar.innerHTML = `<span style="color: #e3b341;">✔ Correção aprendida! Salva no SQLite.</span>`;
      loadPreferences();
    }
  } catch (err) {
    alert("Erro ao salvar correção: " + err);
  }
}

// Controles Físicos Diretos (Gateway eWeLink Dual-Mode)
async function triggerGatePulse(deviceId = "portao_garagem", btnEl = null) {
  const originalText = btnEl ? btnEl.innerText : "⚡ Pulso 1s";
  if (btnEl) {
    btnEl.innerText = "⏳ Pulso 1s...";
    btnEl.style.background = "rgba(46, 160, 67, 0.4)";
  }

  try {
    await fetch(`/ewelink/control?target_device=${encodeURIComponent(deviceId)}&command=pulse`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pulse_duration_ms: 1000 })
    });
    updateDashboard();
  } finally {
    if (btnEl) {
      setTimeout(() => {
        btnEl.innerText = originalText;
        btnEl.style.background = "";
      }, 1000);
    }
  }
}

async function controlDevice(deviceId, command, params = null) {
  await fetch(`/ewelink/control?target_device=${encodeURIComponent(deviceId)}&command=${encodeURIComponent(command)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params || {})
  });
  updateDashboard();
}

async function adjustTemp(delta) {
  currentAcTemp += delta;
  await controlDevice("ar_condicionado_sala", "set_state", { temperature: currentAcTemp, mode: "cool" });
}

async function simulateTelemetry(temp, motion) {
  await fetch("/telemetry", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ device_id: "sensor_temp_sala", metric: "temperature", value: temp, unit: "C" })
  });
  await fetch("/telemetry", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ device_id: "sensor_presenca_sala", metric: "motion", value: motion, unit: "detected" })
  });
  updateDashboard();
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

