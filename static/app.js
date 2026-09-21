
let authState={authenticated:false,passkey:false,webauthn:false};
const b64uToBytes=s=>{s=s.replace(/-/g,'+').replace(/_/g,'/');while(s.length%4)s+='=';const bin=atob(s);return Uint8Array.from(bin,c=>c.charCodeAt(0))};
const bytesToB64u=b=>{let s='';const a=new Uint8Array(b);for(const x of a)s+=String.fromCharCode(x);return btoa(s).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'')};
function publicKeyFromJSON(o){const x=JSON.parse(JSON.stringify(o));if(x.challenge)x.challenge=b64uToBytes(x.challenge);if(x.user?.id)x.user.id=b64uToBytes(x.user.id);if(x.excludeCredentials)x.excludeCredentials=x.excludeCredentials.map(c=>({...c,id:b64uToBytes(c.id)}));if(x.allowCredentials)x.allowCredentials=x.allowCredentials.map(c=>({...c,id:b64uToBytes(c.id)}));return x}
function credentialToJSON(c){const r={id:c.id,rawId:bytesToB64u(c.rawId),type:c.type,response:{}};if(c.response.clientDataJSON)r.response.clientDataJSON=bytesToB64u(c.response.clientDataJSON);if(c.response.attestationObject)r.response.attestationObject=bytesToB64u(c.response.attestationObject);if(c.response.authenticatorData)r.response.authenticatorData=bytesToB64u(c.response.authenticatorData);if(c.response.signature)r.response.signature=bytesToB64u(c.response.signature);if(c.response.userHandle)r.response.userHandle=bytesToB64u(c.response.userHandle);if(c.response.getTransports)r.response.transports=c.response.getTransports();return r}
async function authFetch(url,opt={}){const r=await fetch(url,{...opt,headers:{...(opt.headers||{}),'Cache-Control':'no-store'}});let d={};try{d=await r.json()}catch{}if(!r.ok)throw Error(d.error||'인증에 실패했습니다.');return d}
function showLogin(){$('authScreen')?.classList.remove('hidden');document.body.classList.add('authLocked')}
function hideLogin(){$('authScreen')?.classList.add('hidden');document.body.classList.remove('authLocked')}
async function passwordLogin(){const i=$('authPassword'),b=$('passwordLoginBtn'),m=$('authMessage');b.disabled=true;m.textContent='';try{await authFetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:i.value})});i.value='';hideLogin();await startApp()}catch(e){m.textContent=e.message}finally{b.disabled=false}}
async function passkeyLogin(){const b=$('passkeyLoginBtn'),m=$('authMessage');b.disabled=true;m.textContent='';try{if(!window.PublicKeyCredential)throw Error('이 브라우저에서는 Face ID 로그인을 사용할 수 없습니다.');const o=await authFetch('/api/auth/passkey/authenticate/options',{method:'POST'});const c=await navigator.credentials.get({publicKey:publicKeyFromJSON(o)});if(!c)throw Error('Face ID 인증이 취소되었습니다.');await authFetch('/api/auth/passkey/authenticate/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(credentialToJSON(c))});hideLogin();await startApp()}catch(e){m.textContent=e.message}finally{b.disabled=false}}
async function registerPasskey(){try{const o=await authFetch('/api/auth/passkey/register/options',{method:'POST'});const c=await navigator.credentials.create({publicKey:publicKeyFromJSON(o)});if(!c)throw Error('Face ID 등록이 취소되었습니다.');await authFetch('/api/auth/passkey/register/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(credentialToJSON(c))});alert('Face ID 등록이 완료되었습니다.');authState.passkey=true;renderSecurity()}catch(e){alert(e.message)}}
async function logout(){try{await authFetch('/api/auth/logout',{method:'POST'})}catch{}location.reload()}
function renderSecurity(){const s=$('securityStatus'),b=$('registerPasskeyBtn');if(!s||!b)return;s.textContent=authState.passkey?'Face ID 등록됨':'Face ID 미등록';b.textContent=authState.passkey?'Face ID 다시 등록':'Face ID 등록'}
async function loadAuthStatus(){try{authState=await authFetch('/api/auth/status');renderSecurity()}catch{}}

const $=id=>document.getElementById(id);
function requireEl(id){const el=$(id);if(!el)throw new Error(`설정 화면이 최신 버전으로 로드되지 않았습니다. 페이지를 새로고침해 주세요. (누락: ${id})`);return el}let boot=null,editingId=null,lastTransactions=[],importDuplicateFlags=[],listLimit=100;
const money=n=>Number(n).toLocaleString('ko-KR')+'원';
function msg(t,ok=false){$('message').textContent=t;$('message').className='message '+(ok?'ok':'err')} function emsg(t,ok=false){$('editMessage').textContent=t;$('editMessage').className='message '+(ok?'ok':'err')}
function ratio(){let u=+$('ratio').value;$('ratioText').textContent=`${u} : ${100-u}`} function eratio(){let u=+$('editRatio').value;$('editRatioText').textContent=`${u} : ${100-u}`}
function inst(id){$(id).innerHTML=Array.from({length:60},(_,i)=>`<option value="${i+1}">${i?' '+(i+1)+'개월':'일시불 (1개월)'}</option>`)}
function esc(s){return String(s).replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]))}
function selects(){let cs=boot.cards.map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join(''),gs=boot.categories.map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('');$('card').innerHTML=cs;$('category').innerHTML='<option value="">자동 분류</option>'+gs;$('filterCard').innerHTML='<option value="">전체 카드</option>'+cs;$('filterCategory').innerHTML='<option value="">전체 카테고리</option>'+gs;$('editCard').innerHTML=cs;$('editCategory').innerHTML='<option value="">자동 분류</option>'+gs;$('userLabel').textContent=boot.settings.user_name||'나';$('wifeLabel').textContent=boot.settings.wife_name||'배우자';$('editUserLabel').textContent=boot.settings.user_name||'나';$('editWifeLabel').textContent=boot.settings.wife_name||'배우자';$('dbStatus').textContent='DB 연결됨'}
function fmt(id){let e=$(id),r=e.value.replace(/[^0-9-]/g,'');if(r&&r!=='-'){let n=r.startsWith('-'),d=r.replace(/-/g,'');e.value=(n?'-':'')+Number(d).toLocaleString('ko-KR')}}
function query(){let p=new URLSearchParams({limit:listLimit}),s=$('search').value.trim();if(s)p.set('search',s);if($('dateFrom').value)p.set('date_from',$('dateFrom').value);if($('dateTo').value)p.set('date_to',$('dateTo').value);if($('filterCard').value)p.set('card_id',$('filterCard').value);if($('filterCategory').value)p.set('category_id',$('filterCategory').value);return p}
async function list(){let r=await fetch('/api/transactions?'+query());let d=await r.json();if(!r.ok)throw Error(d.error);lastTransactions=d.transactions;render(d);$('loadMoreBtn').classList.toggle('hidden',d.transactions.length>=d.total);$('loadMoreBtn').textContent=d.total>listLimit?'더보기 ('+(d.total-d.transactions.length)+'건 남음)':'더보기';}
let selectedTx=new Set();
function updateBulkUI(){const n=selectedTx.size;$('bulkSelectedCount').textContent=`${n}건 선택`;$('bulkToolbar').classList.remove('hidden');$('bulkActions').classList.toggle('hidden',!n);$('selectAllTransactions').checked=!!lastTransactions.length&&lastTransactions.every(t=>selectedTx.has(t.id));}
function render(d){const visible=new Set(d.transactions.map(t=>t.id));selectedTx.forEach(id=>{if(!visible.has(id))selectedTx.delete(id)});$('count').textContent=`총 ${d.total}건`;if(!d.transactions.length){$('list').innerHTML='<div class="empty">조건에 맞는 지출 내역이 없습니다.</div>';updateBulkUI();return} $('list').innerHTML=d.transactions.map(t=>`<div class="transactionItem ${selectedTx.has(t.id)?'selected':''}"><label class="txCheckWrap"><input class="txCheck" type="checkbox" data-id="${t.id}" ${selectedTx.has(t.id)?'checked':''}></label><button class="item itemBtn" data-id="${t.id}" type="button"><div class="row"><div class="itemMain"><div class="date">${t.tx_date}${t.tx_time?` · ${esc(t.tx_time)}`:''}</div><div class="content">${esc(t.content||'(내용 없음)')}</div><div class="meta">${esc(t.card_name)} · ${esc(t.category_name)} · ${t.user_percent}:${t.wife_percent}${t.installment_months>1?' · '+t.installment_months+'개월':' · 일시불'}</div></div><div class="amount ${t.amount<0?'negative':''}">${money(t.amount)}</div></div></button></div>`).join('');document.querySelectorAll('.itemBtn').forEach(x=>x.onclick=()=>openEdit(+x.dataset.id));document.querySelectorAll('.txCheck').forEach(x=>x.onchange=()=>{const id=Number(x.dataset.id);x.checked?selectedTx.add(id):selectedTx.delete(id);render({transactions:lastTransactions,total:lastTransactions.length})});updateBulkUI()}
async function bulkCategory(){if(!selectedTx.size)return;$('bulkCategorySelect').innerHTML=boot.categories.map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('');$('bulkCategoryModal').classList.remove('hidden')}
async function bulkCategorySave(){try{const r=await fetch('/api/transactions/bulk_update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids:[...selectedTx],category_id:$('bulkCategorySelect').value})}),d=await r.json();if(!r.ok||!d.ok)throw Error(d.error);selectedTx.clear();$('bulkCategoryModal').classList.add('hidden');msg(`${d.count}건의 카테고리를 변경했습니다.`,true);await list()}catch(e){msg(e.message)}}
async function bulkDelete(){if(!selectedTx.size)return;if(!confirm(`${selectedTx.size}건의 지출 내역을 삭제할까요?`))return;try{const r=await fetch('/api/transactions/bulk_delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids:[...selectedTx]})}),d=await r.json();if(!r.ok||!d.ok)throw Error(d.error);selectedTx.clear();msg(`${d.count}건을 삭제했습니다.`,true);await list()}catch(e){msg(e.message)}}
async function submit(e){e.preventDefault();let payload={date:$('date').value,time:$('time').value,amount:$('amount').value,card_id:$('card').value,category_id:$('category').value||null,content:$('content').value,user_percent:+$('ratio').value,installment_months:+$('installment').value};try{let r=await fetch('/api/transactions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),d=await r.json();if(!r.ok||!d.ok)throw Error(d.error);msg('저장했습니다.',true);reset(false);await list()}catch(e){msg(e.message)}}
function reset(clear=true){$('date').value=new Date().toISOString().slice(0,10);$('time').value=new Date().toTimeString().slice(0,5);$('amount').value='';$('content').value='';$('category').value='';$('ratio').value=50;$('installment').value=1;ratio();if(clear)msg('');$('amount').focus()}
async function openEdit(id,provided=null){let t=provided||lastTransactions.find(x=>x.id===id);if(!t){try{let r=await fetch('/api/transactions/'+encodeURIComponent(id));let d=await r.json();if(!r.ok)throw Error(d.error);t=d}catch(e){msg(e.message);return}}editingId=id;const editDate=String(t.tx_date||'');const editTime=String(t.tx_time||'');$('editDate').value=/^\d{4}-\d{2}-\d{2}$/.test(editDate)?editDate:'';$('editTime').value=/^\d{2}:\d{2}$/.test(editTime)?editTime:'';$('editAmount').value=Number(t.amount).toLocaleString('ko-KR');$('editCard').value=t.card_id;$('editCategory').value=t.category_id||'';$('editContent').value=t.content||'';$('editRatio').value=t.user_percent;$('editInstallment').value=t.installment_months;eratio();emsg('');$('editModal').classList.remove('hidden')}
function closeEdit(){editingId=null;$('editModal').classList.add('hidden')}
async function saveEdit(e){e.preventDefault();let p={date:$('editDate').value,time:$('editTime').value,amount:$('editAmount').value,card_id:$('editCard').value,category_id:$('editCategory').value||null,content:$('editContent').value,user_percent:+$('editRatio').value,installment_months:+$('editInstallment').value};try{let r=await fetch('/api/transactions/'+editingId,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)}),d=await r.json();if(!r.ok||!d.ok)throw Error(d.error);closeEdit();msg('수정했습니다.',true);await list()}catch(e){emsg(e.message)}}
async function del(){if(!editingId)return;let t=lastTransactions.find(x=>x.id===editingId);if(!confirm(`${t?.tx_date||''} / ${t?.content||'(내용 없음)'} / ${money(t?.amount||0)}\n\n정말 삭제할까요?`))return;try{let r=await fetch('/api/transactions/'+editingId,{method:'DELETE'}),d=await r.json();if(!r.ok||!d.ok)throw Error(d.error);closeEdit();msg('삭제했습니다.',true);await list()}catch(e){msg(e.message)}}
let timer;function filter(){clearTimeout(timer);timer=setTimeout(()=>list().catch(e=>msg(e.message)),250)}
$('expenseForm').onsubmit=submit;$('ratio').oninput=ratio;$('amount').oninput=()=>fmt('amount');$('resetBtn').onclick=()=>reset();$('editForm').onsubmit=saveEdit;$('editRatio').oninput=eratio;$('editAmount').oninput=()=>fmt('editAmount');$('closeModal').onclick=closeEdit;$('deleteBtn').onclick=del;$('editModal').onclick=e=>{if(e.target.id==='editModal')closeEdit()};$('search').oninput=filter;$('filterReset').onclick=()=>{['search','dateFrom','dateTo'].forEach(id=>$(id).value='');$('filterCard').value='';$('filterCategory').value='';list().catch(e=>msg(e.message))};['dateFrom','dateTo','filterCard','filterCategory'].forEach(id=>$(id).onchange=()=>list().catch(e=>msg(e.message)));
$('selectAllTransactions').onchange=()=>{if($('selectAllTransactions').checked)lastTransactions.forEach(t=>selectedTx.add(t.id));else lastTransactions.forEach(t=>selectedTx.delete(t.id));render({transactions:lastTransactions,total:lastTransactions.length})};$('bulkCategoryBtn').onclick=bulkCategory;$('bulkCategorySave').onclick=bulkCategorySave;$('bulkCategoryCancel').onclick=()=>$('bulkCategoryModal').classList.add('hidden');$('bulkCategoryClose').onclick=()=>$('bulkCategoryModal').classList.add('hidden');$('bulkDeleteBtn').onclick=bulkDelete;$('bulkClearBtn').onclick=()=>{selectedTx.clear();render({transactions:lastTransactions,total:lastTransactions.length})};
$('passwordLoginBtn').onclick=passwordLogin;
$('passkeyLoginBtn').onclick=passkeyLogin;
$('registerPasskeyBtn').onclick=registerPasskey;
$('logoutBtn').onclick=logout;
async function bootAuth(){
  try{
    authState=await authFetch('/api/auth/status');
    $('passkeyLoginBtn').hidden=!authState.passkey;
    if(authState.authenticated){hideLogin();await startApp()}
    else showLogin();
  }catch(e){showLogin();$('authMessage').textContent=e.message}
}
bootAuth();

let settlementCenter = new Date();
settlementCenter.setDate(1);

function ym(d){ return d.toISOString().slice(0,7); }
function money2(n){ return Number(n).toLocaleString('ko-KR'); }
async function loadSettlement(){
  const r=await fetch('/api/settlement?month='+ym(settlementCenter));
  const data=await r.json();
  if(!r.ok) throw new Error(data.error||'결산을 불러오지 못했습니다.');
  $('settlementTitle').textContent='3개월 결산';
  $('settlementMonths').innerHTML=settlementGridHtml(data);
}
function openSettlementDetails(kind,id,month){
  const modal=$('settlementDetailModal');
  $('settlementDetailTitle').textContent=kind==='card'?'카드별 내역':kind==='category'?'카테고리별 내역':kind==='user'?boot.settings.user_name+' 지출 내역':kind==='wife'?boot.settings.wife_name+' 지출 내역':'총 지출 내역';
  $('settlementDetailSub').textContent=month.slice(0,4)+'년 '+Number(month.slice(5))+'월 결산 반영 내역';
  $('settlementDetailList').innerHTML='<div class="empty">불러오는 중…</div>';
  modal.classList.remove('hidden');
  fetch('/api/settlement/details?month='+encodeURIComponent(month)+'&kind='+encodeURIComponent(kind)+(id!==''?'&id='+encodeURIComponent(id):''))
    .then(r=>r.json().then(d=>({r,d}))).then(({r,d})=>{if(!r.ok)throw Error(d.error);$('settlementDetailTotal').textContent=money(d.total);$('settlementDetailList').innerHTML=d.transactions.length?d.transactions.map(t=>`<button type="button" class="settleDetailItem" data-tx-id="${t.id}"><div class="settleDetailTop"><span>${esc(t.date)}${t.time?' · '+esc(t.time):''}</span><strong>${money(t.allocation)}</strong></div><div class="settleDetailContent">${esc(t.content)}</div><div class="settleDetailMeta">${esc(t.card_name)} · ${esc(t.category_name)} · ${t.user_percent}:${t.wife_percent}${t.installment_months>1?' · '+t.installment_months+'개월 할부':' · 일시불'}${t.installment_months>1?' · 원금 '+money(t.amount):''}</div></button>`).join(''):'<div class="empty">해당 월에 반영된 내역이 없습니다.</div>';document.querySelectorAll('[data-tx-id]').forEach(b=>b.onclick=()=>{const t=d.transactions.find(x=>String(x.id)===String(b.dataset.txId));closeSettlementDetails();setView('expenseView');openEdit(Number(b.dataset.txId),t)})})
    .catch(e=>{$('settlementDetailList').innerHTML='<div class="empty">'+esc(e.message)+'</div>'});
}
function closeSettlementDetails(){$('settlementDetailModal').classList.add('hidden')}
function settlementGridHtml(data){
  const ms=data.months;
  const colHead=ms.map(m=>`<div class="settleCell monthHead">${Number(m.month.slice(5))}월</div>`).join('');
  const row=(label, values, cls='', kind='total', itemId=null)=>`<div class="settleRow"><div class="settleLabel ${cls}">${label}</div>${values.map((v,i)=>`<button type="button" class="settleCell settleLink ${Number(v)<0?'negative':''}" data-settle-kind="${kind}" data-settle-id="${itemId??''}" data-settle-month="${ms[i].month}">${money2(v)}</button>`).join('')}</div>`;
  const cardNames=[...new Map(ms.flatMap(m=>m.cards.map(c=>[c.id,c.name]))).entries()];
  const catNames=[...new Map(ms.flatMap(m=>m.categories.map(c=>[c.id,c.name]))).entries()];
  const cardRows=cardNames.map(([id,name])=>row(esc(name),ms.map(m=>m.cards.find(c=>c.id==id)?.amount||0),'','card',id)).join('');
  const catRows=catNames.map(([id,name])=>row(esc(name),ms.map(m=>m.categories.find(c=>c.id==id)?.amount||0),'','category',id)).join('');
  return `<div class="settleGrid">
    <div class="settleRow settleHeader"><div class="settleLabel">항목</div>${colHead}</div>
    ${row('총 지출',ms.map(m=>m.total),'strongRow')}
    ${row(esc(data.user_name),ms.map(m=>m.user),'','user')}
    ${row(esc(data.wife_name),ms.map(m=>m.wife),'','wife')}
    <div class="sectionRow"><div>💳 카드별</div></div>
    ${cardRows || '<div class="empty">내역 없음</div>'}
    <div class="sectionRow"><div>🏷️ 카테고리별</div></div>
    ${catRows || '<div class="empty">내역 없음</div>'}
  </div>`;
}
async function startApp(){reset(false);inst('installment');inst('editInstallment');$('ratio').oninput=ratio;$('editRatio').oninput=eratio;ratio();eratio();await loadBoot();await list();await loadNotes();await loadAuthStatus()}

function setView(viewId){
  document.querySelectorAll('.navBtn').forEach(b=>b.classList.toggle('active',b.dataset.view===viewId));
  document.querySelectorAll('.expenseView').forEach(s=>s.classList.toggle('hidden',viewId!=='expenseView'));
  $('settlementView').classList.toggle('hidden',viewId!=='settlementView');
  $('notesView').classList.toggle('hidden',viewId!=='notesView');
  $('settingsView').classList.toggle('hidden',viewId!=='settingsView');
  if(viewId==='settlementView') loadSettlement().catch(e=>console.error(e));
  if(viewId==='notesView') loadNotes().catch(e=>console.error(e));
}
document.querySelectorAll('.navBtn').forEach(b=>b.addEventListener('click',()=>setView(b.dataset.view)));
$('prevMonth').addEventListener('click',()=>{settlementCenter.setMonth(settlementCenter.getMonth()-1);loadSettlement().catch(e=>console.error(e));});
$('nextMonth').addEventListener('click',()=>{settlementCenter.setMonth(settlementCenter.getMonth()+1);loadSettlement().catch(e=>console.error(e));});

async function loadNotes(){
  const r=await fetch('/api/notes'); const d=await r.json();
  if(!r.ok) throw Error(d.error||'메모를 불러오지 못했습니다.');
  $('noteCount').textContent=`${d.notes.length}개`;
  $('notesList').innerHTML=d.notes.length?d.notes.map(n=>`<div class="noteItem ${n.done?'noteDone':''}">
    <button class="noteCheck" type="button" data-note-toggle="${n.id}">${n.done?'✓':''}</button>
    <div class="noteText" data-note-edit="${n.id}">${esc(n.content)}<div class="noteMeta">${n.done?'완료':'진행 중'}</div></div>
    <button class="noteEdit" type="button" data-note-delete="${n.id}">×</button>
  </div>`).join(''):'<div class="empty">메모가 없습니다.</div>';
  document.querySelectorAll('[data-note-toggle]').forEach(b=>b.onclick=async()=>{await fetch('/api/notes/toggle/'+b.dataset.noteToggle,{method:'POST'});loadNotes()});
  document.querySelectorAll('[data-note-delete]').forEach(b=>b.onclick=async()=>{if(!confirm('이 메모를 삭제할까요?'))return;await fetch('/api/notes/'+b.dataset.noteDelete,{method:'DELETE'});loadNotes()});
  document.querySelectorAll('[data-note-edit]').forEach(b=>b.ondblclick=()=>openNoteEdit(+b.dataset.noteEdit));
}
async function addNote(e){e.preventDefault();let c=$('noteContent').value.trim();if(!c)return;$('noteContent').disabled=true;try{let r=await fetch('/api/notes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:c})});let d=await r.json();if(!r.ok)throw Error(d.error);$('noteContent').value='';await loadNotes()}finally{$('noteContent').disabled=false;$('noteContent').focus()}}
let editingNoteId=null;
function openNoteEdit(id){let el=document.querySelector(`[data-note-edit="${id}"]`);if(!el)return;editingNoteId=id;$('editNoteContent').value=el.childNodes[0].textContent.trim();$('noteEditMessage').textContent='';$('noteEditMessage').className='message';$('noteEditModal').classList.remove('hidden');setTimeout(()=>$('editNoteContent').focus(),50)}
function closeNoteEdit(){editingNoteId=null;$('noteEditModal').classList.add('hidden')}
async function saveNoteEdit(e){e.preventDefault();if(!editingNoteId)return;let v=$('editNoteContent').value.trim();if(!v){$('noteEditMessage').textContent='내용을 입력하세요.';return}try{let r=await fetch('/api/notes/'+editingNoteId,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:v})});let d=await r.json();if(!r.ok||!d.ok)throw Error(d.error||'수정에 실패했습니다.');closeNoteEdit();await loadNotes()}catch(e){$('noteEditMessage').textContent=e.message;$('noteEditMessage').className='message err'}}
$('noteForm').onsubmit=addNote;
$('noteEditForm').onsubmit=saveNoteEdit;$('closeNoteModal').onclick=closeNoteEdit;$('cancelNoteModal').onclick=closeNoteEdit;$('noteEditModal').onclick=e=>{if(e.target.id==='noteEditModal')closeNoteEdit()};


async function apiSettings(kind, id=null, method='GET', body=null){
  const url='/api/settings/'+kind+(id!==null?'/'+id:'');
  const opt={method};
  if(body!==null){opt.headers={'Content-Type':'application/json'};opt.body=JSON.stringify(body)}
  const r=await fetch(url,opt), d=await r.json();
  if(!r.ok||d.ok===false) throw Error(d.error||'설정을 처리하지 못했습니다.');
  return d;
}
function settingButton(label, cls='', extra=''){return `<button type="button" class="${cls}" ${extra}>${label}</button>`}
async function loadSettings(){
  try{
    const [cards,cats,rules,names]=await Promise.all([
      apiSettings('cards'),apiSettings('categories'),apiSettings('rules'),apiSettings('names')
    ]);
    $('settingsUserName').value=names.user_name||'';$('settingsWifeName').value=names.wife_name||'';
    $('newRuleCategory').innerHTML=boot.categories.map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('');
    const renderOrderList=(items,kind)=>items.map(x=>{
      const sub=x.active?'사용 중':'사용 중지';
      const extra=kind==='cards'?(x.payment_day&&x.period_start_day&&x.period_end_day?' · 💳 '+x.payment_day+'일 · 📅 전월 '+x.period_start_day+'일 ~ 당월 '+x.period_end_day:' · ⚠️ 카드 정산 설정 미완료'):'';
      const displayName=kind==='rules'?(x.keyword||''):(x.name||'');
      const displaySub=kind==='rules'?'→ '+(x.category_name||'미정')+' · '+sub:sub+extra;
      return '<div class="settingsItem '+(x.active?'':'inactive')+'" data-setting-kind="'+kind+'" data-id="'+x.id+'"><div class="dragHandle" role="button" aria-label="순서 변경" title="끌어서 순서 변경">☷</div><div class="settingsMain"><div class="settingsName">'+esc(displayName)+'</div><div class="settingsSub">'+esc(displaySub)+'</div></div>'+settingButton('수정','settingsActionBtn',`onclick="settingsEditClick('${kind}',${x.id})"`)+settingButton(x.active?'중지':'사용','danger settingsActionBtn','data-settings-toggle="'+kind+'" data-id="'+x.id+'"')+'</div>';
    }).join('')||'<div class="empty">항목이 없습니다.</div>';
    $('cardsSettingsList').innerHTML=renderOrderList(cards.items,'cards');
    $('categoriesSettingsList').innerHTML=renderOrderList(cats.items,'categories');
    $('rulesSettingsList').innerHTML=renderOrderList(rules.items,'rules');
    bindSettingsEvents(cards.items,cats.items,rules.items);
    bindSettingsActionButtons();bindSettingDrag();
  }catch(e){msg(e.message)}
}
function bindSettingDrag(){
  let dragState=null;
  const finishDrag=async(cancel=false)=>{
    if(!dragState)return;
    const st=dragState; dragState=null;
    document.removeEventListener('pointermove',onMove);
    document.removeEventListener('pointerup',onUp);
    document.removeEventListener('pointercancel',onCancel);
    st.el.classList.remove('dragging');
    document.querySelectorAll('.settingsItem.dragOver').forEach(x=>x.classList.remove('dragOver'));
    document.body.style.userSelect='';
    if(cancel)return;
    const list=st.el.parentElement,kind=st.el.dataset.settingKind;
    const ids=[...list.querySelectorAll('.settingsItem[data-setting-kind]')].map(x=>Number(x.dataset.id));
    const previous=st.previousIds;
    try{
      await apiSettings('order',null,'POST',{kind,ids});
      const reorder=(arr)=>ids.map(id=>arr.find(x=>Number(x.id)===id)).filter(Boolean);
      if(kind==='cards'){cardsCache=reorder(cardsCache);if(boot)boot.cards=reorder(boot.cards)}
      if(kind==='categories'){catsCache=reorder(catsCache);if(boot)boot.categories=reorder(boot.categories)}
      if(kind==='rules'){rulesCache=reorder(rulesCache);if(boot)boot.rules=reorder(boot.rules)}
    }catch(err){
      msg(err.message);
      [...list.querySelectorAll('.settingsItem[data-setting-kind]')].sort((a,b)=>previous.indexOf(Number(a.dataset.id))-previous.indexOf(Number(b.dataset.id))).forEach(x=>list.appendChild(x));
    }
  };
  const onMove=e=>{
    if(!dragState||e.pointerId!==dragState.pointerId)return;
    e.preventDefault();
    const under=document.elementFromPoint(e.clientX,e.clientY)?.closest('.settingsItem[data-setting-kind]');
    if(!under||under.parentElement!==dragState.el.parentElement||under===dragState.el)return;
    document.querySelectorAll('.settingsItem.dragOver').forEach(x=>x.classList.remove('dragOver'));
    under.classList.add('dragOver');
    const r=under.getBoundingClientRect();
    if(e.clientY<r.top+r.height/2)under.parentElement.insertBefore(dragState.el,under);
    else under.parentElement.insertBefore(dragState.el,under.nextSibling);
  };
  const onUp=e=>{if(dragState&&e.pointerId===dragState.pointerId)finishDrag(false)};
  const onCancel=e=>{if(dragState&&e.pointerId===dragState.pointerId)finishDrag(true)};
  document.querySelectorAll('#settingsView .dragHandle').forEach(handle=>{
    handle.onpointerdown=e=>{
      e.preventDefault();e.stopPropagation();
      const el=handle.closest('.settingsItem');if(!el)return;
      dragState={el,pointerId:e.pointerId,previousIds:[...el.parentElement.querySelectorAll('.settingsItem[data-setting-kind]')].map(x=>Number(x.dataset.id))};
      el.classList.add('dragging');document.body.style.userSelect='none';
      document.addEventListener('pointermove',onMove,{passive:false});
      document.addEventListener('pointerup',onUp,{passive:false});
      document.addEventListener('pointercancel',onCancel,{passive:false});
    };
  });
}
async function moveSetting(kind,id,direction){
  const arr=kind==='cards'?cardsCache:kind==='categories'?catsCache:rulesCache;
  const idx=arr.findIndex(x=>x.id===Number(id));
  const to=idx+direction;
  if(idx<0||to<0||to>=arr.length)return;
  const ids=arr.map(x=>x.id);[ids[idx],ids[to]]=[ids[to],ids[idx]];
  try{await apiSettings('order',null,'POST',{kind,ids});await loadSettings();await refreshAfterSettings()}catch(e){msg(e.message)}
}
let settingsEditKind=null, settingsEditId=null;
function ensureSettingsEditUI(){
  const needed=['settingsEditModal','settingsEditTitle','settingsEditSub','settingsNameField','settingsRuleFields','settingsEditMessage','settingsEditKeyword','settingsEditCategory','settingsNameLabel','settingsEditName','settingsEditForm'];
  if(needed.every(id=>$(id))) return;
  const old=$('settingsEditModal'); if(old) old.remove();
  const wrap=document.createElement('div');
  wrap.id='settingsEditModal'; wrap.className='modal hidden';
  wrap.innerHTML=`<div class="modalBox settingsModalBox">
    <div class="modalHead"><div><h2 id="settingsEditTitle">설정 수정</h2><div id="settingsEditSub" class="modalSub"></div></div><button type="button" id="closeSettingsModal" class="iconBtn">×</button></div>
    <form id="settingsEditForm">
      <div id="settingsNameField"><label id="settingsNameLabel">이름<input id="settingsEditName" type="text" autocomplete="off"></label></div>
      <div id="settingsRuleFields" class="hidden"><label>키워드<input id="settingsEditKeyword" type="text" autocomplete="off"></label><label>카테고리<select id="settingsEditCategory"></select></label></div>
      <div class="buttons"><button class="primary" type="submit">수정 저장</button><button type="button" id="cancelSettingsModal">취소</button></div>
      <p id="settingsEditMessage" class="message"></p>
    </form>
  </div>`;
  document.body.appendChild(wrap);
  $('settingsEditForm').onsubmit=saveSettingsEdit;
  $('closeSettingsModal').onclick=closeSettingsEdit;
  $('cancelSettingsModal').onclick=closeSettingsEdit;
  wrap.onclick=e=>{if(e.target===wrap)closeSettingsEdit()};
}
function openSettingsEdit(kind,id,items,categories){
  const item=items.find(x=>x.id===id); if(!item)return;
  settingsEditKind=kind; settingsEditId=id;
  const isRule=kind==='rules';
  const old=document.getElementById('settingsEditModalDynamic');
  if(old)old.remove();
  const modal=document.createElement('div');
  modal.id='settingsEditModalDynamic';
  modal.className='modal';
  const title=isRule?'자동분류 수정':kind==='cards'?'카드 수정':'카테고리 수정';
  const sub=isRule?'키워드가 포함된 지출을 자동으로 분류합니다.':(item.active?'현재 사용 중':'현재 사용 중지');
  const categoryOptions=isRule?categories.map(x=>`<option value="${x.id}" ${String(x.id)===String(item.category_id)?'selected':''}>${esc(x.name)}${x.active?'':' (사용 중지)'}</option>`).join(''):'';
  modal.innerHTML=`<div class="modalBox settingsModalBox">
    <div class="modalHead"><div><h2>${title}</h2><div class="modalSub">${esc(sub)}</div></div><button type="button" class="iconBtn" data-close>×</button></div>
    <form data-settings-form>
      ${isRule?`<label>키워드<input type="text" data-keyword autocomplete="off" value="${esc(item.keyword||'')}"></label><label>카테고리<select data-category>${categoryOptions}</select></label>`:kind==='cards'?`<label>카드 이름<input type="text" data-name autocomplete="off" value="${esc(item.name||'')}"></label><label>결제일<input type="number" min="1" max="31" data-payment-day value="${item.payment_day||''}" placeholder="예: 20"></label><div class="grid2"><label>이용기간 시작일<input type="number" min="1" max="31" data-period-start value="${item.period_start_day||''}" placeholder="전월 예: 9"></label><label>이용기간 종료일<input type="number" min="1" max="31" data-period-end value="${item.period_end_day||''}" placeholder="당월 예: 8"></label></div><div class="settingsHint">예: 20일 결제 · 전월 9일 ~ 당월 8일</div>`:`<label>카테고리 이름<input type="text" data-name autocomplete="off" value="${esc(item.name||'')}"></label>`}
      <div class="buttons"><button class="primary" type="submit">수정 저장</button><button type="button" data-cancel>취소</button></div>
      <p class="message" data-message></p>
    </form>
  </div>`;
  document.body.appendChild(modal);
  const form=modal.querySelector('[data-settings-form]');
  const close=()=>{settingsEditKind=null;settingsEditId=null;modal.remove()};
  modal.querySelector('[data-close]').onclick=close;
  modal.querySelector('[data-cancel]').onclick=close;
  modal.onclick=e=>{if(e.target===modal)close()};
  form.onsubmit=async e=>{
    e.preventDefault();
    const message=modal.querySelector('[data-message]');
    try{
      let body;
      if(isRule){
        body={keyword:modal.querySelector('[data-keyword]').value.trim(),category_id:Number(modal.querySelector('[data-category]').value),active:!!item.active};
        if(!body.keyword){message.textContent='키워드를 입력하세요.';message.className='message err';return;}
      }else{
        body={name:modal.querySelector('[data-name]').value.trim(),active:!!item.active}; if(kind==='cards'){body.payment_day=Number(modal.querySelector('[data-payment-day]').value);body.period_start_day=Number(modal.querySelector('[data-period-start]').value);body.period_end_day=Number(modal.querySelector('[data-period-end]').value)}
        if(!body.name){message.textContent='이름을 입력하세요.';message.className='message err';return;}
      }
      await apiSettings(kind,id,'POST',body);
      close();
      await loadSettings();
      await refreshAfterSettings();
    }catch(e){message.textContent=e.message;message.className='message err'}
  };
  const focusEl=modal.querySelector(isRule?'[data-keyword]':'[data-name]');
  setTimeout(()=>focusEl?.focus(),50);
}
function closeSettingsEdit(){
  settingsEditKind=null; settingsEditId=null;
  if($('settingsEditModal')) $('settingsEditModal').classList.add('hidden');
}
async function saveSettingsEdit(e){
  e.preventDefault(); if(!settingsEditKind||settingsEditId===null)return;
  try{
    const kind=settingsEditKind,id=settingsEditId;
    let body;
    if(kind==='rules') body={keyword:$('settingsEditKeyword').value.trim(),category_id:Number($('settingsEditCategory').value),active:true};
    else body={name:$('settingsEditName').value.trim(),active:true};
    if(kind==='rules' && !body.keyword){$('settingsEditMessage').textContent='키워드를 입력하세요.';$('settingsEditMessage').className='message err';return}
    if(kind!=='rules' && !body.name){$('settingsEditMessage').textContent='이름을 입력하세요.';$('settingsEditMessage').className='message err';return}
    const current=(kind==='rules'?rulesCache:kind==='cards'?cardsCache:catsCache).find(x=>x.id===id);
    if(current)body.active=!!current.active;
    await apiSettings(kind,id,'POST',body); closeSettingsEdit(); await loadSettings(); await refreshAfterSettings();
  }catch(e){
    if($('settingsEditMessage')){$('settingsEditMessage').textContent=e.message;$('settingsEditMessage').className='message err'}
    else msg(e.message);
  }
}
let cardsCache=[],catsCache=[],rulesCache=[];
function bindSettingsEvents(cards,cats,rules){
  cardsCache=cards;catsCache=cats;rulesCache=rules;
}
function settingsEditClick(kind,id){
  const items=kind==='cards'?cardsCache:kind==='categories'?catsCache:rulesCache;
  openSettingsEdit(kind,Number(id),items,catsCache);
}
function bindSettingsActionButtons(){
  document.querySelectorAll('#settingsView [data-settings-toggle]').forEach(btn=>{
    btn.onclick=function(e){
      e.preventDefault(); e.stopPropagation();
      handleSettingsToggle(this.getAttribute('data-settings-toggle'),Number(this.getAttribute('data-id')));
      return false;
    };
  });
}

let settingsConfirmResolve=null;
function openSettingsConfirm(title,message,actionLabel='사용 중지'){
  $('settingsConfirmTitle').textContent=title;
  $('settingsConfirmMessage').textContent=message;
  $('settingsConfirmAction').textContent=actionLabel;
  $('settingsConfirmModal').classList.remove('hidden');
  return new Promise(resolve=>{settingsConfirmResolve=resolve});
}
function closeSettingsConfirm(result=false){
  $('settingsConfirmModal').classList.add('hidden');
  if(settingsConfirmResolve){const r=settingsConfirmResolve;settingsConfirmResolve=null;r(result)}
}
async function handleSettingsToggle(kind,id){
  const arr=kind==='cards'?cardsCache:kind==='categories'?catsCache:rulesCache;
  const item=arr.find(x=>x.id===id); if(!item)return;
  const active=!!item.active;
  const noun=kind==='cards'?'카드':kind==='categories'?'카테고리':'자동분류';
  const ok=await openSettingsConfirm(active?`${noun} 사용 중지`:`${noun} 다시 사용`,
    active?`“${item.name||item.keyword}”을(를) 사용 중지할까요?`:`“${item.name||item.keyword}”을(를) 다시 사용할까요?`,
    active?'사용 중지':'다시 사용');
  if(!ok)return;
  try{
    const body=kind==='rules'?{keyword:item.keyword,category_id:item.category_id,active:!active}:{name:item.name,active:!active};
    await apiSettings(kind,id,'POST',body);
    await loadSettings();
    await refreshAfterSettings();
  }catch(e){msg(e.message)}
}

async function refreshAfterSettings(){
  const r=await fetch('/api/bootstrap'),x=await r.json();boot=x;selects();
  $('newRuleCategory').innerHTML=boot.categories.map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('');
  await list();
}
$('namesForm').onsubmit=async e=>{e.preventDefault();try{await apiSettings('names',null,'POST',{user_name:$('settingsUserName').value.trim(),wife_name:$('settingsWifeName').value.trim()});$('namesMessage').textContent='저장했습니다.';$('namesMessage').className='message ok';await refreshAfterSettings()}catch(e){$('namesMessage').textContent=e.message;$('namesMessage').className='message err'}};
$('cardAddForm').onsubmit=async e=>{e.preventDefault();const v=$('newCardName').value.trim();if(!v)return;try{await apiSettings('cards',null,'POST',{name:v});$('newCardName').value='';await loadSettings();await refreshAfterSettings()}catch(e){alert(e.message)}};
$('categoryAddForm').onsubmit=async e=>{e.preventDefault();const v=$('newCategoryName').value.trim();if(!v)return;try{await apiSettings('categories',null,'POST',{name:v});$('newCategoryName').value='';await loadSettings();await refreshAfterSettings()}catch(e){alert(e.message)}};
$('ruleAddForm').onsubmit=async e=>{e.preventDefault();const v=$('newRuleKeyword').value.trim();if(!v)return;try{await apiSettings('rules',null,'POST',{keyword:v,category_id:$('newRuleCategory').value});$('newRuleKeyword').value='';await loadSettings()}catch(e){alert(e.message)}};
document.querySelectorAll('.navBtn').forEach(b=>b.addEventListener('click',()=>{if(b.dataset.view==='settingsView')loadSettings()}));

$('settingsConfirmAction').onclick=()=>closeSettingsConfirm(true);
$('settingsConfirmCancel').onclick=()=>closeSettingsConfirm(false);
$('settingsConfirmClose').onclick=()=>closeSettingsConfirm(false);
$('settingsConfirmModal').onclick=e=>{if(e.target.id==='settingsConfirmModal')closeSettingsConfirm(false)};

// v0.7.0 — Excel / Google Sheets import
let importRows=[];
function importRatioUpdate(){let u=Math.max(0,Math.min(100,Number($('importRatio').value)||0));$('importRatio').value=u;$('importRatioText').textContent=`${u} : ${100-u}`}
function parseImportDate(raw){let s=String(raw??'').trim().replace(/\./g,'-').replace(/\//g,'-');s=s.replace(/\s*\([^)]*\)\s*$/,'');let m=s.match(/^(\d{1,2})-(\d{1,2})$/);if(m){const y=new Date().getFullYear();return `${y}-${String(+m[1]).padStart(2,'0')}-${String(+m[2]).padStart(2,'0')}`}let iso=s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);if(iso)return `${iso[1]}-${String(+iso[2]).padStart(2,'0')}-${String(+iso[3]).padStart(2,'0')}`;return null}
function parseImportAmount(raw){let s=String(raw??'').trim().replace(/[,원₩\s]/g,'');if(!s||!/^[-+]?\d+$/.test(s))return null;return Number(s)}
function parseImportInstallment(cells){const text=cells.join(' ');let m=text.match(/할부\s*\(\s*(\d+)\s*개월\s*\/\s*(\d+)\s*회차\s*\)/i);if(m)return +m[1];m=text.match(/할부\s*\(\s*(\d+)\s*\/\s*(\d+)\s*\)/i);if(m)return +m[2];m=text.match(/할부\s*\(\s*(\d+)\s*개월\s*\/\s*(\d+)\s*회차?\s*\)/i);if(m)return +m[1];return null}
function splitImportLine(line){if(line.includes('\t'))return line.split('\t');if(line.includes('|')){const t=line.trim();if(/^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?$/.test(t))return [];return t.replace(/^\|/,'').replace(/\|$/,'').split('|').map(x=>x.trim())}return line.split(/\s{2,}/)}
function looksLikeHeader(cells){const s=cells.join(' ').toLowerCase();return /날짜|일자|사용일|시간|가맹점|내용|금액|이용금액|결제금액/.test(s)}
function parseImportText(){const raw=$('importText').value.replace(/\r/g,'').trim();if(!raw)return {rows:[],errors:['붙여넣은 내용이 없습니다.']};const lines=raw.split('\n').filter(x=>x.trim());const start=looksLikeHeader(splitImportLine(lines[0]))?1:0;const rows=[],errors=[];for(let i=start;i<lines.length;i++){const cells=splitImportLine(lines[i]).map(x=>x.trim().replace(/^\|\s*|\s*\|$/g,''));if(!cells.length)continue;if(cells.length<4){errors.push(`${i+1}행: 열이 4개가 아닙니다.`);continue}const [dateRaw,timeRaw,contentRaw,amountRaw]=cells;const txDate=parseImportDate(dateRaw),amount=parseImportAmount(amountRaw),installmentMonths=parseImportInstallment(cells);if(!txDate)errors.push(`${i+1}행: 날짜를 읽을 수 없습니다 (${dateRaw})`);if(amount===null)errors.push(`${i+1}행: 금액을 읽을 수 없습니다 (${amountRaw})`);if(!String(contentRaw||'').trim())errors.push(`${i+1}행: 가맹점/내용이 비어 있습니다.`);if(installmentMonths!==null&&(!Number.isInteger(installmentMonths)||installmentMonths<1||installmentMonths>60))errors.push(`${i+1}행: 할부 개월 수가 올바르지 않습니다.`);if(txDate&&amount!==null&&String(contentRaw||'').trim())rows.push({date:txDate,time:String(timeRaw||'').trim(),content:String(contentRaw).trim(),amount,installment_months:installmentMonths||1})}return {rows,errors}}
function renderImportPreview(parsed){const el=$('importPreview');importRows=parsed.rows;const rules=boot.rules||[];const autoCount=parsed.rows.filter(x=>rules.some(r=>String(x.content).toUpperCase().includes(String(r.keyword).toUpperCase()))).length;const noAuto=parsed.rows.length-autoCount;const dupCount=importDuplicateFlags.filter(x=>x.exact||x.similar).length;$('importSaveBtn').disabled=!!parsed.errors.length||!parsed.rows.length;if(parsed.errors.length){el.innerHTML=`<div class="importSummary importBad">⚠️ ${parsed.errors.length}건 확인 필요</div>${parsed.errors.slice(0,8).map(x=>`<div class="importRow importBad"><span></span><span class="store">${esc(x)}</span><span></span></div>`).join('')}${parsed.errors.length>8?`<div class="importRow importBad"><span></span><span class="store">외 ${parsed.errors.length-8}건</span><span></span></div>`:''}`;return}if(!parsed.rows.length){el.innerHTML='<div class="importEmpty">가져올 데이터가 없습니다.</div>';return}const total=parsed.rows.reduce((a,x)=>a+x.amount,0);const catMsg=` · 자동분류 ${autoCount}건 · 미정 ${noAuto}건`;const dupMsg=dupCount?` · 🟠 중복 의심 ${dupCount}건`:'';el.innerHTML=`<div class="importSummary">${parsed.rows.length}건 · 합계 ${money(total)}${catMsg}${dupMsg}</div>`+parsed.rows.slice(0,100).map((x,i)=>{const dup=importDuplicateFlags[i]||{};const hit=rules.filter(r=>String(x.content).toUpperCase().includes(String(r.keyword).toUpperCase()))[0];const catLabel=hit?'자동분류 → '+esc(hit.category||''):'미정';const cls=dup.exact?'importDup':dup.similar?'importWarn':(!hit?'importBad':'');const badge=dup.exact?' · 🟠 중복 의심':dup.similar?' · 🟡 유사 내역':'';const instLabel=x.installment_months>1?`할부 ${x.installment_months}개월`:'일시불';return `<div class="importRow ${cls}"><span class="dateTime">${esc(x.date.slice(5))}<br>${esc(x.time||'')}</span><span class="store">${esc(x.content)}<small>${catLabel} · ${instLabel}${badge}</small></span><span class="amt">${money(x.amount)}</span></div>`}).join('')+(parsed.rows.length>100?`<div class="importRow"><span></span><span class="store">외 ${parsed.rows.length-100}건</span><span></span></div>`:'')}
async function refreshImportPreview(){const parsed=parseImportText();if(parsed.errors.length||!parsed.rows.length){importDuplicateFlags=[];renderImportPreview(parsed);return parsed}try{const r=await fetch('/api/transactions/check_duplicates',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({card_id:$('importCard').value,rows:parsed.rows})});const d=await r.json();if(!r.ok)throw Error(d.error);importDuplicateFlags=d.duplicates||[]}catch(e){importDuplicateFlags=[]}renderImportPreview(parsed);return parsed}
function openImport(){$('importCard').innerHTML=boot.cards.map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('');$('importUserName').textContent=boot.settings.user_name||'나';$('importWifeName').textContent=boot.settings.wife_name||'배우자';$('importRatio').value=50;importRatioUpdate();importRows=[];importDuplicateFlags=[];$('importPreview').innerHTML='<div class="importEmpty">엑셀/Sheets에서 표를 복사해 붙여넣은 뒤<br>미리보기를 눌러주세요.</div>';$('importSaveBtn').disabled=true;$('importMessage').textContent='';$('importMessage').className='message';$('importModal').classList.remove('hidden');setTimeout(()=>$('importText').focus(),50)}
function closeImport(){$('importModal').classList.add('hidden');importRows=[]}
$('openImportBtn').onclick=openImport;$('closeImportModal').onclick=closeImport;$('importModal').onclick=e=>{if(e.target.id==='importModal')closeImport()};$('importRatio').oninput=importRatioUpdate;$('importCard').onchange=()=>{if(importRows.length)refreshImportPreview()};$('importPreviewBtn').onclick=async()=>{const parsed=await refreshImportPreview();if(parsed.errors.length)$('importMessage').textContent='오류를 먼저 수정한 뒤 다시 미리보기 해주세요.';else if(importDuplicateFlags.some(x=>x.exact||x.similar)){$('importMessage').textContent='🟠 중복 의심 내역이 있습니다. 확인 후 등록하세요.';$('importMessage').className='message';}else $('importMessage').textContent=''};
$('importSaveBtn').onclick=async()=>{if(!importRows.length)return;const ratioVal=Math.max(0,Math.min(100,Number($('importRatio').value)||0));const minjeong=boot.categories.find(x=>x.name==='미정');if(!minjeong){$('importMessage').textContent='미정 카테고리가 없습니다. 페이지를 새로고침해 주세요.';$('importMessage').className='message err';return}const rules=boot.rules||[];const rows=importRows.map(x=>{const hit=rules.filter(r=>String(x.content).toUpperCase().includes(String(r.keyword).toUpperCase()))[0];return {date:x.date,time:x.time,amount:x.amount,card_id:$('importCard').value,category_id:hit?hit.category_id:minjeong.id,content:x.content,user_percent:ratioVal,installment_months:x.installment_months||1}});const btn=$('importSaveBtn');btn.disabled=true;$('importMessage').textContent='등록 중…';$('importMessage').className='message';try{const r=await fetch('/api/transactions/import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rows})});const d=await r.json();if(!r.ok||!d.ok)throw Error(d.error);closeImport();msg(`${d.count}건을 등록했습니다.`,true);await list()}catch(e){$('importMessage').textContent=e.message;$('importMessage').className='message err';btn.disabled=false}}


document.addEventListener('click',e=>{const b=e.target.closest('.settleLink');if(!b)return;openSettlementDetails(b.dataset.settleKind,b.dataset.settleId,b.dataset.settleMonth)});$('closeSettlementDetail').onclick=closeSettlementDetails;$('settlementDetailModal').onclick=e=>{if(e.target.id==='settlementDetailModal')closeSettlementDetails()};
$('loadMoreBtn').onclick=()=>{listLimit+=100;list().catch(e=>msg(e.message))};
