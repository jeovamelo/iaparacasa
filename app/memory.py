"""
Gerenciador de Memória Contínua (Curto e Longo Prazo) para Edge AI.
Combina buffer circular volátil para contexto imediato com persistência SQLite e
mecanismo leve de In-Context Learning sem necessidade de retreinamento do modelo.
"""

import json
import logging
import math
import os
import re
import sqlite3
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings

logger = logging.getLogger("iaparacasa.memory")


class ShortTermBuffer:
    """
    Buffer circular de memória de curto prazo (em RAM).
    Armazena os últimos eventos de telemetria e interações recentes.
    """

    def __init__(self, maxlen: int = 25):
        self._lock = threading.Lock()
        self._events: deque = deque(maxlen=maxlen)
        self._latest_telemetry: Dict[str, Dict[str, Any]] = {}

    def add_telemetry(self, device_id: str, metric: str, value: float, unit: str = ""):
        with self._lock:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            event = {
                "type": "telemetry",
                "device_id": device_id,
                "metric": metric,
                "value": value,
                "unit": unit,
                "timestamp": timestamp,
            }
            self._events.append(event)
            self._latest_telemetry[device_id] = {
                "metric": metric,
                "value": value,
                "unit": unit,
                "timestamp": timestamp,
            }

    def add_interaction(self, prompt: str, target_device: str, command: str, action: str):
        with self._lock:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            event = {
                "type": "action",
                "prompt": prompt,
                "target_device": target_device,
                "command": command,
                "action": action,
                "timestamp": timestamp,
            }
            self._events.append(event)

    def get_latest_telemetry(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return dict(self._latest_telemetry)

    def get_recent_events(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._events)
            return items[-limit:]

    def format_for_prompt(self) -> str:
        with self._lock:
            if not self._events:
                return "Nenhum evento recente registrado."

            lines = []
            # Resume as últimas leituras consolidadas
            if self._latest_telemetry:
                lines.append("[ESTADO ATUAL DOS SENSORES]:")
                for dev, data in self._latest_telemetry.items():
                    unit = f" {data['unit']}" if data["unit"] else ""
                    lines.append(f"- {dev} ({data['metric']}): {data['value']}{unit} (às {data['timestamp']})")

            # Últimas ações do morador / atuadores
            recent_actions = [e for e in self._events if e["type"] == "action"][-5:]
            if recent_actions:
                lines.append("[ÚLTIMAS AÇÕES RECENTES]:")
                for act in recent_actions:
                    lines.append(
                        f"- [{act['timestamp']}] Ação '{act['action']}' ({act['command']}) em '{act['target_device']}' (Comando do morador: '{act['prompt']}')"
                    )

            return "\n".join(lines)


class LongTermMemory:
    """
    Gerenciador de Memória Semântica e Aprendizado Contínuo (In-Context Learning).
    Persiste hábitos, rotinas e correções em SQLite local, recuperando-os
    dinamicamente via ranking de relevância semântica e contextual.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or settings.DB_PATH
        self._ensure_db_dir()
        self._init_sqlite()

    def _ensure_db_dir(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_sqlite(self):
        """Inicializa as tabelas de preferências aprendidas, histórico e telemetria."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Tabela de Preferências e Hábitos Consolidados
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                trigger_condition TEXT NOT NULL,
                preference_rule TEXT NOT NULL,
                target_device TEXT NOT NULL,
                action_template TEXT,
                confidence REAL DEFAULT 0.8,
                times_reinforced INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # Tabela de Histórico de Interações e Feedback de Usuário
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS interaction_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_prompt TEXT NOT NULL,
                sensor_context TEXT,
                decision_json TEXT NOT NULL,
                feedback_status TEXT DEFAULT 'pending',
                user_correction TEXT,
                applied_preference_id INTEGER,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # Tabela de Histórico Temporal de Telemetria
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS telemetry_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                metric TEXT NOT NULL,
                value REAL NOT NULL,
                unit TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # Registro persistente de dispositivos eWeLink e chaves LAN AES (devicekey)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS ewelink_device_registry (
                device_id TEXT PRIMARY KEY,
                ewelink_device_id TEXT,
                device_key TEXT,
                self_apikey TEXT DEFAULT '123',
                name TEXT,
                device_type TEXT,
                group_name TEXT,
                ip_address TEXT,
                port INTEGER DEFAULT 8081,
                is_multichannel INTEGER DEFAULT 0,
                channel_outlet INTEGER DEFAULT 0,
                online INTEGER DEFAULT 1,
                last_state TEXT DEFAULT '{}',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            conn.commit()

        # Insere preferências iniciais padrão se a base estiver vazia
        self._seed_default_rules()

    def _seed_default_rules(self):
        """Popula regras básicas de fábrica (segurança e conforto)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM preferences")
            if cursor.fetchone()[0] == 0:
                default_rules = [
                    (
                        "security",
                        "portao garagem acionamento",
                        "Portões de garagem (portao_garagem) devem SEMPRE usar comando 'pulse' de 1000ms para evitar danos ao motor.",
                        "portao_garagem",
                        json.dumps({"command": "pulse", "params": {"pulse_duration_ms": 1000}}),
                        1.0,
                        5,
                    ),
                    (
                        "thermal",
                        "calor sala acima de 26 graus",
                        "Quando a temperatura da sala ultrapassar 26°C e houver presença, morador prefere ar_condicionado_sala em 23°C no modo cool.",
                        "ar_condicionado_sala",
                        json.dumps({"command": "set_state", "params": {"temperature": 23, "mode": "cool"}}),
                        0.85,
                        2,
                    ),
                    (
                        "lighting",
                        "noite corredor presenca",
                        "Ao detectar presença no corredor após as 22h, acender corredor_cima para segurança.",
                        "corredor_cima",
                        json.dumps({"command": "power_on", "params": {}}),
                        0.80,
                        1,
                    ),
                ]
                cursor.executemany("""
                    INSERT INTO preferences (category, trigger_condition, preference_rule, target_device, action_template, confidence, times_reinforced)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, default_rules)
                conn.commit()

    def record_telemetry(self, device_id: str, metric: str, value: float, unit: str = ""):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO telemetry_history (device_id, metric, value, unit) VALUES (?, ?, ?, ?)",
                (device_id, metric, value, unit),
            )
            conn.commit()

    def record_interaction(
        self,
        user_prompt: str,
        sensor_context: str,
        decision_json: str,
        applied_preference_id: Optional[int] = None,
    ) -> int:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO interaction_log (user_prompt, sensor_context, decision_json, applied_preference_id)
                VALUES (?, ?, ?, ?)
                """,
                (user_prompt, sensor_context, decision_json, applied_preference_id),
            )
            conn.commit()
            return cursor.lastrowid

    def record_feedback(
        self,
        interaction_id: int,
        approved: bool,
        correction: Optional[str] = None,
        preference_to_learn: Optional[str] = None,
    ) -> Tuple[bool, Optional[int]]:
        """
        Processa feedback do morador:
        - Se aprovado: reforça a preferência associada (aumenta confidence e times_reinforced).
        - Se reprovado / corrigido: extrai ou grava o novo padrão de preferência.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM interaction_log WHERE id = ?", (interaction_id,))
            row = cursor.fetchone()
            if not row:
                logger.warning(f"Interação {interaction_id} não encontrada para feedback.")
                return False, None

            status = "confirmed" if approved else "corrected"
            cursor.execute(
                "UPDATE interaction_log SET feedback_status = ?, user_correction = ? WHERE id = ?",
                (status, correction, interaction_id),
            )

            pref_id = row["applied_preference_id"]
            if approved and pref_id:
                # Reforça preferência existente
                cursor.execute(
                    """
                    UPDATE preferences
                    SET times_reinforced = times_reinforced + 1,
                        confidence = MIN(1.0, confidence + 0.05),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (pref_id,),
                )
                conn.commit()
                return True, pref_id

            # Se houve correção ou nova regra aprendida
            if not approved or preference_to_learn:
                rule_text = preference_to_learn or correction or f"Correção para: '{row['user_prompt']}'"
                # Extrai dados da decisão anterior para base
                try:
                    decision_data = json.loads(row["decision_json"])
                    target_device = decision_data.get("target_device", "general_device")
                except Exception:
                    target_device = "general_device"

                # Cria ou atualiza preferência corrigida
                cursor.execute(
                    """
                    INSERT INTO preferences (category, trigger_condition, preference_rule, target_device, confidence, times_reinforced)
                    VALUES (?, ?, ?, ?, 0.90, 1)
                    """,
                    ("user_correction", row["user_prompt"][:100], rule_text, target_device),
                )
                new_pref_id = cursor.lastrowid
                conn.commit()
                return True, new_pref_id

            conn.commit()
            return True, None

    def add_preference(
        self,
        category: str,
        trigger_condition: str,
        preference_rule: str,
        target_device: str,
        action_template: Optional[Dict[str, Any]] = None,
        confidence: float = 0.85,
    ) -> int:
        action_str = json.dumps(action_template) if action_template else None
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO preferences (category, trigger_condition, preference_rule, target_device, action_template, confidence, times_reinforced)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (category, trigger_condition, preference_rule, target_device, action_str, confidence),
            )
            conn.commit()
            return cursor.lastrowid

    def get_all_preferences(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM preferences ORDER BY times_reinforced DESC, confidence DESC")
            return [dict(row) for row in cursor.fetchall()]

    def delete_preference(self, pref_id: int) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM preferences WHERE id = ?", (pref_id,))
            conn.commit()
            return cursor.rowcount > 0

    def _tokenize(self, text: str) -> List[str]:
        """Normalização de tokens em português (remove pontuação e stop words comuns)."""
        stop_words = {
            "a", "o", "as", "os", "de", "do", "da", "dos", "das", "em", "no", "na",
            "nos", "nas", "para", "por", "com", "e", "ou", "um", "uma", "uns", "umas",
            "se", "que", "quando", "meu", "minha", "eu", "esta", "este"
        }
        words = re.findall(r"\b[a-zA-Z0-9_áéíóúâêîôûãõç]{2,}\b", text.lower())
        return [w for w in words if w not in stop_words]

    def get_relevant_preferences(
        self,
        query: str,
        current_telemetry: Optional[Dict[str, Any]] = None,
        top_k: int = 3
    ) -> List[Dict[str, Any]]:
        """
        Recuperação Semântica Híbrida Leve (Zero Overhead de GPU/PyTorch).
        Calcula pontuação com base em:
        1. Similaridade léxica ponderada (overlap de termos de gatilho e dispositivo).
        2. Alinhamento com a telemetria atual (ex: se temperatura > 25°C, regras térmicas ganham bônus).
        3. Histórico de reforço (times_reinforced e confidence).
        """
        all_prefs = self.get_all_preferences()
        if not all_prefs:
            return []

        query_tokens = set(self._tokenize(query))

        # Extrai contexto numérico de telemetria
        telemetry_context = ""
        current_temp: Optional[float] = None
        if current_telemetry:
            for k, v in current_telemetry.items():
                telemetry_context += f" {k} {v}"
                if any(t in k.lower() for t in ["temp", "temperatura", "clima"]):
                    try:
                        current_temp = float(v)
                    except (ValueError, TypeError):
                        pass

        telemetry_tokens = set(self._tokenize(telemetry_context))
        scored_prefs: List[Tuple[float, Dict[str, Any]]] = []

        for pref in all_prefs:
            score = 0.0
            trigger_tokens = set(self._tokenize(pref["trigger_condition"]))
            rule_tokens = set(self._tokenize(pref["preference_rule"]))
            device_tokens = set(self._tokenize(pref["target_device"]))

            all_pref_tokens = trigger_tokens | rule_tokens | device_tokens

            # 1. Overlap com o comando do usuário
            query_overlap = len(query_tokens & all_pref_tokens)
            if query_tokens:
                score += (query_overlap / len(query_tokens)) * 3.0

            # 2. Overlap com telemetria
            if telemetry_tokens:
                telem_overlap = len(telemetry_tokens & all_pref_tokens)
                score += (telem_overlap / len(telemetry_tokens)) * 1.5

            # 3. Regras térmicas contextuais
            if current_temp is not None and pref["category"] == "thermal":
                if current_temp >= 25.0 and any(w in all_pref_tokens for w in ["calor", "ar", "frio", "26", "25", "refrigera"]):
                    score += 2.0
                elif current_temp <= 19.0 and any(w in all_pref_tokens for w in ["frio", "aquecer", "aquecedor"]):
                    score += 2.0

            # 4. Multiplicador de confiança e reforço
            confidence = float(pref.get("confidence", 0.8))
            reinforcements = int(pref.get("times_reinforced", 1))
            score *= (confidence * (1.0 + math.log1p(reinforcements) * 0.2))

            # Bônus para dispositivo citado diretamente
            if pref["target_device"].lower() in query.lower():
                score += 3.0

            scored_prefs.append((score, pref))

        # Ordena decrescente por score
        scored_prefs.sort(key=lambda x: x[0], reverse=True)

        # Filtra preferências com score mínimo
        top_results = [p for score, p in scored_prefs[:top_k] if score > 0.4]
        return top_results

    def upsert_ewelink_device(self, device: Dict[str, Any]) -> None:
        """Persiste ou atualiza metadados e chaves locais AES (devicekey) de dispositivos eWeLink."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO ewelink_device_registry (
                    device_id, ewelink_device_id, device_key, self_apikey, name,
                    device_type, group_name, ip_address, port, is_multichannel,
                    channel_outlet, online, last_state, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(device_id) DO UPDATE SET
                    ewelink_device_id = COALESCE(excluded.ewelink_device_id, ewelink_device_registry.ewelink_device_id),
                    device_key = COALESCE(excluded.device_key, ewelink_device_registry.device_key),
                    self_apikey = COALESCE(excluded.self_apikey, ewelink_device_registry.self_apikey),
                    name = COALESCE(excluded.name, ewelink_device_registry.name),
                    device_type = COALESCE(excluded.device_type, ewelink_device_registry.device_type),
                    group_name = COALESCE(excluded.group_name, ewelink_device_registry.group_name),
                    ip_address = COALESCE(excluded.ip_address, ewelink_device_registry.ip_address),
                    port = excluded.port,
                    is_multichannel = excluded.is_multichannel,
                    channel_outlet = excluded.channel_outlet,
                    online = excluded.online,
                    last_state = excluded.last_state,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    device["device_id"],
                    device.get("ewelink_device_id"),
                    device.get("device_key"),
                    device.get("self_apikey", "123"),
                    device.get("name"),
                    device.get("device_type", "latching_relay"),
                    device.get("group_name", "Atuadores Locais"),
                    device.get("ip_address"),
                    int(device.get("port", 8081)),
                    1 if device.get("is_multichannel") else 0,
                    int(device.get("channel_outlet", 0)),
                    1 if device.get("online", True) else 0,
                    json.dumps(device.get("last_state") or {}),
                ),
            )
            conn.commit()

    def get_ewelink_devices(self) -> List[Dict[str, Any]]:
        """Recupera dispositivos e chaves locais LAN persistidas para operação offline."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM ewelink_device_registry ORDER BY device_id ASC")
            rows = []
            for row in cursor.fetchall():
                item = dict(row)
                try:
                    item["last_state"] = json.loads(item.get("last_state") or "{}")
                except Exception:
                    item["last_state"] = {}
                item["is_multichannel"] = bool(item.get("is_multichannel"))
                item["online"] = bool(item.get("online"))
                rows.append(item)
            return rows

    def format_preferences_for_prompt(
        self,
        query: str,
        current_telemetry: Optional[Dict[str, Any]] = None,
        top_k: int = 3
    ) -> Tuple[str, List[str], Optional[int]]:
        """
        Formata o bloco <learned_preferences> pronto para injeção no prompt do LLM.
        Retorna:
          - Texto formatado do bloco
          - Lista de regras incluídas
          - ID da preferência mais relevante (para amarração de feedback)
        """
        relevant = self.get_relevant_preferences(query, current_telemetry, top_k=top_k)
        if not relevant:
            return "Nenhuma preferência personalizada específica para este contexto.", [], None

        lines = ["[PREFERÊNCIAS E HÁBITOS APRENDIDOS DO MORADOR]:"]
        applied_rules = []
        top_id = relevant[0]["id"] if relevant else None

        for p in relevant:
            rule_desc = f"- ({p['category'].upper()}) Dispositivo '{p['target_device']}': {p['preference_rule']} (Confiança: {p['confidence']:.2f}, Reforços: {p['times_reinforced']})"
            lines.append(rule_desc)
            applied_rules.append(p["preference_rule"])

        return "\n".join(lines), applied_rules, top_id


class ContextManager:
    """
    Fachada unificada para memória de curto e longo prazo.
    Monta o contexto dinâmico completo para a tomada de decisão do SLM.
    """

    def __init__(self, db_path: Optional[str] = None, max_short_term: int = 25):
        self.short_term = ShortTermBuffer(maxlen=max_short_term)
        self.long_term = LongTermMemory(db_path=db_path)

    def assemble_context(
        self,
        query: str,
        immediate_telemetry: Optional[Dict[str, Any]] = None
    ) -> Tuple[str, List[str], Optional[int]]:
        """
        Constrói o bloco hierárquico com curto prazo e hábitos de longo prazo.
        """
        # Se veio telemetria imediata na requisição, registra no buffer
        if immediate_telemetry:
            for k, v in immediate_telemetry.items():
                val = float(v) if isinstance(v, (int, float)) else 1.0
                unit = "C" if "temp" in k.lower() else ""
                self.short_term.add_telemetry(k, "telemetry_input", val, unit)

        short_term_text = self.short_term.format_for_prompt()
        long_term_text, applied_rules, top_pref_id = self.long_term.format_preferences_for_prompt(
            query=query,
            current_telemetry=immediate_telemetry,
            top_k=3
        )

        full_context = (
            f"<short_term_context>\n{short_term_text}\n</short_term_context>\n\n"
            f"<learned_preferences>\n{long_term_text}\n</learned_preferences>"
        )

        return full_context, applied_rules, top_pref_id

