@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist frontend\dist\index.html (echo [错误] 前端未构建（frontend/dist 缺失），请确认交付包完整 & pause & exit /b 1)
where py >nul 2>nul || where python >nul 2>nul || (echo [错误] 未找到 Python（需要 3.9+），请先运行 install.bat 或安装 Python & pause & exit /b 1)
(set PY=py -3) 2>nul || (set PY=python)
%PY% -c "import fastapi, uvicorn, openai, sse_starlette" || (echo [错误] Python 依赖未安装，请先运行 install.bat & pause & exit /b 1)
netstat -ano | findstr ":8501 " | findstr "LISTENING" >nul && (echo [错误] 端口 8501 已被占用，请关闭占用程序后重试（V-L4） & pause & exit /b 1)
echo 正在启动 DimensionCoder 服务（Ctrl+C 停止）...
cd python-backend && %PY% -m dc_server.server
cd ..
pause
