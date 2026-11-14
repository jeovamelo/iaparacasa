@echo off
chcp 65001 >nul
title Exportador Docker - iaparacasa2
echo ======================================================
echo    EXPORTADOR DOCKER - IAPARACASA 2
echo ======================================================
echo.

set DOCKER_HOST=tcp://127.0.0.1:2375
set DEST_DIR=C:\Users\jeova\OneDrive\ChatGPT
set PROJECT_DIR=%~dp0

if not exist "%DEST_DIR%" (
    echo Criando pasta de destino: %DEST_DIR%
    mkdir "%DEST_DIR%"
)

echo [1/5] Verificando Docker...
docker -H %DOCKER_HOST% version >nul 2>&1
if errorlevel 1 (
    echo [INFO] Conexao via porta 2375 nao respondeu, usando Docker padrão...
    set DOCKER_CMD=docker
) else (
    echo [INFO] Usando Docker via %DOCKER_HOST%...
    set DOCKER_CMD=docker -H %DOCKER_HOST%
)

echo.
echo [2/5] Consolidando estado atual do contêiner iaparacasa2-edge...
%DOCKER_CMD% commit iaparacasa2-edge iaparacasa2:latest
if errorlevel 1 (
    echo [ERRO] Falha ao fazer commit do contêiner iaparacasa2-edge.
    pause
    exit /b 1
)

echo.
echo [3/5] Exportando imagens Docker (iaparacasa2 + mosquitto)...
echo       Salvando em: %DEST_DIR%\iaparacasa2_image.tar
echo       Aguarde alguns instantes...
%DOCKER_CMD% save -o "%DEST_DIR%\iaparacasa2_image.tar" iaparacasa2:latest eclipse-mosquitto:2
if errorlevel 1 (
    echo [ERRO] Falha ao exportar a imagem do Docker.
    pause
    exit /b 1
)

echo.
echo [4/5] Copiando arquivos essenciais para rodar em outra máquina...
if not exist "%DEST_DIR%\models" mkdir "%DEST_DIR%\models"
if not exist "%DEST_DIR%\data2" mkdir "%DEST_DIR%\data2"

echo - Copiando modelo GGUF...
xcopy /y /d "%PROJECT_DIR%models\*" "%DEST_DIR%\models\" >nul 2>&1

echo - Copiando banco de dados SQLite e configs...
xcopy /y /d "%PROJECT_DIR%data2\*" "%DEST_DIR%\data2\" >nul 2>&1

echo - Copiando variaveis de ambiente e compose...
copy /y "%PROJECT_DIR%.env" "%DEST_DIR%\.env" >nul
copy /y "%PROJECT_DIR%docker-compose.iaparacasa2.yml" "%DEST_DIR%\docker-compose.yml" >nul

echo.
echo [5/5] Exportação concluída com sucesso!
echo ======================================================
echo Pacote completo pronto em: %DEST_DIR%
echo - iaparacasa2_image.tar (Imagens Docker)
echo - models\ (Modelo neural GGUF)
echo - data2\ (Banco de dados e configs MQTT)
echo - .env e docker-compose.yml
echo - iniciar_em_outra_maquina.bat
echo ======================================================
echo.
pause
