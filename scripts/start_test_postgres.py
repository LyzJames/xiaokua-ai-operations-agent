"""Starts a task-owned, loopback-only disposable PostgreSQL instance; no credentials printed."""
import secrets,subprocess,time
from pathlib import Path
root=Path(__file__).resolve().parents[1];data=root/'data';data.mkdir(exist_ok=True)
password=secrets.token_urlsafe(32)
result=subprocess.run(['docker','run','--rm','-d','--name','xiaokua-postgres-test','--label','xiaokua.purpose=acceptance','-e','POSTGRES_PASSWORD='+password,'-e','POSTGRES_DB=xiaokua_test','-p','127.0.0.1:55432:5432','postgres:17-alpine'],capture_output=True,text=True)
if result.returncode:
    print('PostgreSQL test container could not start; existing containers were not changed.')
    raise SystemExit(1)
cid=result.stdout.strip();(data/'postgres-test-container').write_text(cid)
url='postgresql+psycopg://postgres:'+password+'@127.0.0.1:55432/xiaokua_test'
(data/'postgres-test-url').write_text(url)
for _ in range(40):
    r=subprocess.run(['docker','exec',cid,'pg_isready','-U','postgres'],capture_output=True)
    if r.returncode==0: print('PostgreSQL acceptance database ready');break
    time.sleep(1)
else: print('PostgreSQL startup timed out');raise SystemExit(1)
