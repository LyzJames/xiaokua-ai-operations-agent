"""Model transport contracts, output validation and bounded failure handling."""
import json
import httpx
import pytest
from backend import model_runtime as runtime

pytestmark = pytest.mark.agent
EVIDENCE = ['gid://shopify/Order/1']
VALID = {'finding': '一笔已同步测试订单待履约。', 'evidence': EVIDENCE, 'limitations': ['没有退货原因数据。']}


def wire(monkeypatch, handler, fmt='json'):
    original = httpx.Client
    monkeypatch.setattr(runtime, 'credentials', lambda: {'DEEPSEEK_API_KEY': 'synthetic-model-key'})
    monkeypatch.setenv('DEEPSEEK_ANALYSIS_FORMAT', fmt)
    monkeypatch.setattr(runtime.httpx, 'Client', lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(runtime.time, 'sleep', lambda _: None)
    return original


def response(value=VALID, finish='stop'):
    return httpx.Response(200, json={'choices': [{'finish_reason': finish, 'message': {'content': json.dumps(value)}}],
        'usage': {'prompt_tokens': 120, 'completion_tokens': 40}})


@pytest.mark.parametrize('value,code', [
    ({**VALID, 'refund': {'amount': 50}}, 'OUTPUT_SCHEMA'),
    ({**VALID, 'finding': '  '}, 'OUTPUT_SCHEMA'),
    ({**VALID, 'finding': 123}, 'OUTPUT_SCHEMA'),
    ({**VALID, 'finding': 'a' * 3501}, 'OUTPUT_SCHEMA'),
    ({**VALID, 'evidence': ['foreign-private-source']}, 'OUTPUT_EVIDENCE'),
    ({**VALID, 'evidence': []}, 'OUTPUT_EVIDENCE'),
    ({**VALID, 'limitations': ['x'] * 9}, 'OUTPUT_SCHEMA'),
    (['not-an-object'], 'OUTPUT_SCHEMA'),
])
def test_unsafe_or_ungrounded_output_is_not_repaired_or_executed(monkeypatch, value, code):
    calls = []
    def handler(request):
        calls.append(request)
        return response(value)
    original = wire(monkeypatch, handler)
    with pytest.raises(runtime.ModelFailure) as failure:
        runtime.analyze_json('只读分析', {'evidence_ids': EVIDENCE}, EVIDENCE)
    assert failure.value.code == code
    assert len(calls) == 1 and failure.value.telemetry['attempts'] == 1
    assert 'foreign-private-source' not in str(failure.value)


def test_stable_json_and_beta_strict_tools_both_validate_locally(monkeypatch):
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path.startswith('/beta/'):
            return httpx.Response(200, json={'choices': [{'message': {'tool_calls': [{'type': 'function',
                'function': {'name': 'submit_analysis', 'arguments': json.dumps(VALID)}}]}}]})
        return response()
    wire(monkeypatch, handler)
    output, telemetry = runtime.analyze_json('只读', {'evidence_ids': EVIDENCE}, EVIDENCE)
    assert output == VALID and telemetry['input_tokens'] == 120
    monkeypatch.setenv('DEEPSEEK_ANALYSIS_FORMAT', 'strict_tools')
    output, telemetry = runtime.analyze_json('只读', {'evidence_ids': EVIDENCE}, EVIDENCE)
    assert output == VALID and telemetry['input_tokens'] is None
    assert calls[0][1]['response_format'] == {'type': 'json_object'}
    tool = calls[1][1]['tools'][0]['function']
    assert tool['strict'] is True and tool['parameters']['additionalProperties'] is False
    assert 'maxLength' not in json.dumps(tool['parameters'])
    assert 'maxItems' not in json.dumps(tool['parameters'])


@pytest.mark.parametrize('status,code', [(401, 'MODEL_AUTH'), (400, 'MODEL_REQUEST'), (429, 'MODEL_RATE_LIMIT')])
def test_provider_errors_do_not_leak_credentials_or_retry_permanent_errors(monkeypatch, status, code):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text='private-response-must-not-leak', headers={'Retry-After': '60'})
    wire(monkeypatch, handler)
    with pytest.raises(runtime.ModelFailure) as failure:
        runtime.analyze_json('只读', {}, [])
    assert failure.value.code == code and len(calls) == 1
    assert 'private-response-must-not-leak' not in str(failure.value)
    assert 'synthetic-model-key' not in repr(failure.value.telemetry)


def test_transient_errors_are_bounded_and_report_usage(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(503) if len(calls) == 1 else response()
    original = wire(monkeypatch, handler)
    output, telemetry = runtime.analyze_json('只读', {}, EVIDENCE)
    assert output == VALID and telemetry['attempts'] == 2 and len(calls) == 2
    calls.clear()
    def unavailable(request):
        calls.append(request)
        raise httpx.ReadTimeout('private timeout data', request=request)
    monkeypatch.setattr(runtime.httpx, 'Client', lambda **kwargs: original(transport=httpx.MockTransport(unavailable), **kwargs))
    with pytest.raises(runtime.ModelFailure) as failure:
        runtime.analyze_json('只读', {}, EVIDENCE)
    assert failure.value.code == 'MODEL_TRANSPORT' and len(calls) == 2
    assert failure.value.telemetry['attempts'] == 2


@pytest.mark.parametrize('payload,code', [
    ({'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}]}, 'OUTPUT_TRUNCATED'),
    ({'choices': [{'message': {'content': ''}}]}, 'OUTPUT_JSON'),
    ({'choices': []}, 'OUTPUT_ENVELOPE'),
    ({'choices': [{'message': {'tool_calls': [{'function': {'name': 'refund'}}]}}]}, 'OUTPUT_TOOL'),
])
def test_empty_truncated_and_unexpected_tool_responses(monkeypatch, payload, code):
    wire(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(runtime.ModelFailure) as failure:
        runtime.analyze_json('只读', {}, EVIDENCE)
    assert failure.value.code == code


def test_missing_model_key_is_explicit_and_does_not_open_http(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_ANALYSIS_FORMAT', 'json')
    monkeypatch.setattr(runtime, 'credentials', lambda: {})
    monkeypatch.setattr(runtime.httpx, 'Client', lambda **kw: pytest.fail('No key must not open a client'))
    with pytest.raises(runtime.ModelFailure) as failure:
        runtime.analyze_json('只读', {}, [])
    assert failure.value.code == 'MODEL_NOT_CONFIGURED' and failure.value.telemetry['attempts'] == 0
