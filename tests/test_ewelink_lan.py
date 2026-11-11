"""
Testes do cliente LAN criptografado AES-128-CBC (mDNS TXT, multi-canal switches,
inching portao_garagem e persistência offline de devicekey no SQLite).
"""

import io
import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.ewelink_gateway import (
    EWeLinkGateway,
    decrypt_lan_payload,
    encrypt_lan_payload,
    ewelink_gateway,
)
from app.main import app
from app.memory import LongTermMemory
from app.schemas import CommandType


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.unlink(path)


def test_aes128_cbc_encrypt_decrypt_roundtrip():
    """Valida derivação de chave MD5(devicekey), IV Base64 16 bytes e padding PKCS7."""
    device_key = "11112222-3333-4444-5555-666677778888"
    inner = {"switches": [{"switch": "on", "outlet": 1}]}

    encrypted = encrypt_lan_payload(inner, device_key)
    assert "data" in encrypted and "iv" in encrypted

    decrypted = decrypt_lan_payload(encrypted["data"], encrypted["iv"], device_key)
    assert decrypted == inner


def test_encrypted_lan_single_and_multichannel_requests(temp_db):
    """Garante envio AES-128-CBC para /zeroconf/switch, /zeroconf/switches e /zeroconf/pulse."""
    mem = LongTermMemory(db_path=temp_db)
    gw = EWeLinkGateway(start_zeroconf=False)
    gw.attach_memory(mem)

    gw.pair_device(
        target_device="luz_sala",
        ewelink_device_id="1000aaaaaa",
        device_key="aaabbbcc-1111-2222-3333-444455556666",
        self_apikey="owner-apikey-xyz",
        ip_address="192.168.1.151",
        is_multichannel=True,
        channel_outlet=1,
    )
    gw.pair_device(
        target_device="portao_garagem",
        ewelink_device_id="1000bbbbbb",
        device_key="99998888-1111-2222-3333-444455556666",
        self_apikey="owner-apikey-xyz",
        ip_address="192.168.1.150",
        is_multichannel=False,
    )

    captured_requests = []

    def fake_urlopen(req, timeout=1.5):
        body = json.loads(req.data.decode("utf-8"))
        captured_requests.append((req.full_url, body))
        resp = MagicMock()
        resp.read.return_value = json.dumps({"seq": 1, "error": 0}).encode("utf-8")
        resp.__enter__.return_value = resp
        return resp

    prev_bridge = settings.ENABLE_HARDWARE_BRIDGE
    settings.ENABLE_HARDWARE_BRIDGE = True
    try:
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res_light = gw.execute_command("luz_sala", CommandType.POWER_ON)
            res_gate = gw.execute_command("portao_garagem", CommandType.POWER_ON)
    finally:
        settings.ENABLE_HARDWARE_BRIDGE = prev_bridge

    assert res_light["mode"] == "lan_encrypted_aes"
    url_light, body_light = captured_requests[0]
    assert url_light == "http://192.168.1.151:8081/zeroconf/switches"
    assert body_light["encrypt"] is True
    assert body_light["deviceid"] == "1000aaaaaa"
    assert body_light["selfApikey"] == "owner-apikey-xyz"
    assert decrypt_lan_payload(
        body_light["data"],
        body_light["iv"],
        "aaabbbcc-1111-2222-3333-444455556666",
    ) == {"switches": [{"switch": "on", "outlet": 1}]}

    # Portão deve converter power_on -> pulseWidth 1000ms criptografado
    assert res_gate["mode"] == "lan_encrypted_aes"
    assert res_gate["command"] == "pulse"
    url_gate, body_gate = captured_requests[1]
    assert url_gate == "http://192.168.1.150:8081/zeroconf/pulse"
    assert decrypt_lan_payload(
        body_gate["data"],
        body_gate["iv"],
        "99998888-1111-2222-3333-444455556666",
    ) == {"pulse": "on", "pulseWidth": 1000}


def test_sync_devices_persists_devicekey_for_offline_restart(temp_db):
    """
    Simula sincronização única com a CoolKit OpenAPI v2 (/v2/device/thing),
    verifica persistência de devicekey no SQLite e recarregamento totalmente offline.
    """
    mem = LongTermMemory(db_path=temp_db)
    gw1 = EWeLinkGateway(start_zeroconf=False)
    gw1.attach_memory(mem)

    mock_cloud_response = {
        "error": 0,
        "data": {
            "thingList": [
                {
                    "itemData": {
                        "name": "Jardim Deck",
                        "deviceid": "1000deck01",
                        "devicekey": "secret-deck-key-123456",
                        "apikey": "owner-api-123",
                        "online": True,
                        "params": {
                            "switches": [
                                {"switch": "off", "outlet": 0},
                                {"switch": "on", "outlet": 1},
                            ]
                        },
                    }
                }
            ]
        },
    }

    fake_resp = MagicMock()
    fake_resp.read.return_value = json.dumps(mock_cloud_response).encode("utf-8")
    fake_resp.__enter__.return_value = fake_resp

    prev_token = settings.EWELINK_CLOUD_TOKEN
    settings.EWELINK_CLOUD_TOKEN = "dummy-oauth-token"
    try:
        with patch("urllib.request.urlopen", return_value=fake_resp):
            sync_res = gw1.sync_devices()
    finally:
        settings.EWELINK_CLOUD_TOKEN = prev_token

    assert sync_res["success"] is True
    assert sync_res["synced_count"] == 1

    # Nova instância após reinício offline (sem cloud token) recupera chave do SQLite
    gw2 = EWeLinkGateway(start_zeroconf=False)
    gw2.attach_memory(LongTermMemory(db_path=temp_db))
    deck = gw2.get_device("jardim_deck")
    assert deck is not None
    assert deck.ewelink_device_id == "1000deck01"
    assert deck.device_key == "secret-deck-key-123456"
    assert deck.self_apikey == "owner-api-123"
    assert deck.is_multichannel is True


def test_mdns_txt_encrypted_state_decoding(temp_db):
    """Valida decodificação de pacotes TXT _ewelink._tcp.local. divididos em data1+data2."""
    mem = LongTermMemory(db_path=temp_db)
    gw = EWeLinkGateway(start_zeroconf=False)
    gw.attach_memory(mem)
    gw.pair_device(
        target_device="corredor_cima",
        ewelink_device_id="1000corr01",
        device_key="corr-secret-key-987654",
    )

    enc = encrypt_lan_payload({"switch": "on", "rssi": -54}, "corr-secret-key-987654")
    half = len(enc["data"]) // 2

    fake_info = MagicMock()
    fake_info.addresses = [bytes([192, 168, 1, 177])]
    fake_info.port = 8081
    fake_info.properties = {
        b"id": b"1000corr01",
        b"encrypt": b"true",
        b"iv": enc["iv"].encode("utf-8"),
        b"data1": enc["data"][:half].encode("utf-8"),
        b"data2": enc["data"][half:].encode("utf-8"),
    }

    fake_zc = MagicMock()
    fake_zc.get_service_info.return_value = fake_info

    from zeroconf import ServiceStateChange
    gw._on_service_state_change(
        fake_zc, "_ewelink._tcp.local.", "eWeLink_1000corr01._ewelink._tcp.local.", ServiceStateChange.Added
    )

    dev = gw.get_device("corredor_cima")
    assert dev.ip_address == "192.168.1.177"
    assert dev.online is True
    assert dev.last_state["switch"] == "on"


def test_pair_endpoint_masks_device_key_in_public_list(temp_db):
    """Valida endpoint /ewelink/devices/pair e garantia de que device_key não vaza no GET."""
    prev_db = settings.DB_PATH
    prev_mock = settings.USE_MOCK_ENGINE
    settings.DB_PATH = temp_db
    settings.USE_MOCK_ENGINE = True
    try:
        with TestClient(app) as client:
            pair_resp = client.post("/ewelink/devices/pair", json={
                "target_device": "jardim_principal",
                "ewelink_device_id": "1000jardin",
                "device_key": "super-secret-lan-key",
                "self_apikey": "owner-secret-api",
                "ip_address": "192.168.1.188",
            })
            assert pair_resp.status_code == 200
            pair_data = pair_resp.json()
            assert pair_data["success"] is True
            assert pair_data["device"]["has_device_key"] is True
            assert pair_data["device"]["lan_encryption_ready"] is True
            assert "super-secret-lan-key" not in json.dumps(pair_data)

            list_resp = client.get("/ewelink/devices")
            assert list_resp.status_code == 200
            assert "super-secret-lan-key" not in list_resp.text
            assert "owner-secret-api" not in list_resp.text
    finally:
        ewelink_gateway._long_term_memory = None
        settings.DB_PATH = prev_db
        settings.USE_MOCK_ENGINE = prev_mock
