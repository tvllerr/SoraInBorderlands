@echo off
title Sora in Borderlands - uninstall
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\setup.ps1" -Remove %*
echo.
pause
