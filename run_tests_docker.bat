@echo off
title iaparacasa - Executar Testes Unitarios e Integracao
echo ========================================================
echo    Executando bateria de testes no container Docker
echo ========================================================
echo.
docker exec -it iaparacasa-edge pytest tests/ -v
echo.
pause

