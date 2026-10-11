"""Render the capability acceptance report without raw pytest totals."""
import argparse
import json
import os
import sys
from pathlib import Path


def render(report):
    labels = {'passed': '通过', 'failed': '需修复', 'incomplete': '部分运行 / 待补充环境'}
    lines = ['| 验收类别 | 结果 |', '| --- | --- |']
    for category in report['categories']:
        lines.append('| '+category['title']+' | '+labels[category['status']]+' |')
    if report.get('exit_code', 0):
        lines += ['', '测试进程未成功结束；请查看工作流诊断，不能仅凭分类表认定完整验收通过。']
    return '\n'.join(lines)+'\n'


if __name__ == '__main__':
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--github-summary', action='store_true')
    args = parser.parse_args()
    text = render(json.loads(args.input.read_text(encoding='utf-8')))
    print(text)
    if args.github_summary and os.getenv('GITHUB_STEP_SUMMARY'):
        with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a', encoding='utf-8') as output:
            output.write(text)
