@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在啟動 picOrganize 本機與手機版伺服器...
uv run pic publish --port 8765
pause
