"""
Configurações da aplicação iaparacasa.
Carrega variáveis de ambiente com suporte a defaults seguros para Edge AI.
"""

import os
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Identificação do Modelo e Runtime
    MODEL_PATH: str = Field(
        default=str(BASE_DIR / "models" / "qwen2.5-1.5b-instruct-q4_k_m.gguf"),
        description="Caminho local para o arquivo GGUF quantizado"
    )
    MODEL_REPO_ID: str = Field(
        default="Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        description="Repositório no Hugging Face"
    )
    MODEL_FILENAME: str = Field(
        default="qwen2.5-1.5b-instruct-q4_k_m.gguf",
        description="Nome do arquivo GGUF para download"
    )

    # Parâmetros de Inferência e Otimização para Edge
    N_CTX: int = Field(
        default=2048,
        description="Tamanho da janela de contexto (2048 mantém KV-cache < 150MB)"
    )
    N_THREADS: int = Field(
        default=max(1, (os.cpu_count() or 4) - 1),
        description="Número de threads CPU para inferência"
    )
    N_BATCH: int = Field(
        default=256,
        description="Tamanho do lote para avaliação de prompts (menor batch = menor pico de RAM)"
    )
    USE_MMAP: bool = Field(
        default=False,
        description="False carrega o GGUF direto para RAM evitando lentidão em volumes montados (ex: Docker Desktop/WSL2)"
    )
    TEMPERATURE: float = Field(
        default=0.1,
        description="Temperatura de amostragem baixa para determinismo e conformidade a regras"
    )
    TOP_P: float = Field(
        default=0.9,
        description="Top-p para amostragem nucleada"
    )

    # Persistência de Dados e Memória Semântica
    DB_PATH: str = Field(
        default=str(BASE_DIR / "data" / "iaparacasa_memory.db"),
        description="Caminho do banco SQLite de memória contínua e preferências"
    )
    SYSTEM_PROMPT_PATH: str = Field(
        default=str(BASE_DIR / "system_prompt.txt"),
        description="Caminho do prompt de sistema com regras de automação"
    )
    MAX_SHORT_TERM_EVENTS: int = Field(
        default=25,
        description="Capacidade máxima do buffer circular de curto prazo"
    )

    # Flags Operacionais e Bridge de Hardware
    USE_MOCK_ENGINE: bool = Field(
        default=False,
        description="Ativa motor mock de regras caso o arquivo GGUF ainda não tenha sido baixado"
    )
    ENABLE_HARDWARE_BRIDGE: bool = Field(
        default=False,
        description="Ativa despacho de comandos físicos para relés Sonoff/eWeLink LAN e MQTT"
    )
    EWELINK_CLOUD_TOKEN: str = Field(
        default="",
        description="Access Token Bearer OAuth2 para fallback na Nuvem CoolKit / eWeLink"
    )
    EWELINK_REFRESH_TOKEN: str = Field(
        default="",
        description="Refresh Token OAuth2 opcional da CoolKit / eWeLink Open Platform"
    )
    EWELINK_APP_ID: str = Field(
        default="",
        description="AppID da integração eWeLink Open Platform (CoolKit API v2)"
    )
    EWELINK_APP_SECRET: str = Field(
        default="",
        description="AppSecret da integração eWeLink Open Platform (usado na assinatura HMAC-SHA256)"
    )
    EWELINK_REGION: str = Field(
        default="us",
        description="Região da API CoolKit v2 (us, eu, cn, as)"
    )
    EWELINK_USER_EMAIL: str = Field(
        default="",
        description="E-mail da conta eWeLink para autenticação CoolKit API v2 (definido apenas no .env local)"
    )
    EWELINK_USER_PASSWORD: str = Field(
        default="",
        description="Senha da conta eWeLink para autenticação CoolKit API v2 (definido apenas no .env local)"
    )
    EWELINK_COUNTRY_CODE: str = Field(
        default="+55",
        description="Código DDI da conta eWeLink (ex: +55)"
    )
    EWELINK_CLOUD_BASE_URL: str = Field(
        default="https://web.ewelink.cc",
        description="URL base do painel Web eWeLink e referência OAuth"
    )
    EWELINK_DEVICE_ID_MAP: dict = Field(
        default_factory=dict,
        description="Mapeamento opcional de alias lógico (ex: 'luz_sala') para deviceid hexadecimal real na nuvem eWeLink"
    )
    EWELINK_DEVICE_KEYS: dict = Field(
        default_factory=dict,
        description="Mapeamento opcional de chaves locais LAN (device_id -> {ewelink_device_id, device_key, ip_address, outlet, is_multichannel})"
    )
    HOME_ASSISTANT_WEBHOOK_URL: str = Field(
        default="",
        description="URL de webhook do Home Assistant para despacho de eventos"
    )
    MQTT_BROKER_HOST: str = "localhost"
    MQTT_BROKER_PORT: int = 1883
    SONOFF_DEVICE_MAP: dict = Field(
        default_factory=lambda: {
            "varanda_superior": "192.168.1.140",
            "corredor_cima": "192.168.1.141",
            "jardim_deck": "192.168.1.142",
            "corredor_externo": "192.168.1.143",
            "jardim_varanda": "192.168.1.144",
            "jardim_garagem": "192.168.1.145",
            "jardim_principal": "192.168.1.146",
            "portao_garagem": "192.168.1.150",
            "luz_sala": "192.168.1.151",
            "ar_condicionado_sala": "192.168.1.152",
        },
        description="Mapeamento de nomes de dispositivos para endereços IP locais em modo DIY"
    )

    # Servidor HTTP
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    LOG_LEVEL: str = "info"


settings = Settings()

