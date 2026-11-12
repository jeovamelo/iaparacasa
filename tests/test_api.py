"""
Testes de integração dos endpoints FastAPI (/health, /chat, /feedback, /telemetry).
"""

import os
import tempfile
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture(scope="module")
def client():
    # Cria banco temporário para a bateria de testes de API
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    prev_db = settings.DB_PATH
    prev_mock = settings.USE_MOCK_ENGINE
    prev_bridge = settings.ENABLE_HARDWARE_BRIDGE
    prev_mqtt = settings.MQTT_ENABLED

    settings.DB_PATH = path
    settings.USE_MOCK_ENGINE = True  # Garante execução sem baixar 1GB em teste
    settings.ENABLE_HARDWARE_BRIDGE = False
    settings.MQTT_ENABLED = False

    with TestClient(app) as test_client:
        yield test_client

    settings.DB_PATH = prev_db
    settings.USE_MOCK_ENGINE = prev_mock
    settings.ENABLE_HARDWARE_BRIDGE = prev_bridge
    settings.MQTT_ENABLED = prev_mqtt
    if os.path.exists(path):
        os.unlink(path)


def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "mock_mode" in data
    assert data["long_term_preferences_count"] >= 3


def test_chat_gate_inching_command(client):
    """Garante que a API retorna o comando 'pulse' de 1000ms e bloco eWeLink para portao_garagem."""
    response = client.post("/chat", json={
        "prompt": "Abra o portão da garagem",
        "user_id": "test_user"
    })
    assert response.status_code == 200
    data = response.json()
    decision = data["decision"]
    assert decision["action"] == "execute"
    assert decision["target_device"] == "portao_garagem"
    assert decision["command"] == "pulse"
    assert decision["params"]["pulse_duration_ms"] == 1000
    assert data["ewelink_command"]["integration"] == "ewelink"
    assert data["ewelink_command"]["action"] == "pulse_inching"
    assert data["ewelink_command"]["params"]["pulse_ms"] == 1000


def test_chat_ac_with_telemetry(client):
    """Testa comando térmico e retorno de ID de interação e bloco eWeLink."""
    response = client.post("/chat", json={
        "prompt": "Está muito calor, ligue o ar da sala",
        "telemetry": {"sensor_temp_sala": 29.0},
        "user_id": "test_user"
    })
    assert response.status_code == 200
    data = response.json()
    assert data["interaction_id"] > 0
    assert data["decision"]["target_device"] == "ar_condicionado_sala"
    assert data["ewelink_command"]["integration"] == "ewelink"
    assert data["ewelink_command"]["action"] == "set_device_status"


def test_feedback_loop(client):
    """Executa um chat e em seguida submete correção de hábito via feedback."""
    chat_resp = client.post("/chat", json={
        "prompt": "Ligue a luz da sala",
        "user_id": "test_user"
    })
    interaction_id = chat_resp.json()["interaction_id"]

    feedback_resp = client.post("/feedback", json={
        "interaction_id": interaction_id,
        "approved": False,
        "correction": "Sempre prefiro a luz da sala com brilho em 50%",
        "preference_to_learn": "Morador prefere luz da sala com 50% de luminosidade"
    })
    assert feedback_resp.status_code == 200
    data = feedback_resp.json()
    assert data["status"] == "success"
    assert data["preference_id"] is not None


def test_telemetry_ingestion(client):
    """Testa a ingestão e consulta de leituras recentes de sensores."""
    telem_resp = client.post("/telemetry", json={
        "device_id": "sensor_temp_quarto",
        "metric": "temperature",
        "value": 26.8,
        "unit": "C"
    })
    assert telem_resp.status_code == 200

    recent_resp = client.get("/telemetry/recent")
    assert recent_resp.status_code == 200
    data = recent_resp.json()
    assert "sensor_temp_quarto" in data
    assert data["sensor_temp_quarto"]["value"] == 26.8


def test_list_and_delete_preference(client):
    """Testa a governança de preferências (listagem e exclusão)."""
    list_resp = client.get("/preferences")
    assert list_resp.status_code == 200
    prefs = list_resp.json()
    assert len(prefs) > 0

    first_id = prefs[0]["id"]
    del_resp = client.delete(f"/preferences/{first_id}")
    assert del_resp.status_code == 200


def test_dashboard_endpoint(client):
    """Garante que a interface web moderna é servida nas rotas raiz e dashboard."""
    resp_root = client.get("/")
    assert resp_root.status_code == 200
    assert "iaparacasa" in resp_root.text

    resp_dash = client.get("/dashboard")
    assert resp_dash.status_code == 200
    assert "iaparacasa" in resp_dash.text


def test_ewelink_endpoints(client):
    """Testa a listagem, sincronização e controle dos 10 dispositivos Minha Casa eWeLink/Sonoff."""
    devs_resp = client.get("/ewelink/devices")
    assert devs_resp.status_code == 200
    devs = devs_resp.json()
    dev_map = {d["device_id"]: d for d in devs}

    expected_ids = [
        "varanda_superior", "corredor_cima",
        "jardim_deck", "corredor_externo", "jardim_varanda", "jardim_garagem", "jardim_principal",
        "portao_garagem", "luz_sala", "ar_condicionado_sala"
    ]
    for dev_id in expected_ids:
        assert dev_id in dev_map, f"Dispositivo {dev_id} ausente na topologia"

    assert dev_map["varanda_superior"]["online"] is False
    assert dev_map["varanda_superior"]["group"] == "Grupo Casa"
    assert dev_map["jardim_garagem"]["group"] == "Grupo Externa"

    # Jardim da garagem não deve ser tratado como portão (aceita power_on normal)
    garden_ctrl = client.post("/ewelink/control?target_device=jardim_garagem&command=power_on")
    assert garden_ctrl.status_code == 200
    assert garden_ctrl.json()["command"] == "power_on"

    # Sincronização Dual-Mode / CoolKit
    sync_resp = client.post("/ewelink/sync")
    assert sync_resp.status_code == 200
    assert sync_resp.json()["success"] is True
    assert sync_resp.json()["total_devices"] >= 10

    # Teste de controle de portão (também com alias legado) com garantia de Inching
    ctrl_resp = client.post("/ewelink/control?target_device=sonoff_rele_portao&command=pulse")
    assert ctrl_resp.status_code == 200
    res = ctrl_resp.json()
    assert res["success"] is True
    assert res["target_device"] == "portao_garagem"
    assert res["command"] == "pulse"

    # Teste do endpoint de login CoolKit (valida tratamento limpo sem AppID/Secret)
    login_resp = client.post("/ewelink/login")
    assert login_resp.status_code == 200
    login_data = login_resp.json()
    assert "success" in login_data
    assert "password" not in str(login_data).lower()

