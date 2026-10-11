import argparse,os,sys,subprocess,uuid
from pathlib import Path
import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url
root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description='Run regression against a task-owned disposable PostgreSQL database')
parser.add_argument('--full',action='store_true',help='Run all test categories, including SQLite and model contracts')
parser.add_argument('--category-report',type=Path)
args=parser.parse_args()
url=make_url((root/'data'/'postgres-test-url').read_text().strip())
name='xiaokua_test_'+uuid.uuid4().hex
admin=dict(host=url.host,port=url.port,user=url.username,password=url.password,dbname=url.database)
with psycopg.connect(**admin,autocommit=True) as c:c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
try:
    env=dict(os.environ,XIAOKUA_TEST_POSTGRES=url.set(database=name).render_as_string(hide_password=False))
    command=[sys.executable,'-m','pytest','-q']
    if not args.full:command.append('tests/test_postgres.py')
    if args.category_report:command+=['--category-report',str(args.category_report)]
    code=subprocess.run(command,cwd=root,env=env).returncode
finally:
    with psycopg.connect(**admin,autocommit=True) as c:c.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
raise SystemExit(code)
