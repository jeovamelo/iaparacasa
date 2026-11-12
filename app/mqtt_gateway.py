"""
Cliente MQTT local e drivers de Firmware Aberto (Tasmota, ESPHome e JSON MQTT genérico)
para operação 100% offline/LAN via broker Mosquitto sem dependência de nuvem eWeLink.
"""

from datetime import datetime
import base64
import json
import logging
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode
import urllib.error
import urllib.request

from app.config import settings
from app.schemas import CommandType

logger = logging.getLogger("iaparacasa.mqtt")

try:
    import paho.mqtt.client as paho_mqtt
    try:
        from paho.mqtt.enums import CallbackAPIVersion as PahoCallbackAPIVersion
    except ImportError:  # paho-mqtt 1.x
        PahoCallbackAPIVersion = None
    PAHO_AVAILABLE = True
except ImportError:
    paho_mqtt = None
    PahoCallbackAPIVersion = None
    PAHO_AVAILABLE = False


OPEN_FIRMWARE_TYPES = ("tasmota", "esphome", "mqtt_json")


def ms_to_tasmota_pulsetime(pulse_ms: int) -> int:
    """
    Converte milissegundos para a escala nativa PulseTime do Tasmota:
    - 1..111 equivalem a 0.1s .. 11.1s (ex: 1000ms -> PulseTime 10)
    - 112..64900 equivalem a (valor - 100) segundos
    """
    if pulse_ms <= 0:
        pulse_ms = 1000
    if pulse_ms <= 11100:
        return max(1, int(round(pulse_ms / 100.0)))
    seconds = max(12, int(round(pulse_ms / 1000.0)))
    return min(64900, 100 + seconds)


def build_tasmota_command(
    command: CommandType,
    params: Optional[Dict[str, Any]] = None,
    is_multichannel: bool = False,
    channel_outlet: int = 0,
) -> Tuple[str, str]:
    """
    Traduz CommandType do iaparacasa para subtópico cmnd e payload do Tasmota.
    Para pulso/inching em portões envia Backlog PulseTime+Power ON para desligamento garantido no hardware.
    """
    params = dict(params or {})
    channel_num = int(channel_outlet or 0) + 1
    suffix = str(channel_num) if (is_multichannel or channel_num > 1) else ""

    if command == CommandType.POWER_ON:
        return f"POWER{suffix}", "ON"
    if command == CommandType.POWER_OFF:
        return f"POWER{suffix}", "OFF"
    if command == CommandType.PULSE:
        pulse_ms = int(params.get("pulse_duration_ms", params.get("pulse_ms", 1000)))
        pulse_time = ms_to_tasmota_pulsetime(pulse_ms)
        return "Backlog", f"PulseTime{suffix} {pulse_time}; Power{suffix} ON"

    if "cmnd" in params:
        return str(params["cmnd"]), str(params.get("value", ""))
    if "switch" in params:
        sw = str(params["switch"]).strip().lower()
        return f"POWER{suffix}", ("ON" if sw in ("on", "true", "1") else "OFF")
    if "temperature" in params:
        ir_payload = {
            "Vendor": str(params.get("vendor", "COOLIX")),
            "Model": -1,
            "Power": "On",
            "Mode": str(params.get("mode", "Cool")).capitalize(),
            "Celsius": "On",
            "Temp": int(params["temperature"]),
        }
        return "IRHVAC", json.dumps(ir_payload, separators=(",", ":"))
    return f"POWER{suffix}", "ON"


def send_tasmota_http(
    ip_address: str,
    cmnd: str,
    payload: str = "",
    port: int = 80,
    username: str = "",
    password: str = "",
) -> Tuple[bool, Any]:
    """Envia comando direto para a API HTTP local do Tasmota (/cm?cmnd=...)."""
    full_cmnd = f"{cmnd} {payload}".strip()
    url = f"http://{ip_address}:{port}/cm?cmnd={quote(full_cmnd)}"
    headers: Dict[str, str] = {}
    if username or password:
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = {"raw": raw}
            return (200 <= resp.status < 300), parsed
    except Exception as e:
        return False, str(e)


def send_esphome_http(
    ip_address: str,
    command: CommandType,
    params: Optional[Dict[str, Any]] = None,
    entity_domain: str = "switch",
    entity_id: str = "relay",
    port: int = 80,
) -> Tuple[bool, Any]:
    """Envia comando direto via REST API local do web_server do ESPHome."""
    params = dict(params or {})
    domain = (entity_domain or "switch").strip().lower()
    slug = (entity_id or "relay").strip()

    if command == CommandType.POWER_ON:
        path = f"/{domain}/{slug}/turn_on"
    elif command == CommandType.POWER_OFF:
        path = f"/{domain}/{slug}/turn_off"
    elif command == CommandType.PULSE:
        if domain == "button":
            path = f"/button/{slug}/press"
        else:
            path = f"/{domain}/{slug}/turn_on"
    elif domain == "climate" or "temperature" in params:
        query: Dict[str, Any] = {}
        if "mode" in params:
            query["mode"] = str(params["mode"]).lower()
        if "temperature" in params:
            query["target_temperature"] = params["temperature"]
        qs = f"?{urlencode(query)}" if query else ""
        path = f"/climate/{slug}/set{qs}"
    else:
        sw = str(params.get("switch", "on")).strip().lower()
        action = "turn_on" if sw in ("on", "true", "1") else "turn_off"
        path = f"/{domain}/{slug}/{action}"

    url = f"http://{ip_address}:{port}{path}"
    try:
        req = urllib.request.Request(url, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            return (200 <= resp.status < 300), {"http_status": resp.status, "body": raw, "url": url}
    except Exception as e:
        return False, str(e)


def extract_tasmota_telemetry(payload: Dict[str, Any]) -> List[Tuple[str, float, str]]:
    """Extrai métricas ambientais e elétricas padronizadas do JSON tele/<topico>/SENSOR do Tasmota."""
    readings: List[Tuple[str, float, str]] = []
    temp_unit = str(payload.get("TempUnit", "C"))

    def _walk(obj: Any):
        if isinstance(obj, dict):
            for key, val in obj.items():
                key_low = str(key).lower()
                if key_low in ("temperature", "temp") and isinstance(val, (int, float)):
                    readings.append(("temperature", float(val), temp_unit))
                elif key_low in ("humidity", "hum") and isinstance(val, (int, float)):
                    readings.append(("humidity", float(val), "%"))
                elif key_low == "power" and isinstance(val, (int, float)):
                    readings.append(("power", float(val), "W"))
                elif key_low == "voltage" and isinstance(val, (int, float)):
                    readings.append(("voltage", float(val), "V"))
                elif key_low in ("illuminance", "lux") and isinstance(val, (int, float)):
                    readings.append(("luminosity", float(val), "lux"))
                elif isinstance(val, dict):
                    _walk(val)

    _walk(payload)
    return readings


class MqttOpenFirmwareGateway:
    """
    Gerencia conexão MQTT assíncrona com o broker Mosquitto local e troca de mensagens
    bidirecional para dispositivos Tasmota, ESPHome e JSON MQTT genérico.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._client: Any = None
        self._connected: bool = False
        self._last_error: Optional[str] = None
        self._started: bool = False
        self._connect_event = threading.Event()
        self._subscribed_topics: List[str] = []
        self._messages_received: int = 0
        self._messages_published: int = 0
        self._last_message_at: Optional[str] = None

        # Callbacks desacoplados para sincronizar estado de dispositivos e telemetria
        self._device_update_cb: Optional[Callable[[str, Dict[str, Any]], None]] = None
        self._telemetry_cb: Optional[Callable[[str, str, float, str], None]] = None
        self._discovery_cb: Optional[Callable[[Dict[str, Any]], None]] = None
        self._topic_resolver_cb: Optional[Callable[[str], Optional[str]]] = None

    def attach_callbacks(
        self,
        device_update_cb: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        telemetry_cb: Optional[Callable[[str, str, float, str], None]] = None,
        discovery_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
        topic_resolver_cb: Optional[Callable[[str], Optional[str]]] = None,
    ) -> None:
        self._device_update_cb = device_update_cb
        self._telemetry_cb = telemetry_cb
        self._discovery_cb = discovery_cb
        self._topic_resolver_cb = topic_resolver_cb

    def _create_client(self):
        client_id = f"{getattr(settings, 'MQTT_CLIENT_ID', 'iaparacasa-edge')}"
        if PahoCallbackAPIVersion is not None:
            client = paho_mqtt.Client(
                callback_api_version=PahoCallbackAPIVersion.VERSION2,
                client_id=client_id,
            )
        else:
            client = paho_mqtt.Client(client_id=client_id)

        username = getattr(settings, "MQTT_USERNAME", "")
        password = getattr(settings, "MQTT_PASSWORD", "")
        if username:
            client.username_pw_set(username, password)

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        return client

    def start(self, wait_timeout: float = 0.5) -> bool:
        """Inicia conexão MQTT não bloqueante caso MQTT_ENABLED esteja ativo."""
        if not getattr(settings, "MQTT_ENABLED", False):
            return False
        if not PAHO_AVAILABLE:
            self._last_error = "Biblioteca paho-mqtt não instalada."
            logger.warning(self._last_error)
            return False

        with self._lock:
            if self._started and self._client is not None:
                return self._connected
            try:
                host = getattr(settings, "MQTT_BROKER_HOST", "localhost")
                port = int(getattr(settings, "MQTT_BROKER_PORT", 1883))
                self._connect_event.clear()
                self._client = self._create_client()
                self._client.connect_async(host, port, keepalive=60)
                self._client.loop_start()
                self._started = True
                self._last_error = None
                logger.info(f"Cliente MQTT iniciado em segundo plano ({host}:{port}).")
            except Exception as e:
                self._last_error = str(e)
                self._connected = False
                logger.warning(f"Falha ao iniciar cliente MQTT: {e}")
                return False

        if wait_timeout > 0:
            self._connect_event.wait(timeout=wait_timeout)
        return self._connected

    def stop(self) -> None:
        with self._lock:
            self._started = False
            self._connected = False
            if self._client is not None:
                try:
                    self._client.loop_stop()
                    self._client.disconnect()
                except Exception:
                    pass
                self._client = None

    def is_connected(self) -> bool:
        return bool(self._connected and self._client is not None)

    def get_status(self) -> Dict[str, Any]:
        return {
            "paho_available": PAHO_AVAILABLE,
            "enabled": bool(getattr(settings, "MQTT_ENABLED", False)),
            "connected": self.is_connected(),
            "broker_host": getattr(settings, "MQTT_BROKER_HOST", "localhost"),
            "broker_port": int(getattr(settings, "MQTT_BROKER_PORT", 1883)),
            "topic_prefix": getattr(settings, "MQTT_TOPIC_PREFIX", "iaparacasa"),
            "subscribed_topics": list(self._subscribed_topics),
            "messages_received": self._messages_received,
            "messages_published": self._messages_published,
            "last_message_at": self._last_message_at,
            "last_error": self._last_error,
        }

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        rc_val = getattr(reason_code, "value", reason_code)
        if rc_val == 0 or str(reason_code).lower() == "success":
            self._connected = True
            self._last_error = None
            prefix = getattr(settings, "MQTT_TOPIC_PREFIX", "iaparacasa")
            topics = [
                ("stat/+/+", 0),
                ("tele/+/+", 0),
                ("tasmota/discovery/+/config", 0),
                ("+/switch/+/state", 0),
                ("+/status", 0),
                (f"{prefix}/+/state", 0),
                (f"{prefix}/+/telemetry", 0),
            ]
            for topic, qos in topics:
                client.subscribe(topic, qos=qos)
            self._subscribed_topics = [t for t, _ in topics]
            logger.info("MQTT conectado ao broker e inscrito em tópicos Tasmota/ESPHome/iaparacasa.")
        else:
            self._connected = False
            self._last_error = f"Falha de conexão MQTT (code={reason_code})"
            logger.warning(self._last_error)
        self._connect_event.set()

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code=None, properties=None):
        self._connected = False
        if reason_code not in (None, 0) and str(reason_code).lower() != "normal disconnection":
            self._last_error = f"MQTT desconectado ({reason_code})"

    def _on_message(self, client, userdata, msg):
        try:
            payload_text = msg.payload.decode("utf-8", errors="ignore").strip()
            self.handle_incoming_message(msg.topic, payload_text)
        except Exception as e:
            logger.warning(f"Erro ao processar mensagem MQTT no tópico {getattr(msg, 'topic', '?')}: {e}")

    def _resolve_device_id(self, topic_segment: str) -> str:
        if self._topic_resolver_cb:
            resolved = self._topic_resolver_cb(topic_segment)
            if resolved:
                return resolved
        return topic_segment.strip().lower().replace(" ", "_").replace("-", "_")

    def handle_incoming_message(self, topic: str, payload_text: str) -> None:
        """Processa mensagens recebidas de Tasmota, ESPHome ou sensores JSON locais."""
        self._messages_received += 1
        self._last_message_at = datetime.now().isoformat()
        parts = [p for p in topic.split("/") if p]
        if not parts:
            return

        # 1. Tasmota Discovery (tasmota/discovery/<mac>/config)
        if len(parts) >= 4 and parts[0] == "tasmota" and parts[1] == "discovery" and parts[-1] == "config":
            try:
                cfg = json.loads(payload_text)
                if self._discovery_cb and isinstance(cfg, dict):
                    self._discovery_cb(cfg)
            except Exception:
                pass
            return

        # 2. Tasmota stat/<topic>/POWER*, RESULT e tele/<topic>/LWT, STATE, SENSOR
        if len(parts) >= 3 and parts[0] in ("stat", "tele"):
            dev_topic = parts[1]
            sub = parts[2].upper()
            device_id = self._resolve_device_id(dev_topic)

            if parts[0] == "tele" and sub == "LWT":
                is_online = payload_text.lower() == "online"
                if self._device_update_cb:
                    self._device_update_cb(device_id, {"online": is_online, "mqtt_topic": dev_topic})
                return

            if sub.startswith("POWER"):
                switch_val = "on" if payload_text.upper() == "ON" else "off"
                if self._device_update_cb:
                    self._device_update_cb(device_id, {
                        "online": True,
                        "mqtt_topic": dev_topic,
                        "last_state": {"switch": switch_val},
                    })
                return

            if sub in ("RESULT", "STATE", "SENSOR"):
                try:
                    data = json.loads(payload_text)
                except Exception:
                    return
                if not isinstance(data, dict):
                    return

                if sub in ("RESULT", "STATE"):
                    state_patch: Dict[str, Any] = {}
                    for k, v in data.items():
                        if str(k).upper().startswith("POWER") and isinstance(v, str):
                            state_patch["switch"] = "on" if v.upper() == "ON" else "off"
                    if state_patch and self._device_update_cb:
                        self._device_update_cb(device_id, {
                            "online": True,
                            "mqtt_topic": dev_topic,
                            "last_state": state_patch,
                        })

                if sub == "SENSOR" and self._telemetry_cb:
                    for metric, val, unit in extract_tasmota_telemetry(data):
                        self._telemetry_cb(device_id, metric, val, unit)
                return

        # 3. ESPHome MQTT (<prefix>/status ou <prefix>/switch/<entity>/state)
        if len(parts) == 2 and parts[1] == "status":
            device_id = self._resolve_device_id(parts[0])
            if self._device_update_cb:
                self._device_update_cb(device_id, {
                    "online": payload_text.lower() == "online",
                    "mqtt_topic": parts[0],
                })
            return

        if len(parts) >= 4 and parts[1] in ("switch", "light") and parts[-1] == "state":
            device_id = self._resolve_device_id(parts[0])
            switch_val = "on" if payload_text.upper() == "ON" else "off"
            if self._device_update_cb:
                self._device_update_cb(device_id, {
                    "online": True,
                    "mqtt_topic": parts[0],
                    "last_state": {"switch": switch_val},
                })
            return

        # 4. Genérico <prefix>/<device>/state ou <prefix>/<device>/telemetry
        prefix = getattr(settings, "MQTT_TOPIC_PREFIX", "iaparacasa")
        if len(parts) >= 3 and parts[0] == prefix:
            device_id = self._resolve_device_id(parts[1])
            kind = parts[2].lower()
            try:
                data = json.loads(payload_text)
            except Exception:
                data = {"raw": payload_text}

            if kind == "state" and isinstance(data, dict) and self._device_update_cb:
                self._device_update_cb(device_id, {"online": True, "last_state": data})
            elif kind == "telemetry" and isinstance(data, dict) and self._telemetry_cb:
                metric = str(data.get("metric", "temperature"))
                if "value" in data:
                    self._telemetry_cb(
                        device_id,
                        metric,
                        float(data["value"]),
                        str(data.get("unit", "")),
                    )

    def publish_raw(
        self,
        topic: str,
        payload: str,
        qos: int = 1,
        retain: bool = False,
    ) -> Tuple[bool, Any]:
        """Publica mensagem no broker MQTT (sem retain em comandos de atuação por segurança)."""
        with self._lock:
            if not self.is_connected():
                return False, self._last_error or "Broker MQTT desconectado."
            try:
                info = self._client.publish(topic, payload=payload, qos=qos, retain=retain)
                rc = getattr(info, "rc", 0)
                if rc != 0:
                    return False, f"Erro ao publicar no tópico '{topic}' (rc={rc})"
                self._messages_published += 1
                return True, {"topic": topic, "payload": payload, "qos": qos, "retain": retain}
            except Exception as e:
                self._last_error = str(e)
                return False, str(e)

    def publish_tasmota(
        self,
        mqtt_topic: str,
        command: CommandType,
        params: Optional[Dict[str, Any]] = None,
        is_multichannel: bool = False,
        channel_outlet: int = 0,
    ) -> Tuple[bool, Any]:
        cmnd_subtopic, payload = build_tasmota_command(
            command=command,
            params=params,
            is_multichannel=is_multichannel,
            channel_outlet=channel_outlet,
        )
        clean_topic = mqtt_topic.strip("/")
        full_topic = f"cmnd/{clean_topic}/{cmnd_subtopic}"
        return self.publish_raw(full_topic, payload, qos=1, retain=False)

    def publish_esphome(
        self,
        mqtt_topic: str,
        command: CommandType,
        params: Optional[Dict[str, Any]] = None,
        entity_domain: str = "switch",
        entity_id: str = "relay",
    ) -> Tuple[bool, Any]:
        params = dict(params or {})
        domain = (entity_domain or "switch").strip().lower()
        slug = (entity_id or "relay").strip()
        prefix = mqtt_topic.strip("/")
        full_topic = f"{prefix}/{domain}/{slug}/command"

        if command == CommandType.POWER_ON:
            payload = "ON"
        elif command == CommandType.POWER_OFF:
            payload = "OFF"
        elif command == CommandType.PULSE:
            payload = "PRESS" if domain == "button" else "ON"
        else:
            sw = str(params.get("switch", "ON")).strip().upper()
            payload = "ON" if sw in ("ON", "TRUE", "1") else "OFF"

        return self.publish_raw(full_topic, payload, qos=1, retain=False)

    def publish_json(self, mqtt_topic: str, payload_dict: Dict[str, Any]) -> Tuple[bool, Any]:
        prefix = getattr(settings, "MQTT_TOPIC_PREFIX", "iaparacasa").strip("/")
        clean_topic = mqtt_topic.strip("/")
        full_topic = clean_topic if "/" in clean_topic else f"{prefix}/{clean_topic}/cmd"
        return self.publish_raw(
            full_topic,
            json.dumps(payload_dict, separators=(",", ":")),
            qos=1,
            retain=False,
        )


# Instância global compartilhada pelo ciclo de vida FastAPI
mqtt_gateway = MqttOpenFirmwareGateway()
