"""
Gateway e Conector eWeLink / Sonoff Dual-Mode (LAN/DIY e Nuvem CoolKit).
Suporta controle local via ZeroConf/mDNS (baixa latência e sem dependência de internet)
e fallback para API de Nuvem CoolKit, com garantia física de segurança para relés e portões.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
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
    from cryptography.hazmat.primitives import padding as aes_padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    CRYPTOGRAPHY_AVAILABLE = False
    logger.warning("Pacote 'cryptography' não encontrado. Controle LAN criptografado AES-128-CBC indisponível.")

try:
    from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf
    ZEROCONF_AVAILABLE = True
except ImportError:
    ZEROCONF_AVAILABLE = False
    logger.warning("zeroconf não encontrado. Descoberta mDNS local funcionará via tabela estática.")


def encrypt_lan_payload(inner_data: Dict[str, Any], device_key: str) -> Dict[str, str]:
    """
    Criptografa o bloco 'data' para o protocolo eWeLink LAN (AES-128-CBC + PKCS7).
    A chave AES de 128 bits deriva do MD5 da devicekey obtida na eWeLink Open Platform.
    """
    if not CRYPTOGRAPHY_AVAILABLE:
        raise RuntimeError("Biblioteca 'cryptography' necessária para cifra AES-128-CBC LAN.")

    plaintext = json.dumps(inner_data, separators=(",", ":")).encode("utf-8")
    key = hashlib.md5(device_key.encode("utf-8")).digest()
    iv = os.urandom(16)

    padder = aes_padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()

    return {
        "data": base64.b64encode(ciphertext).decode("utf-8"),
        "iv": base64.b64encode(iv).decode("utf-8"),
    }


def decrypt_lan_payload(ciphertext_b64: str, iv_b64: str, device_key: str) -> Dict[str, Any]:
    """
    Decifra payloads AES-128-CBC recebidos nos registros TXT mDNS (_ewelink._tcp.local.)
    ou respostas locais da porta 8081 usando a devicekey do dispositivo.
    """
    if not CRYPTOGRAPHY_AVAILABLE:
        raise RuntimeError("Biblioteca 'cryptography' necessária para decifrar pacotes AES-128-CBC.")

    key = hashlib.md5(device_key.encode("utf-8")).digest()
    iv = base64.b64decode(iv_b64)
    ciphertext = base64.b64decode(ciphertext_b64)

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()

    unpadder = aes_padding.PKCS7(128).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()
    return json.loads(plaintext.decode("utf-8"))


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
    device_key: Optional[str] = None
    self_apikey: str = "123"
    is_multichannel: bool = False
    channel_outlet: int = 0
    ip_address: Optional[str] = None
    port: int = 8081
    online: bool = True
    last_state: Dict[str, Any] = field(default_factory=dict)
    last_seen: str = field(default_factory=lambda: datetime.now().isoformat())
    mode_available: str = "dual"  # 'lan', 'cloud', 'dual'

    def to_public_dict(self) -> Dict[str, Any]:
        """Serializa o dispositivo mascarando segredos criptográficos para exposição em API."""
        data = asdict(self)
        data.pop("device_key", None)
        data.pop("self_apikey", None)
        data["has_device_key"] = bool(self.device_key)
        data["lan_encryption_ready"] = bool(self.device_key and self.ip_address)
        return data


class EWeLinkGateway:
    """
    Controlador central de comunicação com relés e sensores Sonoff/eWeLink.
    Modo Operacional Dual-Mode: LAN/DIY First para menor latência, fallback para CoolKit API v2.
    """

    def __init__(self, start_zeroconf: bool = True):
        self.devices: Dict[str, SonoffDevice] = {}
        self.zeroconf: Optional[Zeroconf] = None
        self.browser: Optional[ServiceBrowser] = None
        self._long_term_memory: Any = None
        self._runtime_access_token: str = ""
        self._runtime_refresh_token: str = ""
        self._runtime_region: str = ""
        self._load_static_device_topology()
        self._apply_env_device_keys()

        # Inicia descoberta ZeroConf se disponível
        if start_zeroconf and ZEROCONF_AVAILABLE:
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

    def _apply_env_device_keys(self):
        """Aplica chaves locais configuradas via EWELINK_DEVICE_KEYS."""
        env_keys = getattr(settings, "EWELINK_DEVICE_KEYS", {}) or {}
        for dev_id, cfg in env_keys.items():
            if not isinstance(cfg, dict):
                continue
            canonical = self.resolve_device_id(dev_id)
            if canonical in self.devices:
                dev = self.devices[canonical]
                dev.ewelink_device_id = cfg.get("ewelink_device_id", dev.ewelink_device_id)
                dev.device_key = cfg.get("device_key", dev.device_key)
                dev.self_apikey = cfg.get("self_apikey", dev.self_apikey)
                dev.ip_address = cfg.get("ip_address", dev.ip_address)
                dev.is_multichannel = bool(cfg.get("is_multichannel", dev.is_multichannel))
                dev.channel_outlet = int(cfg.get("outlet", cfg.get("channel_outlet", dev.channel_outlet)))

    def attach_memory(self, long_term_memory: Any) -> None:
        """Conecta o gateway ao SQLite local e recarrega chaves/IPs salvos para uso 100% offline."""
        self._long_term_memory = long_term_memory
        if not long_term_memory:
            return
        try:
            saved_rows = long_term_memory.get_ewelink_devices()
            for row in saved_rows:
                dev_id = row["device_id"]
                if dev_id in self.devices:
                    dev = self.devices[dev_id]
                    dev.ewelink_device_id = row.get("ewelink_device_id") or dev.ewelink_device_id
                    dev.device_key = row.get("device_key") or dev.device_key
                    dev.self_apikey = row.get("self_apikey") or dev.self_apikey
                    dev.ip_address = row.get("ip_address") or dev.ip_address
                    dev.port = int(row.get("port") or dev.port)
                    dev.is_multichannel = bool(row.get("is_multichannel", dev.is_multichannel))
                    dev.channel_outlet = int(row.get("channel_outlet", dev.channel_outlet))
                    if row.get("last_state"):
                        dev.last_state.update(row["last_state"])
                else:
                    self.devices[dev_id] = SonoffDevice(
                        device_id=dev_id,
                        name=row.get("name") or dev_id,
                        device_type=row.get("device_type") or "latching_relay",
                        group=row.get("group_name") or "Atuadores Locais",
                        ewelink_device_id=row.get("ewelink_device_id"),
                        device_key=row.get("device_key"),
                        self_apikey=row.get("self_apikey") or "123",
                        is_multichannel=bool(row.get("is_multichannel")),
                        channel_outlet=int(row.get("channel_outlet") or 0),
                        ip_address=row.get("ip_address"),
                        port=int(row.get("port") or 8081),
                        online=bool(row.get("online", True)),
                        last_state=row.get("last_state") or {},
                    )
            if saved_rows:
                logger.info(f"[SQLite] Carregados {len(saved_rows)} registros/chaves LAN eWeLink persistidos.")
        except Exception as e:
            logger.warning(f"Erro ao carregar registro eWeLink persistido no SQLite: {e}")

    def _persist_device(self, dev: SonoffDevice) -> None:
        if not self._long_term_memory:
            return
        try:
            self._long_term_memory.upsert_ewelink_device({
                "device_id": dev.device_id,
                "ewelink_device_id": dev.ewelink_device_id,
                "device_key": dev.device_key,
                "self_apikey": dev.self_apikey,
                "name": dev.name,
                "device_type": dev.device_type,
                "group_name": dev.group,
                "ip_address": dev.ip_address,
                "port": dev.port,
                "is_multichannel": dev.is_multichannel,
                "channel_outlet": dev.channel_outlet,
                "online": dev.online,
                "last_state": dev.last_state,
            })
        except Exception as e:
            logger.warning(f"Erro ao persistir dispositivo {dev.device_id} no SQLite: {e}")

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

    def _decode_mdns_properties(self, properties: Dict[Any, Any]) -> Dict[str, str]:
        decoded: Dict[str, str] = {}
        for k, v in (properties or {}).items():
            key_str = k.decode("utf-8", errors="ignore") if isinstance(k, bytes) else str(k)
            val_str = v.decode("utf-8", errors="ignore") if isinstance(v, bytes) else ("" if v is None else str(v))
            decoded[key_str] = val_str
        return decoded

    def _find_device_by_cloud_id(self, hardware_id: str) -> Optional[SonoffDevice]:
        clean = (hardware_id or "").lower()
        for dev in self.devices.values():
            if dev.ewelink_device_id and dev.ewelink_device_id.lower() == clean:
                return dev
            if dev.device_id.lower() == clean:
                return dev
        return None

    def _on_service_state_change(self, zeroconf: Zeroconf, service_type: str, name: str, state_change: ServiceStateChange):
        """
        Callback mDNS (_ewelink._tcp.local.).
        Atualiza IP/porta, identifica hardware por TXT 'id' ou nome e decifra registros data1..data4
        via AES-128-CBC quando a devicekey estiver disponível.
        """
        if state_change not in (ServiceStateChange.Added, ServiceStateChange.Updated):
            return
        try:
            info = zeroconf.get_service_info(service_type, name)
            if not info or not info.addresses:
                return

            ip = socket.inet_ntoa(info.addresses[0])
            props = self._decode_mdns_properties(info.properties)
            raw_service = name.split(".")[0]
            stripped_id = raw_service[8:] if raw_service.lower().startswith("ewelink_") else raw_service
            hardware_id = props.get("id") or stripped_id

            dev = self._find_device_by_cloud_id(hardware_id)
            if dev is None:
                slug = stripped_id.lower()
                dev = SonoffDevice(
                    device_id=slug,
                    name=f"Dispositivo Sonoff ({hardware_id})",
                    device_type="gate_pulse" if is_momentary_gate_device(slug) else "latching_relay",
                    group="eWeLink LAN",
                    model_hint=props.get("type") or "Sonoff mDNS",
                    ewelink_device_id=hardware_id,
                    ip_address=ip,
                    port=info.port or 8081,
                    online=True,
                )
                self.devices[slug] = dev

            dev.ip_address = ip
            dev.port = info.port or 8081
            dev.online = True
            dev.last_seen = datetime.now().isoformat()

            # Decodifica estado embutido nos pacotes TXT mDNS (data1..data4)
            encrypted_blob = "".join(props.get(f"data{i}", "") for i in range(1, 5))
            if encrypted_blob:
                is_encrypted = props.get("encrypt", "").lower() == "true" or bool(props.get("iv"))
                if is_encrypted and props.get("iv") and dev.device_key:
                    try:
                        decoded_state = decrypt_lan_payload(encrypted_blob, props["iv"], dev.device_key)
                        if isinstance(decoded_state, dict):
                            dev.last_state.update(decoded_state)
                            if isinstance(decoded_state.get("switches"), list):
                                dev.is_multichannel = True
                    except Exception as dec_err:
                        logger.warning(f"[mDNS] Falha ao decifrar pacote de {dev.device_id}: {dec_err}")
                elif not is_encrypted:
                    try:
                        decoded_state = json.loads(encrypted_blob)
                        if isinstance(decoded_state, dict):
                            dev.last_state.update(decoded_state)
                    except Exception:
                        pass

            self._persist_device(dev)
            logger.info(f"[mDNS eWeLink] {dev.device_id} ({hardware_id}) detectado em {ip}:{dev.port}")
        except Exception as e:
            logger.error(f"Erro ao processar anúncio mDNS: {e}")

    def pair_device(
        self,
        target_device: str,
        ewelink_device_id: str,
        device_key: str,
        self_apikey: str = "123",
        ip_address: Optional[str] = None,
        port: int = 8081,
        is_multichannel: bool = False,
        channel_outlet: int = 0,
    ) -> Dict[str, Any]:
        """Registra manualmente ou via API a devicekey e parâmetros LAN de um módulo eWeLink."""
        canonical = self.resolve_device_id(target_device)
        dev = self.devices.get(canonical)
        if dev is None:
            dev = SonoffDevice(
                device_id=canonical,
                name=canonical,
                device_type="gate_pulse" if is_momentary_gate_device(canonical) else "latching_relay",
            )
            self.devices[canonical] = dev

        dev.ewelink_device_id = ewelink_device_id.strip()
        dev.device_key = device_key.strip()
        dev.self_apikey = self_apikey.strip() or "123"
        if ip_address:
            dev.ip_address = ip_address.strip()
        dev.port = port
        dev.is_multichannel = is_multichannel
        dev.channel_outlet = channel_outlet
        dev.last_seen = datetime.now().isoformat()
        self._persist_device(dev)
        return {"success": True, "device": dev.to_public_dict()}

    def get_all_devices(self) -> List[Dict[str, Any]]:
        """Retorna a lista de todos os dispositivos conhecidos com segredos mascarados."""
        return [dev.to_public_dict() for dev in self.devices.values()]

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
                device_key = item.get("devicekey")
                self_apikey = item.get("apikey") or "123"
                raw_name = (item.get("name") or "").strip().lower().replace(" ", "_")
                online = bool(item.get("online", False))
                params = item.get("params", {}) or {}
                is_multi = isinstance(params.get("switches"), list)

                # Tenta associar por ewelink_device_id ou nome normalizado
                matched_dev = next(
                    (d for d in self.devices.values() if d.ewelink_device_id == cloud_id),
                    None,
                )
                if not matched_dev and raw_name:
                    matched_dev = self.devices.get(self.resolve_device_id(raw_name))

                if matched_dev:
                    matched_dev.ewelink_device_id = cloud_id
                    if device_key:
                        matched_dev.device_key = device_key
                    if self_apikey:
                        matched_dev.self_apikey = self_apikey
                    if is_multi:
                        matched_dev.is_multichannel = True
                    matched_dev.online = online
                    matched_dev.last_state.update(params)
                    matched_dev.last_seen = datetime.now().isoformat()
                    self._persist_device(matched_dev)
                    updated += 1
                elif cloud_id:
                    slug = raw_name or f"ewelink_{cloud_id}"
                    new_dev = SonoffDevice(
                        device_id=slug,
                        name=item.get("name") or slug,
                        device_type="gate_pulse" if is_momentary_gate_device(slug) else "latching_relay",
                        group="eWeLink Cloud",
                        model_hint=item.get("productModel") or "eWeLink Cloud Device",
                        ewelink_device_id=cloud_id,
                        device_key=device_key,
                        self_apikey=self_apikey,
                        is_multichannel=is_multi,
                        online=online,
                        last_state=params,
                        mode_available="dual" if device_key else "cloud",
                    )
                    self.devices[slug] = new_dev
                    self._persist_device(new_dev)
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

        # 1. Tenta Modo Local LAN (AES-128-CBC ou DIY REST na porta 8081)
        if device and device.ip_address and device.online:
            lan_success, lan_details = self._send_lan_diy(device, command, params)
            if lan_success:
                self._update_device_state(canonical_id, command, params)
                lan_mode = "lan_encrypted_aes" if device.device_key else "lan_diy"
                return {
                    "success": True,
                    "mode": lan_mode,
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
        """
        Comunicação direta na rede local via porta 8081 (/zeroconf/...).
        Suporta tanto Sonoff DIY em texto claro quanto dispositivos eWeLink padrão
        com criptografia AES-128-CBC (device_key) e canais múltiplos (switches).
        """
        is_multi = bool(device.is_multichannel or "outlet" in params)
        outlet = int(params.get("outlet", device.channel_outlet))

        if command == CommandType.PULSE:
            width = int(params.get("pulse_duration_ms", params.get("pulse_ms", 1000)))
            if is_multi:
                endpoint = "pulses"
                inner_data: Dict[str, Any] = {
                    "pulses": [{"pulse": "on", "width": width, "outlet": outlet}]
                }
            else:
                endpoint = "pulse"
                inner_data = {"pulse": "on", "pulseWidth": width}
        elif command in (CommandType.POWER_ON, CommandType.POWER_OFF):
            switch_state = "on" if command == CommandType.POWER_ON else "off"
            if is_multi:
                endpoint = "switches"
                inner_data = {"switches": [{"switch": switch_state, "outlet": outlet}]}
            else:
                endpoint = "switch"
                inner_data = {"switch": switch_state}
        else:
            endpoint = "switches" if is_multi else "switch"
            inner_data = dict(params)

        url = f"http://{device.ip_address}:{device.port}/zeroconf/{endpoint}"
        payload: Dict[str, Any] = {
            "sequence": str(int(time.time() * 1000)),
            "deviceid": device.ewelink_device_id or device.device_id,
            "selfApikey": device.self_apikey or "123",
        }

        if device.device_key:
            encrypted = encrypt_lan_payload(inner_data, device.device_key)
            payload["encrypt"] = True
            payload["iv"] = encrypted["iv"]
            payload["data"] = encrypted["data"]
        else:
            payload["data"] = inner_data

        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return (data.get("error", 0) == 0), data
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

