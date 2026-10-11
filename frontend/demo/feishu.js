async function api(path, body) {
  const bootstrap = await (await fetch('/api/bootstrap')).json();
  const reply = await fetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':bootstrap.csrf},body:JSON.stringify(body)});
  const value = await reply.json();
  if (!reply.ok) throw new Error(value.detail || '请先登录演示工作台');
  return value;
}
function element(tag, text, parent) { const value=document.createElement(tag);value.textContent=text;parent.append(value);return value; }
async function refresh() {
  try {
    const data=await api('/api/demo/outbox');
    document.getElementById('user').textContent='当前合成成员：'+data.user;
    const messages=document.getElementById('messages'); messages.replaceChildren();
    for (const message of data.messages.slice().reverse()) {
      const card=element('article','',messages);
      if (message.type==='interactive') {
        element('h2',message.content.header.title.content,card);
        for (const part of message.content.elements) {
          if (part.text) element('pre',part.text.content,card);
          if (part.content) element('pre',part.content.replace(/\*\*/g,''),card);
          for (const action of part.actions || []) {
            const button=element('button',action.text.content,card);button.style.marginRight='12px';
            button.addEventListener('click',async()=>{
              try { const value=await api('/api/demo/events',action.value);document.getElementById('status').textContent=value.toast?.content || '事件已进入持久队列'; }
              catch(error) { document.getElementById('status').textContent=error.message; }
            });
          }
        }
      } else element('pre',message.content.text,card);
    }
  } catch(error) { document.getElementById('status').textContent=error.message; }
}
document.getElementById('refresh').addEventListener('click',refresh);
document.getElementById('send').addEventListener('click',async()=>{
  try { await api('/api/demo/events',{text:document.getElementById('command').value});document.getElementById('status').textContent='模拟消息已保存，后台将创建任务并回传结果';document.getElementById('command').value=''; }
  catch(error) { document.getElementById('status').textContent=error.message; }
});
refresh();
