@echo off
title Sora in Borderlands - update
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\setup.ps1" -Update %*
echo.
pause
