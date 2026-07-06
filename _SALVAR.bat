@echo off
chcp 65001 >nul
cd /d "%~dp0"
for %%I in ("%cd%") do set REPO=%%~nxI
echo ================================================
echo    WorkPulse - Salvar e enviar:  %REPO%
echo ================================================
echo.
rem --- destrava caso tenha sobrado um lock ---
if exist ".git\index.lock" del /f /q ".git\index.lock"
rem --- garante o vinculo com o GitHub ---
git remote get-url origin >nul 2>&1 || git remote add origin https://github.com/caiob/%REPO%.git
rem --- prepara todas as alteracoes ---
git add -A
echo Alteracoes que serao salvas:
git status -s
echo.
set /p MSG="Descreva a mudanca (ou tecle ENTER para usar data/hora): "
if "%MSG%"=="" set MSG=Atualizacao %date% %time%
git commit -m "%MSG%"
echo.
echo Enviando para o GitHub...
git push -u origin main
echo.
echo ================================================
echo    Concluido. Pode fechar esta janela.
echo ================================================
pause
