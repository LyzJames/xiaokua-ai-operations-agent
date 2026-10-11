"""Check a tracked or clean public tree; report paths and categories, never secrets."""
import argparse
import json
import re
import subprocess
from pathlib import Path


TOKEN = re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,}|shp(?:at|ca|ss)_[a-fA-F0-9]{24,}|sk-[A-Za-z0-9]{24,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)')
MACHINE_PATH = re.compile(r'(?:[A-Z]:[\\/]Users[\\/][^\s/\\]+|' + '/' + r'Users/[^\s/]+)', re.I)
EMAIL = re.compile(r'[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
SAFE_DOMAINS = {'example.com','example.invalid','users.noreply.github.com','noreply.github.com'}
TEXT_EXTENSIONS = {'.py','.md','.txt','.json','.yml','.yaml','.toml','.ts','.tsx','.js','.css','.html','.ini','.mako','.csv','.lock','.example'}


def check(root, all_files=False):
    root = Path(root).resolve()
    if all_files:
        files = [p for p in root.rglob('*') if p.is_file() and '.git' not in p.relative_to(root).parts]
    else:
        result = subprocess.run(['git','ls-files','-z'],cwd=root,capture_output=True,check=True)
        files = [root / p.decode('utf-8') for p in result.stdout.split(b'\0') if p]
    issues = []
    def issue(path, category):
        issues.append({'file':path.relative_to(root).as_posix(),'category':category})
    for path in sorted(files):
        relative = path.relative_to(root)
        parts = relative.parts
        name = path.name.lower()
        if (any(p in {'data','.deploy','backups','release','.venv','node_modules','__pycache__'} for p in parts)
                or name in {'key.txt','api_key.txt','setup.token'}
                or (name.startswith('.env') and name != '.env.example')
                or path.suffix.lower() in {'.sqlite','.db','.key','.pem','.pyc','.zip','.tar'}):
            issue(path,'forbidden runtime/private artifact')
            continue
        if not path.exists():
            issue(path,'tracked file missing')
            continue
        raw = path.read_bytes()
        if path.suffix.lower() in {'.jpg','.jpeg','.png'}:
            if not (raw.startswith(b'\xff\xd8') or raw.startswith(b'\x89PNG\r\n\x1a\n')):
                issue(path,'image extension does not match format')
            if b'Exif\x00\x00' in raw or b'http://ns.adobe.com/xap/1.0/' in raw or (raw.startswith(b'\x89PNG') and any(tag in raw for tag in (b'tEXt',b'iTXt',b'zTXt',b'eXIf'))):
                issue(path,'image metadata requires review')
            continue
        if path.suffix.lower() not in TEXT_EXTENSIONS and path.name not in {'LICENSE','Dockerfile','Caddyfile','.gitignore','.dockerignore','.gitattributes'}:
            issue(path,'unreviewed file type')
            continue
        text = raw.decode('utf-8-sig')
        if TOKEN.search(text):
            issue(path,'credential/private key pattern')
        if MACHINE_PATH.search(text):
            issue(path,'personal machine path')
        if any((domain := m.group().split('@',1)[1].lower()) not in SAFE_DOMAINS
               and not domain.endswith(('.example','.invalid')) for m in EMAIL.finditer(text)):
            issue(path,'non-example contact requires review')
        if path.suffix.lower() == '.md':
            for match in re.finditer(r'!?\[[^\]]*\]\(([^\s)]+)\)',text):
                target = match.group(1).split('#',1)[0]
                if target and not re.match(r'[a-zA-Z][a-zA-Z0-9+.-]*:',target) and not (path.parent/target).exists():
                    issue(path,'broken relative documentation link')
    return {'files_checked':len(files),'issues':issues,'passed':not issues,
            'image_pixels_reviewed_by_scanner':False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--all-files',action='store_true')
    args = parser.parse_args()
    report = check(args.root,args.all_files)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    raise SystemExit(0 if report['passed'] else 1)
