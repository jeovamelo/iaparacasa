"""
Testes unitários dos esquemas Pydantic e regras de segurança de automação.
"""

import pytest
from app.schemas import ActionType, CommandType, DecisionOutput


def test_decision_output_valid():
    """Valida a construção e serialização de uma decisão normal."""
    decision = DecisionOutput(
        action=ActionType.EXECUTE,
        target_device="sonoff_luz_sala",
        command=CommandType.POWER_ON,
        params={"brightness": 80},
        learned_preference=None,
        response_text="Liguei a luz da sala."
    )
    assert decision.action == ActionType.EXECUTE
    assert decision.target_device == "sonoff_luz_sala"
    assert decision.command == CommandType.POWER_ON
    assert decision.params["brightness"] == 80


def test_gate_safety_guardrail_enforces_pulse():
    """
    Testa a guarda de segurança crítica:
    Se um comando power_on for direcionado para um portão ou garagem,
    o validador DEVE convertê-lo para pulse de 1000ms automaticamente.
    """
    unsafe_decision = DecisionOutput(
        action=ActionType.EXECUTE,
        target_device="sonoff_rele_portao_garagem",
        command=CommandType.POWER_ON,  # Inseguro para motor de portão
        params={},
        learned_preference=None,
        response_text="Abrindo portão."
    )
    # A guarda de segurança deve intervir
    assert unsafe_decision.command == CommandType.PULSE
    assert unsafe_decision.params.get("pulse_duration_ms") == 1000


def test_gateway_payload_formatting():
    """Verifica a exportação no formato de gateway Sonoff / eWeLink LAN / MQTT."""
    pulse_decision = DecisionOutput(
        action=ActionType.EXECUTE,
        target_device="sonoff_portao",
        command=CommandType.PULSE,
        params={"pulse_duration_ms": 1000},
        response_text="Pulso enviado"
    )
    payload = pulse_decision.to_gateway_payload()
    assert payload["device"] == "sonoff_portao"
    assert payload["cmd"] == "pulse"
    assert payload["pulse"]["pulse"] == "on"
    assert payload["pulse"]["pulseWidth"] == 1000

    power_decision = DecisionOutput(
        action=ActionType.EXECUTE,
        target_device="sonoff_luz_cozinha",
        command=CommandType.POWER_ON,
        params={},
        response_text="Luz acesa"
    )
    p_payload = power_decision.to_gateway_payload()
    assert p_payload["switch"] == "on"


def test_jardim_garagem_allows_power_on():
    """Garante que 'jardim_garagem' (luz externa) NÃO aciona a guarda de pulso do portão."""
    garden_light = DecisionOutput(
        action=ActionType.EXECUTE,
        target_device="jardim_garagem",
        command=CommandType.POWER_ON,
        params={},
        response_text="Liguei o Jardim da Garagem."
    )
    assert garden_light.command == CommandType.POWER_ON
    ewelink_block = garden_light.to_ewelink_payload()
    assert ewelink_block == {
        "integration": "ewelink",
        "action": "set_device_status",
        "target_device": "jardim_garagem",
        "params": {"switch": "on"},
    }


def test_portao_garagem_enforces_pulse_and_ewelink_payload():
    """Garante que 'portao_garagem' converte power_on -> pulse_inching de 1000ms."""
    gate_cmd = DecisionOutput(
        action=ActionType.EXECUTE,
        target_device="portao_garagem",
        command=CommandType.POWER_ON,
        params={},
        response_text="Acionando portão."
    )
    assert gate_cmd.command == CommandType.PULSE
    assert gate_cmd.params["pulse_duration_ms"] == 1000
    assert gate_cmd.to_ewelink_payload() == {
        "integration": "ewelink",
        "action": "pulse_inching",
        "target_device": "portao_garagem",
        "params": {"switch": "on", "pulse_ms": 1000},
    }


def test_ewelink_sync_devices_payload():
    """Verifica emissão de sync_devices no contrato eWeLink para consultas."""
    query_cmd = DecisionOutput(
        action=ActionType.QUERY,
        target_device="ewelink_gateway",
        command=CommandType.SET_STATE,
        params={"query_type": "sync_devices"},
        response_text="Sincronizando dispositivos."
    )
    ewelink_block = query_cmd.to_ewelink_payload()
    assert ewelink_block["integration"] == "ewelink"
    assert ewelink_block["action"] == "sync_devices"
    assert ewelink_block["target_device"] == "ewelink_gateway"

