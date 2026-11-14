"""Regressões para despacho físico, persistência e segurança do portão."""
from unittest.mock import MagicMock, patch
import json
import pytest
from app.config import settings
from app.ewelink_gateway import EWeLinkGateway
from app.memory import LongTermMemory
from app.mqtt_gateway import MqttOpenFirmwareGateway
from app.schemas import CommandType


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setattr(settings, 'SONOFF_DEVICE_MAP', {})
    monkeypatch.setattr(settings, 'EWELINK_DEVICE_KEYS', {})
    monkeypatch.setattr(settings, 'OPEN_FIRMWARE_DEVICE_MAP', {})
    monkeypatch.setattr(settings, 'ENABLE_HARDWARE_BRIDGE', True)
    return EWeLinkGateway(start_zeroconf=False)


def test_no_placeholder_addresses(gateway):
    assert all(not d.ip_address for d in gateway.devices.values())


def test_address_override_preserves_sqlite_keys(gateway, tmp_path, monkeypatch):
    mem = LongTermMemory(db_path=str(tmp_path / 'registry.db'))
    gateway.attach_memory(mem)
    gateway.pair_device('luz_sala', '1000aaaaaa', 'secret', ip_address='192.168.1.151')
    monkeypatch.setattr(settings, 'SONOFF_DEVICE_MAP', {'luz_sala': '192.168.100.50'})
    other = EWeLinkGateway(start_zeroconf=False)
    other.attach_memory(mem)
    assert other.get_device('luz_sala').ip_address == '192.168.100.50'
    assert other.get_device('luz_sala').device_key == 'secret'
    other.update_device_address('luz_sala', '192.168.100.51')
    row = next(d for d in mem.get_ewelink_devices() if d['device_id'] == 'luz_sala')
    assert row['ip_address'] == '192.168.100.51'
    assert row['device_key'] == 'secret'


def test_offline_device_retries_lan_and_failure_is_not_simulated(gateway):
    dev = gateway.get_device('varanda_superior')
    dev.ip_address = '192.168.100.50'
    dev.online = False
    before = dict(dev.last_state)
    with patch.object(gateway, '_send_lan_diy', return_value=(False, 'timeout')) as lan, patch.object(gateway, '_send_cloud_coolkit', return_value=(False, 'no token')):
        result = gateway.execute_command(dev.device_id, CommandType.POWER_ON)
    lan.assert_called_once()
    assert result['success'] is False
    assert result['mode'] == 'hardware_unreachable'
    assert dev.last_state == before


@pytest.mark.parametrize('command', [CommandType.POWER_ON, CommandType.PULSE])
def test_gate_forces_exact_1000ms_and_activates_after_configuration(gateway, command):
    gateway.update_device_address('portao_garagem', '192.168.100.50')
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = b'{"error":0}'
    with patch('urllib.request.urlopen', return_value=response) as send, patch('time.sleep'):
        result = gateway.execute_command('portao_garagem', command, {'pulse_duration_ms': 90000, 'outlet': 7})
    assert result['params'] == {'pulse_duration_ms': 1000}
    assert [r.args[0].full_url.rsplit('/', 1)[-1] for r in send.call_args_list] == ['pulse', 'switch']
    assert json.loads(send.call_args_list[0].args[0].data)['data']['pulseWidth'] == 1000
    assert json.loads(send.call_args_list[1].args[0].data)['data'] == {'switch':'on'}


def test_failed_pulse_configuration_never_sends_on(gateway):
    dev = gateway.get_device('portao_garagem')
    dev.ip_address = '192.168.100.50'
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = b'{"error":401}'
    with patch('urllib.request.urlopen', return_value=response) as send:
        ok, detail = gateway._send_lan_diy(dev, CommandType.PULSE, {'pulse_duration_ms':1000})
    assert not ok
    assert 'device_key' in detail
    assert send.call_count == 1


def test_mqtt_power_updates_existing_topology_and_sqlite(gateway, tmp_path):
    mem = LongTermMemory(db_path=str(tmp_path / 'registry.db'))
    gateway.attach_memory(mem)
    mqtt = MqttOpenFirmwareGateway()
    mqtt.attach_callbacks(device_update_cb=gateway.handle_mqtt_device_update, topic_resolver_cb=gateway.resolve_device_by_mqtt_topic)
    mqtt.handle_incoming_message('stat/luz_sala/POWER', 'ON')
    assert gateway.get_device('luz_sala').online
    row = next(d for d in mem.get_ewelink_devices() if d['device_id']=='luz_sala')
    assert row['online']
    assert row['last_state']['switch']=='on'

@pytest.mark.parametrize('firmware', ['esphome', 'mqtt_json'])
def test_gate_rejects_drivers_without_guaranteed_timer(gateway, firmware):
    gateway.pair_open_firmware_device('portao_garagem', firmware_type=firmware)
    with patch('urllib.request.urlopen') as http:
        result = gateway.execute_command('portao_garagem', CommandType.POWER_ON)
    assert result['success'] is False
    assert result['mode'] == 'unsafe_pulse_configuration'
    http.assert_not_called()


def test_gate_rejects_set_state_bypass(gateway):
    result = gateway.execute_command('portao_garagem', CommandType.SET_STATE, {'switch':'on'})
    assert result['success'] is False
    assert result['mode'] == 'unsafe_gate_command'


def test_pair_without_key_preserves_existing_secret(gateway, tmp_path):
    mem = LongTermMemory(db_path=str(tmp_path / 'registry.db'))
    gateway.attach_memory(mem)
    gateway.pair_device('luz_sala', '1000aaaaaa', 'secret')
    gateway.pair_device('luz_sala', '1000aaaaaa', ip_address='192.168.100.50')
    assert gateway.get_device('luz_sala').device_key == 'secret'
    gateway.pair_device('luz_sala', '1000aaaaaa', device_key='')
    row = next(d for d in mem.get_ewelink_devices() if d['device_id']=='luz_sala')
    assert not row['device_key']

def test_address_api_validation_and_secret_masking(gateway):
    from fastapi.testclient import TestClient
    from app.main import app
    gateway.pair_device('luz_sala', '1000aaaaaa', 'lan-secret')
    client = TestClient(app)
    with patch('app.main.ewelink_gateway', gateway):
        response = client.patch('/ewelink/devices/luz_sala/address', params={'ip_address':'192.168.100.50'})
        assert response.status_code == 200
        assert response.json()['device']['ip_address'] == '192.168.100.50'
        assert 'lan-secret' not in response.text
        assert client.patch('/ewelink/devices/luz_sala/address', params={'ip_address':'invalid'}).status_code == 422
        assert client.patch('/ewelink/devices/luz_sala/address', params={'ip_address':'192.168.100.50','port':70000}).status_code == 422
        assert client.patch('/ewelink/devices/missing/address', params={'ip_address':'192.168.100.50'}).status_code == 404

def test_decision_payload_also_forces_gate_duration():
    from app.schemas import DecisionOutput
    decision = DecisionOutput(action='execute', target_device='portao_garagem', command='power_on', params={'pulse_duration_ms':60000}, response_text='Abrir')
    assert decision.command == CommandType.PULSE
    assert decision.to_gateway_payload()['pulse']['pulseWidth'] == 1000
