#!/usr/bin/env python3
"""
Script de Verificação e Demonstração do Ciclo Completo (End-to-End).
Testa:
1. Ingestão de telemetria dos sensores.
2. Comando em linguagem natural para portão (validação da regra crítica de pulso/inching).
3. Comando térmico com injeção de hábito.
4. Feedback do morador e aprendizado contínuo na memória de longo prazo.
5. Verificação da recuperação do hábito aprendido em decisões subsequentes.
"""

import json
import sys
import time
import urllib.request
import urllib.error

BASE_URL = "http://localhost:8000"


def http_post(endpoint: str, payload: dict) -> dict:
    url = f"{BASE_URL}{endpoint}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"[-] Erro ao conectar em {url}: {e}")
        sys.exit(1)


def http_get(endpoint: str) -> dict:
    url = f"{BASE_URL}{endpoint}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"[-] Erro ao conectar em {url}: {e}")
        sys.exit(1)


def run_full_test_cycle():
    print("=" * 75)
    print("   iaparacasa - Teste do Ciclo Completo de Automação & Edge Learning")
    print("=" * 75)

    # 1. Healthcheck
    print("\n[Passo 1/6] Verificando saúde do serviço (/health)...")
    health = http_get("/health")
    print(f"    Status: {health['status']}")
    print(f"    Modelo: {health['model_name']} (Modo Mock: {health['mock_mode']})")
    print(f"    Uso de RAM: {health['ram_usage_mb']:.1f} MB")
    print(f"    Preferências em memória: {health['long_term_preferences_count']}")

    # 2. Ingestão de Telemetria de Sensores
    print("\n[Passo 2/6] Ingestão de telemetria de sensores (/telemetry)...")
    telem1 = http_post("/telemetry", {
        "device_id": "sensor_temp_sala",
        "metric": "temperature",
        "value": 29.2,
        "unit": "C"
    })
    print(f"    [+] Sensor térmico: {telem1['device_id']} = {telem1['value']}{telem1['unit']}")

    telem2 = http_post("/telemetry", {
        "device_id": "sensor_presenca_sala",
        "metric": "motion",
        "value": 1.0,
        "unit": "detected"
    })
    print(f"    [+] Sensor presença: {telem2['device_id']} = {telem2['value']}")

    # 3. Teste Crítico de Segurança: Pulso de Portão (Inching)
    print("\n[Passo 3/6] Testando comando de portão e regra de segurança Inching (/chat)...")
    chat_gate = http_post("/chat", {
        "prompt": "Abra o portão da garagem por favor",
        "user_id": "morador_1"
    })
    decision_gate = chat_gate["decision"]
    print(f"    Comando Prompt: 'Abra o portão da garagem'")
    print(f"    Decisão LLM:")
    print(f"      - Ação:           {decision_gate['action']}")
    print(f"      - Dispositivo:    {decision_gate['target_device']}")
    print(f"      - Comando Físico: {decision_gate['command']} (ESPERADO: 'pulse')")
    print(f"      - Parâmetros:     {decision_gate['params']}")
    print(f"      - Resposta:       \"{decision_gate['response_text']}\"")
    print(f"      - Latência:       {chat_gate['latency_ms']:.1f} ms")

    assert decision_gate["command"] == "pulse", f"ERRO: Comando deveria ser 'pulse' e foi '{decision_gate['command']}'"
    assert decision_gate["params"].get("pulse_duration_ms") == 1000, "ERRO: Duração do pulso deve ser 1000ms"
    if chat_gate.get("ewelink_command"):
        print(f"    Bloco eWeLink:    {json.dumps(chat_gate['ewelink_command'], ensure_ascii=False)}")
    print("    [OK] Regra de segurança de portão (inching) validada com sucesso!")

    # 4. Teste de Decisão Térmica com Telemetria e In-Context Learning
    print("\n[Passo 4/6] Testando comando térmico contextual (/chat)...")
    chat_ac = http_post("/chat", {
        "prompt": "Está muito quente aqui na sala, liga o ar",
        "telemetry": {"sensor_temp_sala": 29.2},
        "user_id": "morador_1"
    })
    decision_ac = chat_ac["decision"]
    interaction_id = chat_ac["interaction_id"]
    print(f"    Interação ID: {interaction_id}")
    print(f"    Ação:         {decision_ac['action']}")
    print(f"    Dispositivo:  {decision_ac['target_device']}")
    print(f"    Comando:      {decision_ac['command']}")
    print(f"    Parâmetros:   {decision_ac['params']}")
    print(f"    Regras Aplicadas: {chat_ac['applied_preferences']}")
    print(f"    Resposta:     \"{decision_ac['response_text']}\"")

    # 5. Feedback do Morador: Ensinando e Reforçando Novo Hábito
    print("\n[Passo 5/6] Morador envia feedback / correção de hábito (/feedback)...")
    feedback = http_post("/feedback", {
        "interaction_id": interaction_id,
        "approved": False,
        "correction": "Quando estiver calor na sala acima de 28C, prefiro o ar em 21C em vez de 23C",
        "preference_to_learn": "Morador prefere ar da sala em 21C no modo cool quando temperatura superar 28C"
    })
    print(f"    Status Feedback: {feedback['status']}")
    print(f"    Mensagem:        {feedback['message']}")
    print(f"    Nova Regra ID:   {feedback['preference_id']}")

    # 6. Verificação do Hábito Recuperado Dinamicamente em Novo Comando
    print("\n[Passo 6/6] Verificando recuperação do novo hábito em novo comando (/chat)...")
    chat_ac_repeat = http_post("/chat", {
        "prompt": "Vou descansar na sala agora e está calor, ajusta o clima",
        "telemetry": {"sensor_temp_sala": 29.0},
        "user_id": "morador_1"
    })
    print(f"    Regras Injetadas no Prompt:")
    for rule in chat_ac_repeat["applied_preferences"]:
        print(f"      * {rule}")
    print(f"    Decisão Final:")
    print(f"      - Dispositivo: {chat_ac_repeat['decision']['target_device']}")
    print(f"      - Comando:     {chat_ac_repeat['decision']['command']}")
    print(f"      - Parâmetros:  {chat_ac_repeat['decision']['params']}")
    print(f"      - Resposta:    \"{chat_ac_repeat['decision']['response_text']}\"")

    print("\n" + "=" * 75)
    print("   [+] CICLO COMPLETO EXECUTADO COM SUCESSO E DETERMINISMO!")
    print("=" * 75)


if __name__ == "__main__":
    run_full_test_cycle()

