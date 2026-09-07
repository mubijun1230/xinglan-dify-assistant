@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0docker"

where docker >nul 2>&1
if errorlevel 1 (
  echo 未找到 docker 命令，请先启动 Docker Desktop。
  pause
  exit /b 1
)

docker info >nul 2>&1
if errorlevel 1 (
  echo Docker 引擎未运行，请先打开 Docker Desktop 再试。
  pause
  exit /b 1
)

echo 正在启动 Dify ...
docker compose up -d
if errorlevel 1 (
  echo 启动失败，请查看上方报错。
  pause
  exit /b 1
)

if exist "%~dp0dingtalk-bridge\docker-compose.yml" (
  echo 正在启动钉钉桥接 ...
  pushd "%~dp0dingtalk-bridge"
  docker compose up -d --build
  popd
)

echo.
echo Dify 已启动，浏览器打开 http://localhost
echo 首次安装请访问 http://localhost/install
start "" "http://localhost"
endlocal
exit /b 0
