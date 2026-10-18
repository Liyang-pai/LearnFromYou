// 正式前端入口：页面导航与课堂、录音生命周期分离，全部业务调用真实后端。
import {state,api,emptySession,applyEvent} from './service.js';
import {Microphone} from './audio.js';
import {render} from './views.js';
import {confirmation} from './prototype.js';

const view=document.querySelector('#view'), mic=new Microphone(), testMic=new Microphone();
let socket=null,testSocket=null,routeToken=0,pageTimer,healthTimer,asrTimer,reportWait=0,creating=false,startingTest=false;
let currentUrl=location.pathname+location.search;
const path=()=>location.pathname.replace(/\/$/,'') || '/';
function refresh() {
  const active=document.activeElement, id=active?.id, name=active?.name;
  const selection=active && 'selectionStart' in active ? [active.selectionStart,active.selectionEnd] : null;
  const positions=[...view.querySelectorAll('[data-scroll],.chat-body')].map(el=>[el.id || el.className,el.scrollTop,el.scrollHeight-el.clientHeight-el.scrollTop<40]);
  const openDetails=[...view.querySelectorAll('details[open]')].map(el=>el.dataset.key || el.querySelector('summary')?.textContent);
  view.innerHTML=render(state,path(),location.search);
  for(const el of view.querySelectorAll('details')) if(openDetails.includes(el.dataset.key || el.querySelector('summary')?.textContent)) el.open=true;
  for(const el of view.querySelectorAll('[data-scroll],.chat-body')) {
    const saved=positions.find(row=>row[0]===(el.id || el.className));
    el.scrollTop=saved ? (saved[2] ? el.scrollHeight : saved[1]) : el.scrollHeight;
  }
  const focus=id ? document.getElementById(id) : name ? [...view.querySelectorAll('[name]')].find(el=>el.name===name) : null;
  if(focus && !focus.disabled) { focus.focus({preventScroll:true}); if(selection && focus.setSelectionRange && !['number','checkbox'].includes(focus.type)) focus.setSelectionRange(...selection); }
  document.querySelectorAll('.sidebar a.nav-item').forEach(a=>a.classList.toggle('active',a.pathname==='/' ? path()==='/' || path()==='/history' : path().startsWith(a.pathname)));
}
function notice(message) { state.notice=message; refresh(); const node=document.querySelector('#toast'); node.textContent=message; node.hidden=false; clearTimeout(notice.timer); notice.timer=setTimeout(()=>node.hidden=true,6000); }
async function confirmAction(title,message,label='确认') {
  const dialog=document.querySelector('#action-confirm');if(dialog.open)return false;
  dialog.innerHTML=confirmation(title,message,label,state);dialog.returnValue='cancel';dialog.showModal();
  return new Promise(resolve=>dialog.addEventListener('close',()=>resolve(dialog.returnValue==='confirm'),{once:true}));
}
async function action(task) { try { await task(); } catch(error) { notice(error.message || '操作失败，请重试'); } }
async function customChoice(field){
  const dialog=document.querySelector('#action-confirm');if(dialog.open)return;
  dialog.innerHTML=confirmation(field==='grade'?'自定义年级':'自定义学科','填写你的教学场景','保存',state);
  const body=dialog.querySelector('.modal-body');body.innerHTML='<input id="custom-choice" class="input" maxlength="100" aria-label="自定义内容" placeholder="填写你的教学场景" required>';
  dialog.querySelector('.modal-footer button:first-child').textContent='取消';dialog.querySelector('.modal-footer button:first-child').formNoValidate=true;
  dialog.returnValue='cancel';dialog.showModal();await new Promise(resolve=>dialog.addEventListener('close',resolve,{once:true}));
  const value=dialog.querySelector('input').value.trim();if(dialog.returnValue==='confirm' && value)state.setup[field]=value;refresh();
}
async function navigate(url,push=true) {
  const dirty=state.settingsDraft && (!!state.settingsDraft.api_key || ['tts','asr_review'].some(key=>state.settingsDraft[key]!==state.settings[key]));
  if(currentUrl.startsWith('/settings') && !url.startsWith('/settings') && dirty){
    if(!await confirmAction('还有未保存的设置','现在离开，刚才的修改将不会保存。','保存后离开')){if(!push)history.pushState(null,'',currentUrl);return;}
    try{await saveSettings();}catch(error){if(!push)history.pushState(null,'',currentUrl);throw error;}
  }
  if(push) history.pushState(null,'',url); routeToken++; clearTimeout(pageTimer);
  currentUrl=url;
  const nextPath=path(),nextId=nextPath.split('/').pop();
  if(nextPath.startsWith('/report/') && state.report.session_id!==nextId)state.report={session_id:nextId,status:'idle',loading:true};
  if(nextPath.startsWith('/history/') && state.detail.id!==nextId)state.detail={id:nextId,items:[],warnings:[],review:{},before:null,status:'indexing'};
  const token=routeToken; state.notice=''; refresh();window.scrollTo({top:0,behavior:'instant'});
  const p=path();
  if(p==='/' || p==='/history') await loadHistory(false,token);
  else if(/^\/history\/[0-9a-f]{12}$/.test(p)) await loadDetail(p.split('/').pop(),false,token);
  else if(/^\/report\/[0-9a-f]{12}$/.test(p)) await loadReport(p.split('/').pop(),'GET',token);
  else if(p==='/settings') { state.settings=await api('/api/settings'); if(!state.settingsDraft) state.settingsDraft={...state.settings,api_key:''}; refresh(); }
  else if(p==='/settings/asr') { await loadAsr(); }
}
document.addEventListener('click',event=>{
  const link=event.target.closest('a[href]');
  if(!link || event.defaultPrevented || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button!==0 || link.target || link.download) return;
  const url=new URL(link.href,location.href);
  if(url.origin===location.origin && !url.pathname.startsWith('/api/') && !url.pathname.startsWith('/debug')) {
    event.preventDefault(); void action(()=>navigate(url.pathname+url.search));
  }
});
window.addEventListener('popstate',()=>void action(()=>navigate(location.pathname+location.search,false)));
window.addEventListener('beforeunload',event=>{ if(state.session.active || state.session.starting || state.session.ending || state.test.active) {event.preventDefault();event.returnValue='';} });

function send(event) {
  if(socket?.readyState!==WebSocket.OPEN) throw new Error('本机服务连接已断开，草稿已保留');
  socket.send(JSON.stringify(event));
}
async function connect(url,onmessage,onclose) {
  const ws=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}${url}`);
  ws.onmessage=e=>{ try { onmessage(JSON.parse(e.data)); } catch(error) { notice('服务消息无法显示：'+error.message); } };
  ws.onclose=onclose;
  await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=()=>reject(new Error('无法连接本机服务，请检查后台是否运行'));});
  return ws;
}
async function createClass() {
  if(creating)return;creating=true;
  try{await createClassRequest();}finally{creating=false;}
}
async function createClassRequest() {
  const s=state.session;
  if(s.active || s.starting || s.ending) return navigate('/classroom');
  const form=document.querySelector('#setup-form'); if(form && !form.reportValidity()) return;
  if(!state.setup.topic.trim() || !state.setup.student_profile.trim()) throw new Error('请填写主题，并生成或填写学生设定');
  await loadHealth();
  if(state.health.asr_busy || state.health.classroom_active || state.test.active) throw new Error('课堂或识别测试正在占用资源，请先在原页面结束');
  if(s.pendingImage)URL.revokeObjectURL(s.pendingImage.url);
  const next=emptySession(); Object.assign(next,{draft:s.draft,lesson:{...state.setup},starting:true,tts:state.settings.tts,status:'正在创建课堂，尚未开启麦克风'}); state.session=next;
  if(socket) { socket.onclose=null; socket.close(); }
  refresh();
  try {
    socket=await connect('/ws/session',classEvent,()=>{
      const current=state.session,unexpected=current.active || current.starting || current.ending;
      current.active=current.starting=current.ending=current.mic=false; current.pending=null;
      void mic.stop(null,null);
      if(unexpected) { current.error='连接已断开，已有课堂记录已保留。刷新或断线后的课堂不能续讲，请查看历史或创建新课堂。'; current.status='连接已断开'; }
      refresh(); void loadHealth();
    });
    send({type:'start',lesson:next.lesson,muted:false,tts:next.tts,asr_review:state.createReview??state.settings.asr_review});
    await navigate('/classroom');
  } catch(error) { next.starting=false;next.error=error.message;refresh();throw error; }
}
function classEvent(event) {
  applyEvent(state.session,event);
  const s=state.session,d=event.data || {};
  if(event.type==='ready') { sessionStorage.setItem('last-classroom',s.id); void loadHealth(); }
  if(event.type==='finished') {
    void mic.stop(null,null); reportWait=30;
    // finished precedes resource cleanup and report task registration; poll GET only.
    void action(()=>navigate(`/report/${s.id}`));
  }
  if(event.type==='report_unavailable') { state.report={session_id:d.session_id,status:'failed',error:d.message}; reportWait=0; }
  if(event.type==='error' && ['audio_failure','audio_backlog','asr_failure','no_audio_frames'].includes(d.code)) {
    if(d.code==='no_audio_frames')s.retryable=false;
    void mic.stop(socket).then(()=>{s.mic=false;refresh();});
  }
  if(!['audio_input','llm_output','llm_request','state','candidate','vad','question_release'].includes(event.type)) refresh();
}
async function message() {
  const s=state.session;
  if(!s.active || s.ending || s.pending) return;
  if(s.uploading || s.uncertain || s.pendingImage) throw new Error('请先确认使用截图，或取消待上传图片，再发送讲授');
  const text=s.draft; if(!text.trim()) return;
  const id=crypto.randomUUID(); s.pending={id,text};s.error='';
  try { send({type:'text',text,client_message_id:id}); } catch(error) { s.pending=null; throw error; }
  refresh();
}
async function toggleMic() {
  const s=state.session;
  if(!s.active || s.ending || mic.busy) return;
  if(mic.active) {
    s.micPending=true;refresh();
    try{await mic.stop(socket); s.mic=false;}finally{s.micPending=false;refresh();}
  }
  else {
    if(s.uploading || s.uncertain || s.pendingImage) throw new Error('截图尚未确认，请确认或取消截图后开启麦克风');
    s.micPending=true;refresh();
    try{s.micError='';await mic.start(socket,undefined,error=>{s.mic=false;s.error=error.message;refresh();});s.mic=true;}
    catch(error){s.micError=error.message;s.mic=false;}
    finally{s.micPending=false;refresh();}
  }
  refresh();
}
async function endClass() {
  const s=state.session;
  if(!s.active || s.ending) return;
  if(!await confirmAction('结束本次试讲？','结束后不能继续本课堂，系统会整理已有讲授并生成教学报告。','结束并生成报告')) return;
  s.ending=true;s.status='正在收尾课堂，请稍候'; refresh();
  await mic.stop(socket); send({type:'end'});
}
async function generateProfile() {
  if(state.profileBusy) return;
  if(!state.setup.topic.trim()) throw new Error('请先填写试讲主题');
  if(state.setup.student_profile.trim() && !await confirmAction('重新生成学生画像？','生成成功后会替换当前画像；取消或生成失败会保留原内容。','重新生成'))return;
  state.profileBusy=true;state.profileError='';refresh();
  const {student_profile,...settings}=state.setup;
  try { const result=await api('/api/student-profile','POST',settings); Object.assign(state.setup,result); notice('学生画像已生成，可以编辑后确认创建'); }
  catch(error){state.profileError=error.message;throw error;}
  finally { state.profileBusy=false;refresh(); }
}

async function reconcileImage() {
  const s=state.session; s.image=await api(`/api/sessions/${s.id}/image`); s.uncertain=false;refresh();
}
async function upload(file) {
  const s=state.session;
  if(!file || !s.active || s.ending || s.uploading) return;
  if(file.size>5*1024*1024 || !['image/png','image/jpeg','image/webp'].includes(file.type)) throw new Error('请选择不超过 5 MiB 的 PNG、JPEG 或 WebP 图片');
  // Pause capture while a new image is unconfirmed, so audio cannot depend on it.
  if(mic.active) { await mic.stop(socket);s.mic=false; }
  s.pendingImage={file,name:file.name || '粘贴的截图'};await confirmImage();
}
function cancelImage() {
  const s=state.session;if(s.uploading)return;
  if(s.pendingImage)URL.revokeObjectURL(s.pendingImage.url);s.pendingImage=null;refresh();
}
async function confirmImage() {
  const s=state.session,file=s.pendingImage?.file;if(!file || s.uploading || !s.active)return;
  s.uploading=true;refresh();
  try { s.image={...await api(`/api/sessions/${s.id}/image`,'POST',file),name:file.name || '粘贴的截图'};s.uncertain=false;cancelImageAfterUpload(s); }
  catch(error) {
    if(!error.status) {s.uncertain=true;try {await reconcileImage();} catch {} }
    throw new Error(error.message+'；草稿与已有截图已保留'+(s.uncertain?'，请点击重新确认截图状态':''));
  } finally {s.uploading=false;cancelImageAfterUpload(s);refresh();}
}
function cancelImageAfterUpload(s){if(s.pendingImage?.url)URL.revokeObjectURL(s.pendingImage.url);s.pendingImage=null;}
async function removeImage() {
  const s=state.session;if(s.uploading || !s.active)return;
  s.uploading=true;refresh();
  try { await api(`/api/sessions/${s.id}/image/clear`,'POST');s.image=null;s.uncertain=false; }
  catch(error) { if(!error.status) {s.uncertain=true;try {await reconcileImage();} catch {}} throw error; }
  finally {s.uploading=false;refresh();}
}
const picker=document.querySelector('#image-picker');
picker.addEventListener('change',()=>{const file=picker.files[0];picker.value='';void action(()=>upload(file));});
document.addEventListener('paste',event=>{
  if(!event.target.closest('#message-form,.composer') || !state.session.active) return;
  const item=[...(event.clipboardData?.items || [])].find(item=>item.type.startsWith('image/'));
  if(item) {event.preventDefault();void action(()=>upload(item.getAsFile()));}
});
const preview=document.querySelector('#image-preview');
document.querySelector('#close-preview').onclick=()=>preview.close();
preview.addEventListener('click',event=>{if(event.target===preview)preview.close();});

async function loadHistory(more=false,token=routeToken) {
  const h=state.history; if(h.loading && h.loadingToken===token)return;h.loading=true;h.loadingToken=token;refresh();
  try {
    const data=await api(`/api/history?offset=${more?h.offset:0}&limit=${path()==='/'?5:20}`);
    if(token!==routeToken)return;
    h.items=more?[...h.items,...data.items]:data.items;h.total=data.total;h.offset=h.items.length;h.indexing=data.indexing;h.error='';
    if(data.indexing) pageTimer=setTimeout(()=>void action(()=>loadHistory(false,token)),1500);
  } catch(error) {if(token===routeToken)h.error=error.message;} finally {if(h.loadingToken===token)h.loading=false;if(token===routeToken)refresh();}
}
async function loadDetail(id,earlier=false,token=routeToken) {
  if(state.detail.id!==id) state.detail={id,items:[],warnings:[],review:{},before:null};
  const d=state.detail;
  try {
    const meta=await api(`/api/history/${id}`); if(token!==routeToken)return;
    Object.assign(d,meta,{id});
    if(meta.status!=='ready') {pageTimer=setTimeout(()=>void action(()=>loadDetail(id,false,token)),1200);refresh();return;}
    const data=await api(`/api/history/${id}/dialogue${earlier&&d.before!==null?'?before='+d.before:''}`); if(token!==routeToken)return;
    if(data.status!=='ready') {pageTimer=setTimeout(()=>void action(()=>loadDetail(id,false,token)),1200);return;}
    if(earlier && d.revision && d.revision!==data.revision) return loadDetail(id,false,token);
    d.items=earlier?[...data.items,...d.items]:data.items;d.before=data.before;d.revision=data.revision;d.error='';
  } catch(error) {if(token===routeToken)d.error=error.message;} finally {if(token===routeToken)refresh();}
}
async function loadReport(id,method='GET',token=routeToken) {
  if(method==='POST'){state.report={session_id:id,status:'generating',progress:'正在提交报告生成任务…'};refresh();}
  try {
    const data=await api(`/api/teaching-report/${id}`,method); if(token!==routeToken)return;
    state.report=data;
    if(data.status!=='idle') reportWait=0;
    if(data.status==='generating' || data.status==='idle' && reportWait-->0) pageTimer=setTimeout(()=>void action(()=>loadReport(id,'GET',token)),1200);
  } catch(error) {if(token===routeToken){state.report={session_id:id,status:'failed',error:error.message};reportWait=0;}}
  finally {if(token===routeToken)refresh();}
}
async function saveSettings() {
  const draft=state.settingsDraft || state.settings;
  if(draft.saving)return;draft.saving=true;refresh();
  try {
    state.settings=await api('/api/settings','POST',{api_key:draft.api_key || '',tts:draft.tts,asr_review:draft.asr_review});
    state.settingsDraft={...state.settings,api_key:'',saveMessage:'设置已保存'};notice('设置已保存，进行中的课堂保持原配置');await loadHealth();
  }finally{draft.saving=false;refresh();}
}
async function loadHealth() {state.health=await api('/api/health');}
async function loadAsr() {
  state.asr=await api('/api/asr/models');
  try{state.devices=(await navigator.mediaDevices.enumerateDevices()).filter(device=>device.kind==='audioinput');}catch{}
  if(path()==='/settings/asr')refresh();clearTimeout(asrTimer);
  const jobs=state.asr.models?.some(m=>['downloading','verifying','preparing','queued','cancelling'].includes(m.job?.state) || m.job?.state==='running');
  if(jobs) asrTimer=setTimeout(()=>void action(loadAsr),1200);
}
async function startTest() {
  if(startingTest)return;startingTest=true;
  try{await startTestRequest();}finally{startingTest=false;}
}
async function startTestRequest() {
  if(state.test.active || state.test.starting)return;
  await loadHealth();if(state.session.active || state.session.starting || state.health.asr_busy)throw new Error('课堂正在占用识别资源，请结束课堂后再测试');
  state.test.starting=true;state.test.status='等待浏览器麦克风授权，授权后开始本机识别';refresh();
  if(testSocket){testSocket.onclose=null;testSocket.onmessage=null;testSocket.close();testSocket=null;}
  try {
    testSocket=await connect('/ws/asr-debug',event=>{
      const d=event.data || {};
      if(event.type==='ready'){state.test.active=true;state.test.starting=false;state.test.status='正在识别，说话后稍作停顿';}
      if(event.type==='transcript'){state.test.texts.push(d.text);(state.test.records ||= []).push(d);}
      if(event.type==='status')state.test.status=d.message;
      if(event.type==='finished'){state.test.active=false;state.test.status=`识别完成，共 ${d.segments} 段`;void loadAsr();}
      if(event.type==='error'){state.test.status=d.message;state.test.active=state.test.starting=false;void testMic.stop(null,null);}
      if(['ready','transcript','status','finished','error'].includes(event.type))refresh();
    },()=>{state.test.active=state.test.starting=false;void testMic.stop(null,null);refresh();void loadAsr();});
    await testMic.start(testSocket,rate=>({type:'start',sample_rate:rate}),error=>{state.test.status=error.message;refresh();});
  } catch(error) {state.test.active=state.test.starting=false;testSocket?.close();throw error;}finally{refresh();}
}
async function stopTest() {if(state.test.stopping)return;state.test.stopping=true;state.test.status='正在处理末尾录音';refresh();try{await testMic.stop(testSocket,{type:'stop'});}finally{state.test.stopping=false;refresh();}}
function downloadText(name,text) {
  const url=URL.createObjectURL(new Blob([text],{type:'text/plain;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}

view.addEventListener('input',event=>{
  const el=event.target;
  if(el.id==='teacher-draft' || el.name==='draft'){state.session.draft=el.value;return;}
  if(el.closest('#setup-form') && el.name in state.setup && !['自定义…','其他（自定义）'].includes(el.value))state.setup[el.name]=el.value;
  if(el.closest('#setup-form')){
    const generate=view.querySelector('[data-action="generate-profile"]'),create=view.querySelector('[data-action="create"]');
    if(generate){generate.disabled=state.profileBusy || !state.setup.topic.trim();generate.classList.toggle('primary',!generate.disabled);}
    if(create){create.disabled=state.profileBusy || !state.setup.student_profile.trim();create.classList.toggle('primary',!create.disabled);}
  }
  if(el.name==='device_id'){state.deviceId=el.value;testMic.deviceId=mic.deviceId=el.value;}
  else if(el.closest('#settings-form') && el.name){state.settingsDraft ||= {...state.settings,api_key:''};state.settingsDraft[el.name]=el.type==='checkbox'?el.checked:el.value;}
});
view.addEventListener('change',event=>{
  const el=event.target;
  if(el.closest('#setup-form') && el.name in state.setup){if(['自定义…','其他（自定义）'].includes(el.value)){void action(()=>customChoice(el.name));return;}state.setup[el.name]=el.value;}
  if(el.closest('#setup-form') && el.name==='asr_review')state.createReview=el.checked;
  if(el.name==='device_id'){state.deviceId=el.value;testMic.deviceId=mic.deviceId=el.value;}
  else if(el.closest('#settings-form') && el.name){state.settingsDraft ||= {...state.settings,api_key:''};state.settingsDraft[el.name]=el.type==='checkbox'?el.checked:el.value;}
});
view.addEventListener('submit',event=>{
  event.preventDefault();
  if(event.target.id==='message-form')void action(message);
  if(event.target.id==='setup-form')void action(createClass);
  if(event.target.id==='settings-form')void action(saveSettings);
});
view.addEventListener('click',event=>{
  const button=event.target.closest('[data-action]');if(!button || button.disabled || button.getAttribute('aria-disabled')==='true')return;
  const a=button.dataset.action,id=button.dataset.id,s=state.session;
  void action(async()=>{
    if(a==='create')await createClass();
    else if(a==='setup-choice'){state.setup[button.dataset.field]=button.dataset.value;refresh();}
    else if(a==='toggle-create-review'){state.createReview=!(state.createReview??state.settings.asr_review);refresh();}
    else if(a==='open-asr')await navigate('/settings/asr');
    else if(a==='class-details')await navigate('/classroom/details');
    else if(a==='back-class')await navigate('/classroom');
    else if(a==='report-back')await navigate(`/history/${id}`);
    else if(a==='home-create')await navigate(state.session.active?'/classroom':'/create');
    else if(a==='open-settings')await navigate('/settings');
    else if(a==='history-all')await navigate('/history');
    else if(a==='history-close')await navigate('/');
    else if(a==='history-open')await navigate(`/report/${id}`);
    else if(a==='generate-profile')await generateProfile();
    else if(a==='toggle-mic')await toggleMic();
    else if(a==='dismiss-mic-error'){s.micError='';refresh();}
    else if(a==='toggle-muted'){send({type:'mute',value:!s.muted});}
    else if(a==='toggle-tts'){if(!s.muted && s.active){const value=!s.tts;send({type:'tts',value});s.tts=value;s.speaking=false;refresh();}}
    else if(a==='toggle-sidebar'){s.sidebar=!s.sidebar;refresh();}
    else if(a==='end-class' || a==='generate-report')await endClass();
    else if(a==='retry-class'){send({type:'retry'});s.error='';s.retryable=false;refresh();}
    else if(a==='upload-image')picker.click();
    else if(a==='confirm-image')await confirmImage();
    else if(a==='cancel-image')cancelImage();
    else if(a==='remove-image')await removeImage();
    else if(a==='reconcile-image')await reconcileImage();
    else if(a==='preview-image' && (s.pendingImage || s.image)){preview.querySelector('img').src=(s.pendingImage || s.image).url;preview.showModal();}
    else if(a==='history-more')await loadHistory(true);
    else if(a==='history-earlier')await loadDetail(state.detail.id,true);
    else if(a==='export-session'){
      const archived=path().startsWith('/history/'),record=archived?state.detail:s;
      let rows=record.dialogue || [];
      if(archived){
        let before=null,revision='';rows=[];
        do {
          const page=await api(`/api/history/${record.id}/dialogue${before===null?'':'?before='+before}`);
          if(page.status!=='ready' || revision && revision!==page.revision)throw new Error('课堂记录正在更新，请重新导出');
          revision=page.revision;rows=[...page.items,...rows];before=page.before;
        }while(before!==null);
      }
      const heading=record.topic || record.lesson?.topic || '课堂记录';
      downloadText(`${heading}.txt`,[heading,...rows.filter(row=>row.role!=='teacher' || row.request_count>0).map(row=>`${row.role==='teacher'?'教师':'小纸'}：${row.text}`)].join('\n\n'));
    }
    else if(a==='report-start')await loadReport(path().split('/').pop(),'POST');
    else if(a==='toggle-setting'){state.settingsDraft ||= {...state.settings,api_key:''};state.settingsDraft[button.dataset.field]=!state.settingsDraft[button.dataset.field];refresh();}
    else if(a==='save-settings')await saveSettings();
    else if(a==='cancel-settings'){state.settingsDraft={...state.settings,api_key:''};refresh();}
    else if(a==='test-connection'){
      const draft=state.settingsDraft;if(draft.testing)return;draft.testing=true;refresh();
      try{if(draft.api_key?.trim())throw new Error('请先保存新密钥，再测试连接');const result=await api('/api/settings/test','POST');draft.connectionMessage=result.message+'（仅检查连通与鉴权）';}
      finally{draft.testing=false;refresh();}
    }
    else if(a==='asr-refresh')await loadAsr();
    else if(a==='asr-open'){await api('/api/asr/open-folder','POST');notice('已请求打开模型目录');}
    else if(a==='asr-test-page')await navigate('/settings/asr?test=1');
    else if(a==='asr-test')await startTest();
    else if(a==='asr-stop')await stopTest();
    else if(a==='asr-clear'){state.test.texts=[];state.test.records=[];refresh();}
    else if(a==='asr-export')downloadText('语音识别测试.txt',state.test.texts.join('\n'));
    else if(a.startsWith('asr-') && id){
      await api(`/api/asr/models/${encodeURIComponent(id)}/${a.slice(4)}`,'POST');await loadAsr();
    }
  });
});
view.addEventListener('keydown',event=>{const target=event.target.closest('[role="button"][data-action]');if(target && (event.key==='Enter' || event.key===' ')){event.preventDefault();target.click();}});

async function boot() {
  try { [state.health,state.settings]=await Promise.all([api('/api/health'),api('/api/settings')]);state.settingsDraft={...state.settings,api_key:''}; }
  catch(error){state.notice=error.message;}
  await action(()=>navigate(location.pathname+location.search,false));
  healthTimer=setInterval(()=>void loadHealth().then(()=>{if(['/', '/create'].includes(path()))refresh();}).catch(()=>{}),8000);
}
void boot();
