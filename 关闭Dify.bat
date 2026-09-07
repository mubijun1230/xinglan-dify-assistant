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
  echo Docker 引擎未运行，无需关闭。
  pause
  exit /b 0
)

if exist "%~dp0dingtalk-bridge\docker-compose.yml" (
  echo 正在关闭钉钉桥接 ...
  pushd "%~dp0dingtalk-bridge"
  docker compose stop
  popd
)

echo 正在关闭 Dify（数据会保留）...
docker compose stop
if errorlevel 1 (
  echo 关闭失败，请查看上方报错。
  pause
  exit /b 1
)

echo Dify 已关闭。
timeout /t 2 /nobreak >nul
endlocal
exit /b 0
