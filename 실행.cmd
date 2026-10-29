@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if exist "%~dp0dist\출장증빙캡처_v1.4.0.exe" (
    start "" "%~dp0dist\출장증빙캡처_v1.4.0.exe"
    exit /b 0
)
if exist "%~dp0releases\v1.4.0\windows-x64\BTR.exe" (
    start "" "%~dp0releases\v1.4.0\windows-x64\BTR.exe"
    exit /b 0
)
if exist "%~dp0dist\출장증빙캡처_v1.3.1.exe" (
    start "" "%~dp0dist\출장증빙캡처_v1.3.1.exe"
    exit /b 0
)
if exist "%~dp0dist\출장증빙캡처_v1.3.exe" (
    start "" "%~dp0dist\출장증빙캡처_v1.3.exe"
    exit /b 0
)
if exist "%~dp0dist\출장증빙캡처_v1.2.1.exe" (
    start "" "%~dp0dist\출장증빙캡처_v1.2.1.exe"
    exit /b 0
)
if exist "%~dp0dist\출장증빙캡처_v1.2.exe" (
    start "" "%~dp0dist\출장증빙캡처_v1.2.exe"
    exit /b 0
)
if exist "%~dp0dist\출장증빙캡처_v1.1.exe" (
    start "" "%~dp0dist\출장증빙캡처_v1.1.exe"
    exit /b 0
)
if exist "%~dp0dist\출장증빙캡처.exe" (
    start "" "%~dp0dist\출장증빙캡처.exe"
    exit /b 0
)
where pyw >nul 2>nul
if not errorlevel 1 (
    start "" pyw -3 "%~dp0main.py"
    exit /b 0
)
where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%~dp0main.py"
    exit /b
)
where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw "%~dp0main.py"
    exit /b 0
)
where python >nul 2>nul
if not errorlevel 1 (
    python "%~dp0main.py"
    exit /b
)
echo Python을 찾지 못했습니다. 배포된 EXE를 실행하거나 Python 3.12를 설치해 주세요.
pause
exit /b 1
