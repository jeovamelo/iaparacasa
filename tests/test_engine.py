"""
Testes unitários para o EdgeLLMEngine (parsing JSON, schema e fallback seguro).
"""

import pytest
from app.config import settings
from app.engine import DECISION_JSON_SCHEMA, EdgeLLMEngine
from app.schemas import ActionType, CommandType


def test_decision_json_schema_structure():
    """Garante que o schema JSON achatado para llama.cpp contém todos os campos exigidos."""
    props = DECISION_JSON_SCHEMA["properties"]
    assert set(DECISION_JSON_SCHEMA["required"]) == {
        "action",
        "target_device",
        "command",
        "params",
        "learned_preference",
        "response_text",
    }
    assert props["action"]["enum"] == ["execute", "query", "suggest", "learn"]
    assert props["command"]["enum"] == ["power_on", "power_off", "pulse", "set_state"]


def test_engine_parse_json_with_markdown_fences(monkeypatch):
    """Testa resiliência do parser JSON mesmo se o modelo emitir blocos markdown ou texto extra."""
    monkeypatch.setattr(settings, "USE_MOCK_ENGINE", True)
    engine = EdgeLLMEngine()

    raw = (
        "Aqui está a decisão solicitada:\n```json\n"
        '{"action": "execute", "target_device": "sonoff_rele_portao", '
        '"command": "power_on", "params": {}, "learned_preference": null, '
        '"response_text": "Portão acionado."}\n```'
    )
    decision = engine._parse_and_validate_json(raw, "abra o portão")
    assert decision.action == ActionType.EXECUTE
    assert decision.target_device == "sonoff_rele_portao"
    # Guarda de segurança converte power_on em pulse automaticamente!
    assert decision.command == CommandType.PULSE
    assert decision.params["pulse_duration_ms"] == 1000

