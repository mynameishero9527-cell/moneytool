@echo off
rem moneytool 诊断：检查环境、数据库与回补进度、失败任务、数据源连通，并汇总日志中的错误。
rem 主程序运行中也可以用。报告保存为 reports\doctor-latest.txt（每次覆盖）并自动用记事本打开，遇到问题把该文件发给开发者即可。
rem 额外参数原样传给 doctor，例如：doctor.bat --offline（跳过数据源检查）、doctor.bat --log-lines 200
setlocal EnableExtensions
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"

set "VPY=.venv\Scripts\python.exe"
if not exist "%VPY%" (
  echo 还没有安装运行环境，请先双击 start.bat 完成首次安装。
  pause
  exit /b 1
)

set "REPORT=reports\doctor-latest.txt"

echo 正在诊断，检查数据源连通约需 30 秒...
echo.
"%VPY%" -m moneytool doctor --out "%REPORT%" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
  echo 诊断完成：未发现问题。
) else (
  echo 诊断完成：发现问题，详见报告末尾的「结论」。
)
if exist "%REPORT%" start "" notepad "%REPORT%"
pause
exit /b %RC%
