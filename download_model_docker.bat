@echo off
title iaparacasa - Baixar Modelo Quantizado
echo ========================================================
echo    Baixando Qwen 2.5 1.5B Instruct Q4_K_M no container
echo ========================================================
echo.
docker exec -it iaparacasa-edge python scripts/download_model.py
echo.
pause

