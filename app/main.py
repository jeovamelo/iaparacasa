"""
Servidor de API FastAPI para Automação Residencial Edge AI (iaparacasa).
Disponibiliza endpoints de inferência contextual (/chat), aprendizado por feedback (/feedback),
ingestão de telemetria (/telemetry) e governança de preferências locais.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.engine import EdgeLLMEngine
from app.ewelink_gateway import ewelink_gateway
from app.memory import ContextManager
from app.schemas import (
    ActionType,
    ChatRequest,
    ChatResponse,
    CommandType,
    DecisionOutput,
    EWeLinkDevicePairRequest,
    FeedbackRequest,
    FeedbackResponse,
    HealthResponse,
    PreferenceItem,
    TelemetryEvent,
)

# Configuração de Logs
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("iaparacasa.api")

# Instâncias globais gerenciadas pelo ciclo de vida (lifespan)
context_mgr: Optional[ContextManager] = None
engine: Optional[EdgeLLMEngine] = None


def get_engine() -> EdgeLLMEngine:
    global engine
    if engine is None:
        engine = EdgeLLMEngine()
    return engine


def get_context_mgr() -> ContextManager:
    global context_mgr
    if context_mgr is None:
        context_mgr = ContextManager(
            db_path=settings.DB_PATH,
            max_short_term=settings.MAX_SHORT_TERM_EVENTS
        )
    return context_mgr


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicialização e desligamento gracioso dos serviços e modelos."""
    logger.info("Iniciando iaparacasa Edge AI Automation Service...")
    c_mgr = get_context_mgr()
    ewelink_gateway.attach_memory(c_mgr.long_term)
    eng = get_engine()

    ram_mb = eng.get_memory_usage_mb()
    logger.info(
        f"Inicialização concluída! Modo Mock: {eng.is_mock} | Consumo de RAM inicial: {ram_mb:.1f} MB"
    )

    yield

    logger.info("Encerrando serviços do iaparacasa...")
    ewelink_gateway.close()


app = FastAPI(
    title="iaparacasa Edge AI Microservice",
    description="Pipeline de LLM local compacto para automação residencial, relés Sonoff/eWeLink e aprendizado de preferências.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse, tags=["Interface Web"])
@app.get("/dashboard", response_class=HTMLResponse, tags=["Interface Web"])
def get_dashboard():
    """Renderiza a interface web moderna e responsiva do assistente doméstico."""
    html_file = TEMPLATES_DIR / "index.html"
    if html_file.exists():
        return HTMLResponse(content=html_file.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>iaparacasa Edge AI</h1><p>Template web não encontrado.</p>")


@app.get("/health", response_model=HealthResponse, tags=["Monitoramento"])
def health_check():
    """
    Retorna a saúde do microservice, consumo atual de memória RAM e contadores de contexto.
    Ideal para verificação em Docker healthchecks ou gateways IoT.
    """
    c_mgr = get_context_mgr()
    eng = get_engine()

    prefs = c_mgr.long_term.get_all_preferences()
    recent = c_mgr.short_term.get_recent_events(limit=100)

    return HealthResponse(
        status="healthy",
        model_loaded=(not eng.is_mock),
        model_name=Path(settings.MODEL_PATH).name,
        mock_mode=eng.is_mock,
        ram_usage_mb=round(eng.get_memory_usage_mb(), 2),
        short_term_events_count=len(recent),
        long_term_preferences_count=len(prefs),
    )


@app.post("/model/reload", tags=["Monitoramento"])
def reload_llm_model():
    """Recarrega o arquivo GGUF do disco sem reiniciar o container Docker."""
    eng = get_engine()
    loaded = eng.reload_model()
    return {
        "model_loaded": loaded,
        "mock_mode": eng.is_mock,
        "model_path": str(eng.model_path),
        "ram_usage_mb": round(eng.get_memory_usage_mb(), 2),
    }


@app.post("/chat", response_model=ChatResponse, tags=["Inferência e Decisão"])
def process_chat(request: ChatRequest):
    """
    Interpreta comandos em linguagem natural e telemetria para emitir decisões de atuadores.
    Injeta dinamicamente contexto de curto prazo e hábitos aprendidos via In-Context Learning.
    """
    c_mgr = get_context_mgr()
    eng = get_engine()

    # 1. Montagem do contexto hierárquico
    context_block, applied_rules, top_pref_id = c_mgr.assemble_context(
        query=request.prompt,
        immediate_telemetry=request.telemetry
    )

    # 2. Execução da inferência guiada por esquema
    decision, latency_ms = eng.generate_decision(
        user_prompt=request.prompt,
        context_block=context_block
    )

    # 3. Registro da interação no banco SQLite para auditoria e feedback
    interaction_id = c_mgr.long_term.record_interaction(
        user_prompt=request.prompt,
        sensor_context=context_block,
        decision_json=decision.model_dump_json(),
        applied_preference_id=top_pref_id
    )

    # 4. Atualização do buffer de curto prazo com a ação tomada
    c_mgr.short_term.add_interaction(
        prompt=request.prompt,
        target_device=decision.target_device,
        command=decision.command.value,
        action=decision.action.value
    )

    # 5. Despacho para hardware físico (eWeLink / Sonoff LAN ou Nuvem) se for comando de execução
    if decision.action == ActionType.EXECUTE:
        try:
            ewelink_gateway.execute_command(
                target_device=decision.target_device,
                command=decision.command,
                params=decision.params
            )
        except Exception as e:
            logger.warning(f"Erro ao despachar comando para atuador físico: {e}")

    # 6. Aprendizado automático se o modelo detectou nova preferência descritiva válida
    if decision.learned_preference:
        pref_clean = decision.learned_preference.strip()
        is_valid_rule = (
            len(pref_clean) >= 15
            and " " in pref_clean
            and pref_clean.lower() not in {"null", "none", decision.target_device.lower()}
        )
        if is_valid_rule:
            try:
                c_mgr.long_term.add_preference(
                    category="user_preference",
                    trigger_condition=request.prompt[:100],
                    preference_rule=pref_clean,
                    target_device=decision.target_device,
                    action_template=decision.params,
                    confidence=0.88
                )
                logger.info(f"Nova preferência aprendida automaticamente: {pref_clean}")
            except Exception as e:
                logger.error(f"Erro ao persistir preferência aprendida: {e}")

    engine_name = "mock_heuristics" if eng.is_mock else "llama_cpp_qwen2.5_q4"

    return ChatResponse(
        decision=decision,
        ewelink_command=decision.to_ewelink_payload(),
        interaction_id=interaction_id,
        applied_preferences=applied_rules,
        latency_ms=round(latency_ms, 2),
        engine=engine_name
    )


@app.post("/feedback", response_model=FeedbackResponse, tags=["Aprendizado e Feedback"])
def process_feedback(request: FeedbackRequest):
    """
    Recebe feedback do morador (ou do gateway) sobre uma decisão anterior.
    - Se approved=True: reforça a confiança e prioridade da preferência aplicada.
    - Se approved=False: grava a correção do usuário como nova regra de alta prioridade.
    """
    c_mgr = get_context_mgr()

    success, pref_id = c_mgr.long_term.record_feedback(
        interaction_id=request.interaction_id,
        approved=request.approved,
        correction=request.correction,
        preference_to_learn=request.preference_to_learn
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Interação ID {request.interaction_id} não encontrada"
        )

    if request.approved:
        msg = "Feedback positivo registrado! Hábito reforçado com sucesso na memória."
        reinforced = True
    else:
        msg = "Correção registrada! Nova preferência aprendida e pronta para os próximos comandos."
        reinforced = False

    return FeedbackResponse(
        status="success",
        message=msg,
        preference_id=pref_id,
        reinforced=reinforced
    )


@app.post("/telemetry", tags=["Sensores e Telemetria"])
def ingest_telemetry(event: TelemetryEvent):
    """
    Recebe leituras de sensores (ex: Sonoff TH16, Zigbee, ESPHome) e atualiza o estado da casa.
    """
    c_mgr = get_context_mgr()

    c_mgr.short_term.add_telemetry(
        device_id=event.device_id,
        metric=event.metric,
        value=event.value,
        unit=event.unit
    )
    c_mgr.long_term.record_telemetry(
        device_id=event.device_id,
        metric=event.metric,
        value=event.value,
        unit=event.unit
    )

    return {
        "status": "ingested",
        "device_id": event.device_id,
        "metric": event.metric,
        "value": event.value,
        "unit": event.unit
    }


@app.get("/telemetry/recent", tags=["Sensores e Telemetria"])
def get_recent_telemetry():
    """Retorna o estado consolidado mais recente de cada sensor."""
    c_mgr = get_context_mgr()
    return c_mgr.short_term.get_latest_telemetry()


@app.get("/preferences", response_model=List[PreferenceItem], tags=["Memória de Longo Prazo"])
def list_preferences():
    """Lista todas as preferências e hábitos aprendidos salvos em SQLite."""
    c_mgr = get_context_mgr()
    prefs = c_mgr.long_term.get_all_preferences()
    return prefs


@app.delete("/preferences/{pref_id}", tags=["Memória de Longo Prazo"])
def delete_preference(pref_id: int):
    """Remove uma preferência aprendida específica."""
    c_mgr = get_context_mgr()
    deleted = c_mgr.long_term.delete_preference(pref_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Preferência não encontrada")
    return {"status": "deleted", "preference_id": pref_id}


@app.get("/devices", tags=["Topologia de Dispositivos"])
def list_supported_devices():
    """Retorna os tipos de atuadores e relés Sonoff/eWeLink integráveis."""
    return {
        "supported_families": [
            {
                "type": "inching_pulse_relay",
                "models": ["Sonoff 4CH Pro R3", "Sonoff Dry Contact (RE5V1C)", "Sonoff Mini R2/R4 (Inching)"],
                "target_use": "Portões de garagem, portões sociais, interfones, campainhas",
                "enforced_command": "pulse",
                "recommended_pulse_ms": 1000
            },
            {
                "type": "latching_relay",
                "models": ["Sonoff Basic R2/R3", "Sonoff Mini", "Sonoff Dual R3", "Sonoff TX Touch Switch"],
                "target_use": "Iluminação geral, tomadas inteligentes, bombas d'água",
                "enforced_command": "power_on / power_off"
            },
            {
                "type": "climate_controller",
                "models": ["Sonoff TH16 / THR316D (Temp/Hum)", "Sonoff iFan04", "Broadlink/IR Bridge"],
                "target_use": "Ar condicionado, climatizadores, termostatos",
                "enforced_command": "set_state"
            },
            {
                "type": "sensors",
                "models": ["Sonoff SNZB-03 (Zigbee Motion)", "Sonoff SNZB-02 (Temp/Hum)", "Sonoff DW2 (Porta/Janela)"],
                "target_use": "Telemetria de presença, segurança perimetral e clima"
            }
        ]
    }


@app.get("/ewelink/devices", tags=["Gateway eWeLink"])
def get_ewelink_devices():
    """Retorna o estado em tempo real de todos os dispositivos Sonoff gerenciados (LAN/Cloud)."""
    return ewelink_gateway.get_all_devices()


@app.post("/ewelink/sync", tags=["Gateway eWeLink"])
def sync_ewelink_devices():
    """Sincroniza dispositivos via eWeLink CoolKit Open Platform API v2 ou topologia LAN Dual-Mode."""
    return ewelink_gateway.sync_devices()


@app.post("/ewelink/login", tags=["Gateway eWeLink"])
def login_ewelink_cloud():
    """Autentica no portal CoolKit Open Platform API v2 utilizando credenciais configuradas no .env."""
    return ewelink_gateway.login_cloud()


@app.post("/ewelink/devices/pair", tags=["Gateway eWeLink"])
def pair_ewelink_device_lan(request: EWeLinkDevicePairRequest):
    """Registra/persiste no SQLite a devicekey AES-128 e parâmetros LAN de um dispositivo eWeLink."""
    return ewelink_gateway.pair_device(
        target_device=request.target_device,
        ewelink_device_id=request.ewelink_device_id,
        device_key=request.device_key,
        self_apikey=request.self_apikey,
        ip_address=request.ip_address,
        port=request.port,
        is_multichannel=request.is_multichannel,
        channel_outlet=request.channel_outlet,
    )


@app.post("/ewelink/control", tags=["Gateway eWeLink"])
def control_ewelink_device(
    target_device: str,
    command: CommandType,
    params: Optional[Dict[str, Any]] = None
):
    """
    Aciona diretamente um dispositivo Sonoff/eWeLink via LAN DIY ou Nuvem.
    Aplica compulsoriamente a proteção de Inching para portões.
    """
    return ewelink_gateway.execute_command(
        target_device=target_device,
        command=command,
        params=params or {}
    )
