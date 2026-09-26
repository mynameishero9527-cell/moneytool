@echo off
rem 拉取 develop 最新代码后启动；依赖有变化时 start.bat 会自动重新安装。
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

git pull origin develop
if errorlevel 1 (
  echo 拉取代码失败，请检查网络或本地是否有未提交的改动。
  pause
  exit /b 1
)
call "%~dp0start.bat" %*
