"""Opt-in, read-only own-account checks; output status only, never identifiers."""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.config import credentials
from backend.integrations import shopify_readonly, IntegrationError
from backend.production.feishu import tenant_token, FeishuError
from backend.model_runtime import analyze_json, ModelFailure


def run(platforms=False, model=False):
    checks=[]
    def record(name, call):
        try:
            extra=call()
            valid=all(extra.get(k,True) for k in ('read_access','query_completed','evidence_validated')) if extra else True
            checks.append({'name':name,'passed':bool(valid),**(extra or {})})
        except ModelFailure as error:
            checks.append({'name':name,'passed':False,'code':error.code})
        except (IntegrationError,FeishuError):
            checks.append({'name':name,'passed':False,'code':'AUTHORIZATION_OR_PLATFORM_ACCESS'})
        except Exception:
            checks.append({'name':name,'passed':False,'code':'CHECK_UNAVAILABLE'})
    if platforms:
        record('Shopify installed-app read access',lambda: {'read_access':bool(shopify_readonly('health').get('shop'))})
        record('Shopify protected order query',lambda: {'query_completed':isinstance(shopify_readonly('orders').get('orders'),dict)})
        def feishu():
            keys=credentials()
            token=tenant_token({'app_id':keys.get('FEISHU_APP_ID',''),'app_secret':keys.get('FEISHU_APP_SECRET','')})
            if not token:raise FeishuError('No token')
            return {'application_authentication':True}
        record('Feishu application authentication',feishu)
    if model:
        def inference():
            # Never send store data, tenant information, IDs or customer text to this probe.
            output,metrics=analyze_json('你是只读研究验收助手，输入为合成数据。明确说明无真实订单。',
                {'statistics':{'synthetic_order_count':1},'evidence_ids':['synthetic-evidence-001']},['synthetic-evidence-001'])
            return {'schema_validated':True,'evidence_validated':output['evidence']==['synthetic-evidence-001'],
                'format':metrics['format'],'attempts':metrics['attempts']}
        record('DeepSeek bounded structured analysis',inference)
    return {'schema':1,'captured_at':datetime.now(timezone.utc).isoformat(),
        'scope':'read-only connectivity; not full event or business acceptance',
        'model_input':'synthetic only','platform_writes':0,'messages_sent':0,'payments':0,
        'checks':checks,'passed':bool(checks) and all(c['passed'] and
            c.get('read_access',True) and c.get('query_completed',True) and c.get('evidence_validated',True) for c in checks)}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platforms-readonly',action='store_true')
    parser.add_argument('--model',action='store_true',help='Makes a bounded synthetic model request; provider charges may apply')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if not args.platforms_readonly and not args.model:parser.error('Select an explicit read-only check scope')
    report=run(args.platforms_readonly,args.model)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    raise SystemExit(0 if report['passed'] else 1)
