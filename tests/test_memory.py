"""
Testes unitários do sistema de memória híbrida (curto e longo prazo).
"""

import os
import tempfile
import pytest

from app.memory import ContextManager, LongTermMemory, ShortTermBuffer


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.unlink(path)


def test_short_term_buffer_capacity():
    """Valida o comportamento de buffer circular deslizante."""
    buf = ShortTermBuffer(maxlen=3)
    buf.add_telemetry("sensor_1", "temp", 20.0)
    buf.add_telemetry("sensor_2", "temp", 22.0)
    buf.add_telemetry("sensor_3", "temp", 24.0)
    buf.add_telemetry("sensor_4", "temp", 26.0)

    events = buf.get_recent_events(limit=10)
    assert len(events) == 3
    assert events[-1]["device_id"] == "sensor_4"


def test_long_term_memory_seeding(temp_db):
    """Verifica se as regras padrão de fábrica foram inseridas no SQLite."""
    mem = LongTermMemory(db_path=temp_db)
    prefs = mem.get_all_preferences()
    assert len(prefs) >= 3

    categories = [p["category"] for p in prefs]
    assert "security" in categories
    assert "thermal" in categories


def test_contextual_retrieval_ranking(temp_db):
    """Testa se a recuperação semântica recupera a regra correta para temperatura."""
    mem = LongTermMemory(db_path=temp_db)

    # Teste de consulta sobre calor e ar condicionado com telemetria de 29°C
    results = mem.get_relevant_preferences(
        query="Está calor na sala, liga o ar",
        current_telemetry={"sensor_temp_sala": 29.5},
        top_k=2
    )

    assert len(results) > 0
    top_rule = results[0]
    assert top_rule["category"] == "thermal"
    assert "ar_condicionado_sala" in top_rule["target_device"]


def test_feedback_reinforcement(temp_db):
    """Testa o reforço de confiança quando o usuário aprova uma automação."""
    mem = LongTermMemory(db_path=temp_db)

    # Cria uma interação simulada
    int_id = mem.record_interaction(
        user_prompt="Ligue o ar",
        sensor_context="temp=28",
        decision_json='{"action":"execute","target_device":"ac_sala"}',
        applied_preference_id=2
    )

    # Usuário aprova
    success, pref_id = mem.record_feedback(
        interaction_id=int_id,
        approved=True
    )
    assert success is True

    # Verifica se a regra 2 foi reforçada
    prefs = {p["id"]: p for p in mem.get_all_preferences()}
    assert prefs[2]["times_reinforced"] > 2


def test_context_manager_assembly(temp_db):
    """Valida a montagem unificada dos blocos de contexto para o prompt."""
    ctx_mgr = ContextManager(db_path=temp_db)
    context_block, rules, top_id = ctx_mgr.assemble_context(
        query="Portão da garagem",
        immediate_telemetry={"temperatura": 24.0}
    )

    assert "<short_term_context>" in context_block
    assert "<learned_preferences>" in context_block
    assert "portao_garagem" in context_block

