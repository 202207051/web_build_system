import os
import uuid
import shutil
import subprocess
import pymysql
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI()

class CompileRequest(BaseModel):
    language: str
    source_code: str

@app.post("/compile")
def compile_code(req: CompileRequest):
    language = req.language
    source_code = req.source_code

    if not language or not source_code:
        return {"status": "ERROR", "output": "Invalid input data."}

    # 1. 컨테이너 내부 작업 디렉터리 생성 (파일 생성용)
    task_id = "task_" + uuid.uuid4().hex[:12]
    container_workspace_dir = os.path.abspath(f"./workspace/{task_id}")
    os.makedirs(container_workspace_dir, exist_ok=True)

    # 2. 호스트 기준의 절대 경로 설정 (Docker 데몬 볼륨 마운트용)
    host_base_dir = os.getenv("HOST_WORKSPACE_BASE", "/opt/web_build_system/html/workspace")
    host_workspace_dir = f"{host_base_dir}/{task_id}"

    # 3. 언어별 소스 파일 생성 및 호스트 경로 기반 도커 명령어 설정
    container_cmd = []
    if language == 'python':
        with open(os.path.join(container_workspace_dir, "main.py"), "w", encoding="utf-8") as f:
            f.write(source_code)
        container_cmd = ["docker", "run", "--rm", "--network", "none", "-m", "256m", "-v", f"{host_workspace_dir}:/app:z", "python:3.11-slim", "python", "/app/main.py"]
    elif language == 'cpp':
        with open(os.path.join(container_workspace_dir, "main.cpp"), "w", encoding="utf-8") as f:
            f.write(source_code)
        container_cmd = ["docker", "run", "--rm", "--network", "none", "-m", "256m", "-v", f"{host_workspace_dir}:/app:z", "gcc:latest", "bash", "-c", "g++ /app/main.cpp -o /app/main && /app/main"]
    elif language == 'java':
        with open(os.path.join(container_workspace_dir, "Main.java"), "w", encoding="utf-8") as f:
            f.write(source_code)
        container_cmd = ["docker", "run", "--rm", "--network", "none", "-m", "256m", "-v", f"{host_workspace_dir}:/app:z", "openjdk:17-slim", "bash", "-c", "javac /app/Main.java && java -cp /app Main"]
    else:
        shutil.rmtree(container_workspace_dir, ignore_errors=True)
        return {"status": "ERROR", "output": "Unsupported language."}

    # 4. 프로세스 실행 및 5초 타임아웃 제어
    output = ""
    status = "SUCCESS"
    try:
        result = subprocess.run(container_cmd, capture_output=True, text=True, timeout=5)
        output = result.stdout + result.stderr
        if result.returncode != 0:
            status = "ERROR"
    except subprocess.TimeoutExpired:
        output = "Process execution timed out (5 seconds limit exceeded)."
        status = "TIMEOUT"
    except Exception as e:
        output = f"Process execution failed: {str(e)}"
        status = "ERROR"

    # 5. 연산 완료 후 MariaDB 적재
    db_saved = False
    try:
        connection = pymysql.connect(
            host='build_mariadb',
            user='admin',
            password='1234',
            database='build_system',
            charset='utf8mb4'
        )
        with connection.cursor() as cursor:
            sql = "INSERT INTO code_submissions (language, source_code, build_output, status) VALUES (%s, %s, %s, %s)"
            cursor.execute(sql, (language, source_code, output, status))
            connection.commit()
        connection.close()
        db_saved = True
    except Exception:
        pass

    # 6. 임시 작업 디렉터리 정리 (Clean-up) 및 컨테이너 자동 소멸 (--rm)
    shutil.rmtree(container_workspace_dir, ignore_errors=True)

    return {
        "task_id": task_id,
        "status": status,
        "db_saved": db_saved,
        "output": output
    }

app.mount("/", StaticFiles(directory="/var/www/html", html=True), name="static")