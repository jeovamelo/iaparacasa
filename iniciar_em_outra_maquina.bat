@echo off
chcp 65001 >nul
title Iniciar iaparacasa2 na Nova Máquina
echo ======================================================
echo       INICIALIZADOR DO IAPARACASA 2 (EDGE AI)
echo ======================================================
echo.

cd /d "%~dp0"

echo [1/3] Verificando Docker na máquina...
docker version >nul 2>&1
if errorlevel 1 (
    echo [ERRO] Docker nao foi detectado ou nao esta em execucao!
    echo Certifique-se de que o Docker Desktop esta aberto e tente novamente.
    echo.
    pause
    exit /b 1
)

echo [2/3] Verificando e importando imagens Docker se necessario...
docker image inspect iaparacasa2:latest >nul 2>&1
if errorlevel 1 (
    echo Imagem local nao encontrada. Importando iaparacasa2_image.tar...
    echo Isso pode levar de 1 a 2 minutos. Aguarde...
    docker load -i iaparacasa2_image.tar
    if errorlevel 1 (
        echo [ERRO] Falha ao carregar iaparacasa2_image.tar!
        pause
        exit /b 1
    )
    echo Imagens carregadas com sucesso!
) else (
    echo [OK] Imagem iaparacasa2:latest ja esta presente no Docker.
)

echo.
echo [3/3] Iniciando contêineres (iaparacasa2-edge e iaparacasa2-mqtt)...
docker compose -f docker-compose.yml up -d
if errorlevel 1 (
    echo [ERRO] Falha ao subir os containers via docker compose.
    pause
    exit /b 1
)

echo.
echo ======================================================
echo    TUDO PRONTO! SISTEMA INICIADO COM SUCESSO!
echo ======================================================
echo.
echo O modelo neural local (Qwen 2.5 1.5B) leva cerca de
echo 40 a 50 segundos para carregar na memoria RAM.
echo.
echo Apos inicializar, acesse no navegador:
echo 👉 http://localhost:8001
echo.
echo Para ver os logs em tempo real:
echo    docker logs -f iaparacasa2-edge
echo.
echo Para parar o sistema:
echo    docker compose -f docker-compose.yml down
echo.
pause
