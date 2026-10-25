import os
import time
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

def init_database():
    """MariaDB 서버가 준비될 때까지 재시도하며 데이터베이스와 테이블을 자동 생성하는 함수"""
    db_host = os.getenv("DB_HOST", "build_mariadb")
    db_name = os.getenv("DB_NAME", "build_system")
    root_password = os.getenv("DB_ROOT_PASSWORD", "secure_password")
    
    max_retries = 10
    retry_delay = 3

    for attempt in range(max_retries):
        try:
            # 1. 루트(Root) 권한으로 접속하여 데이터베이스가 없으면 생성
            conn_root = pymysql.connect(
                host=db_host,
                user='root',
                password=root_password,
                charset='utf8mb4'
            )
            with conn_root.cursor() as cursor:
                cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{db_name}` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
            conn_root.close()

            # 2. 일반 계정(admin)으로 생성된 데이터베이스에 접속하여 테이블이 없으면 생성
            conn_user = pymysql.connect(
                host=db_host,
                user=os.getenv("DB_USER", "admin"),
                password=os.getenv("DB_PASS", "1234"),
                database=db_name,
                charset='utf8mb4'
            )
            with conn_user.cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS code_submissions (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        language VARCHAR(20) NOT NULL,
                        source_code TEXT NOT NULL,
                        build_output TEXT,
                        status VARCHAR(20) NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB;
                """)
            conn_user.close()
            print("Database and tables initialized successfully.")
            return
            
        except Exception as e:
            print(f"Database connection attempt {attempt + 1}/{max_retries} failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
            else:
                print("Failed to initialize database after maximum retries.")
                raise e

# 애플리케이션 구동 시 MariaDB 및 테이블 자동 생성 검사 실행
init_database()

@app.post("/compile")
def compile_code(req: CompileRequest):
    language = req.language
    source_code = req.source_code

    if not language or not source_code:
        return {"status": "ERROR", "output": "Invalid input data."}

    # 1. 고유 작업 디렉터리 생성 (workspace/task_[id])
    task_id = "task_" + uuid.uuid4().hex[:12]
    container_workspace_dir = os.path.abspath(f"./workspace/{task_id}")
    os.makedirs(container_workspace_dir, exist_ok=True)

    host_base_dir = os.getenv("HOST_WORKSPACE_BASE", "/opt/web_build_system/html/workspace")
    host_workspace_dir = f"{host_base_dir}/{task_id}"

    # 2. 선택된 언어에 따른 소스 파일 생성 및 Docker 컨테이너 실행 명령어 설정
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
        return {"status": "ERROR", "output": "Unsupported language selected."}

    # 3. 프로세스 실행 및 5초 타임아웃 제어 (격리된 Docker 컨테이너 빌드 및 실행)
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

    # 4. 빌드 후 결과 MariaDB 적재 (DB/테이블이 없으면 자동 생성된 환경에 저장)
    db_saved = False
    try:
        connection = pymysql.connect(
            host=os.getenv("DB_HOST", "build_mariadb"),
            user=os.getenv("DB_USER", "admin"),
            password=os.getenv("DB_PASS", "1234"),
            database=os.getenv("DB_NAME", "build_system"),
            charset='utf8mb4'
        )
        with connection.cursor() as cursor:
            sql = "INSERT INTO code_submissions (language, source_code, build_output, status) VALUES (%s, %s, %s, %s)"
            cursor.execute(sql, (language, source_code, output, status))
            connection.commit()
        connection.close()
        db_saved = True
    except Exception as e:
        print(f"DB save error: {e}")
        pass

    # 5. 임시 작업 디렉터리 정리 및 컨테이너 자동 닫힘 (--rm 옵션 및 소스 정리)
    shutil.rmtree(container_workspace_dir, ignore_errors=True)

    # 6. 웹페이지 출력용 결과 반환
    return {
        "task_id": task_id,
        "status": status,
        "db_saved": db_saved,
        "output": output
    }

# 정적 파일(index.html 등) 서빙
app.mount("/", StaticFiles(directory="/var/www/html", html=True), name="static")
