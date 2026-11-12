@echo off
chcp 65001 >nul
echo ==============================================================================
echo  iaparacasa - Configuracao de Permissoes do Docker para Codex / CLI
echo ==============================================================================
echo.

:: Verifica se esta executando como Administrador
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [AVISO] Este script precisa ser executado como Administrador!
    echo.
    echo Por favor, clique com o BOTAO DIREITO neste arquivo e escolha:
    echo "Executar como Administrador" (Run as administrator)
    echo.
    pause
    exit /b 1
)

echo Adicionando o usuario "%USERNAME%" ao grupo "docker-users"...
net localgroup docker-users "%USERNAME%" /add

if %errorlevel% equ 0 (
    echo.
    echo [SUCESSO] Usuario "%USERNAME%" adicionado ao grupo docker-users com sucesso!
    echo.
    echo O Codex, terminais e ferramentas CLI agora possuem acesso direto ao Docker.
    echo OBS: Pode ser necessario reiniciar o Docker Desktop ou fazer logoff/login
    echo      no Windows para que o Windows aplique as novas permissoes de grupo.
) else (
    echo.
    echo [INFO] O usuario ja faz parte do grupo ou ocorreu um erro de permissao.
)

echo.
pause
