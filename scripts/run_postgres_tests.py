import os,sys,subprocess,uuid
from pathlib import Path
import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url
root=Path(__file__).resolve().parents[1]
url=make_url((root/'data'/'postgres-test-url').read_text().strip())
name='xiaokua_test_'+uuid.uuid4().hex
admin=dict(host=url.host,port=url.port,user=url.username,password=url.password,dbname=url.database)
with psycopg.connect(**admin,autocommit=True) as c:c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
try:
    env=dict(os.environ,XIAOKUA_TEST_POSTGRES=url.set(database=name).render_as_string(hide_password=False))
    code=subprocess.run([sys.executable,'-m','pytest','tests/test_postgres.py','-q'],cwd=root,env=env).returncode
finally:
    with psycopg.connect(**admin,autocommit=True) as c:c.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
raise SystemExit(code)
