@echo off
title iaparacasa - Iniciar Servico Docker
echo ========================================================
echo    Iniciando iaparacasa Edge AI via Docker Compose
echo ========================================================
echo.
docker compose up -d --build
if %errorlevel% neq 0 (
    echo.
    echo [*] Tentando com comando legado 'docker-compose'...
    docker-compose up -d --build
)
echo.
echo Status dos containers ativos:
docker ps
echo.
echo ========================================================
echo    Servico disponivel em: http://localhost:8000
echo    Para abrir o terminal do container, use: enter_container.bat
echo ========================================================
echo.
pause

