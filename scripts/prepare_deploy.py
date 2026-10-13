"""Creates private local deployment configuration. Never prints generated secrets."""
import argparse,os,re,secrets
from pathlib import Path
from cryptography.fernet import Fernet
root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser();parser.add_argument('--domain',required=True);args=parser.parse_args()
if not re.fullmatch(r'[a-z0-9][a-z0-9.-]+\.[a-z]{2,}',args.domain):raise SystemExit('Provide a valid DNS hostname without scheme or path')
directory=root/'.deploy';private=directory/'secrets';private.mkdir(parents=True,exist_ok=True)
if os.name!='nt':
    os.chmod(directory,0o700);os.chmod(private,0o700)
def write_once(path,text,secret=False):
    # Compose file secrets are bind mounts: container uid 10001 must be able to read.
    # Host protection is the private 0700 parent directory, not a root-only 0600 mount.
    mode=0o444 if secret and os.name!='nt' else 0o600
    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,mode)
    with os.fdopen(fd,'w',encoding='utf-8') as f:f.write(text)
password=secrets.token_urlsafe(40)
if (private/'db-password').exists():password=(private/'db-password').read_text().strip()
else:write_once(private/'db-password',password,True)
if not (private/'database-url').exists():write_once(private/'database-url','postgresql+psycopg://xiaokua:'+password+'@db:5432/xiaokua',True)
if not (private/'encryption-key').exists():write_once(private/'encryption-key',Fernet.generate_key().decode(),True)
if os.name!='nt':
    for path in private.iterdir():
        if path.is_file():os.chmod(path,0o444)
if not (directory/'production.env').exists():write_once(directory/'production.env','PUBLIC_HOST='+args.domain+'\nPUBLIC_URL=https://'+args.domain+'\nDEEPSEEK_MODEL=deepseek-flash\nDEEPSEEK_API_KEY=\nSHOPIFY_CLIENT_ID=\nSHOPIFY_CLIENT_SECRET=\nFEISHU_CONNECTION_ID=\n')
print('Private deployment files prepared. Fill platform credentials in .deploy/production.env; back up secrets separately.')
