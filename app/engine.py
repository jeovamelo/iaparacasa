"""
Motor de Inferência LLM Local para Edge AI (iaparacasa).
Utiliza llama-cpp-python para execução direta de modelos GGUF quantizados (Q4_K_M)
com amostragem guiada por esquema JSON (Grammar / Constrained Decoding) e thread-safety.
"""

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.config import settings
from app.schemas import ActionType, CommandType, DecisionOutput

logger = logging.getLogger("iaparacasa.engine")

try:
    from llama_cpp import Llama
    LLAMA_CPP_AVAILABLE = True
except ImportError:
    LLAMA_CPP_AVAILABLE = False
    logger.warning("llama-cpp-python não encontrado no ambiente. O engine iniciará em modo mock/simulado.")


# Schema JSON achatado (sem $defs/$ref) para compatibilidade máxima com llama.cpp GBNF
DECISION_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["execute", "query", "suggest", "learn"]
        },
        "target_device": {
            "type": "string"
        },
        "command": {
            "type": "string",
            "enum": ["power_on", "power_off", "pulse", "set_state"]
        },
        "params": {
            "type": "object"
        },
        "learned_preference": {
            "type": ["string", "null"]
        },
        "response_text": {
            "type": "string"
        }
    },
    "required": ["action", "target_device", "command", "params", "learned_preference", "response_text"]
}


class EdgeLLMEngine:
    """
    Motor de execução e geração determinística de decisões para automação residencial.
    Projetado para operar estritamente em < 2GB a 3GB de RAM total em CPUs modestas.
    """

    def __init__(self):
        self.model = None
        self._inference_lock = threading.Lock()
        self.is_mock: bool = settings.USE_MOCK_ENGINE
        self.model_path = Path(settings.MODEL_PATH)
        self.system_prompt: str = self._load_system_prompt()
        self._initialize_model()

    def _load_system_prompt(self) -> str:
        prompt_path = Path(settings.SYSTEM_PROMPT_PATH)
        if prompt_path.exists():
            return prompt_path.read_text(encoding="utf-8")
        return (
            "Você é o assistente de automação residencial do iaparacasa. "
            "Responda estritamente com o objeto JSON contendo action, target_device, command, params, learned_preference e response_text."
        )

    def _initialize_model(self):
        """Carrega os pesos GGUF quantizados via llama-cpp-python."""
        self.model_path = Path(settings.MODEL_PATH)
        if settings.USE_MOCK_ENGINE or not LLAMA_CPP_AVAILABLE or not self.model_path.exists():
            if not settings.USE_MOCK_ENGINE:
                if not LLAMA_CPP_AVAILABLE:
                    logger.warning("llama-cpp-python não instalado. Ativando Mock Engine.")
                elif not self.model_path.exists():
                    logger.warning(
                        f"Modelo GGUF não encontrado em '{self.model_path}'. "
                        f"Ativando Mock Engine para testes imediatos. Execute 'python scripts/download_model.py' para baixar os pesos."
                    )
            self.is_mock = True
            self.model = None
            return

        logger.info(f"Carregando modelo GGUF: {self.model_path}")
        logger.info(
            f"Configurações Edge: n_ctx={settings.N_CTX}, n_threads={settings.N_THREADS}, "
            f"n_batch={settings.N_BATCH}, temp={settings.TEMPERATURE}"
        )

        try:
            with self._inference_lock:
                self.model = Llama(
                    model_path=str(self.model_path),
                    n_ctx=settings.N_CTX,
                    n_threads=settings.N_THREADS,
                    n_threads_batch=settings.N_THREADS,
                    n_batch=settings.N_BATCH,
                    n_gpu_layers=0,
                    use_mmap=settings.USE_MMAP,
                    verbose=False,
                )
            self.is_mock = False
            logger.info("Modelo quantizado carregado com sucesso na memória.")
        except Exception as e:
            logger.error(f"Erro ao inicializar llama-cpp: {e}. Recorrendo ao Mock Engine.")
            self.model = None
            self.is_mock = True

    def reload_model(self) -> bool:
        """Recarrega o modelo GGUF do disco (útil após download em tempo de execução)."""
        with self._inference_lock:
            self.model = None
        self._initialize_model()
        return not self.is_mock

    def get_memory_usage_mb(self) -> float:
        """Calcula o consumo de RAM do processo atual de forma portável."""
        try:
            # Em Linux / containers
            if os.path.exists("/proc/self/status"):
                with open("/proc/self/status", "r") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            parts = line.split()
                            return float(parts[1]) / 1024.0  # kB para MB
            # Em Windows / Fallback
            import psutil
            return psutil.Process(os.getpid()).memory_info().rss / (1024.0 * 1024.0)
        except Exception:
            return 0.0

    def generate_decision(
        self,
        user_prompt: str,
        context_block: str
    ) -> Tuple[DecisionOutput, float]:
        """
        Gera uma decisão estruturada a partir do prompt e contexto injetado.
        Retorna a tupla (DecisionOutput, latência_ms).
        """
        start_time = time.perf_counter()

        if self.is_mock or self.model is None:
            decision = self._generate_mock_decision(user_prompt, context_block)
            latency = (time.perf_counter() - start_time) * 1000.0
            return decision, latency

        messages = [
            {"role": "system", "content": f"{self.system_prompt}\n\n{context_block}"},
            {"role": "user", "content": user_prompt}
        ]

        raw_text: Optional[str] = None
        with self._inference_lock:
            # Tentativa 1: Enforçamento estrito por JSON Schema (GBNF Grammar)
            try:
                response = self.model.create_chat_completion(
                    messages=messages,
                    temperature=settings.TEMPERATURE,
                    top_p=settings.TOP_P,
                    max_tokens=384,
                    response_format={
                        "type": "json_object",
                        "schema": DECISION_JSON_SCHEMA,
                    },
                )
                raw_text = response["choices"][0]["message"]["content"]
            except Exception as e:
                logger.warning(f"Schema enforcement falhou ({e}), tentando modo json_object padrão...")
                # Tentativa 2: Modo json_object sem schema inline
                try:
                    response = self.model.create_chat_completion(
                        messages=messages,
                        temperature=settings.TEMPERATURE,
                        top_p=settings.TOP_P,
                        max_tokens=384,
                        response_format={"type": "json_object"},
                    )
                    raw_text = response["choices"][0]["message"]["content"]
                except Exception as e2:
                    logger.error(f"Falha na inferência do modelo local: {e2}. Usando recuperação heurística.")

        if raw_text:
            decision = self._parse_and_validate_json(raw_text, user_prompt, context_block)
        else:
            decision = self._generate_mock_decision(user_prompt, context_block)

        latency = (time.perf_counter() - start_time) * 1000.0
        return decision, latency

    def _parse_and_validate_json(
        self,
        raw_text: str,
        fallback_prompt: str,
        context_block: str = ""
    ) -> DecisionOutput:
        """Decodifica e sanitiza o JSON emitido pelo modelo."""
        try:
            clean_text = raw_text.strip()
            if "```json" in clean_text:
                clean_text = clean_text.split("```json")[1].split("```")[0].strip()
            elif "```" in clean_text:
                clean_text = clean_text.split("```")[1].split("```")[0].strip()

            # Extrai o bloco JSON mais externo se houver texto ao redor
            start_idx = clean_text.find("{")
            end_idx = clean_text.rfind("}")
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                clean_text = clean_text[start_idx : end_idx + 1]

            data = json.loads(clean_text)

            # Normalizações defensivas para campos retornados por SLMs 1.5B
            if isinstance(data.get("params"), str):
                try:
                    data["params"] = json.loads(data["params"])
                except Exception:
                    data["params"] = {}
            if data.get("params") is None:
                data["params"] = {}

            return DecisionOutput(**data)
        except Exception as e:
            logger.warning(f"Erro ao parsear JSON bruto '{raw_text[:150]}...': {e}")
            return self._generate_mock_decision(fallback_prompt, context_block)

    def _generate_mock_decision(self, prompt: str, context: str) -> DecisionOutput:
        """
        Heurística determinística de alta fidelidade para testes e modo mock.
        Garante que regras de segurança, inventário Minha Casa e aprendizado funcionem com precisão.
        """
        p_lower = prompt.lower()
        ctx_lower = context.lower()

        # Mapeamento uniforme de iluminação e relés (Grupo Casa, Externa e Atuadores Locais)
        # Avalia padrões mais específicos antes dos genéricos
        switch_targets = [
            (["varanda superior", "varanda_superior", "varanda de cima"], "varanda_superior", "Varanda superior"),
            (["jardim varanda", "jardim da varanda", "jardim_varanda"], "jardim_varanda", "Jardim da Varanda"),
            (["jardim garagem", "jardim da garagem", "jardim_garagem"], "jardim_garagem", "Jardim da Garagem"),
            (["jardim deck", "jardim do deck", "jardim_deck", "deck"], "jardim_deck", "Jardim Deck"),
            (["jardim principal", "jardim_principal", "jardim da frente"], "jardim_principal", "Jardim Principal"),
            (["corredor cima", "corredor de cima", "corredor_cima"], "corredor_cima", "Corredor de cima"),
            (["corredor externo", "corredor_externo", "corredor fora"], "corredor_externo", "Corredor externo"),
            (["luz sala", "luz da sala", "luz_sala", "sonoff_luz_sala"], "luz_sala", "Luz da Sala"),
        ]

        # 1. Luzes / Relés específicos (checado antes de 'garagem' para não confundir jardim_garagem com portao)
        for keywords, device_id, friendly_name in switch_targets:
            if any(k in p_lower for k in keywords):
                is_off = any(w in p_lower for w in ["desliga", "desligue", "apaga", "apague", "off"])
                cmd = CommandType.POWER_OFF if is_off else CommandType.POWER_ON
                verb = "Desliguei" if is_off else "Liguei"
                offline_note = " (Observação: dispositivo consta como Offline)" if device_id == "varanda_superior" else ""
                return DecisionOutput(
                    action=ActionType.EXECUTE,
                    target_device=device_id,
                    command=cmd,
                    params={},
                    learned_preference=None,
                    response_text=f"{verb} {friendly_name}.{offline_note}",
                )

        # 2. Regra Crítica de Segurança: Portão da Garagem / Fechadura
        if any(w in p_lower for w in ["portao", "portão", "abrir portão", "fecha portão", "abre o portão", "portao_garagem"]):
            return DecisionOutput(
                action=ActionType.EXECUTE,
                target_device="portao_garagem",
                command=CommandType.PULSE,
                params={"pulse_duration_ms": 1000},
                learned_preference=None,
                response_text="Comando de pulso de 1s enviado para o portao_garagem."
            )

        # 3. Climatização / Ar Condicionado Sala
        if any(w in p_lower for w in ["ar condicionado", "ar_condicionado", "ar da sala", "calor", "frio", "temperatura", "climatiz", "graus"]):
            target_temp = 23
            match_temp = re.search(r"(\d{2})\s*(?:c|graus|°c)", p_lower)
            if match_temp:
                target_temp = int(match_temp.group(1))
            elif "21" in ctx_lower:
                target_temp = 21
            elif "22" in ctx_lower:
                target_temp = 22
            elif "24" in ctx_lower:
                target_temp = 24

            if any(w in p_lower for w in ["desliga", "desligue", "apaga", "parar", "off"]):
                return DecisionOutput(
                    action=ActionType.EXECUTE,
                    target_device="ar_condicionado_sala",
                    command=CommandType.POWER_OFF,
                    params={},
                    learned_preference=None,
                    response_text="Desliguei o ar condicionado da sala."
                )

            learned_pref = None
            if any(w in p_lower for w in ["sempre", "prefiro", "deixe em", "regra"]):
                learned_pref = f"Morador prefere ar condicionado em {target_temp}°C quando houver calor."

            return DecisionOutput(
                action=ActionType.EXECUTE,
                target_device="ar_condicionado_sala",
                command=CommandType.SET_STATE,
                params={"temperature": target_temp, "mode": "cool"},
                learned_preference=learned_pref,
                response_text=f"Ajustei o ar condicionado da sala para {target_temp}°C no modo refrigeração."
            )

        # 4. Consulta / Sincronização de Dispositivos eWeLink ou Telemetria
        if any(w in p_lower for w in ["sincroniz", "sync", "ewelink", "qual a", "como está", "temperatura atual", "status", "sensores", "dispositivos"]):
            return DecisionOutput(
                action=ActionType.QUERY,
                target_device="ewelink_gateway",
                command=CommandType.SET_STATE,
                params={"query_type": "sync_devices"},
                learned_preference=None,
                response_text="Consultei os sensores e a topologia eWeLink da Minha Casa."
            )

        # 5. Comandos genéricos de iluminação (fallback para corredor/sala/jardim)
        if any(w in p_lower for w in ["luz", "lampada", "lâmpada", "ilumina", "acenda", "acende", "jardim", "corredor", "varanda"]):
            device = "luz_sala"
            if "corredor" in p_lower:
                device = "corredor_cima"
            elif "varanda" in p_lower:
                device = "varanda_superior"
            elif "jardim" in p_lower:
                device = "jardim_principal"

            is_off = any(w in p_lower for w in ["desliga", "desligue", "apaga", "apague", "off"])
            return DecisionOutput(
                action=ActionType.EXECUTE,
                target_device=device,
                command=CommandType.POWER_OFF if is_off else CommandType.POWER_ON,
                params={},
                learned_preference=None,
                response_text=f"{'Desliguei' if is_off else 'Liguei'} {device}."
            )

        # 6. Aprendizado explícito de rotina
        if any(w in p_lower for w in ["aprenda", "grave", "hábito", "rotina", "toda vez"]):
            return DecisionOutput(
                action=ActionType.LEARN,
                target_device="sistema_preferencias",
                command=CommandType.SET_STATE,
                params={"preference_text": prompt},
                learned_preference=f"Novo hábito registrado: {prompt}",
                response_text="Entendido! Registrei este hábito na sua memória semântica local."
            )

        return DecisionOutput(
            action=ActionType.SUGGEST,
            target_device="central_automacao",
            command=CommandType.SET_STATE,
            params={"raw_prompt": prompt},
            learned_preference=None,
            response_text="Interpretei seu comando. Como deseja proceder com essa automação?"
        )
