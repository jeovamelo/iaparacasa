"""
Testes completos de integração MQTT local (Mosquitto) e firmwares abertos:
Tasmota (MQTT + fallback HTTP), ESPHome (MQTT + REST API), Auto-Discovery,
telemetria inbound de sensores, migração SQLite e segurança de pulso para portões.
"""

import json
import os
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.ewelink_gateway import EWeLinkGateway, ewelink_gateway
from app.main import app, _ingest_mqtt_telemetry, context_mgr
from app.memory import ContextManager, LongTermMemory
from app.mqtt_gateway import (
    MqttOpenFirmwareGateway,
    build_tasmota_command,
    extract_tasmota_telemetry,
    ms_to_tasmota_pulsetime,
    mqtt_gateway,
)
from app.schemas import CommandType


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.unlink(path)


def test_tasmota_pulsetime_conversion_and_commands():
    assert ms_to_tasmota_pulsetime(500) == 5
    assert ms_to_tasmota_pulsetime(1000) == 10
    assert ms_to_tasmota_pulsetime(11100) == 111
    assert ms_to_tasmota_pulsetime(15000) == 115

    assert build_tasmota_command(CommandType.POWER_ON) == ("POWER", "ON")
    assert build_tasmota_command(CommandType.POWER_OFF, is_multichannel=True, channel_outlet=1) == ("POWER2", "OFF")
    assert build_tasmota_command(CommandType.PULSE, {"pulse_duration_ms": 1000}) == (
        "Backlog",
        "PulseTime 10; Power ON",
    )
    assert build_tasmota_command(
        CommandType.PULSE, {"pulse_duration_ms": 1000}, is_multichannel=True, channel_outlet=2
    ) == ("Backlog", "PulseTime3 10; Power3 ON")


def test_tasmota_mqtt_publish_and_gate_inching_safety(temp_db):
    """Garante envio MQTT com retain=False e conversão compulsória power_on -> Backlog PulseTime 10 em portões."""
    mem = LongTermMemory(db_path=temp_db)
    gw = EWeLinkGateway(start_zeroconf=False)
    gw.attach_memory(mem)

    gw.pair_open_firmware_device(
        target_device="luz_sala",
        firmware_type="tasmota",
        mqtt_topic="luz_sala",
        ip_address="192.168.1.151",
    )
    gw.pair_open_firmware_device(
        target_device="portao_garagem",
        firmware_type="tasmota",
        mqtt_topic="portao_garagem",
        ip_address="192.168.1.150",
    )

    published = []

    def fake_publish(topic, payload, qos=1, retain=False):
        published.append((topic, payload, qos, retain))
        return True, {"topic": topic, "payload": payload, "retain": retain}

    prev_bridge = settings.ENABLE_HARDWARE_BRIDGE
    settings.ENABLE_HARDWARE_BRIDGE = True
    try:
        with patch.object(mqtt_gateway, "is_connected", return_value=True), patch.object(
            mqtt_gateway, "publish_raw", side_effect=fake_publish
        ):
            res_light = gw.execute_command("luz_sala", CommandType.POWER_ON)
            # Envia POWER_ON proposital no portão; deve virar PULSE 1000ms seguro!
            res_gate = gw.execute_command("portao_garagem", CommandType.POWER_ON)
    finally:
        settings.ENABLE_HARDWARE_BRIDGE = prev_bridge

    assert res_light["success"] is True
    assert res_light["mode"] == "mqtt_tasmota"
    assert published[0] == ("cmnd/luz_sala/POWER", "ON", 1, False)

    assert res_gate["success"] is True
    assert res_gate["mode"] == "mqtt_tasmota"
    assert res_gate["command"] == "pulse"
    assert published[1] == ("cmnd/portao_garagem/Backlog", "PulseTime 10; Power ON", 1, False)


def test_tasmota_http_fallback_when_mqtt_offline(temp_db):
    """Se broker MQTT estiver desconectado, Tasmota cai automaticamente para HTTP local /cm?cmnd=..."""
    mem = LongTermMemory(db_path=temp_db)
    gw = EWeLinkGateway(start_zeroconf=False)
    gw.attach_memory(mem)
    gw.pair_open_firmware_device(
        target_device="jardim_deck",
        firmware_type="tasmota",
        mqtt_topic="jardim_deck",
        ip_address="192.168.1.142",
        port=80,
    )

    called_urls = []

    def fake_urlopen(req, timeout=2.0):
        called_urls.append(req.full_url)
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = json.dumps({"POWER": "OFF"}).encode("utf-8")
        resp.__enter__.return_value = resp
        return resp

    prev_bridge = settings.ENABLE_HARDWARE_BRIDGE
    settings.ENABLE_HARDWARE_BRIDGE = True
    try:
        with patch.object(mqtt_gateway, "is_connected", return_value=False), patch(
            "urllib.request.urlopen", side_effect=fake_urlopen
        ):
            res = gw.execute_command("jardim_deck", CommandType.POWER_OFF)
    finally:
        settings.ENABLE_HARDWARE_BRIDGE = prev_bridge

    assert res["success"] is True
    assert res["mode"] == "tasmota_http"
    assert called_urls[0] == "http://192.168.1.142:80/cm?cmnd=POWER%20OFF"


def test_esphome_mqtt_and_rest_fallback(temp_db):
    """Testa driver ESPHome via MQTT e fallback REST web_server (button/press e climate/set)."""
    mem = LongTermMemory(db_path=temp_db)
    gw = EWeLinkGateway(start_zeroconf=False)
    gw.attach_memory(mem)
    gw.pair_open_firmware_device(
        target_device="corredor_cima",
        firmware_type="esphome",
        mqtt_topic="corredor_cima",
        entity_domain="switch",
        entity_id="corredor_cima",
    )
    gw.pair_open_firmware_device(
        target_device="ar_condicionado_sala",
        firmware_type="esphome",
        mqtt_topic="ar_sala",
        ip_address="192.168.1.152",
        port=80,
        entity_domain="climate",
        entity_id="ar_sala",
    )

    published = []
    http_requests = []

    def fake_publish(topic, payload, qos=1, retain=False):
        published.append((topic, payload, qos, retain))
        return True, {"topic": topic, "payload": payload}

    def fake_urlopen(req, timeout=2.0):
        http_requests.append((req.get_method(), req.full_url))
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = b"OK"
        resp.__enter__.return_value = resp
        return resp

    prev_bridge = settings.ENABLE_HARDWARE_BRIDGE
    settings.ENABLE_HARDWARE_BRIDGE = True
    try:
        with patch.object(mqtt_gateway, "is_connected", return_value=True), patch.object(
            mqtt_gateway, "publish_raw", side_effect=fake_publish
        ):
            res_mqtt = gw.execute_command("corredor_cima", CommandType.POWER_ON)

        with patch.object(mqtt_gateway, "is_connected", return_value=False), patch(
            "urllib.request.urlopen", side_effect=fake_urlopen
        ):
            res_http = gw.execute_command(
                "ar_condicionado_sala",
                CommandType.SET_STATE,
                {"temperature": 22, "mode": "cool"},
            )
    finally:
        settings.ENABLE_HARDWARE_BRIDGE = prev_bridge

    assert res_mqtt["mode"] == "mqtt_esphome"
    assert published[0] == ("corredor_cima/switch/corredor_cima/command", "ON", 1, False)

    assert res_http["mode"] == "esphome_http"
    assert http_requests[0][0] == "POST"
    assert http_requests[0][1].startswith("http://192.168.1.152:80/climate/ar_sala/set?")
    assert "mode=cool" in http_requests[0][1]
    assert "target_temperature=22" in http_requests[0][1]


def test_inbound_mqtt_tasmota_lwt_discovery_and_sensor_telemetry(temp_db):
    """Valida recebimento de telemetria Tasmota SENSOR, LWT Online/Offline, POWER e discovery."""
    c_mgr = ContextManager(db_path=temp_db)
    gw = EWeLinkGateway(start_zeroconf=False)
    gw.attach_memory(c_mgr.long_term)
    gw.pair_open_firmware_device("varanda_superior", firmware_type="tasmota", mqtt_topic="tasmota_varanda")

    telemetry_records = []

    def telemetry_cb(dev_id, metric, value, unit):
        telemetry_records.append((dev_id, metric, value, unit))
        c_mgr.short_term.add_telemetry(
            device_id=("sensor_umidade_sala" if metric == "humidity" else dev_id),
            metric=metric,
            value=value,
            unit=unit,
        )

    local_mqtt = MqttOpenFirmwareGateway()
    local_mqtt.attach_callbacks(
        device_update_cb=gw.handle_mqtt_device_update,
        telemetry_cb=telemetry_cb,
        discovery_cb=gw.handle_tasmota_discovery,
        topic_resolver_cb=gw.resolve_device_by_mqtt_topic,
    )

    local_mqtt.handle_incoming_message("tele/tasmota_varanda/LWT", "Offline")
    assert gw.get_device("varanda_superior").online is False

    local_mqtt.handle_incoming_message("tele/tasmota_varanda/LWT", "Online")
    local_mqtt.handle_incoming_message("stat/tasmota_varanda/POWER", "ON")
    dev = gw.get_device("varanda_superior")
    assert dev.online is True
    assert dev.last_state["switch"] == "on"

    local_mqtt.handle_incoming_message(
        "tasmota/discovery/AABBCCDDEEFF/config",
        json.dumps({"ip": "192.168.1.199", "t": "jardim_deck", "fn": ["Jardim Deck"], "rl": [1]}),
    )
    deck = gw.get_device("jardim_deck")
    assert deck.firmware_type == "tasmota"
    assert deck.ip_address == "192.168.1.199"
    assert deck.port == 80

    local_mqtt.handle_incoming_message(
        "tele/sensor_temp_sala/SENSOR",
        json.dumps({"Time": "2026-11-11T02:00:00", "DS18B20": {"Temperature": 29.4, "Humidity": 58}, "TempUnit": "C"}),
    )
    assert ("sensor_temp_sala", "temperature", 29.4, "C") in telemetry_records
    assert ("sensor_temp_sala", "humidity", 58.0, "%") in telemetry_records
    latest = c_mgr.short_term.get_latest_telemetry()
    assert latest["sensor_temp_sala"]["value"] == 29.4
    assert latest["sensor_umidade_sala"]["value"] == 58.0


def test_sqlite_legacy_migration_and_mqtt_pair_endpoints(temp_db):
    """Garante migração segura de tabela antiga SQLite e endpoints /mqtt/devices/pair + /mqtt/status."""
    # Cria tabela no formato antigo (sem colunas MQTT/open firmware)
    with sqlite3.connect(temp_db) as conn:
        conn.execute("""
            CREATE TABLE ewelink_device_registry (
                device_id TEXT PRIMARY KEY,
                ewelink_device_id TEXT,
                device_key TEXT,
                self_apikey TEXT DEFAULT '123',
                name TEXT,
                device_type TEXT,
                group_name TEXT,
                ip_address TEXT,
                port INTEGER DEFAULT 8081,
                is_multichannel INTEGER DEFAULT 0,
                channel_outlet INTEGER DEFAULT 0,
                online INTEGER DEFAULT 1,
                last_state TEXT DEFAULT '{}',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()

    prev_db = settings.DB_PATH
    prev_mock = settings.USE_MOCK_ENGINE
    prev_bridge = settings.ENABLE_HARDWARE_BRIDGE
    prev_mqtt = settings.MQTT_ENABLED

    import app.main as app_main
    prev_cm = app_main.context_mgr
    app_main.context_mgr = None

    settings.DB_PATH = temp_db
    settings.USE_MOCK_ENGINE = True
    settings.ENABLE_HARDWARE_BRIDGE = False
    settings.MQTT_ENABLED = False

    try:
        with TestClient(app) as client:
            status_resp = client.get("/mqtt/status")
            assert status_resp.status_code == 200
            assert "broker_host" in status_resp.json()

            pair_resp = client.post("/mqtt/devices/pair", json={
                "target_device": "jardim_principal",
                "firmware_type": "tasmota",
                "mqtt_topic": "jardim_principal",
                "ip_address": "192.168.1.146",
                "port": 80,
            })
            assert pair_resp.status_code == 200
            data = pair_resp.json()
            assert data["success"] is True
            assert data["device"]["firmware_type"] == "tasmota"
            assert data["device"]["mqtt_topic"] == "jardim_principal"
            assert data["device"]["open_firmware_ready"] is True

        # Reinício offline preserva configuração Tasmota salva no SQLite
        gw2 = EWeLinkGateway(start_zeroconf=False)
        gw2.attach_memory(LongTermMemory(db_path=temp_db))
        jardin = gw2.get_device("jardim_principal")
        assert jardin.firmware_type == "tasmota"
        assert jardin.mqtt_topic == "jardim_principal"
        assert jardin.ip_address == "192.168.1.146"
        assert jardin.port == 80
    finally:
        ewelink_gateway._long_term_memory = None
        app_main.context_mgr = prev_cm
        settings.DB_PATH = prev_db
        settings.USE_MOCK_ENGINE = prev_mock
        settings.ENABLE_HARDWARE_BRIDGE = prev_bridge
        settings.MQTT_ENABLED = prev_mqtt
