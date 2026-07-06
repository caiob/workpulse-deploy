@echo off
chcp 65001 >nul
cd /d "%~dp0"
for %%I in ("%cd%") do set REPO=%%~nxI
echo ================================================
echo    WorkPulse - Atualizar do GitHub:  %REPO%
echo ================================================
echo.
if exist ".git\index.lock" del /f /q ".git\index.lock"
git remote get-url origin >nul 2>&1 || git remote add origin https://github.com/caiob/%REPO%.git
echo Buscando a versao mais recente do GitHub...
git pull origin main
echo.
echo ================================================
echo    Atualizado. Pode fechar esta janela.
echo ================================================
pause
