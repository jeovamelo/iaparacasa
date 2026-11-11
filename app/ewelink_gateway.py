"""
Gateway e Conector eWeLink / Sonoff Dual-Mode (LAN/DIY e Nuvem CoolKit).
Suporta controle local via ZeroConf/mDNS (baixa latência e sem dependência de internet)
e fallback para API de Nuvem CoolKit, com garantia física de segurança para relés e portões.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import socket
import time
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.schemas import ActionType, CommandType, DecisionOutput, is_momentary_gate_device

logger = logging.getLogger("iaparacasa.ewelink")

try:
    from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf
    ZEROCONF_AVAILABLE = True
except ImportError:
    ZEROCONF_AVAILABLE = False
    logger.warning("zeroconf não encontrado. Descoberta mDNS local funcionará via tabela estática.")


DEVICE_ALIASES: Dict[str, str] = {
    "sonoff_rele_portao": "portao_garagem",
    "sonoff_rele_portao_garagem": "portao_garagem",
    "sonoff_portao": "portao_garagem",
    "sonoff_luz_sala": "luz_sala",
    "ac_sala": "ar_condicionado_sala",
    "sonoff_corredor_cima": "corredor_cima",
    "sonoff_varanda_superior": "varanda_superior",
}


@dataclass
class SonoffDevice:
    device_id: str
    name: str
    device_type: str  # 'gate_pulse', 'latching_relay', 'climate', 'sensor'
    group: str = "Atuadores Locais"
    model_hint: str = "Sonoff Switch"
    ewelink_device_id: Optional[str] = None
    ip_address: Optional[str] = None
    port: int = 8081
    online: bool = True
    last_state: Dict[str, Any] = field(default_factory=dict)
    last_seen: str = field(default_factory=lambda: datetime.now().isoformat())
    mode_available: str = "dual"  # 'lan', 'cloud', 'dual'


class EWeLinkGateway:
    """
    Controlador central de comunicação com relés e sensores Sonoff/eWeLink.
    Modo Operacional Dual-Mode: LAN/DIY First para menor latência, fallback para CoolKit API v2.
    """

    def __init__(self):
        self.devices: Dict[str, SonoffDevice] = {}
        self.zeroconf: Optional[Zeroconf] = None
        self.browser: Optional[ServiceBrowser] = None
        self._runtime_access_token: str = ""
        self._runtime_refresh_token: str = ""
        self._runtime_region: str = ""
        self._load_static_device_topology()

        # Inicia descoberta ZeroConf se disponível
        if ZEROCONF_AVAILABLE:
            try:
                self.zeroconf = Zeroconf()
                self.browser = ServiceBrowser(
                    self.zeroconf,
                    "_ewelink._tcp.local.",
                    handlers=[self._on_service_state_change]
                )
                logger.info("Descoberta mDNS ZeroConf Sonoff/eWeLink iniciada em segundo plano.")
            except Exception as e:
                logger.warning(f"Não foi possível iniciar ServiceBrowser ZeroConf: {e}")

    def _load_static_device_topology(self):
        """Topologia oficial 'Minha Casa' com grupos e dispositivos Sonoff/eWeLink configurados."""
        id_map = getattr(settings, "EWELINK_DEVICE_ID_MAP", {}) or {}
        ip_map = getattr(settings, "SONOFF_DEVICE_MAP", {}) or {}

        initial_devices = [
            # 1. Grupo Casa
            SonoffDevice(
                device_id="varanda_superior",
                name="Varanda superior",
                device_type="latching_relay",
                group="Grupo Casa",
                model_hint="Sonoff Relay",
                ewelink_device_id=id_map.get("varanda_superior"),
                ip_address=ip_map.get("varanda_superior", "192.168.1.140"),
                online=False,  # Reportado como Offline no inventario inicial
                last_state={"switch": "off"},
            ),
            SonoffDevice(
                device_id="corredor_cima",
                name="Corredor de cima",
                device_type="latching_relay",
                group="Grupo Casa",
                model_hint="Sonoff Switch",
                ewelink_device_id=id_map.get("corredor_cima"),
                ip_address=ip_map.get("corredor_cima", "192.168.1.141"),
                online=True,
                last_state={"switch": "off"},
            ),
            # 2. Grupo Externa
            SonoffDevice(
                device_id="jardim_deck",
                name="Jardim Deck",
                device_type="latching_relay",
                group="Grupo Externa",
                model_hint="Sonoff Switch",
                ewelink_device_id=id_map.get("jardim_deck"),
                ip_address=ip_map.get("jardim_deck", "192.168.1.142"),
                online=True,
                last_state={"switch": "off"},
            ),
            SonoffDevice(
                device_id="corredor_externo",
                name="Corredor",
                device_type="latching_relay",
                group="Grupo Externa",
                model_hint="Sonoff Switch",
                ewelink_device_id=id_map.get("corredor_externo"),
                ip_address=ip_map.get("corredor_externo", "192.168.1.143"),
                online=True,
                last_state={"switch": "off"},
            ),
            SonoffDevice(
                device_id="jardim_varanda",
                name="Jardim da Varanda",
                device_type="latching_relay",
                group="Grupo Externa",
                model_hint="Sonoff Switch",
                ewelink_device_id=id_map.get("jardim_varanda"),
                ip_address=ip_map.get("jardim_varanda", "192.168.1.144"),
                online=True,
                last_state={"switch": "off"},
            ),
            SonoffDevice(
                device_id="jardim_garagem",
                name="Jardim da Garagem",
                device_type="latching_relay",
                group="Grupo Externa",
                model_hint="Sonoff Switch",
                ewelink_device_id=id_map.get("jardim_garagem"),
                ip_address=ip_map.get("jardim_garagem", "192.168.1.145"),
                online=True,
                last_state={"switch": "off"},
            ),
            SonoffDevice(
                device_id="jardim_principal",
                name="Jardim Principal",
                device_type="latching_relay",
                group="Grupo Externa",
                model_hint="Sonoff Switch",
                ewelink_device_id=id_map.get("jardim_principal"),
                ip_address=ip_map.get("jardim_principal", "192.168.1.146"),
                online=True,
                last_state={"switch": "off"},
            ),
            # 3. Atuadores Locais / Controles
            SonoffDevice(
                device_id="portao_garagem",
                name="Portão da Garagem",
                device_type="gate_pulse",
                group="Atuadores Locais",
                model_hint="Sonoff Contato Seco (Inching 1000ms)",
                ewelink_device_id=id_map.get("portao_garagem"),
                ip_address=ip_map.get("portao_garagem", "192.168.1.150"),
                online=True,
                last_state={"pulse": "off", "pulseWidth": 1000},
            ),
            SonoffDevice(
                device_id="luz_sala",
                name="Luz da Sala",
                device_type="latching_relay",
                group="Atuadores Locais",
                model_hint="Sonoff Switch",
                ewelink_device_id=id_map.get("luz_sala"),
                ip_address=ip_map.get("luz_sala", "192.168.1.151"),
                online=True,
                last_state={"switch": "off"},
            ),
            SonoffDevice(
                device_id="ar_condicionado_sala",
                name="Ar Condicionado Sala",
                device_type="climate",
                group="Atuadores Locais",
                model_hint="Clima / Cool",
                ewelink_device_id=id_map.get("ar_condicionado_sala"),
                ip_address=ip_map.get("ar_condicionado_sala", "192.168.1.152"),
                online=True,
                last_state={"switch": "off", "temperature": 23, "mode": "cool"},
            ),
            # 4. Sensores Ambientais
            SonoffDevice(
                device_id="sensor_temp_sala",
                name="Sensor Temperatura/Umidade (TH16)",
                device_type="sensor",
                group="Sensores",
                model_hint="Sonoff TH16 / SNZB-02",
                ip_address="192.168.1.160",
                online=True,
                last_state={"temperature": 25.0, "humidity": 60.0},
            ),
            SonoffDevice(
                device_id="sensor_presenca_sala",
                name="Sensor de Presença Sala (SNZB-03)",
                device_type="sensor",
                group="Sensores",
                model_hint="Sonoff SNZB-03",
                ip_address="192.168.1.161",
                online=True,
                last_state={"motion": 0},
            ),
        ]
        for dev in initial_devices:
            self.devices[dev.device_id] = dev

    def resolve_device_id(self, target_device: str) -> str:
        """Resolve aliases legados para os IDs canônicos da topologia Minha Casa."""
        clean = (target_device or "").strip().lower().replace(" ", "_").replace("-", "_")
        return DEVICE_ALIASES.get(clean, clean)

    def _active_cloud_token(self) -> str:
        return self._runtime_access_token or getattr(settings, "EWELINK_CLOUD_TOKEN", "")

    def get_cloud_api_base_url(self, region_override: Optional[str] = None) -> str:
        region = (region_override or self._runtime_region or getattr(settings, "EWELINK_REGION", "us") or "us").lower()
        return f"https://{region}-apia.coolkit.cc"

    def _sign_coolkit_payload(self, body_str: str = "") -> str:
        """Assina mensagens com HMAC-SHA256 em Base64 segundo especificação CoolKit OpenAPI v2."""
        app_secret = getattr(settings, "EWELINK_APP_SECRET", "")
        if not app_secret:
            return ""
        digest = hmac.new(
            app_secret.encode("utf-8"),
            body_str.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    def _build_coolkit_headers(self, body_str: str = "", force_sign: bool = False) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "X-CK-Nonce": secrets.token_hex(4),
        }
        app_id = getattr(settings, "EWELINK_APP_ID", "")
        cloud_token = self._active_cloud_token()
        if app_id:
            headers["X-CK-Appid"] = app_id
        if cloud_token and not force_sign:
            headers["Authorization"] = f"Bearer {cloud_token}"
        elif app_id and getattr(settings, "EWELINK_APP_SECRET", ""):
            headers["Authorization"] = f"Sign {self._sign_coolkit_payload(body_str)}"
        return headers

    def login_cloud(
        self,
        email: Optional[str] = None,
        password: Optional[str] = None,
        country_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Autentica na API CoolKit Open Platform v2 (POST /v2/user/login) usando HMAC-SHA256.
        Suporta redirecionamento automático de região (erro 10004).
        """
        user_email = email or getattr(settings, "EWELINK_USER_EMAIL", "")
        user_password = password or getattr(settings, "EWELINK_USER_PASSWORD", "")
        ddi = country_code or getattr(settings, "EWELINK_COUNTRY_CODE", "+55") or "+55"
        app_id = getattr(settings, "EWELINK_APP_ID", "")
        app_secret = getattr(settings, "EWELINK_APP_SECRET", "")

        if not user_email or not user_password:
            return {
                "success": False,
                "error": "credentials_missing",
                "message": "EWELINK_USER_EMAIL e EWELINK_USER_PASSWORD não configurados no .env.",
            }

        if not app_id or not app_secret:
            return {
                "success": False,
                "error": "appid_secret_required",
                "email": user_email,
                "message": (
                    "Credenciais de usuário carregadas via .env, porém a CoolKit OpenAPI v2 "
                    "exige EWELINK_APP_ID e EWELINK_APP_SECRET (https://dev.ewelink.cc) ou um "
                    "OAuth2 Access Token em EWELINK_CLOUD_TOKEN para assinar requisições."
                ),
            }

        payload = {
            "countryCode": ddi,
            "email": user_email,
            "password": user_password,
        }
        body_str = json.dumps(payload, separators=(",", ":"))
        region = self._runtime_region or getattr(settings, "EWELINK_REGION", "us") or "us"

        for attempt in range(2):
            url = f"{self.get_cloud_api_base_url(region)}/v2/user/login"
            try:
                req = urllib.request.Request(
                    url,
                    data=body_str.encode("utf-8"),
                    method="POST",
                    headers=self._build_coolkit_headers(body_str, force_sign=True),
                )
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    data = json.loads(resp.read().decode("utf-8"))

                err_code = data.get("error")
                if err_code == 10004 and attempt == 0:
                    new_region = data.get("data", {}).get("region")
                    if new_region:
                        region = new_region
                        continue

                if err_code != 0:
                    return {
                        "success": False,
                        "error": f"coolkit_error_{err_code}",
                        "region": region,
                        "message": data.get("msg", "Falha no login CoolKit API v2"),
                    }

                login_data = data.get("data", {})
                self._runtime_access_token = login_data.get("at", "")
                self._runtime_refresh_token = login_data.get("rt", "")
                self._runtime_region = login_data.get("region") or region
                return {
                    "success": True,
                    "region": self._runtime_region,
                    "authenticated": bool(self._runtime_access_token),
                    "message": "Autenticado com sucesso na eWeLink Open Platform.",
                }
            except Exception as e:
                logger.warning(f"Erro ao autenticar na CoolKit API v2 ({url}): {e}")
                return {"success": False, "error": "network_error", "message": str(e)}

        return {"success": False, "error": "login_failed"}

    def _on_service_state_change(self, zeroconf: Zeroconf, service_type: str, name: str, state_change: ServiceStateChange):
        """Callback acionado quando um dispositivo Sonoff DIY anuncia serviço mDNS na rede."""
        if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
            try:
                info = zeroconf.get_service_info(service_type, name)
                if info and info.addresses:
                    ip = socket.inet_ntoa(info.addresses[0])
                    dev_id = name.split(".")[0].lower()
                    logger.info(f"[mDNS eWeLink] Dispositivo detectado: {dev_id} no IP {ip}:{info.port}")

                    # Atualiza ou registra
                    if dev_id in self.devices:
                        self.devices[dev_id].ip_address = ip
                        self.devices[dev_id].port = info.port
                        self.devices[dev_id].online = True
                        self.devices[dev_id].last_seen = datetime.now().isoformat()
                    else:
                        self.devices[dev_id] = SonoffDevice(
                            device_id=dev_id,
                            name=f"Dispositivo Sonoff ({dev_id})",
                            device_type="latching_relay",
                            ip_address=ip,
                            port=info.port,
                            online=True
                        )
            except Exception as e:
                logger.error(f"Erro ao processar anúncio mDNS: {e}")

    def get_all_devices(self) -> List[Dict[str, Any]]:
        """Retorna a lista de todos os dispositivos conhecidos e seus estados atuais."""
        return [asdict(dev) for dev in self.devices.values()]

    def get_device(self, device_id: str) -> Optional[SonoffDevice]:
        canonical = self.resolve_device_id(device_id)
        return self.devices.get(canonical)

    def sync_devices(self) -> Dict[str, Any]:
        """
        Sincroniza o inventário de dispositivos via CoolKit Open Platform API v2 (/v2/device/thing)
        ou retorna o status da topologia local Dual-Mode caso OAuth Cloud ainda não esteja autenticado.
        """
        cloud_token = self._active_cloud_token()
        if (
            not cloud_token
            and getattr(settings, "EWELINK_APP_ID", "")
            and getattr(settings, "EWELINK_APP_SECRET", "")
            and getattr(settings, "EWELINK_USER_EMAIL", "")
            and getattr(settings, "EWELINK_USER_PASSWORD", "")
        ):
            login_res = self.login_cloud()
            if login_res.get("success"):
                cloud_token = self._active_cloud_token()

        base_api = self.get_cloud_api_base_url()
        portal_url = getattr(settings, "EWELINK_CLOUD_BASE_URL", "https://web.ewelink.cc")
        has_user_creds = bool(getattr(settings, "EWELINK_USER_EMAIL", "") and getattr(settings, "EWELINK_USER_PASSWORD", ""))

        if not cloud_token:
            msg = (
                "Conta eWeLink configurada no .env, aguardando EWELINK_APP_ID/EWELINK_APP_SECRET (dev.ewelink.cc) ou EWELINK_CLOUD_TOKEN para nuvem. Topologia local 'Minha Casa' ativa."
                if has_user_creds
                else "Topologia local 'Minha Casa' carregada. Configure EWELINK_CLOUD_TOKEN/EWELINK_APP_ID/EWELINK_APP_SECRET no .env para sincronização ativa com a nuvem."
            )
            return {
                "success": True,
                "mode": "local_topology",
                "cloud_configured": False,
                "user_credentials_present": has_user_creds,
                "portal_url": f"{portal_url}/#/home",
                "api_endpoint": f"{base_api}/v2/device/thing",
                "synced_count": 0,
                "total_devices": len(self.devices),
                "message": msg,
                "devices": self.get_all_devices(),
            }

        url = f"{base_api}/v2/device/thing"
        try:
            req = urllib.request.Request(
                url,
                method="GET",
                headers=self._build_coolkit_headers(""),
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            if data.get("error") != 0:
                return {
                    "success": False,
                    "mode": "cloud_coolkit",
                    "cloud_configured": True,
                    "details": data,
                    "devices": self.get_all_devices(),
                }

            thing_list = data.get("data", {}).get("thingList", [])
            updated = 0
            for thing in thing_list:
                item = thing.get("itemData", {})
                cloud_id = item.get("deviceid")
                raw_name = (item.get("name") or "").strip().lower().replace(" ", "_")
                online = bool(item.get("online", False))
                params = item.get("params", {}) or {}

                # Tenta associar por ewelink_device_id ou nome normalizado
                matched_dev = next(
                    (d for d in self.devices.values() if d.ewelink_device_id == cloud_id),
                    None,
                )
                if not matched_dev and raw_name:
                    matched_dev = self.devices.get(self.resolve_device_id(raw_name))

                if matched_dev:
                    matched_dev.ewelink_device_id = cloud_id
                    matched_dev.online = online
                    matched_dev.last_state.update(params)
                    matched_dev.last_seen = datetime.now().isoformat()
                    updated += 1
                elif cloud_id:
                    slug = raw_name or f"ewelink_{cloud_id}"
                    self.devices[slug] = SonoffDevice(
                        device_id=slug,
                        name=item.get("name") or slug,
                        device_type="gate_pulse" if is_momentary_gate_device(slug) else "latching_relay",
                        group="eWeLink Cloud",
                        model_hint=item.get("productModel") or "eWeLink Cloud Device",
                        ewelink_device_id=cloud_id,
                        online=online,
                        last_state=params,
                        mode_available="cloud",
                    )
                    updated += 1

            return {
                "success": True,
                "mode": "cloud_coolkit",
                "cloud_configured": True,
                "synced_count": updated,
                "total_devices": len(self.devices),
                "devices": self.get_all_devices(),
            }
        except Exception as e:
            logger.warning(f"Falha ao sincronizar com CoolKit API v2 ({url}): {e}")
            return {
                "success": False,
                "mode": "cloud_coolkit_error",
                "cloud_configured": True,
                "error": str(e),
                "devices": self.get_all_devices(),
            }

    def execute_command(
        self,
        target_device: str,
        command: CommandType,
        params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Executa comando físico com estratégia híbrida Dual-Mode (LAN primeiro, Cloud fallback).
        Aplica regras compulsórias de proteção de relé (Inching) sem afetar luzes externas.
        """
        params = dict(params or {})
        canonical_id = self.resolve_device_id(target_device)
        device = self.devices.get(canonical_id)
        is_gate = is_momentary_gate_device(canonical_id)

        # Guarda física de segurança
        if is_gate and command == CommandType.POWER_ON:
            logger.warning(f"Tentativa de 'power_on' no dispositivo {canonical_id}. Convertendo compulsoriamente para 'pulse'.")
            command = CommandType.PULSE
            params["pulse_duration_ms"] = params.get("pulse_duration_ms", 1000)

        is_offline = bool(device and not device.online)

        # Se a bridge de hardware estiver desativada (padrão para testes locais), simula instantaneamente
        if not settings.ENABLE_HARDWARE_BRIDGE:
            logger.info(f"[Modo Simulação Local] Atuador {canonical_id} acionado com comando {command.value}.")
            self._update_device_state(canonical_id, command, params)
            detail_msg = (
                f"Dispositivo '{canonical_id}' consta como Offline na topologia; estado simulado localmente."
                if is_offline
                else "Bridge física desativada (ENABLE_HARDWARE_BRIDGE=false); estado atualizado em memória."
            )
            return {
                "success": True,
                "mode": "simulated_local",
                "target_device": canonical_id,
                "command": command.value,
                "params": params,
                "offline_warning": is_offline,
                "details": detail_msg,
            }

        # 1. Tenta Modo Local LAN (ZeroConf / DIY Mode HTTP REST) se dispositivo estiver online na LAN
        if device and device.ip_address and device.online:
            lan_success, lan_details = self._send_lan_diy(device, command, params)
            if lan_success:
                self._update_device_state(canonical_id, command, params)
                return {
                    "success": True,
                    "mode": "lan_diy",
                    "target_device": canonical_id,
                    "command": command.value,
                    "params": params,
                    "details": lan_details
                }
            logger.warning(f"Modo LAN falhou para {canonical_id}. Tentando fallback de Nuvem...")

        # 2. Fallback: Modo Nuvem CoolKit / eWeLink API v2
        cloud_device_id = (device.ewelink_device_id if device and device.ewelink_device_id else canonical_id)
        cloud_success, cloud_details = self._send_cloud_coolkit(cloud_device_id, command, params)
        if cloud_success:
            self._update_device_state(canonical_id, command, params)
            return {
                "success": True,
                "mode": "cloud_coolkit",
                "target_device": canonical_id,
                "command": command.value,
                "params": params,
                "details": cloud_details
            }

        # 3. Se nenhuma rota física respondeu, emite simulação segura para teste local
        logger.info(f"[Modo Simulação Local] Atuador {canonical_id} acionado com comando {command.value}.")
        self._update_device_state(canonical_id, command, params)
        return {
            "success": True,
            "mode": "simulated_local",
            "target_device": canonical_id,
            "command": command.value,
            "params": params,
            "offline_warning": is_offline,
            "details": "Rotas físicas indisponíveis; comando registrado no controlador local."
        }

    def _send_lan_diy(self, device: SonoffDevice, command: CommandType, params: Dict[str, Any]) -> Tuple[bool, Any]:
        """Comunicação direta na rede local via protocolo Sonoff DIY (porta 8081)."""
        endpoint = "pulse" if command == CommandType.PULSE else "switch"
        url = f"http://{device.ip_address}:{device.port}/zeroconf/{endpoint}"

        payload: Dict[str, Any] = {
            "sequence": str(int(time.time() * 1000)),
            "deviceid": device.device_id,
            "data": {}
        }

        if command == CommandType.PULSE:
            width = params.get("pulse_duration_ms", 1000)
            payload["data"] = {"pulse": "on", "pulseWidth": width}
        elif command == CommandType.POWER_ON:
            payload["data"] = {"switch": "on"}
        elif command == CommandType.POWER_OFF:
            payload["data"] = {"switch": "off"}
        elif command == CommandType.SET_STATE:
            payload["data"] = params

        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=1.2) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return True, data
        except Exception as e:
            return False, str(e)

    def _send_cloud_coolkit(self, target_device: str, command: CommandType, params: Dict[str, Any]) -> Tuple[bool, Any]:
        """
        Integração com a Nuvem eWeLink CoolKit via OpenAPI v2 (/v2/device/thing/status).
        Usa a região configurada e assinatura HMAC-SHA256 / Bearer token OAuth2.
        """
        cloud_token = self._active_cloud_token()
        if not cloud_token:
            return False, "Credenciais de nuvem eWeLink não configuradas no ambiente (.env)."

        url = f"{self.get_cloud_api_base_url()}/v2/device/thing/status"
        payload: Dict[str, Any] = {
            "type": 1,
            "id": target_device,
            "params": {}
        }

        if command == CommandType.POWER_ON:
            payload["params"]["switch"] = "on"
        elif command == CommandType.POWER_OFF:
            payload["params"]["switch"] = "off"
        elif command == CommandType.PULSE:
            payload["params"]["pulse"] = "on"
            payload["params"]["pulseWidth"] = params.get("pulse_duration_ms", 1000)
        else:
            payload["params"] = params

        body_str = json.dumps(payload, separators=(",", ":"))
        try:
            req = urllib.request.Request(
                url,
                data=body_str.encode("utf-8"),
                headers=self._build_coolkit_headers(body_str)
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return (data.get("error") == 0), data
        except Exception as e:
            return False, str(e)

    def _update_device_state(self, target_device: str, command: CommandType, params: Dict[str, Any]):
        """Mantém a tabela de estados em memória sincronizada."""
        if target_device in self.devices:
            dev = self.devices[target_device]
            dev.last_seen = datetime.now().isoformat()
            if command == CommandType.POWER_ON:
                dev.last_state["switch"] = "on"
            elif command == CommandType.POWER_OFF:
                dev.last_state["switch"] = "off"
            elif command == CommandType.PULSE:
                dev.last_state["last_pulse"] = datetime.now().isoformat()
                dev.last_state["pulseWidth"] = params.get("pulse_duration_ms", 1000)
            elif command == CommandType.SET_STATE:
                dev.last_state.update(params)

    def close(self):
        if self.zeroconf:
            try:
                self.zeroconf.close()
            except Exception:
                pass


# Instância singleton global do gateway
ewelink_gateway = EWeLinkGateway()

