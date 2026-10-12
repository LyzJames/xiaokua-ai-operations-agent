"""Root hooks keep category CLI options discoverable on repeated runs.

Reports contain capability summaries only, never private test parameters.
"""
import json
import os
import platform
from datetime import datetime, timezone
from pathlib import Path
import pytest

CATEGORIES = {'business': '业务闭环', 'security': '权限与安全', 'agent': 'Agent 分析',
    'memory': '记忆与技能', 'reliability': '恢复与发布'}
_results = {}
_groups = {}


def pytest_addoption(parser):
    parser.addoption('--category-report', help='Write a sanitized category acceptance JSON report')


def pytest_collection_modifyitems(items):
    for item in items:
        selected = next((name for name in CATEGORIES if item.get_closest_marker(name)), None)
        if not selected:
            name = item.originalname or item.name
            file = Path(str(item.fspath)).name
            if file in ('test_postgres.py', 'test_feishu_ws.py') or any(w in name for w in ('restart', 'recovery', 'worker', 'lease', 'durable', 'heartbeat', 'backup', 'schedule', 'notification', 'webhook', 'event', 'routing')):
                selected = 'reliability'
            elif any(w in name for w in ('memory', 'knowledge', 'skill', 'support', 'ticket', 'learning')):
                selected = 'memory'
            elif any(w in name for w in ('auth', 'csrf', 'tenant', 'permission', 'approval', 'idempot', 'money', 'refund_split', 'whitelist', 'limit', 'privacy', 'redact', 'signature', 'secret', 'domain', 'ssrf', 'expired', 'expiration', 'audit', 'duplicate', 'guard')):
                selected = 'security'
            elif any(w in name for w in ('analysis', 'parallel', 'incident', 'model')):
                selected = 'agent'
            else:
                selected = 'business'
            item.add_marker(getattr(pytest.mark, selected))
        _groups[item.nodeid] = selected


def pytest_runtest_logreport(report):
    if report.nodeid not in _groups:
        return
    if report.failed:
        _results[report.nodeid] = 'failed'
    elif report.skipped and _results.get(report.nodeid) != 'failed':
        _results[report.nodeid] = 'skipped'
    elif report.when == 'call' and _results.get(report.nodeid) != 'failed':
        _results[report.nodeid] = 'passed'


def pytest_sessionfinish(session, exitstatus):
    target = session.config.getoption('--category-report')
    if not target:
        return
    categories = []
    for key, title in CATEGORIES.items():
        results = [_results.get(node, 'not_run') for node, group in _groups.items() if group == key]
        counts = {status: results.count(status) for status in ('passed', 'failed', 'skipped', 'not_run')}
        categories.append({'id': key, 'title': title, 'status': 'failed' if counts['failed'] else
            'incomplete' if counts['skipped'] or counts['not_run'] or not results else 'passed', 'cases': counts})
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    report = {'schema': 1, 'captured_at': datetime.now(timezone.utc).isoformat(),
        'suite': 'synthetic automated regression', 'python': platform.python_version(),
        'os': platform.system(), 'database': 'SQLite + disposable PostgreSQL' if os.getenv('XIAOKUA_TEST_POSTGRES') else 'SQLite',
        'exit_code': int(exitstatus), 'categories': categories}
    # Never record node parameters, exceptions, stdout, machine paths or credentials.
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
