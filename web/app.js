'use strict';
const $ = selector => document.querySelector(selector);
const state = {data:null, csrf:null, page:'board', module:null, selected:null, mount:null, base:null, pending:null, busy:false, timer:null};
function el(tag, attrs={}, ...children) {
  const node=document.createElement(tag);
  for (const [key,value] of Object.entries(attrs)) {
    if(key==='class')node.className=value;
    else if(key.startsWith('on'))node.addEventListener(key.slice(2),value);
    else if(value!==null && value!==undefined)node.setAttribute(key,String(value));
  }
  for(const child of children.flat(Infinity))if(child!==null && child!==undefined)node.append(child instanceof Node?child:document.createTextNode(String(child)));
  return node;
}
function put(selector,...children){$(selector).replaceChildren(...children.flat());}
function bytes(value){if(value===null||value===undefined)return '확인 불가';return value>=2**30?(value/2**30).toFixed(2)+' GiB':(value/2**20).toFixed(0)+' MiB';}
function time(value){return value?new Date(value*1000).toLocaleTimeString('ko-KR',{hour12:false}):'수집 대기';}
function badge(text,kind=''){return el('span',{class:'badge '+kind},text);}
function notice(message,bad=false){$('#notice').hidden=!message;$('#notice').className='notice'+(bad?' bad':'');$('#notice').textContent=message||'';}
function ports(container){return container.ports.length?[...new Set(container.ports.map(p=>`${p.host.includes(':')?'['+p.host+']':p.host}:${p.port} → ${p.container}`))].join(' / '):'호스트 공개 포트 없음';}
function boardPorts(containers){
  const groups=new Map();
  for(const port of containers.flatMap(container=>container.ports)){
    const host=['0.0.0.0','::'].includes(port.host)?'전체':port.host||'기본 바인딩';
    if(!groups.has(host))groups.set(host,new Map());
    groups.get(host).set(port.port+'/'+port.container,{port:Number(port.port),text:port.port+' → '+port.container.replace(/\/tcp$/,'')});
  }
  return [...groups].map(([host,pairs])=>el('div',{},host+' · '+[...pairs.values()].sort((a,b)=>a.port-b.port).map(pair=>pair.text).join(' · ')));
}
function label(container){return container.health==='unhealthy'?'헬스 이상':container.state!=='running'?'중지':container.health==='healthy'?'헬스 정상':'실행 중';}
function stale(){return !state.data?.collected_at || Boolean(state.data.error) || Date.now()/1000-state.data.collected_at>Math.max(45,(state.data.interval||10)*3);}
async function api(path,body,timeout=8000){
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),timeout);
  try{
    const response=await fetch(path,{method:body===undefined?'GET':'POST',credentials:'same-origin',cache:'no-store',signal:controller.signal,
      headers:body===undefined?{}:{'Content-Type':'application/json','X-CSRF-Token':state.csrf||''},body:body===undefined?undefined:JSON.stringify(body)});
    const result=await response.json();
    if(!response.ok){const error=new Error(result.error||'요청 실패');error.status=response.status;throw error;}
    return result;
  }finally{clearTimeout(timer);}
}
function signedOut(){clearTimeout(state.timer);state.csrf=null;state.data=null;$('#login').hidden=false;$('#console').hidden=true;$('#action-dialog').close();}
function signedIn(session){state.csrf=session.csrf;$('#login').hidden=true;$('#console').hidden=false;$('#login-form').elements.password.value='';poll();}
async function poll(){
  clearTimeout(state.timer);
  try{state.data=await api('/api/state');render();}
  catch(error){if(error.status===401){signedOut();return;}notice('수집 연결 실패 · 표시된 이전 데이터는 최신 상태가 아닙니다.',true);if(state.data){state.data.error='웹/API 연결 실패';renderContainers();}}
  if(state.csrf)state.timer=setTimeout(poll,10000);
}
function page(name){state.page=name;document.querySelectorAll('[data-view]').forEach(n=>n.hidden=n.dataset.view!==name);document.querySelectorAll('[data-page]').forEach(n=>{if(n.dataset.page===name)n.setAttribute('aria-current','page');else n.removeAttribute('aria-current');});}
function render(){
  const d=state.data;
  $('#environment').textContent=d.daemon?`${d.daemon.name} · ${d.daemon.architecture} · Docker ${d.daemon.version}`:'Docker 수집 대기';
  const flash=state.flash?.until>Date.now()?state.flash:null;
  notice(stale()?(d.error||'최신 수집이 지연되고 있습니다. 제어가 잠겼습니다.'):(d.missing?.length?`운영 설정에서 찾지 못한 컨테이너: ${d.missing.join(', ')}`:flash?.message||''),stale()||Boolean(d.missing?.length)||Boolean(flash?.bad));
  renderBoard();renderContainers();renderImages();renderMounts();
}
function p95(items){const values=items.filter(s=>s.http_ms!==null&&!s.error).map(s=>s.http_ms).sort((a,b)=>a-b);return values.length?values[Math.ceil(values.length*.95)-1].toFixed(1)+' ms':'—';}
function spark(items,module){
  const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('class','spark');svg.setAttribute('viewBox','0 0 300 42');svg.setAttribute('preserveAspectRatio','none');svg.setAttribute('role','img');svg.setAttribute('aria-label',module+' 최근 '+items.length+'회 HTTP 전체 응답 추세');
  const max=Math.max(1,...items.map(s=>s.http_ms||0));let segment=[];
  function flush(){if(segment.length>1){const line=document.createElementNS(ns,'polyline');line.setAttribute('points',segment.join(' '));svg.append(line);}segment=[];}
  items.forEach((item,i)=>{const x=items.length>1?i*300/(items.length-1):150;if(item.http_ms===null||item.error){flush();const dot=document.createElementNS(ns,'circle');dot.setAttribute('cx',x);dot.setAttribute('cy','38');dot.setAttribute('r','2');svg.append(dot);}else segment.push(x+','+(36-item.http_ms/max*30));});flush();return svg;
}
function renderBoard(){
  const d=state.data,host=d.host_memory||{},daemon=d.daemon||{},members=d.containers||[];
  const postgres=members.find(container=>container.role==='postgresql');
  const infra=(title,value,detail)=>el('section',{class:'infra-card'},el('h3',{},title),el('div',{class:'number'},value),el('small',{},detail));
  put('#infra',infra('호스트 메모리',bytes(host.total),`사용 추정 ${bytes(host.used_estimate)} · Mac 물리 메모리`),
    infra('Docker VM 메모리',bytes(daemon.memory_total),`컨테이너 사용 합계 ${bytes(members.reduce((n,c)=>n+(c.memory_bytes||0),0))} · VM 총량과 구분`),
    infra('공통 인프라',`${members.filter(c=>c.state==='running').length} / ${members.length} 실행`,
      '관리 대상 · PostgreSQL '+(postgres?label(postgres)+' · '+ports(postgres):'조회 불가')));
  const services=[...(d.services||[])].sort((a,b)=>Number(Boolean(b.error))-Number(Boolean(a.error))||(b.http_ms??Infinity)-(a.http_ms??Infinity)||a.module.localeCompare(b.module));
  put('#services',services.length?services.map(s=>{
    const history=d.trends?.[s.module]||[],containers=members.filter(c=>c.module===s.module);
    const status=s.unhealthy?'헬스 이상':s.oom_history?'OOM 이력':s.error?s.error:s.running<s.total?'일부 중지':s.status>=200&&s.status<300?'HTTP 정상':'HTTP '+s.status;
    return el('article',{class:'service-card'},el('div',{class:'card-heading'},el('h2',{},s.module),badge(status,s.error||s.unhealthy?'bad':s.oom_history||s.running<s.total?'warn':s.status<300?'good':'warn')),
      el('div',{class:'ports'},boardPorts(containers)),
      el('div',{class:'http-value'},s.http_ms===null?'—':s.http_ms.toFixed(1),el('span',{},'ms')),
      el('div',{class:'service-timings'},el('span',{},'연결 '+(s.tcp_ms===null?'—':s.tcp_ms.toFixed(1)+' ms')),el('span',{},'최근 p95 '+p95(history))),spark(history,s.module),
      el('div',{class:'resource'},el('span',{},'CPU ',el('strong',{},s.cpu_percent===null?'—':s.cpu_percent+'%')),el('span',{},'MEM ',el('strong',{},bytes(s.memory_bytes))),el('span',{},'실행 ',el('strong',{},s.running+' / '+s.total))),
      s.restarts?el('p',{class:'footnote'},'컨테이너 자동 재시작 누적 '+s.restarts+'회'+(s.oom_history?' · 메모리 부족 종료 이력':'')):null);
  }):el('p',{class:'empty'},'첫 측정을 수집하고 있습니다.'));
  $('#collection-status').textContent=`최종 수집 ${time(d.collected_at)} · ${d.interval||10}초 간격 · 최근 60회 / 24시간 보관 · 포트: 호스트 → 내부(TCP 기본) · 빨간 점: 측정 실패 · HTTPS 연결은 TLS 포함`;
}
function options(select,values,current){const keys=values.map(v=>v[0]);if(!keys.includes(current))current=keys[0]||null;const signature=JSON.stringify(values);if(select.dataset.options!==signature){select.replaceChildren(...values.map(([key,text])=>el('option',{value:key},text)));select.dataset.options=signature;}if(current!==null)select.value=current;return current;}
function renderContainers(){
  const items=state.data?.containers||[],modules=[...new Set(items.map(c=>c.module))].sort((a,b)=>a.localeCompare(b));
  if(!state.module)state.module=modules.find(module=>items.some(c=>c.module===module&&c.control))||modules[0];
  state.module=options($('#module-select'),modules.map(m=>[m,m+' ('+items.filter(c=>c.module===m).length+')']),state.module);
  const selected=items.filter(c=>c.module===state.module);
  if(!selected.some(c=>c.id===state.selected))state.selected=selected[0]?.id||null;
  $('#container-count').textContent=items.filter(c=>c.state==='running').length+'개 실행';
  $('#module-node').textContent=state.module||'모듈 없음';
  put('#role-nodes',selected.map(c=>el('button',{class:'role-node','aria-pressed':c.id===state.selected,onclick:()=>{state.selected=c.id;renderContainers();}},
    el('strong',{},c.role),el('small',{},label(c)),el('small',{},c.ports.length?[...new Set(c.ports.map(p=>':'+p.port))].join(' / '):'호스트 공개 없음'))));
  const container=items.find(c=>c.id===state.selected);
  if(!container){put('#container-detail',el('p',{class:'muted'},'조회된 컨테이너가 없습니다.'));}
  else{
    const details=el('div',{},el('small',{},'선택 컨테이너'),el('h2',{},container.display_name),
      el('p',{class:'mono'},'실제 이름 '+container.name+' · ID '+container.id.slice(0,12)),el('p',{},badge(label(container),container.health==='unhealthy'?'bad':container.state==='running'?'good':'')),
      el('p',{class:'ports'},ports(container)),el('p',{class:'muted'},container.module+' → '+container.role),
      el('p',{class:'muted'},'자동 재시작 '+container.restart_count+'회'+(container.oom_killed?' · OOM 종료 이력':'')));
    const actions=el('div',{class:'control-actions'});
    for(const [action,title] of [['start','시작'],['stop','중지'],['restart','재시작']]){
      const button=el('button',{onclick:()=>openAction(container,action,title)},title);
      button.disabled=stale()||state.busy||!container.control||(action==='start'?!['exited','created'].includes(container.state):container.state!=='running');actions.append(button);
    }
    if(!container.control)details.append(el('small',{},'조회 전용 · 자기 자신과 운영 제외 대상은 제어하지 않습니다.'));
    put('#container-detail',details,actions);
  }
  const statusNames={succeeded:'검증 완료',unknown:'상태 확인 필요',failed:'실패',rejected:'거절',pending:'진행 중'};
  put('#audit',state.data.audit?.length?state.data.audit.map(a=>el('div',{class:'audit-row'},el('div',{},
    el('strong',{},(items.find(c=>c.id===a.target)?.display_name||a.target.slice(0,12))+' · '+({start:'시작',stop:'중지',restart:'재시작'}[a.action]||a.action)),
    el('small',{},time(a.ts)+' · '+a.actor+' · '+a.message)),badge(statusNames[a.status]||a.status,a.status==='succeeded'?'good':a.status==='pending'?'':'warn'))):el('p',{class:'empty'},'아직 요청한 제어 작업이 없습니다.'));
}
function matchingBase(image,bases){return bases.filter(b=>b.title!=='Ubuntu 26.04'&&b.layers.length&&b.layers.every((layer,i)=>image.layers[i]===layer)).sort((a,b)=>b.layers.length-a.layers.length)[0];}
function renderImages(){
  const data=state.data,bases=data.bases||[],images=data.images||[],groups=new Map();
  for(const image of images){const base=matchingBase(image,bases),key=base?.title||'직접 파생';if(!groups.has(key))groups.set(key,[]);groups.get(key).push({image,base});}
  const baseOrder=bases.filter(base=>base.title!=='Ubuntu 26.04').map(base=>base.title),keys=[...groups.keys()].sort((a,b)=>(baseOrder.includes(a)?baseOrder.indexOf(a):99)-(baseOrder.includes(b)?baseOrder.indexOf(b):99));
  if(!groups.has(state.base))state.base=keys[0];
  put('#base-tabs',keys.map((key,i)=>el('button',{role:'tab',id:'base-'+i,'aria-controls':'layer-diagram','aria-selected':key===state.base,onclick:()=>{state.base=key;renderImages();}},key+' ('+groups.get(key).length+')')));
  const active=keys.indexOf(state.base);$('#layer-diagram').setAttribute('aria-labelledby','base-'+active);
  const entries=groups.get(state.base)||[],os=bases.find(b=>b.title==='Ubuntu 26.04');
  const apps=el('div',{class:'layer-apps'},entries.map(({image,base})=>{
    const containers=data.containers.filter(c=>c.image_id===image.id),own=image.layers.slice(base?.layers.length||0);
    return el('article',{class:'layer-app'},el('h3',{},containers.map(c=>c.display_name).join(' / ')),
      el('small',{},'이미지 '+image.id.replace('sha256:','').slice(0,12)+' · '+bytes(image.size)),
      el('small',{class:'mono'},image.tags.join(' · ')||'태그 없음'),
      own.length?own.map((layer,i)=>({layer,index:(base?.layers.length||0)+i+1})).reverse().map(({layer,index})=>el('div',{class:'layer'},'L'+index+' · '+layer.replace('sha256:','').slice(0,16))):el('small',{},'추가 파일시스템 계층 없음'),
      el('small',{},'전체 '+image.layers.length+'개 / 기반 공유 '+(base?.layers.length||0)+'개'),
      image.digests.length?el('small',{class:'mono'},'참조 digest '+image.digests.join(' · ')):null);
  }));
  const base=entries[0]?.base;
  put('#layer-diagram',entries.length?apps:el('p',{class:'muted'},'계층을 조회하지 못했습니다.'),
    base?el('div',{class:'layer-base'},el('strong',{},base.title+' · 공통 실행환경'),el('p',{},'OS 포함 누적 공유 '+base.layers.length+'개 · 이미지 '+base.id.replace('sha256:','').slice(0,12))):null,
    os?el('div',{class:'layer-base layer-os'},el('strong',{},'Ubuntu 26.04 · 운영체제 기반'),el('p',{},os.layers.length+'개 파일시스템 계층 · '+os.architecture)):null,
    ...(data.image_errors||[]).map(message=>el('p',{class:'error-text'},message)));
}
function renderMounts(){
  const items=state.data?.containers||[];state.mount=options($('#mount-select'),items.map(c=>[c.id,c.display_name]),state.mount);
  const c=items.find(c=>c.id===state.mount);
  put('#mount-list',c?.mounts.length?c.mounts.map(m=>{
    const shared=items.filter(other=>other.mounts.some(n=>m.Type==='volume'?n.Name===m.Name:n.Source===m.Source));
    const path=(title,value)=>el('div',{},el('small',{},title),el('p',{class:'mono'},value||'—'));
    return el('article',{class:'mount-row'},el('div',{class:'card-heading'},el('h3',{},m.Type==='volume'?'Docker named volume':'호스트 '+m.Type+' mount'),badge(m.RW?'RW · 읽기/쓰기':'RO · 읽기 전용')),
      el('div',{class:'mount-paths'},path(m.Type==='volume'?'Docker 볼륨 이름':'호스트 원본 경로',m.Type==='volume'?m.Name:m.Source),el('span',{class:'mount-arrow','aria-hidden':true},'→'),path('컨테이너 경로',m.Destination)),
      m.Type==='volume'?el('p',{class:'mono muted'},'VM 원본: '+m.Source):null,
      shared.length>1?el('p',{class:'footnote'},'공유: '+shared.map(s=>s.display_name).join(' · ')):null);
  }):el('p',{class:'empty'},c?'명시적으로 연결된 마운트가 없습니다.':'컨테이너 수집을 기다리고 있습니다.'));
  $('#backup-policy').textContent=c?.role==='postgresql'?'PostgreSQL은 DB별 논리 백업과 역할·권한을 함께 보존합니다. 운영 복원 시 연결된 모든 writer를 통제합니다.':
    c?.module==='zpl-market'?'H2 파일 DB와 기존 서명 키를 같은 백업 세트로 보존합니다. 정상 정지 후 파일 백업하고 격리 복원에서 계정·라이선스·서명을 확인합니다.':
    c?.module==='mailbox'?'메일 저장소·계정·SMTP 큐를 공유하는 모든 writer를 기준으로 일관성을 확보합니다. 키와 인증 자료는 별도 접근 제한 백업으로 보존합니다.':
    'DB와 일반 파일을 구분하고 관련 writer 및 호스트 작업까지 확인합니다. 실행 중인 DB 파일의 단순 복사를 정상 백업으로 취급하지 않습니다.';
}
function openAction(container,action,title){
  if(stale()||state.busy||!container.control)return;
  state.pending={id:container.id,action,expected_state:container.state,request_id:crypto.randomUUID(),name:container.display_name};
  $('#action-title').textContent=title+' 확인';$('#action-target').textContent=container.display_name+' · '+container.id.slice(0,12);
  $('#action-impact').textContent=container.critical?'공통 PostgreSQL 제어는 DamGoX2와 OnlyPlus에 영향을 줍니다. 관련 writer와 현재 작업을 확인하세요.':'실제 운영 컨테이너에 적용합니다. 계속하려면 '+container.display_name+'을 입력하세요.';
  $('#confirmation').value='';$('#confirmation').placeholder=container.display_name;$('#action-error').textContent='';$('#action-dialog').showModal();
}
$('#login-form').addEventListener('submit',async event=>{event.preventDefault();const form=event.currentTarget;form.querySelector('button').disabled=true;$('#login-error').textContent='';try{signedIn(await api('/api/login',{username:form.elements.username.value,password:form.elements.password.value}));}catch(error){$('#login-error').textContent=error.message;}finally{form.querySelector('button').disabled=false;}});
$('#logout').addEventListener('click',async()=>{try{await api('/api/logout',{});}catch{}signedOut();});
document.querySelectorAll('[data-page]').forEach(button=>button.addEventListener('click',()=>page(button.dataset.page)));
$('#module-select').addEventListener('change',event=>{state.module=event.target.value;state.selected=null;renderContainers();});
$('#mount-select').addEventListener('change',event=>{state.mount=event.target.value;renderMounts();});
$('#wall-mode').addEventListener('click',()=>{const active=document.body.classList.toggle('wall');$('#wall-mode').textContent=active?'메뉴 표시':'관제 전용 모드';});
$('#action-cancel').addEventListener('click',()=>{if(!state.busy)$('#action-dialog').close();});
$('#action-dialog').addEventListener('cancel',event=>{if(state.busy)event.preventDefault();});
$('#action-form').addEventListener('submit',async event=>{
  event.preventDefault();if(state.busy||!state.pending)return;
  if($('#confirmation').value!==state.pending.name){$('#action-error').textContent='확인 이름이 일치하지 않습니다.';return;}
  state.busy=true;$('#action-submit').disabled=true;$('#action-cancel').disabled=true;renderContainers();
  try{const {name,...request}=state.pending;const result=await api('/api/action',{...request,confirmation:$('#confirmation').value},70000);$('#action-dialog').close();state.flash={message:result.status==='succeeded'?'운영 작업과 실제 상태 검증을 완료했습니다.':'작업 이력과 실제 상태를 확인하세요.',bad:result.status!=='succeeded',until:Date.now()+15000};}
  catch(error){$('#action-error').textContent=error.name==='AbortError'?'응답 시간이 초과되었습니다. 재요청 전에 현재 상태와 작업 이력을 확인하세요.':error.message;}
  finally{state.busy=false;$('#action-submit').disabled=false;$('#action-cancel').disabled=false;await poll();}
});
api('/api/session').then(signedIn).catch(signedOut);
