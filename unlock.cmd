@echo off
rem 5 yanlis sifreden sonra kapanan Engent Telegram botunu yeniden acar. Servisi yeniden baslatmak gerekmez.
del /f /q "%~dp0data\lockout" "%~dp0data\fails" 2>nul
if exist "%~dp0data\lockout" (
    echo HATA: %~dp0data\lockout silinemedi.
) else (
    echo Engent botu acildi. Telegram'dan once sifreyi yaz ^(5 hak^).
)
pause
