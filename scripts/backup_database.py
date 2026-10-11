"""Compose PostgreSQL backup + restore. Restore only into an explicitly named empty database."""
import argparse,datetime,hashlib,json,subprocess
from pathlib import Path
root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser();parser.add_argument('operation',choices=['backup','verify','restore']);parser.add_argument('--file');parser.add_argument('--target-db');parser.add_argument('--project-dir',default=str(root));args=parser.parse_args()
project_dir=Path(args.project_dir).resolve()
base=['docker','compose','--env-file','.deploy/production.env','-f','compose.yaml']
if args.operation=='backup':
    directory=project_dir/'backups';directory.mkdir(exist_ok=True)
    path=directory/('xiaokua-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.dump')
    with path.open('wb') as f:
        result=subprocess.run(base+['exec','-T','db','pg_dump','-U','xiaokua','-d','xiaokua','-Fc'],cwd=project_dir,stdout=f,stderr=subprocess.PIPE)
    if result.returncode:raise SystemExit('Backup failed; check the database service')
    path.with_suffix('.json').write_text(json.dumps({'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'format':'pg_dump custom','encryption_key_included':False,'created_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat()}))
    print('Backup created:',path)
else:
    if not args.file:raise SystemExit('--file is required')
    path=Path(args.file).resolve();meta=json.loads(path.with_suffix('.json').read_text())
    if hashlib.sha256(path.read_bytes()).hexdigest()!=meta['sha256']:raise SystemExit('Backup checksum mismatch')
    if args.operation=='verify':print('Backup checksum verified')
    else:
        import re
        target=args.target_db or ''
        if not re.fullmatch(r'xiaokua_restore_[a-z0-9_]+',target):raise SystemExit('Restore target must be a separate xiaokua_restore_* database')
        subprocess.run(base+['exec','-T','db','createdb','-U','xiaokua',target],cwd=project_dir,check=True)
        with path.open('rb') as f:r=subprocess.run(base+['exec','-T','db','pg_restore','-U','xiaokua','-d',target,'--exit-on-error'],cwd=project_dir,stdin=f)
        if r.returncode:raise SystemExit('Restore failed; source database was not changed')
        print('Restored to separate database:',target)
