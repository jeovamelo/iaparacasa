#!/usr/bin/env python3
"""
Simulador de Telemetria de Sensores para Casas Inteligentes.
Emite periodicamente dados sintéticos de temperatura, umidade e presença
para o endpoint /telemetry do iaparacasa.
"""

import json
import math
import random
import time
import urllib.request

API_URL = "http://localhost:8000/telemetry"


def send_telemetry(device_id: str, metric: str, value: float, unit: str):
    payload = {
        "device_id": device_id,
        "metric": metric,
        "value": round(value, 2),
        "unit": unit
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            print(f"[Telemetria Enviada] {data['device_id']}: {data['value']}{data['unit']}")
    except Exception as e:
        print(f"[Falha de Envio] {device_id}: {e}")


def main():
    print("[*] Iniciando gerador contínuo de telemetria doméstica...")
    step = 0
    try:
        while True:
            # Variação senoidal suave de temperatura + ruído aleatório
            temp_base = 25.0 + 4.0 * math.sin(step * 0.1) + random.uniform(-0.3, 0.3)
            humidity = 55.0 + 10.0 * math.cos(step * 0.1) + random.uniform(-1.0, 1.0)
            presence = 1.0 if random.random() > 0.4 else 0.0

            send_telemetry("sensor_temp_sala", "temperature", temp_base, "C")
            send_telemetry("sensor_umidade_sala", "humidity", humidity, "%")
            send_telemetry("sensor_presenca_sala", "motion", presence, "motion")

            step += 1
            time.sleep(5)
    except KeyboardInterrupt:
        print("\n[*] Simulador de telemetria encerrado.")


if __name__ == "__main__":
    main()

