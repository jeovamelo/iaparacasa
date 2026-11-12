"""
Bridge de Integração com Hardware de Automação Residencial (Sonoff / eWeLink / MQTT / Home Assistant).
Despacha os comandos gerados pelo modelo de IA para a camada física de atuadores.
"""

import json
import logging
import urllib.request
import urllib.error
from typing import Any, Dict, Optional

from app.config import settings
from app.mqtt_gateway import mqtt_gateway
from app.schemas import ActionType, CommandType, DecisionOutput

logger = logging.getLogger("iaparacasa.bridge")


class HardwareBridge:
    """
    Roteador de comandos físicos para relés Sonoff, brokers MQTT e Home Assistant.
    Opera de forma assíncrona/não-bloqueante para não degradar a latência do LLM.
    """

    def __init__(self):
        self.device_map = getattr(settings, "SONOFF_DEVICE_MAP", {})
        self.ha_webhook = getattr(settings, "HOME_ASSISTANT_WEBHOOK_URL", None)
        self.mqtt_host = getattr(settings, "MQTT_BROKER_HOST", "localhost")
        self.mqtt_port = getattr(settings, "MQTT_BROKER_PORT", 1883)
        self.enabled = getattr(settings, "ENABLE_HARDWARE_BRIDGE", False)

    def dispatch(self, decision: DecisionOutput) -> Dict[str, Any]:
        """
        Despacha a decisão para o gateway correspondente se action == 'execute'.
        Retorna status de despacho e payload formatado.
        """
        payload = decision.to_gateway_payload()

        if decision.action != ActionType.EXECUTE:
            logger.info(
                f"Decisão do tipo '{decision.action.value}' para '{decision.target_device}'. "
                f"Nenhum comando físico despachado (aguardando confirmação ou consulta)."
            )
            return {
                "dispatched": False,
                "reason": f"Action is {decision.action.value}",
                "payload": payload,
            }

        logger.info(
            f"Despachando comando físico: device='{decision.target_device}', "
            f"cmd='{decision.command.value}', params={decision.params}"
        )

        results = {
            "dispatched": True,
            "target_device": decision.target_device,
            "command": decision.command.value,
            "channels": []
        }

        # 1. Envio para Sonoff DIY Mode / eWeLink LAN (se houver IP mapeado)
        device_ip = self.device_map.get(decision.target_device)
        if device_ip:
            sonoff_res = self._send_sonoff_lan(device_ip, decision)
            results["channels"].append({"channel": "sonoff_lan", "result": sonoff_res})

        # 2. Envio para Webhook do Home Assistant (se configurado)
        if self.ha_webhook:
            ha_res = self._send_ha_webhook(self.ha_webhook, payload)
            results["channels"].append({"channel": "home_assistant_webhook", "result": ha_res})

        # 3. Formato e publicação MQTT padronizada (Tasmota / ESPHome / Home Assistant)
        topic_prefix = getattr(settings, "MQTT_TOPIC_PREFIX", "iaparacasa").strip("/")
        mqtt_topic = f"{topic_prefix}/{decision.target_device}/cmd"
        mqtt_info: Dict[str, Any] = {
            "topic": mqtt_topic,
            "payload": payload,
            "published": False,
        }
        if self.enabled and mqtt_gateway.is_connected():
            ok, mqtt_res = mqtt_gateway.publish_json(decision.target_device, payload)
            mqtt_info["published"] = ok
            results["channels"].append({"channel": "mqtt", "result": mqtt_res})
        results["mqtt"] = mqtt_info

        return results

    def _send_sonoff_lan(self, ip: str, decision: DecisionOutput) -> Dict[str, Any]:
        """
        Comunicação direta com relés Sonoff em modo DIY (porta 8081).
        Suporta endpoints /zeroconf/switch e /zeroconf/pulse.
        """
        endpoint = "pulse" if decision.command == CommandType.PULSE else "switch"
        url = f"http://{ip}:8081/zeroconf/{endpoint}"

        data: Dict[str, Any] = {"sequence": "1", "deviceid": decision.target_device}
        if decision.command == CommandType.PULSE:
            data["data"] = {
                "pulse": "on",
                "pulseWidth": decision.params.get("pulse_duration_ms", 1000)
            }
        elif decision.command == CommandType.POWER_ON:
            data["data"] = {"switch": "on"}
        elif decision.command == CommandType.POWER_OFF:
            data["data"] = {"switch": "off"}
        else:
            data["data"] = decision.params

        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(data).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                return {"status": "success", "code": resp.status}
        except Exception as e:
            logger.warning(f"Falha ao contatar Sonoff LAN em {url}: {e}")
            return {"status": "error", "message": str(e)}

    def _send_ha_webhook(self, webhook_url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Dispara webhook para automações no Home Assistant."""
        try:
            req = urllib.request.Request(
                webhook_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                return {"status": "success", "code": resp.status}
        except Exception as e:
            logger.warning(f"Falha ao enviar webhook do Home Assistant: {e}")
            return {"status": "error", "message": str(e)}

