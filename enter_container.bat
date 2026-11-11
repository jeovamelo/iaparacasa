@echo off
title iaparacasa - Entrar no Container Docker
echo ========================================================
echo    Conectando ao terminal interativo do iaparacasa-edge
echo ========================================================
echo.
docker exec -it iaparacasa-edge bash
if %errorlevel% neq 0 (
    echo.
    echo [*] Tentando com shell 'sh'...
    docker exec -it iaparacasa-edge sh
)
if %errorlevel% neq 0 (
    echo.
    echo [-] Erro: O container nao parece estar em execucao.
    echo     Execute 'start_docker.bat' primeiro para inicia-lo.
    echo.
    pause
)

