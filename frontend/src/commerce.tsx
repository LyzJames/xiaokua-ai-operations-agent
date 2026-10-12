import React, {useEffect, useState} from 'react';
import {ArrowRight, Bot, Plus, ShieldCheck, RefreshCw, Link2} from 'lucide-react';

type Obj = Record<string, any>;
type Api = (path: string, body?: unknown) => Promise<any>;
type Run = (fn: () => Promise<any>, close?: boolean) => Promise<void>;
const names: Obj = {shopify: 'Shopify', amazon: 'Amazon', taobao: '淘宝', jd: '京东', pinduoduo: '拼多多', temu: 'TEMU', feishu: '飞书'};
export const platformName = (provider: string) => names[provider] || provider;
export const storeName = (store?: Obj) => store ? `${platformName(store.provider)} · ${store.info?.label || store.info?.shop?.name || '已授权店铺'}` : '已解除的店铺';

export function ApprovalSummary({action, data}: {action: Obj; data: Obj}) {
  const p=action.payload, b=action.before;
  const change=action.kind==='inventory' ? `可售库存 ${b.quantity} → ${p.quantity}` : action.kind==='pool_adjust' ? `实物在库 ${b.on_hand} → ${p.quantity} · 安全库存 ${p.safety}` : action.kind==='refund' ? `原交易退款 ${p.amount} ${p.currency}` : action.kind==='sandbox_stock' ? `${p.sku} 增加 ${p.quantity} 件虚拟库存` : action.kind==='mcf_create' ? `${p.sku} 沙箱履约 ${p.quantity} 件` : action.kind==='sandbox_status' ? `${b.status} → ${p.status}` : action.kind==='sandbox_sku' ? `创建测试 SKU ${p.sku}` : '';
  return <div className="p-action-summary"><small>{action.kind==='pool_adjust'?'物理共享仓库':storeName(data.connections.find((c:Obj)=>c.id===action.connection_id))}</small>{change&&<strong>{change}</strong>}</div>;
}

export function CommandCenter({data, api, run, busy, initialShop, onCreated}: {
  data: Obj; api: Api; run: Run; busy: boolean; initialShop: string; onCreated: () => void;
}) {
  const stores = data.connections.filter((c: Obj) => c.capabilities?.operations.includes('sync') && c.status === 'active');
  const [selected, setSelected] = useState<string[]>([]);
  const [command, setCommand] = useState('');
  useEffect(() => {setSelected(initialShop ? [initialShop] : []);}, [data.organization.id, initialShop]);
  const canOperate = ['owner', 'approver', 'operator'].includes(data.user.role);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    await run(async () => {
      await api('/commands', {command, connection_ids: selected, use_ai: true, idempotency_key: crypto.randomUUID()});
      setCommand(''); onCreated();
    }, false);
  };
  return <article className="p-card p-command-center">
    <div className="p-card-head"><h2>Web 指挥台</h2><Bot size={21}/></div>
    <p>在这里选择店铺并下达目标。飞书消息进入相同的任务、审批和执行流程。</p>
    <fieldset className="p-shop-selection"><legend>本次指令的店铺范围</legend>
      {stores.map((store: Obj) => <label key={store.id}><input type="checkbox" checked={selected.includes(store.id)}
        onChange={e => setSelected(values => e.target.checked ? [...values, store.id] : values.filter(id => id !== store.id))}/>
        <span>{storeName(store)}<small>{store.capabilities.environment === 'sandbox' ? 'Amazon 沙箱' : data.test_only ? '测试保护' : '真实业务'}</small></span></label>)}
      {!stores.length && <p>先在平台连接中添加自己的店铺，或在本机演示中同步合成店铺。</p>}
    </fieldset>
    <form onSubmit={submit}>
      <textarea aria-label="跨平台运营指令" value={command} onChange={e => setCommand(e.target.value)} required maxLength={2000}
        placeholder="例如：同步并分析所选店铺；将 SKU DEMO-BULB-01 库存设为 8；申请订单 #1001 退款 0.50 USD"/>
      <div className="p-card-foot"><small><ShieldCheck size={14}/> 多店任务分别追踪，写入逐项独立审批</small>
        <button className="p-button primary" disabled={busy || !canOperate || !selected.length || !command.trim()}>下达指令<ArrowRight size={16}/></button></div>
    </form>
    <p className="p-note">关联共享库存池的规格，需要在“共享库存”中明确填写各店配额。数据不完整或对象不唯一时会要求补充指令。</p>
  </article>;
}

export function AmazonConnection({data, api, run, busy}: {data: Obj; api: Api; run: Run; busy: boolean}) {
  const [form, setForm] = useState<Obj>({label: 'Amazon 店铺', region: 'na', marketplace_id: 'ATVPDKIKX0DER', environment: 'sandbox', client_id: '', client_secret: '', refresh_token: ''});
  const change = (key: string, value: string) => setForm(values => ({...values, [key]: value}));
  const owner = data.user.role === 'owner';
  return <article className="p-card">
    <div className="p-card-head"><h2>Amazon SP-API</h2><Link2 size={20}/></div>
    <p>订单与 FBA 库存通过店铺授权读取；动态沙箱支持库存及 MCF 履约的审批流程。生产 MCF 写入需后续完成专门验收。</p>
    <form onSubmit={e => {e.preventDefault();run(async () => {await api('/amazon/connect', form);setForm(values => ({...values, client_id: '', client_secret: '', refresh_token: ''}));}, false);}}>
      <div className="p-config-grid">
        <label className="p-field"><span>店铺标签</span><input required value={form.label} onChange={e => change('label', e.target.value)}/></label>
        <label className="p-field"><span>环境</span><select value={form.environment} onChange={e => change('environment', e.target.value)}><option value="sandbox">Amazon 沙箱</option><option value="production">已授权生产账号（当前只读）</option></select></label>
        <label className="p-field"><span>区域</span><select value={form.region} onChange={e => setForm(values => ({...values, region: e.target.value, marketplace_id: ({na: 'ATVPDKIKX0DER', eu: 'A1PA6795UKMFR9', fe: 'A1VC38T7YXB528'} as Obj)[e.target.value]}))}><option value="na">北美</option><option value="eu">欧洲</option><option value="fe">远东</option></select></label>
        <label className="p-field"><span>Marketplace ID</span><input required value={form.marketplace_id} onChange={e => change('marketplace_id', e.target.value)}/></label>
        {['client_id', 'client_secret', 'refresh_token'].map(key => <label className="p-field" key={key}><span>{({client_id: 'LWA Client ID', client_secret: 'LWA Client Secret', refresh_token: '授权 Refresh Token'} as Obj)[key]}</span><input type="password" autoComplete="off" required value={form[key]} onChange={e => change(key, e.target.value)}/></label>)}
      </div>
      <div className="p-card-foot"><small>凭据仅在后端加密保存</small><button className="p-button primary" disabled={!owner || busy}>验证并连接</button></div>
    </form>
    {!data.deployment.https && <button className="p-text" disabled={!owner || busy} onClick={() => run(() => api('/amazon/import-local', {}), false)}>本机联调：导入私有配置中的 Amazon 沙箱凭据</button>}
  </article>;
}

export function AmazonSandbox({data, connectionId, api, run, busy}: {data: Obj; connectionId: string; api: Api; run: Run; busy: boolean}) {
  const connection = data.connections.find((c: Obj) => c.id === connectionId && c.provider === 'amazon' && c.info.environment === 'sandbox');
  const items = data.variants.filter((v: Obj) => v.connection_id === connectionId);
  const [sku, setSku] = useState('');
  const [newSku, setNewSku] = useState('xiaokua-test-');
  const [quantity, setQuantity] = useState('1');
  const [order, setOrder] = useState('');
  const [status, setStatus] = useState('COMPLETE');
  const [packageId, setPackageId] = useState('');
  const [packageStatus, setPackageStatus] = useState('OUT_FOR_DELIVERY');
  if (!connection) return null;
  const canOperate = ['owner', 'approver', 'operator'].includes(data.user.role);
  const propose = (kind: string, params: Obj) => run(() => api('/tasks', {command: `Amazon 沙箱 ${kind}`, kind, params, connection_id: connectionId, idempotency_key: crypto.randomUUID()}), false);
  return <article className="p-card">
    <div className="p-card-head"><h2>Amazon 动态沙箱业务</h2><ShieldCheck size={20}/></div>
    <p>订单静态样例与 FBA 动态状态分别展示。这里的履约不会产生真实包裹或资金支出。</p>
    <label className="p-field"><span>新的测试 SKU（以 xiaokua-test- 开头）</span><input value={newSku} onChange={e => setNewSku(e.target.value)} maxLength={73}/></label>
    <button className="p-button" disabled={busy || !canOperate || newSku.length <= 13} onClick={() => propose('sandbox_sku', {sku: newSku})}>申请创建沙箱 SKU</button>
    <div className="p-config-grid">
      <label className="p-field"><span>已同步的 FBA 测试 SKU</span><select value={sku} onChange={e => setSku(e.target.value)}><option value="">选择测试 SKU</option>{items.map((v: Obj) => <option key={v.id} value={v.normalized.sku}>{v.normalized.sku} · 可用 {v.normalized.available ?? '未知'}</option>)}</select></label>
      <label className="p-field"><span>数量</span><input type="number" min={1} max={1000000} value={quantity} onChange={e => setQuantity(e.target.value)}/></label>
    </div>
    <div className="p-card-foot"><button className="p-button" disabled={busy || !canOperate || !sku} onClick={() => propose('sandbox_stock', {sku, quantity: Number(quantity)})}>申请增加虚拟库存</button>
      <button className="p-button primary" disabled={busy || !canOperate || !sku} onClick={() => propose('mcf_create', {sku, quantity: Number(quantity)})}>申请 MCF 履约</button></div>
    <div className="p-config-grid">
      <label className="p-field"><span>本店沙箱 MCF 编号（从审批执行结果复制）</span><input value={order} onChange={e => setOrder(e.target.value)} maxLength={40}/></label>
      <label className="p-field"><span>沙箱目标状态</span><select value={status} onChange={e => setStatus(e.target.value)}><option value="PROCESSING">PROCESSING</option><option value="COMPLETE">COMPLETE</option><option value="COMPLETE_PARTIAL">COMPLETE_PARTIAL</option><option value="UNFULFILLABLE">UNFULFILLABLE</option></select></label>
    </div>
    <div className="p-card-foot"><button className="p-button" disabled={busy || !canOperate || !order} onClick={() => propose('mcf_cancel', {order_id: order})}>申请取消 MCF</button>
      <button className="p-button" disabled={busy || !canOperate || !order} onClick={() => propose('sandbox_status', {order_id: order, status})}>申请推进状态</button></div>
    <div className="p-config-grid"><label className="p-field"><span>沙箱包裹编号（从完成状态的执行结果复制）</span><input value={packageId} onChange={e=>setPackageId(e.target.value)} maxLength={120}/></label><label className="p-field"><span>虚拟包裹状态</span><select value={packageStatus} onChange={e=>setPackageStatus(e.target.value)}>{['IN_TRANSIT','OUT_FOR_DELIVERY','DELIVERED','DELAYED','UNDELIVERABLE'].map(s=><option key={s} value={s}>{s}</option>)}</select></label></div>
    <button className="p-button" disabled={busy||!canOperate||!order||!packageId} onClick={()=>propose('sandbox_package',{order_id:order,package_id:packageId,status:packageStatus})}>申请推进虚拟包裹</button>
  </article>;
}

const environments: Obj = {production: '真实业务', test: '测试订单', demo: '合成数据', sandbox_static: 'Amazon 官方静态样例', sandbox_dynamic: 'Amazon 动态沙箱'};
type ResourcesProps = {data: Obj; records: Obj[]; canOperate: boolean; busy: boolean; sync: () => void; open: (kind: string, form: Obj, item: Obj) => void};
export function CommerceOrders({data, records, canOperate, busy, sync, open}: ResourcesProps) {
  return <article className="p-card"><div className="p-card-head"><h2>订单列表 <small>{records.length} 笔</small></h2><button className="p-button" disabled={!canOperate || busy} onClick={sync}><RefreshCw size={15}/>同步</button></div>
    <div className="p-table-wrap"><table><thead><tr><th>订单 / 来源</th><th>金额</th><th>付款状态</th><th>履约状态</th><th>物流</th><th>业务操作</th></tr></thead><tbody>{records.map(record => {
      const order = record.normalized, store = data.connections.find((c: Obj) => c.id === record.connection_id);
      const operations = store?.capabilities?.operations || [];
      return <tr key={record.id}><td><strong>{order.reference}</strong><small>{environments[order.environment]} · {platformName(order.provider)}</small><small>{order.created_at?.slice(0,10)}</small></td>
        <td>{order.total.amount ?? '未提供'} {order.total.currency || ''}</td><td>{order.financial_status === 'UNKNOWN' ? '平台未提供' : order.financial_status}</td><td>{order.cancelled ? '已取消' : order.fulfillment_status}</td>
        <td>{order.tracking.map((t: Obj) => t.number).join(', ') || '—'}</td><td><div className="p-inline-buttons">{['fulfill','tracking','cancel','refund'].filter(k => operations.includes(k)).map(kind => <button className="p-text" key={kind}
          disabled={!canOperate || busy || order.cancelled || data.test_only && !['test','demo'].includes(order.environment) || kind === 'tracking' && !record.data.fulfillments?.length}
          onClick={() => open(kind, {order_id: order.id, reason: 'OTHER', currency: order.currency, fulfillment_id: record.data.fulfillments?.[0]?.id}, record)}>{({fulfill:'履约',tracking:'物流',cancel:'取消',refund:'退款'} as Obj)[kind]}</button>)}</div>{!operations.includes('fulfill') && <small>本平台订单当前只读</small>}</td></tr>;
    })}</tbody></table></div>
    {!records.length && <p className="p-note">尚未同步订单，请先同步当前店铺。</p>}
    <p className="p-note">Amazon 静态订单为所在区域的官方固定样例，不代表授权店铺的真实订单。生产订单依店铺权限读取。取消不自动退款，物流信息尚未接入承运商轨迹。</p>
  </article>;
}

export function CommerceInventory({data, records, canOperate, busy, sync, open}: ResourcesProps) {
  return <article className="p-card"><div className="p-card-head"><h2>商品与可用库存</h2><button className="p-button" disabled={!canOperate || busy} onClick={sync}><RefreshCw size={15}/>同步</button></div>
    <div className="p-table-wrap"><table><thead><tr><th>商品 / SKU</th><th>单价</th><th>库存地点</th><th>可用数量</th><th>操作</th></tr></thead><tbody>{records.flatMap(record => {
      const product = record.normalized, store = data.connections.find((c: Obj) => c.id === record.connection_id);
      const operations = store?.capabilities?.operations || [];
      return (product.locations.length ? product.locations : [null]).map((loc: Obj | null, index: number) => {
        const mapped = data.shared_inventory.mappings.some((m: Obj) => m.connection_id === record.connection_id && m.item_id === product.inventory_item_id && m.location_id === loc?.id);
        return <tr key={record.id+index}><td><strong>{product.title}</strong><small>{product.sku} · {platformName(product.provider)}</small><small>{environments[product.environment]}</small></td><td>{product.price.amount ?? '未提供'} {product.price.currency || ''}</td>
          <td>{loc?.name || '未激活'}{loc?.pool_type === 'platform' && <small>平台仓 · 独立库存</small>}</td><td>{loc?.available ?? '未知'}{!product.locations_complete && <small>地点未全部载入</small>}</td>
          <td><div className="p-inline-buttons">{operations.includes('inventory') && <button className="p-text" disabled={!canOperate || busy || !loc || mapped} onClick={() => open('inventory', {item_id:product.inventory_item_id,location_id:loc?.id,quantity:Math.max(0,loc?.available || 0)}, record)}>{mapped ? '共享池统一分配' : '调整库存'}</button>}
            {operations.includes('product') && <button className="p-text" disabled={!canOperate || busy} onClick={() => open('product', {product_id:product.product_id,description:product.description}, record)}>更新说明</button>}
            {!operations.includes('inventory') && <small>FBA 库存读取；测试写入见下方沙箱</small>}</div></td></tr>;
      });
    })}</tbody></table></div>{!records.length && <p className="p-note">尚未同步商品，请先同步当前店铺。</p>}
    <p className="p-note">同一库存地点的写入使用状态比较校验。已映射的规格须通过共享库存分配，不能绕过库存池直接增加可售数量。</p>
  </article>;
}

export function SharedStock({data, api, run, busy}: {data: Obj; api: Api; run: Run; busy: boolean}) {
  const state = data.shared_inventory || {pools: [], mappings: [], plans: [], journal: []};
  const [poolForm, setPoolForm] = useState<Obj>({name: '', sku: '', warehouse: '', safety: '5', pool_type: 'merchant'});
  const [poolId, setPoolId] = useState('');
  const [variantId, setVariantId] = useState('');
  const [locationId, setLocationId] = useState('');
  const [count, setCount] = useState('0');
  const [targets, setTargets] = useState<Obj>({});
  const owner = data.user.role === 'owner', operate = ['owner', 'approver', 'operator'].includes(data.user.role);
  const pool = state.pools.find((p: Obj) => p.id === poolId);
  const variants = data.variants.filter((v: Obj) => v.normalized);
  const variant = variants.find((v: Obj) => v.id === variantId);
  const mappings = state.mappings.filter((m: Obj) => m.pool_id === poolId);
  useEffect(() => {setTargets(Object.fromEntries(mappings.map((m: Obj) => [m.id, String(m.quota)])));}, [poolId, state.mappings.map((m: Obj) => `${m.id}:${m.quota}`).join('|')]);
  const proposeCount = () => run(() => api('/tasks', {command: `共享库存盘点 ${pool?.sku}`, kind: 'pool_adjust', params: {pool_id: poolId, quantity: Number(count)}, idempotency_key: crypto.randomUUID()}), false);
  return <>
    <div className="p-banner"><ShieldCheck/><div><h3>一个物理库存池，明确分配给各店。</h3><p>订单预留、发货扣实物、取消释放；退款不直接入库。跨店变更先减后增，各写入独立审批。</p></div></div>
    <article className="p-card"><div className="p-card-head"><h2>库存池</h2><RefreshCw size={20}/></div>
      <div className="p-table-wrap"><table><thead><tr><th>仓库 / 主 SKU</th><th>在库 / FBA 可用</th><th>未发货预留</th><th>安全库存</th><th>可分配</th><th>状态</th></tr></thead><tbody>
        {state.pools.map((p: Obj) => <tr key={p.id}><td><strong>{p.name}</strong><small>{p.warehouse} · {p.sku} · {p.pool_type === 'platform' ? '平台仓只读' : '商家仓'}</small></td><td>{p.on_hand}</td><td>{p.reserved}</td><td>{p.safety}</td><td>{p.available}</td><td>{!p.initialized ? '待盘点审批' : p.frozen ? '冻结增库存' : '已启用'}{p.issues.length > 0 && <small>{p.issues.join(' · ')}</small>}</td></tr>)}
      </tbody></table></div>
      {!state.pools.length && <p className="p-note">负责人先创建池并映射平台规格，再申请初始盘点、由另一成员审批，核对现有未发货订单。</p>}
    </article>
    <article className="p-card"><div className="p-card-head"><h2>创建仓库与主 SKU</h2><Plus size={18}/></div>
      <form onSubmit={e => {e.preventDefault();run(async () => {const p = await api('/stock/pools', {...poolForm, safety: Number(poolForm.safety)});setPoolId(p.id);}, false);}}>
        <div className="p-config-grid">{[['name','池名称'],['sku','主 SKU'],['warehouse','物理仓库'],['safety','安全库存']].map(([key,label]) => <label className="p-field" key={key}><span>{label}</span><input required type={key === 'safety' ? 'number' : 'text'} min={0} value={poolForm[key]} onChange={e => setPoolForm(f => ({...f, [key]: e.target.value}))}/></label>)}
          <label className="p-field"><span>仓库类型</span><select value={poolForm.pool_type} onChange={e => setPoolForm(f => ({...f, pool_type: e.target.value}))}><option value="merchant">商家自发货仓</option><option value="platform">Amazon FBA 等平台仓（独立只读）</option></select></label></div>
        <div className="p-card-foot"><small>初始实物数为 0，盘点需独立审批</small><button className="p-button primary" disabled={!owner || busy}>创建库存池</button></div>
      </form>
    </article>
    <article className="p-card"><div className="p-card-head"><h2>盘点与规格映射</h2></div>
      <label className="p-field"><span>当前库存池</span><select value={poolId} onChange={e => setPoolId(e.target.value)}><option value="">选择库存池</option>{state.pools.map((p: Obj) => <option key={p.id} value={p.id}>{p.name} · {p.sku}</option>)}</select></label>
      {pool && <>
        {pool.pool_type === 'merchant' && <div className="p-config-grid"><label className="p-field"><span>核对后的实物在库数量（收货或退货后也填写完整盘点数）</span><input type="number" min={0} value={count} onChange={e => setCount(e.target.value)}/></label><button className="p-button" disabled={busy || !operate} onClick={proposeCount}>申请盘点 / 入库</button></div>}
        <div className="p-config-grid"><label className="p-field"><span>平台规格</span><select value={variantId} onChange={e => {setVariantId(e.target.value);setLocationId('');}}><option value="">选择已同步规格</option>{variants.map((v: Obj) => <option key={v.id} value={v.id}>{storeName(data.connections.find((c: Obj) => c.id === v.connection_id))} · {v.normalized.sku || v.normalized.title}</option>)}</select></label>
          <label className="p-field"><span>该规格的库存地点</span><select value={locationId} onChange={e => setLocationId(e.target.value)}><option value="">选择地点</option>{variant?.normalized.locations.map((l: Obj) => <option key={l.id} value={l.id}>{l.name} · {l.available ?? '未知'}</option>)}</select></label></div>
        <button className="p-button" disabled={!owner || busy || !variant || !locationId} onClick={() => run(() => api(`/stock/pools/${poolId}/mappings`, {connection_id: variant.connection_id, variant_id: variant.external_id, location_id: locationId}), false)}>建立明确映射</button>
      </>}
    </article>
    {pool && <article className="p-card"><div className="p-card-head"><h2>各店可售配额</h2><ShieldCheck size={19}/></div>
      <p>可分配 {pool.available} 件。完整方案包含所有映射，减少配额的店铺先执行并回读，随后增加配额的店铺才进入审批。</p>
      <button className="p-text" disabled={busy || !operate || !mappings.length} onClick={() => run(() => api('/commands', {command:'同步所选店铺',connection_ids:[...new Set(mappings.map((m:Obj)=>m.connection_id))],idempotency_key:crypto.randomUUID(),use_ai:false}), false)}>先同步全部关联店铺（需最近 5 分钟内完整数据）</button>
      <div className="p-config-grid">{mappings.map((m: Obj) => <label className="p-field" key={m.id}><span>{storeName(data.connections.find((c: Obj) => c.id === m.connection_id))}</span><input type="number" min={0} value={targets[m.id] ?? String(m.quota)} onChange={e => setTargets(f => ({...f, [m.id]: e.target.value}))}/></label>)}</div>
      <div className="p-card-foot"><strong>合计 {Object.values(targets).reduce<number>((sum, v) => sum+Number(v), 0)} 件</strong><button className="p-button primary" disabled={busy || !operate || !mappings.length || pool.pool_type !== 'merchant'} onClick={() => run(() => api(`/stock/pools/${poolId}/allocate`, {targets: Object.fromEntries(mappings.map((m: Obj) => [m.id, Number(targets[m.id] ?? m.quota)]))}), false)}>生成逐店审批方案</button></div>
      {pool.frozen && <button className="p-text" disabled={!owner || busy} onClick={() => {const note = prompt('请填写完成平台回查、盘点与差异处理的依据');if (note)run(() => api(`/stock/pools/${poolId}/clear-freeze`, {note}), false);}}>核实差异后申请解除冻结</button>}
    </article>}
    <article className="p-card"><div className="p-card-head"><h2>分配方案与台账</h2></div>{state.plans.map((p: Obj) => <div className="p-stock-plan" key={p.id}><div className="p-list-line"><span><strong>{p.id}</strong><small>{p.status} · {p.steps.length} 项 · 池版本 {p.version}</small></span>{p.status !== 'completed' && p.status !== 'closed' && <button className="p-text" disabled={!owner || busy} onClick={() => {const note = prompt('结案前确认执行中或未知结果已核实，请说明处理依据');if(note)run(() => api(`/stock/plans/${p.id}/close`, {note}), false);}}>核实后关闭旧方案</button>}</div><ol>{p.steps.map((step:Obj)=><li key={step.task_id}>{step.phase==='decrease'?'先减库存':'减量回读后增库存'} · {storeName(data.connections.find((c:Obj)=>c.id===step.connection_id))} · {step.before} → {step.quantity}<small>{data.tasks.find((t:Obj)=>t.id===step.task_id)?.status||'查看对应任务记录'}</small></li>)}</ol></div>)}
      <details><summary>查看库存台账</summary><div className="p-table-wrap"><table><thead><tr><th>记录</th><th>实物变化</th><th>在库</th><th>预留</th></tr></thead><tbody>{state.journal.map((j: Obj) => <tr key={j.id}><td>{j.kind}<small>{j.reference}</small></td><td>{j.delta}</td><td>{j.on_hand_after}</td><td>{j.reserved_after}</td></tr>)}</tbody></table></div></details>
    </article>
  </>;
}
