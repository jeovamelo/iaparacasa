@echo off
title iaparacasa - Parar Servico Docker
echo ========================================================
echo    Encerrando containers do iaparacasa
echo ========================================================
echo.
docker compose down
if %errorlevel% neq 0 (
    docker-compose down
)
echo.
echo Containers finalizados com sucesso.
pause

