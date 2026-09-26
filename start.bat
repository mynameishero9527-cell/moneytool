@echo off
rem moneytool 一键启动（Windows）：首次运行自动建虚拟环境、装依赖、初始化数据目录。
rem 额外参数原样传给 moneytool run，例如：start.bat --port 8765 --no-browser
rem 国内网络安装慢时，可先设置镜像：set PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
setlocal EnableExtensions
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"

set "VPY=.venv\Scripts\python.exe"
if exist "%VPY%" goto deps

echo [1/3] 创建虚拟环境 .venv
set "PY="
for %%V in (3.12 3.11 3.13) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
  )
)
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo 未找到 Python 3.11 及以上版本。请从 https://www.python.org/downloads/ 安装，安装时勾选 Add python.exe to PATH。
  goto fail
)
%PY% -m venv .venv
if errorlevel 1 goto fail

:deps
rem pyproject.toml 变化（如拉取新代码后依赖有增减）时重新安装
fc /b pyproject.toml .venv\pyproject.installed >nul 2>&1
if not errorlevel 1 goto init
echo [2/3] 安装依赖，首次需要几分钟
"%VPY%" -m pip install --upgrade pip --disable-pip-version-check -q
"%VPY%" -m pip install -e . --disable-pip-version-check
if errorlevel 1 goto fail
copy /y pyproject.toml .venv\pyproject.installed >nul

:init
echo [3/3] 检查数据目录
"%VPY%" -m moneytool init
if errorlevel 1 goto fail

echo 启动 moneytool：网页界面、后台接口、定时采集与历史回补都在这一个程序里，无需另外启动前端。
echo 浏览器将打开 http://127.0.0.1:8000 ；本窗口需保持打开，关闭本窗口或按 Ctrl+C 即停止全部功能。
"%VPY%" -m moneytool run %*
if errorlevel 1 goto fail
exit /b 0

:fail
echo.
echo 启动失败，请查看上方错误信息。
pause
exit /b 1
