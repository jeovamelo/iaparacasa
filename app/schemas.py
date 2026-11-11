"""
Esquemas Pydantic e Contrato de Decisão para Automação Residencial.
Garante tipagem estrita, serialização JSON e conformidade com os gateways IoT.
"""

from enum import Enum
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


_LIGHTING_OR_GARDEN_MARKERS = ("jardim", "luz", "lampada", "lâmpada", "ilumina", "refletor", "varanda", "corredor")
_MOMENTARY_GATE_TOKENS = ("portao", "portão", "fechadura_solenoide", "fechadura", "campainha", "interfone")


def is_momentary_gate_device(device_id: str) -> bool:
    """
    Identifica atuadores de contato seco / pulso momentâneo (portões, interfones, fechaduras).
    Evita falso positivo em iluminações externas como 'jardim_garagem'.
    """
    device = (device_id or "").lower()
    if any(token in device for token in _MOMENTARY_GATE_TOKENS):
        return True
    if "garagem" in device and not any(marker in device for marker in _LIGHTING_OR_GARDEN_MARKERS):
        return True
    return False


class ActionType(str, Enum):
    """Tipos de ações operacionais suportadas pelo pipeline."""
    EXECUTE = "execute"
    QUERY = "query"
    SUGGEST = "suggest"
    LEARN = "learn"


class CommandType(str, Enum):
    """Comandos físicos enviados para relés e atuadores."""
    POWER_ON = "power_on"
    POWER_OFF = "power_off"
    PULSE = "pulse"
    SET_STATE = "set_state"


class DecisionOutput(BaseModel):
    """
    Contrato estrito de saída do modelo LLM/SLM.
    Alimenta diretamente gateways de automação (eWeLink LAN, Sonoff DIY, MQTT, Home Assistant).
    """
    action: ActionType = Field(
        ...,
        description="Ação primária: execute (atuar), query (consulta), suggest (sugestão/crítica), learn (aprender hábito)"
    )
    target_device: str = Field(
        ...,
        description="Identificador único canônico do dispositivo ou sensor alvo (ex: 'portao_garagem', 'luz_sala', 'jardim_deck', 'ar_condicionado_sala')"
    )
    command: CommandType = Field(
        ...,
        description="Comando físico a despachar para o atuador (power_on, power_off, pulse, set_state)"
    )
    params: Dict[str, Any] = Field(
        default_factory=dict,
        description="Parâmetros auxiliares (ex: {'pulse_duration_ms': 1000}, {'temperature': 23, 'mode': 'cool'})"
    )
    learned_preference: Optional[str] = Field(
        default=None,
        description="Descrição em texto de regra ou hábito detectado para armazenamento na memória de longo prazo"
    )
    response_text: str = Field(
        ...,
        description="Resposta curta, humanizada e natural em português para o morador"
    )

    @field_validator("target_device")
    @classmethod
    def sanitize_device_name(cls, v: str) -> str:
        clean = v.strip().lower().replace(" ", "_").replace("-", "_")
        return clean or "unknown_device"

    @model_validator(mode="after")
    def validate_safety_guardrails(self) -> "DecisionOutput":
        """
        Guarda de segurança embutida para dispositivos de contato momentâneo (portões e campainhas).
        Garante que comandos para portão nunca fiquem travados em power_on.
        """
        if is_momentary_gate_device(self.target_device) and self.command == CommandType.POWER_ON:
            # Converte com segurança para pulso de 1 segundo (inching mode)
            self.command = CommandType.PULSE
            if "pulse_duration_ms" not in self.params:
                self.params["pulse_duration_ms"] = 1000

        # Para comandos de pulso sem especificação de duração, fixa default seguro de 1000ms
        if self.command == CommandType.PULSE and "pulse_duration_ms" not in self.params:
            self.params["pulse_duration_ms"] = 1000

        return self

    def to_gateway_payload(self) -> Dict[str, Any]:
        """
        Formata o comando no padrão compatível com bridges Sonoff eWeLink LAN / MQTT.
        """
        payload: Dict[str, Any] = {
            "device": self.target_device,
            "action": self.action.value,
            "cmd": self.command.value,
            "params": self.params,
        }
        if self.command == CommandType.PULSE:
            payload["pulse"] = {
                "pulse": "on",
                "pulseWidth": self.params.get("pulse_duration_ms", 1000)
            }
        elif self.command == CommandType.POWER_ON:
            payload["switch"] = "on"
        elif self.command == CommandType.POWER_OFF:
            payload["switch"] = "off"
        return payload

    def to_ewelink_payload(self) -> Dict[str, Any]:
        """
        Formata a decisão estritamente no contrato JSON estruturado da integração eWeLink:
        integration='ewelink', action in ('set_device_status', 'pulse_inching', 'sync_devices').
        """
        if self.action == ActionType.QUERY:
            return {
                "integration": "ewelink",
                "action": "sync_devices",
                "target_device": self.target_device,
                "params": dict(self.params),
            }

        if self.command == CommandType.PULSE:
            pulse_ms = int(self.params.get("pulse_duration_ms", self.params.get("pulse_ms", 1000)))
            return {
                "integration": "ewelink",
                "action": "pulse_inching",
                "target_device": self.target_device,
                "params": {
                    "switch": "on",
                    "pulse_ms": pulse_ms,
                },
            }

        if self.command == CommandType.POWER_ON:
            ewelink_params: Dict[str, Any] = {"switch": "on"}
        elif self.command == CommandType.POWER_OFF:
            ewelink_params = {"switch": "off"}
        else:
            ewelink_params = dict(self.params)
            if self.action == ActionType.EXECUTE and "switch" not in ewelink_params:
                ewelink_params["switch"] = "on"

        return {
            "integration": "ewelink",
            "action": "set_device_status",
            "target_device": self.target_device,
            "params": ewelink_params,
        }


# --- Requisições e Respostas da API REST ---

class ChatRequest(BaseModel):
    prompt: str = Field(..., description="Instrução do usuário em linguagem natural ou alerta de automação")
    telemetry: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Leituras imediatas de sensores contextuais (ex: {'temperatura': 28.5, 'presenca_sala': True})"
    )
    user_id: str = Field(default="resident_principal", description="Identificador do morador para personalização")


class ChatResponse(BaseModel):
    decision: DecisionOutput
    ewelink_command: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Bloco JSON estruturado de execução para o gateway eWeLink/CoolKit"
    )
    interaction_id: int
    applied_preferences: List[str] = Field(default_factory=list, description="Preferências de longo prazo injetadas")
    latency_ms: float
    engine: str = Field(default="qwen-2.5-q4", description="Engine de inferência que processou a decisão")


class FeedbackRequest(BaseModel):
    interaction_id: int = Field(..., description="ID da interação retornada no /chat")
    approved: bool = Field(..., description="True se a decisão do assistente foi correta, False se precisou de correção")
    correction: Optional[str] = Field(
        default=None,
        description="Instrução de correção do morador (ex: 'Na verdade deixe em 22C e não 24C')"
    )
    preference_to_learn: Optional[str] = Field(
        default=None,
        description="Hábito explícito a ser fixado na memória de longo prazo"
    )


class FeedbackResponse(BaseModel):
    status: str
    message: str
    preference_id: Optional[int] = None
    reinforced: bool = False


class TelemetryEvent(BaseModel):
    device_id: str = Field(..., description="Nome do sensor (ex: 'sensor_temp_sala', 'sensor_presenca_corredor')")
    metric: str = Field(..., description="Tipo de métrica (ex: 'temperature', 'humidity', 'motion', 'luminosity')")
    value: float = Field(..., description="Valor numérico aferido")
    unit: str = Field(default="", description="Unidade de medida (ex: 'C', '%', 'lux')")
    timestamp: Optional[str] = Field(default=None, description="ISO timestamp opcional")


class PreferenceItem(BaseModel):
    id: int
    category: str
    trigger_condition: str
    preference_rule: str
    target_device: str
    confidence: float
    times_reinforced: int
    created_at: str
    updated_at: str


class HealthResponse(BaseModel):
    status: str
    model_loaded: bool
    model_name: str
    mock_mode: bool
    ram_usage_mb: float
    short_term_events_count: int
    long_term_preferences_count: int


class EWeLinkDevicePairRequest(BaseModel):
    target_device: str = Field(..., description="ID lógico do dispositivo (ex: 'luz_sala', 'portao_garagem')")
    ewelink_device_id: str = Field(..., description="ID hexadecimal de 10 dígitos do hardware na eWeLink")
    device_key: str = Field(..., description="Chave local AES-128 obtida pela API oficial eWeLink")
    self_apikey: str = Field(default="123", description="API key do proprietário (apikey)")
    ip_address: Optional[str] = Field(default=None, description="IP local opcional na LAN")
    port: int = Field(default=8081, description="Porta HTTP LAN (padrão 8081)")
    is_multichannel: bool = Field(default=False, description="True para módulos multicanal (switches)")
    channel_outlet: int = Field(default=0, ge=0, le=7, description="Índice do canal (outlet) para módulos multicanal")

