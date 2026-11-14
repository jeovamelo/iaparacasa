"""
Bridge de Integração com Hardware de Automação Residencial (Sonoff / eWeLink / MQTT / Home Assistant).
Despacha os comandos gerados pelo modelo de IA para a camada física de atuadores.
"""

import json
import logging
import urllib.request
import urllib.error
from typing import Any, Dict

from app.config import settings
from app.schemas import ActionType, CommandType, DecisionOutput

logger = logging.getLogger("iaparacasa.bridge")


class HardwareBridge:
    """
    Roteador de comandos físicos para relés Sonoff, brokers MQTT e Home Assistant.
    Centraliza os drivers no gateway que aplica as guardas de segurança.
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
        if decision.action != ActionType.EXECUTE:
            return {"dispatched": False, "reason": f"Action is {decision.action.value}"}
        from app.ewelink_gateway import ewelink_gateway
        result = ewelink_gateway.execute_command(
            decision.target_device, decision.command, decision.params
        )
        channels = [{"channel": "ewelink_gateway", "result": result}]
        if settings.ENABLE_HARDWARE_BRIDGE and self.ha_webhook and "command" in result and "params" in result:
            normalized = decision.model_copy(update={
                "command": CommandType(result["command"]), "params": result["params"]
            })
            ha_result = self._send_ha_webhook(self.ha_webhook, normalized.to_gateway_payload())
            channels.append({"channel": "home_assistant_webhook", "result": ha_result})
        return {"dispatched": result["success"], "result": result, "channels": channels}

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

