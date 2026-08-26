@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/2] 检查环境...
where py >nul 2>nul || where python >nul 2>nul || (echo [错误] 未找到 Python，请安装 Python 3.9+ & pause & exit /b 1)
(set PY=py -3) 2>nul || (set PY=python)
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" || (echo [错误] Python 版本过低，需要 3.9+ & pause & exit /b 1)
echo [2/2] 安装 Python 依赖...
cd python-backend && %PY% -m pip install -r requirements.txt || (echo [错误] pip install 失败（内网可加镜像参数 -i https://pypi.tuna.tsinghua.edu.cn/simple，V-10） & pause & exit /b 1)
cd ..
echo 完成。请运行 start.bat 启动服务。
pause
