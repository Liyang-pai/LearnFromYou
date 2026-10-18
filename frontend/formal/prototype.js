// 将唯一基准原型的模板绑定到真实业务；不加载原型模拟脚本。
import {reference,navIcons} from './reference.js';
import {recoveryTasks,retryPending,canRetry} from './service.js';
export const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export function template(id){const t=document.createElement('template');t.innerHTML=reference[id].body;return t.content;}
export function act(node,name,id){if(!node)return;node.dataset.action=name;if(id)node.dataset.id=id;node.removeAttribute('onclick');node.classList.remove('disabled');node.type='button';}
export function named(root,label){return [...root.querySelectorAll('button')].find(el=>el.textContent.trim()===label);}
export function setText(root,selector,value){const el=root.querySelector(selector);if(el)el.textContent=value??'';}
export function link(node,path){if(!node)return;const a=document.createElement('a');a.className=node.className;a.innerHTML=node.innerHTML;a.href=path;node.replaceWith(a);return a;}
export function shell(root,path){
  const top=root.querySelector('.topbar'),sidebar=root.querySelector('.sidebar'),brand=top?.querySelector('.brand');
  if(brand && sidebar){brand.querySelector('.top-divider')?.remove();brand.querySelector('.top-label')?.remove();setText(brand,'.brand-en','LearnFromYou');sidebar.prepend(brand);}
  const actions=root.querySelector('.page-head .row-actions');
  if(actions && top)for(const el of top.querySelectorAll('.top-status > *'))if(el.matches('button') || el.querySelector('b'))actions.append(el);
  top?.remove();
  sidebar?.querySelectorAll('.side-current').forEach(el=>el.remove());
  for(const item of root.querySelectorAll('.sidebar .nav-item')){
    const label=item.textContent.trim();
    if(label==='调试前端'){item.remove();continue;}
    const icon=item.querySelector('svg');if(icon && navIcons[label]){icon.innerHTML=navIcons[label];icon.setAttribute('stroke-width','1.8');icon.setAttribute('aria-hidden','true');}
    const route=label==='首页'?'/':label==='创建试讲'?'/create':label==='我的教室'?'/classroom':label==='设置'?'/settings':'/debug';
    const a=link(item,route);a.classList.toggle('active',route==='/' ? path==='/' || path.startsWith('/history') : route==='/settings'?path.startsWith('/settings'):path.startsWith(route));
  }
  return root;
}
export function html(root){const box=document.createElement('div');box.append(root);return box.innerHTML;}
export function confirmation(title,message,label,state){
  const settings=title.includes('未保存'),root=template(settings?'37':title.includes('画像')?'33':'18'),modal=root.querySelector('.small-modal');
  modal.querySelector('h2').id='confirm-title';modal.querySelector('h2').textContent=title;setText(modal,'.modal-header p',message);
  const close=modal.querySelector('.modal-header>svg');if(close){const b=document.createElement('button');b.type='submit';b.value='cancel';b.formNoValidate=true;b.className='btn ghost';b.setAttribute('aria-label','关闭');b.append(close.cloneNode(true));close.replaceWith(b);}
  setText(modal,'.modal-summary strong',settings?'设置有修改':state.setup?.topic || state.session?.lesson?.topic || '本次试讲');setText(modal,'.modal-summary p',settings?'保存设置不会自动发送模型请求。':'取消会保留当前内容。');
  const buttons=modal.querySelectorAll('.modal-footer button');buttons[0].value='cancel';buttons[0].type='submit';buttons[0].textContent=settings?'继续编辑':title.includes('画像')?'取消':'继续试讲';buttons[1].value='confirm';buttons[1].type='submit';buttons[1].textContent=label;
  const form=document.createElement('form');form.method='dialog';form.className=modal.className;form.innerHTML=modal.innerHTML;return form.outerHTML;
}
const subjectIcons={'语文':'chinese','数学':'math','英语':'english','物理':'physics','化学':'chemistry','生物':'biology','历史':'history','地理':'geography','道德与法治':'civics','科学':'science','信息科技':'computing'};
function recentCard(node,item){
  node.dataset.action='history-open';node.dataset.id=item.session_id;node.removeAttribute('data-history');
  const copy=node.querySelector('.recent-copy');setText(copy,'strong',item.topic);setText(copy,'span',[item.grade,item.subject].filter(Boolean).join(' · '));
  const date=item.started_at?new Date(item.started_at).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'时间未记录';setText(copy,'small','讲解时间：'+date);
  const icon=node.querySelector('.recent-icon');icon.innerHTML=`<img class="subject-icon" src="/static/formal/assets/subjects/${subjectIcons[item.subject]||'other'}.svg" alt="">`;
}
export function home(state,historyModal=false){
  const root=shell(template('01'),'/'),items=state.history.items,grid=root.querySelector('.recent-grid'),sample=grid.firstElementChild.cloneNode(true);
  grid.replaceChildren();for(const item of items.slice(0,3)){const node=sample.cloneNode(true);recentCard(node,item);grid.append(node);}
  if(!items.length){const p=document.createElement('p');p.className='hint';p.textContent=state.history.error || (state.history.loading || state.history.indexing?'正在整理课堂记录…':'还没有试讲记录，从一个新主题开始。');grid.append(p);}
  act(root.querySelector('.welcome-create'),'home-create');act(root.querySelector('[data-action="history-all"]'),'history-all');act(named(root,'检查设置'),'open-settings');
  const badges=root.querySelectorAll('.check-row .badge'),asr=state.health.asr_ready;
  badges[0].textContent=asr?'已选择模型':'待准备';badges[0].classList.toggle('green',!!asr);badges[0].classList.toggle('amber',!asr);
  badges[1].textContent=state.settings.model_configured?'已配置':'待配置';badges[1].classList.toggle('green',state.settings.model_configured);badges[1].classList.toggle('amber',!state.settings.model_configured);
  setText(root,'.home-bottom > .card:last-child .card-head p','当前配置状态');
  if(historyModal){
    const modal=document.createElement('div');modal.className='modal-backdrop';modal.innerHTML=`<section class="modal" role="dialog" aria-modal="true" aria-label="试讲历史"><div class="modal-header"><h2>试讲历史</h2><button class="icon" data-action="history-close" aria-label="关闭">×</button></div><div class="modal-body"><div class="history-list"></div></div><div class="modal-footer"></div></section>`;
    const list=modal.querySelector('.history-list');for(const item of items){const node=sample.cloneNode(true);recentCard(node,item);list.append(node);}
    if(!items.length)list.innerHTML=`<p class="hint">${escape(state.history.error || (state.history.loading?'正在读取课堂记录…':'还没有试讲记录。'))}</p>`;
    if(items.length<state.history.total)modal.querySelector('.modal-footer').innerHTML='<button class="btn" data-action="history-more">加载更多</button>';
    root.append(modal);
  }
  return html(root);
}
export function shellContent(content,path){const root=shell(template('01'),path),main=root.querySelector('.main');main.className='main';main.innerHTML=content;return html(root);}

function formMain(root,id){const main=root.querySelector('.main'),form=document.createElement('form');form.className=main.className;form.id=id;form.innerHTML=main.innerHTML;main.replaceWith(form);return form;}
function field(old,name,value,label,multiline=false,options=[]){
  const node=document.createElement(options.length?'select':multiline?'textarea':'input');node.className=old.className.replace('placeholder','');node.name=name;node.id='setup-'+name;node.setAttribute('aria-label',label);
  if(options.length)for(const option of [...new Set(['',...options,value].filter(v=>v!==undefined))]){const el=document.createElement('option');el.value=option;el.textContent=option||'由 AI 判断';node.append(el);}
  node.value=value||'';if(multiline)node.textContent=value||'';else if(options.length){for(const option of node.options)if(option.value===(value||''))option.setAttribute('selected','');}else node.setAttribute('value',value||'');old.replaceWith(node);return node;
}
export function create(state){
  const s=state.setup,busy=state.profileBusy || state.session.starting,root=shell(template(state.profileBusy?'31':state.profileError?'32':s.student_profile?'03':'02'),'/create');
  const main=formMain(root,'setup-form'),panel=main.querySelector('.profile-form');
  for(const el of root.querySelectorAll('.profile-form>.card-head>.badge,.profile-card>.card-head>.badge'))el.remove();
  const mapping=[['教学主题','topic'],['年级','grade'],['学科','subject'],['准备讲的知识点','points']];
  for(const group of panel.querySelectorAll('.field')){
    const key=mapping.find(([label])=>group.querySelector('label')?.textContent.startsWith(label))?.[1],old=group.querySelector('.input');if(!key || !old)continue;
    const options=key==='grade'?['小学 · 六年级','初中 · 七年级','初中 · 八年级','初中 · 九年级','高中 · 高一','大学 · 大一','成人零基础','自定义…']:key==='subject'?['语文','数学','英语','物理','化学','生物','历史','地理','道德与法治','科学','信息科技','其他（自定义）']:[];
    const node=field(old,key,s[key],mapping.find(row=>row[1]===key)[0],key==='points',options);node.disabled=!!busy;node.maxLength=key==='topic'?500:3000;node.required=key==='topic';node.placeholder=key==='topic'?'例如：惯性的概念':key==='points'?'选填：本次准备讲的知识点':'';
  }
  for(const [i,row] of [...panel.querySelectorAll('.segment-row')].entries()){
    const name=i===0?'student_level':'lesson_type',values=i===0?['weak','normal','strong']:['new','review','application'];
    for(const [j,old] of [...row.querySelectorAll('.segments span')].entries()){
      const b=document.createElement('span');b.setAttribute('role','button');b.tabIndex=0;b.textContent=old.textContent;b.dataset.action='setup-choice';b.dataset.field=name;b.dataset.value=values[j];b.classList.toggle('selected',s[name]===values[j]);b.setAttribute('aria-disabled',String(!!busy));old.replaceWith(b);
    }
  }
  const more=[...panel.querySelectorAll('.input')].find(el=>el.textContent.includes('更多设置'));
  if(more){const details=document.createElement('details');details.className='setup-more';details.innerHTML='<summary>更多设置<span class="hint">教材版本等</span></summary><div class="setup-more-content"><label for="setup-textbook">教材版本（选填）</label><input class="input" id="setup-textbook" name="textbook" maxlength="200" placeholder="例如：人教版 · 八年级物理"></div>';details.querySelector('input').setAttribute('value',s.textbook||'');more.replaceWith(details);}
  const previous=root.querySelector('.profile-card .profile-text,.profile-card .profile-empty');
  if(previous){const editor=document.createElement('textarea');editor.className='profile-text profile-inline';editor.id='setup-student_profile';editor.name='student_profile';editor.setAttribute('aria-label','学生画像');editor.maxLength=3000;editor.disabled=!!busy;editor.value=s.student_profile;editor.textContent=s.student_profile;editor.placeholder='在这里直接填写学生画像。也可以在左侧输入教学主题，建议同时选择学生水平与课型，再点击“生成学生画像”。';previous.replaceWith(editor);}
  root.querySelectorAll('.profile-card .card-head button').forEach(el=>el.remove());
  const generate=[...panel.querySelectorAll('button')].find(el=>/生成.*画像/.test(el.textContent));
  if(generate){act(generate,'generate-profile');generate.classList.add('profile-generate','large');generate.classList.toggle('primary',!!s.topic.trim());generate.disabled=!!busy || !s.topic.trim();setText(generate,'span',busy?'正在生成画像…':s.student_profile?'重新生成学生画像':'生成学生画像');
    const heading=panel.querySelector('.card-head'),scroll=document.createElement('div');scroll.className='setup-scroll';scroll.tabIndex=0;scroll.setAttribute('aria-label','教学设置表单');
    while(heading.nextElementSibling && heading.nextElementSibling!==generate)scroll.append(heading.nextElementSibling);heading.after(scroll);
    const pair=scroll.querySelector('.form-pair');if(pair)for(const row of scroll.querySelectorAll('.segment-row'))pair.before(row);
    const footer=document.createElement('div');footer.className='setup-actions';while(generate.nextElementSibling)footer.append(generate.nextElementSibling);footer.prepend(generate);panel.append(footer);
  }
  const confirm=named(root,'确认设定，创建试讲');act(confirm,'create');if(confirm){confirm.disabled=!!busy || !s.student_profile.trim();confirm.classList.toggle('primary',!confirm.disabled);}
  const toggle=root.querySelector('.profile-card .toggle');if(toggle){const input=document.createElement('button'),on=state.createReview??state.settings.asr_review;input.type='button';input.className='toggle prototype-toggle'+(on?' on':'');input.dataset.action='toggle-create-review';input.setAttribute('role','switch');input.setAttribute('aria-checked',String(on));input.setAttribute('aria-label','语音转文字审核');input.innerHTML='<i></i>';toggle.replaceWith(input);}
  const change=[...panel.querySelectorAll('.subtle-link')].find(el=>el.textContent.includes('更换模型'));
  if(change){
    change.dataset.action='open-asr';change.tabIndex=0;
    change.parentNode.classList.add('setup-asr-status');
    // 只更新模型入口旁的状态文本，不能覆盖导航容器或状态图标。
    const label=[...change.parentNode.childNodes].find(node=>node.nodeType===Node.TEXT_NODE && node.textContent.includes('语音识别已就绪'));
    if(label)label.textContent=state.health.asr_ready?' 语音识别已就绪 ':' 语音识别待准备 ';
  }
  if(state.profileError){const notice=root.querySelector('.notice');if(notice)notice.textContent=state.profileError;}
  return html(root);
}

const controls={mic:'<path d="M9 5a3 3 0 0 1 6 0v7a3 3 0 0 1-6 0zM5 10v2a7 7 0 0 0 14 0v-2M12 19v3m-4 0h8"/>',student:'<path d="M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2h-8l-6 4v-4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z"/><path d="M7 9h10M7 13h6"/>',sound:'<path d="M3 9h4l5-4v14l-5-4H3zM16 8a6 6 0 0 1 0 8M19 5a10 10 0 0 1 0 14"/>',sidebar:'<rect x="2" y="4" width="20" height="16" rx="3"/><path d="M15 4v16m-5-11 3 3-3 3"/>'};
const formatTime=seconds=>`${Math.floor((seconds||0)/60).toString().padStart(2,'0')}:${Math.floor((seconds||0)%60).toString().padStart(2,'0')}`;
function control(key,label,action,on,disabled){return `<button type="button" class="toolbar-control${on?'':' is-off'}" data-action="${action}" aria-pressed="${on}"${disabled?' disabled':''}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${controls[key]}${!on&&key!=='sidebar'?'<path class="icon-slash" d="m3 3 18 18"/>':''}</svg><span>${label}</span></button>`;}
function chatRows(rows){return rows.filter(row=>row.role!=='teacher' || row.request_count>0).map(row=>`<div class="chat-row${row.role==='teacher'?' teacher':''}"><div class="chat-avatar">${row.role==='teacher'?'我':'<img src="/static/formal/assets/小纸-用户原图.png" alt="小纸" style="width:32px;height:32px;object-fit:contain">'}</div><div class="chat-text">${row.role==='teacher'?`<small>${({submitted:'正在处理',processed:'已处理',failed:'处理失败'})[row.processing_status]||''}${row.request_count>1?' · 已重试':''}</small>`:''}${escape(row.text)}</div></div>`).join('');}
function recoveryNotice(s){
  const messages=[],live=s.active && !s.ending;
  if(s.error)messages.push(`<p>${escape(s.error)}</p>`);
  const resume=s.recovery.resume,knowledge=Object.values(s.recovery.knowledge);
  if(resume){
    if(resume.error)messages.push(`<p>恢复回应失败${live?'，可以重试':''}：${escape(resume.error)}</p>`);
    if(live && s.muted)messages.push('<p>学生发言已暂停，恢复发言后再处理回应。</p>');
    else if(live && resume.status==='running')messages.push('<p>正在恢复回应…</p>');
  }
  const errors=[...new Set(knowledge.map(task=>task.error).filter(Boolean))];
  if(errors.length)messages.push(`<p>相关结论尚未完成复核${live?'，课堂可以继续':''}：${errors.map(escape).join('；')}</p>`);
  if(live && knowledge.some(task=>task.status==='running'))messages.push('<p>正在复核相关结论，课堂可以继续。</p>');
  if(live && (s.batchRetryPending || recoveryTasks(s).some(task=>task.status==='queued')))messages.push('<p>已提交，等待处理</p>');
  const retryable=s.retryable || recoveryTasks(s).some(task=>task.retryable);
  if(!messages.length)return '';
  if(retryable && (!s.active || s.ending))messages.push(`<p>${s.ended?'课堂已结束':s.ending?'课堂正在结束':'连接已断开'}，未完成任务不能在本课堂重试。</p>`);
  return `<div class="notice amber" role="${s.error || errors.length || resume?.error?'alert':'status'}" data-recovery-notice><div>${messages.join('')}${retryable?`<button type="button" class="btn soft" data-action="retry-class"${canRetry(s)?'':' disabled'}>${live && retryPending(s)?'已提交，等待处理':'重试未完成任务'}</button>`:''}</div></div>`;
}
export function classroom(state){
  if(!state.session.id && !state.session.starting && state.session.createError){
    const s=state.session;
    return shellContent(`<div class="page-head"><div><h1>我的教室</h1><p>${escape(s.lesson.topic)}</p></div></div><section class="card"><h2>创建试讲失败</h2><p role="alert">${escape(s.createError)}</p><p>教学设置、学生画像和文字草稿已保留。返回创建页后，可以修改设置并重新创建。</p><button type="button" class="btn primary" data-action="back-create">返回创建页</button></section>`,'/classroom');
  }
  const s=state.session,has=!!s.id || s.starting,root=shell(template(has?s.error?(!s.active && !s.ended?'35':'34'):s.muted?'09':s.dialogue.some(r=>r.role==='student')?'08':s.mic?'06':'05':'04'),'/classroom');
  if(!has){act(named(root,'创建试讲'),'home-create');act(named(root,'创建一次试讲'),'home-create');return html(root);}
  setText(root,'.page-head p',[s.lesson.topic,[s.lesson.grade,s.lesson.subject].filter(Boolean).join(' · ')].filter(Boolean).join(' / '));
  for(const button of root.querySelectorAll('button')){const label=button.textContent.trim();if(label==='查看课堂详情')act(button,'class-details');if(label==='结束试讲'){act(button,'end-class');button.disabled=!s.active || s.ending;}if(label==='生成报告'){act(button,'generate-report');button.disabled=!s.active || s.ending;}}
  for(const clock of root.querySelectorAll('.page-head b'))clock.textContent=formatTime(s.elapsed);
  const reply=s.dialogue.filter(r=>r.role==='student').at(-1),bubble=root.querySelector('.speech-bubble');if(bubble){bubble.textContent=reply?.text || '我在听，你继续讲。';bubble.hidden=s.muted;}
  if(s.dialogue.length)setText(root,'.stage-top h2','和小纸一起，把问题讲清楚');
  setText(root,'.stage-note',s.status);setText(root,'.stage-top .badge',s.starting?'正在创建':s.mic?'麦克风已开启':s.ended?'已结束':'本次试讲');
  root.querySelector('.stage-center .character-art').dataset.stageStudent=s.id || 'starting';
  const center=root.querySelector('.stage-center'),wave=root.querySelector('.stage-center .wave') || document.createElement('div');
  wave.className='wave classroom-wave';wave.dataset.micWave='';wave.setAttribute('aria-hidden','true');
  wave.innerHTML='<i style="height:4px"></i>'.repeat(35);center.append(wave);
  const dock=root.querySelector('.control-dock');dock.classList.add('classroom-controls');const off=!s.active || s.ending;
  dock.innerHTML=control('mic',s.mic?'暂停麦克风':'开启麦克风','toggle-mic',s.mic,off || s.micPending || s.uploading || s.uncertain)+control('student',s.muted?'恢复学生发言':'暂停学生发言','toggle-muted',!s.muted,off)+control('sound','声音播报','toggle-tts',s.tts&&!s.muted,off || s.muted)+control('sidebar',s.sidebar?'收起侧栏':'展开侧栏','toggle-sidebar',s.sidebar,false);
  root.querySelector('.class-layout').classList.toggle('sidebar-collapsed',!s.sidebar);root.querySelector('.chat-panel').hidden=!s.sidebar;
  const summary=s.review.unclear[0] || s.review.tentative[0] || s.review.understood[0];
  setText(root,'.student-status .badge',s.muted?'暂不发言':s.speaking?'正在回应':s.mic?'正在听课':s.review.unclear.length?'有些困惑':'继续听课');setText(root,'.student-status p',s.muted?'小纸继续听课，暂时不发出回应。':summary || s.status);
  const chat=root.querySelector('.chat-body');chat.id='dialogue-list';chat.dataset.scroll='';chat.innerHTML=chatRows(s.dialogue) || '<div class="empty">从你的第一段讲解开始。可以输入文字，或开启麦克风。</div>';chat.style.overflowY='auto';
  const old=root.querySelector('.chat-input'),form=document.createElement('form');form.id='message-form';form.className='chat-input screenshot-composer';
  const chip=s.image?`<div class="screenshot-chip"><button type="button" class="screenshot-thumb" data-action="preview-image" aria-label="放大预览教学截图"><img src="${escape(s.image.url)}" alt="当前教学截图"></button><div class="chip-copy"><div class="chip-name">${escape(s.image.name || '当前教学截图')}</div><div class="chip-tip">当前教学截图 · 点击放大</div></div><button type="button" class="remove-shot" data-action="remove-image" aria-label="移除截图"${s.uploading?' disabled':''}>×</button></div>`:'';
  form.innerHTML=`<div class="composer-card">${chip}<div class="composer-line"><textarea class="input" id="teacher-draft" name="draft" aria-label="讲解或提问" placeholder="输入讲解或提问…" maxlength="5000"${off?' disabled':''}>${escape(s.draft)}</textarea><button type="button" class="add-shot" data-action="upload-image" aria-label="添加或更换截图"${off || s.uploading?' disabled':''}>＋</button><button class="btn primary" type="submit"${off || s.pending || s.uploading || s.uncertain?' disabled':''}><span>发送</span></button></div></div><div class="composer-help">${s.uploading?'正在上传截图，请稍候':'支持 Ctrl+V 粘贴截图'}</div>${s.uncertain?'<div class="composer-image-error">截图状态尚未确认。<button type="button" data-action="reconcile-image">核对当前图</button></div>':''}`;
  old.replaceWith(form);
  // Error templates contain preview-only warnings and an inert retry button.
  root.querySelectorAll('.notice').forEach(node=>node.remove());
  named(root,'重试这一段')?.remove();
  const notice=recoveryNotice(s);if(notice)root.querySelector('.page-head').insertAdjacentHTML('afterend',notice);
  if(s.micError){const backdrop=template('36').querySelector('.backdrop');backdrop.setAttribute('role','dialog');backdrop.setAttribute('aria-modal','true');setText(backdrop,'.modal-header p',s.micError);const buttons=backdrop.querySelectorAll('.modal-footer button');act(buttons[0],'dismiss-mic-error');act(buttons[1],'toggle-mic');const close=backdrop.querySelector('.modal-header>svg');if(close){const b=document.createElement('button');b.className='btn ghost';b.textContent='×';b.setAttribute('aria-label','关闭');act(b,'dismiss-mic-error');close.replaceWith(b);}root.append(backdrop);}
  return html(root);
}
export function details(state,archived){
  const s=archived?state.detail:state.session,rows=archived?s.items:s.dialogue,root=shell(template('10'),archived?'/history/'+s.id:'/classroom/details'),view=s.review||{},summary=root.querySelectorAll('.review-summary strong');
  summary[0].textContent=s.topic || s.lesson?.topic || '未记录';summary[1].textContent=formatTime(s.elapsed || Math.max(0,...rows.map(r=>r.elapsed||0)));summary[2].textContent=rows.filter(r=>r.role==='teacher'&&r.request_count>0).length+' 段';
  act(named(root,'返回教室'),archived?'history-all':'back-class');act(named(root,'导出记录'),'export-session');
  const holder=root.querySelector('[data-review-records]');holder.innerHTML=rows.length?rows.map(row=>`<article class="review-record"><div class="review-record-head"><time>${formatTime(row.elapsed)}</time><span>${row.role==='teacher'?row.request_count?'讲授批次':'尚未提交模型':'小纸回应'}</span></div><p>${escape(row.text)}</p>${row.role==='teacher'&&row.segments?.length?`<details class="review-original"><summary>查看识别原文</summary>${row.segments.map(seg=>`<p>${escape(seg.raw_text??'原文未记录')}</p>`).join('')}</details>`:''}</article>`).join(''):'<p class="hint">还没有讲解记录。返回教室开始讲解后，可以在这里回看。</p>';
  if(!archived && s.raw.length)holder.innerHTML+=`<details class="review-original"><summary>转写原文与审核记录</summary>${s.raw.map(row=>`<p>${escape(row.text || row.data?.segments?.map(seg=>seg.raw_text+' → '+seg.text).join('\n') || '')}</p>`).join('')}</details>`;
  if(archived && s.before!==null)holder.insertAdjacentHTML('afterbegin','<button class="btn ghost" data-action="history-earlier">加载更早的记录</button>');
  const cards=root.querySelectorAll('.understanding-card');const groups=[['understood','tentative'],['unclear'],['questions']];
  for(const [i,card] of [...cards].entries()){card.querySelectorAll('ul,p,.understanding-quote').forEach(el=>el.remove());const values=groups[i].flatMap(key=>(view[key]||[]).map(item=>key==='tentative'?'待确认：'+item:item));const content=document.createElement('ul');content.innerHTML=values.length?values.map(value=>`<li>${escape(value)}</li>`).join(''):'<li>暂时没有相关记录。</li>';card.append(content);}
  return html(root);
}
export function report(state,path){
  const r=state.report,id=path.split('/').pop(),ready=r.status==='ready' && r.report,root=shell(template(ready?'20':r.status==='generating' || r.loading?'19':'21'),'/classroom');
  setText(root,'.page-head p',state.session.id===id?state.session.lesson.topic:'本次课堂');
  for(const badge of root.querySelectorAll('.page-head .badge'))badge.textContent=ready?'报告已生成':r.status==='generating'?'正在生成':'等待生成';
  for(const b of root.querySelectorAll('button')){
    const text=b.textContent.trim();if(text==='导出 Markdown'){const a=link(b,`/api/teaching-report/${id}/export`);a.setAttribute('download','');}
    if(text==='导出日志'){const a=link(b,`/api/history/${id}/log`);a.setAttribute('download','');}
    if(/重新试讲|再来一次|再次试讲|再创建一次试讲/.test(text))act(b,'home-create');
    if(/返回|回看|查看课堂记录/.test(text))act(b,'report-back',id);
    if(/重试|重新生成/.test(text)){act(b,'report-start');b.disabled=!!r.loading;}
  }
  if(ready){
    const data=r.report;setText(root,'.report-main>h2','本次教学报告');setText(root,'.report-intro',data.overview?.text);
    const keys=['accuracy','clarity','coherence','checking'];for(const [i,node] of [...root.querySelectorAll('.dimension')].entries()){const d=data.dimensions[keys[i]];setText(node,'.badge',d.label);node.lastElementChild.textContent=d.reason;}
    const sections=root.querySelectorAll('.report-main .report-section');if(sections[1]){const ps=sections[1].querySelectorAll(':scope>p');if(ps[0])ps[0].textContent=data.finding.text;if(ps[1])ps[1].textContent='值得保留：'+(data.keep?.text || '本次材料不足，暂不单列。');}
    setText(root,'.report-highlight p',data.action.text);setText(root,'.practice-card .badge','约 '+data.practice.minutes+' 分钟');setText(root,'.practice-card .task',data.practice.task);
    const descriptions=root.querySelectorAll('.practice-card>p:not(.task)');for(const [i,p] of [...descriptions].entries())p.textContent=i===0?data.practice.task:data.practice.criterion;
  }else{
    const main=root.querySelector('.report-loading,.report-empty,.card');if(main){
      setText(main,':scope>p',r.error || r.progress || (r.loading?'正在读取报告…':'尚未生成报告，点击后才会调用模型。'));
      const warning=main.querySelector('.warning-box');if(warning){warning.textContent=r.error || '';warning.hidden=!r.error;}
      if(r.status!=='failed' && !r.error && !r.loading && r.status!=='generating')setText(main,'h2','尚未生成教学报告');
      if(r.status!=='generating' && !r.loading){let b=[...main.querySelectorAll('button')].find(el=>el.dataset.action==='report-start');if(!b){b=document.createElement('button');b.className='btn primary';main.append(b);}b.textContent=r.status==='failed'?'重试生成报告':'生成报告';act(b,'report-start');}
    }
    const progress=root.querySelector('.progress');if(progress){progress.setAttribute('role','progressbar');progress.setAttribute('aria-label','报告正在处理');progress.querySelector('i')?.removeAttribute('style');}
    setText(root,'.report-empty>.hint',r.error || r.progress || (r.loading?'正在读取报告…':'尚未生成报告。'));
  }
  return html(root);
}
function settingsTabs(root,asr=false){
  const tabs=root.querySelectorAll('.settings-tabs>*');
  for(const [i,node] of [...tabs].entries()){const a=link(node,i?'/settings/asr':'/settings');a.classList.toggle('active',asr?i===1:i===0);}
}
function settingToggle(node,value,field){
  if(!node)return;const button=document.createElement('button');button.type='button';button.className='toggle prototype-toggle'+(value?' on':'');button.innerHTML='<i></i>';button.setAttribute('role','switch');button.setAttribute('aria-checked',String(!!value));button.dataset.action='toggle-setting';button.dataset.field=field;button.setAttribute('aria-label',node.closest('.setting-row')?.querySelector('strong')?.textContent || field);node.replaceWith(button);
}
export function settings(state){
  const root=shell(template('22'),'/settings'),draft=state.settingsDraft || state.settings;
  settingsTabs(root);formMain(root,'settings-form');
  const placeholder=root.querySelector('.setting-form-row .input');const input=document.createElement('input');input.className='input';input.type='password';input.name='api_key';input.id='api-key';input.autocomplete='new-password';input.setAttribute('aria-label','API Key');input.setAttribute('value',draft.api_key || '');input.placeholder=state.settings.model_configured?'已配置，留空保留已有密钥':'填写 API Key';placeholder.replaceWith(input);
  setText(root,'.setting-form-row .hint','密钥仅保存在本机后端；留空保留已有密钥。');
  const toggles=root.querySelectorAll('.setting-row .toggle');settingToggle(toggles[0],draft.tts,'tts');settingToggle(toggles[1],draft.asr_review,'asr_review');
  act(named(root,'测试连接'),'test-connection');named(root,'测试连接').disabled=!!draft.testing;setText(root,'[data-connection-status]',draft.testing?'正在测试…':draft.connectionMessage || '尚未测试');
  act(named(root,'取消更改'),'cancel-settings');act(named(root,'保存设置'),'save-settings');named(root,'保存设置').disabled=!!draft.saving;
  const dirty=!!draft.api_key || ['tts','asr_review'].some(key=>draft[key]!==state.settings[key]);setText(root,'.savebar>span',draft.saving?'正在保存…':dirty?'有未保存的更改':draft.saveMessage || '设置已保存');
  return html(root);
}
const runningJob=model=>['downloading','verifying','preparing','cancelling'].includes(model.job?.state);
const modelState=model=>({available:'未安装',installed:'已安装',incomplete:'需校验修复',downloading:'下载中',verifying:'校验中',preparing:'准备中',failed:'操作失败',cancelled:'已取消',completed:'已完成'})[model.job?.state] || ({available:'未安装',installed:'已安装',incomplete:'需校验修复',failed:'操作失败'})[model.state] || model.state || '待检查';
const sizeText=value=>Number(value)>1024**3?(Number(value)/1024**3).toFixed(1)+' GiB':(Number(value||0)/1024**2).toFixed(0)+' MiB';
function testBinding(root,state){
  const t=state.test,a=state.asr,busy=a.busy || state.session.active || state.session.starting || a.models?.some(runningJob);
  const b=root.querySelector('.microphone-test button');act(b,t.active?'asr-stop':'asr-test');b.querySelector('span').textContent=t.active?'结束录音':t.starting?'等待授权…':'开始收音测试';b.disabled=!!t.stopping || !!t.starting || !t.active && (busy || !a.asr_ready);
  root.querySelector('.microphone-test .wave')?.classList.toggle('active',!!t.active);
  setText(root,'.microphone-test .hint',t.status || '只测试本机收音，不调用学生模型。');
}
export function asr(state,query=''){
  const params=new URLSearchParams(query),modelId=params.get('model');if(modelId)return modelDetails(state,modelId);if(params.has('test'))return recognitionTest(state);
  const a=state.asr,t=state.test,draft=state.settingsDraft || state.settings,models=a.models || [],root=shell(template('24'),'/settings/asr'),busy=a.busy || state.session.active || state.session.starting || t.active || t.starting || models.some(runningJob);
  settingsTabs(root,true);formMain(root,'settings-form');act(named(root,'刷新状态'),'asr-refresh');
  setText(root,'.settings-layout>section .card-head>.badge',busy?'资源占用中':a.asr_ready?'当前空闲':'待准备');
  const grid=root.querySelector('.model-grid'),sample=grid.firstElementChild.cloneNode(true);grid.replaceChildren();
  for(const model of models){const card=sample.cloneNode(true),selected=model.selected || a.selected_id===model.id;card.classList.toggle('selected',selected);const labels=card.querySelectorAll(':scope>div:first-child .badge');labels[0].textContent=model.label || '本机识别';labels[1].textContent='正在使用';labels[1].hidden=!selected;
    const title=card.querySelector('h3');title.innerHTML=`<a href="/settings/asr?model=${encodeURIComponent(model.id)}">${escape(model.name)}</a>`;setText(card,':scope>p',model.description);setText(card,'.model-footer .hint',modelState(model));
    const b=card.querySelector('button');act(b,runningJob(model)?'asr-cancel':model.installed?'asr-select':'asr-download',model.id);b.textContent=runningJob(model)?'取消':selected?'已选择':model.installed?'选择使用':model.state==='incomplete' || model.job?.state==='failed'?'下载修复':'下载模型';b.disabled=runningJob(model)?model.job.state==='cancelling':!!busy || selected || !model.installed&&!model.downloadable;
    if(runningJob(model)){const progress=document.createElement('div');progress.className='progress';const total=model.job.total_bytes,done=model.job.downloaded_bytes;progress.innerHTML=`<i style="width:${total?Math.min(100,done/total*100):0}%"></i>`;card.append(progress);const status=document.createElement('p');status.className='hint';status.textContent=total?sizeText(done)+' / '+sizeText(total):modelState(model);card.append(status);}
    if(model.job?.error){const p=document.createElement('p');p.className='hint';p.textContent=model.job.error;card.append(p);}grid.append(card);
  }
  if(!models.length)grid.innerHTML=`<p class="hint">${escape(a.error || (a.loading?'正在读取模型列表…':'暂无模型信息，请刷新。'))}</p>`;
  act(named(root,'打开文件夹'),'asr-open');act(named(root,'进入'),'asr-test-page');
  const device=root.querySelector('aside .field .input'),select=document.createElement('select');select.className='input';select.name='device_id';select.setAttribute('aria-label','输入设备');select.innerHTML='<option value="">系统默认麦克风</option>'+(state.devices||[]).map(item=>`<option value="${escape(item.deviceId)}"${state.deviceId===item.deviceId?' selected':''}>${escape(item.label || '麦克风')}</option>`).join('');device.replaceWith(select);
  settingToggle(root.querySelector('.toggle'),draft.asr_review,'asr_review');testBinding(root,state);act(named(root,'取消更改'),'cancel-settings');act(named(root,'保存设置'),'save-settings');setText(root,'.savebar>span',a.error || a.notice || draft.saveMessage || '设置已保存');return html(root);
}
function modelDetails(state,id){
  const a=state.asr,model=a.models?.find(item=>item.id===id),root=shell(template('38'),'/settings/asr');act(named(root,'返回模型列表'),'open-asr');
  if(!model){root.querySelector('.settings-layout').innerHTML='<section class="card"><p class="hint">'+escape(a.error || '正在读取模型详情…')+'</p></section>';return html(root);}
  setText(root,'.settings-layout>section h2',model.name);setText(root,'.settings-layout>section .card-head p',model.description);setText(root,'.settings-layout>section .card-head .badge',model.selected?'正在使用':modelState(model));setText(root,'.notice strong',modelState(model));setText(root,'.notice p',model.job?.error || a.error || '识别空闲时可以校验文件、修复下载或选择其他模型。');
  const values=[model.label,sizeText(model.size_bytes),model.directory,(model.files||[]).map(f=>f.name+' · '+({verified:'校验通过',present:'待校验',invalid:'校验失败',missing:'缺失'})[f.state]).join('；'),a.vad_ready?'Silero VAD · 已准备':'Silero VAD · 待准备',model.revision || '未记录'];for(const [i,node] of [...root.querySelectorAll('.version-table strong')].entries())node.textContent=values[i];
  const busy=a.busy || state.session.active || state.test.active || state.test.starting || a.models.some(runningJob);const verify=named(root,'校验 / 修复');act(verify,model.installed?'asr-verify':'asr-download',id);verify.disabled=!!busy || !model.installed&&!model.downloadable;
  const vad=named(root,'准备 VAD');act(vad,'asr-prepare-vad',id);vad.disabled=!!busy || a.vad_ready;act(named(root,'打开模型文件夹'),'asr-open');
  for(const [label,url] of [['模型来源',model.source],['权重许可',model.license_url]]){const b=named(root,label);if(/^https:\/\//.test(url || '')){const anchor=link(b,url);anchor.target='_blank';anchor.rel='noopener';}else b.disabled=true;}
  const aside=root.querySelector('.settings-layout>aside');aside.querySelectorAll('.record').forEach(el=>el.remove());for(const item of a.models.filter(m=>m.job?.state)){const record=document.createElement('div');record.className='record';record.innerHTML=`<div class="record-top"><strong>${escape(item.name)}</strong><span class="badge">${escape(modelState(item))}</span></div><p>${escape(item.job.error || modelState(item))}</p>`;aside.append(record);}if(!aside.querySelector('.record'))aside.insertAdjacentHTML('beforeend','<p class="hint">暂无安装或恢复任务。</p>');return html(root);
}
function recognitionTest(state){
  const root=shell(template('26'),'/settings/asr'),t=state.test,a=state.asr;act(named(root,'返回语音设置'),'open-asr');act(named(root,'导出结果'),'asr-export');named(root,'导出结果').disabled=!t.texts.length;act(named(root,'清空结果'),'asr-clear');
  setText(root,'.two-col>section:first-child .card-head p','当前模型：'+(a.models?.find(m=>m.selected)?.name || '待准备'));setText(root,'.two-col>section:first-child .card-head .badge',t.active?'正在识别':t.starting?'等待授权':'未开始');testBinding(root,state);
  const rows=root.querySelectorAll('.setting-row');setText(rows[0],'.badge',t.active?'已连接':'未连接');setText(rows[1],'.badge',t.active?'识别中':a.asr_ready?'已就绪':'待准备');
  const records=t.records || [],stats=root.querySelectorAll('.stats-strip b');stats[0].textContent=formatTime(records.reduce((sum,r)=>sum+(r.audio_seconds||0),0));stats[1].textContent=t.texts.length;stats[2].textContent=records.reduce((sum,r)=>sum+(r.asr_seconds||0),0).toFixed(2)+' s';
  const results=root.querySelector('.two-col>section:last-child');results.querySelectorAll('.record').forEach(el=>el.remove());for(const [i,text] of t.texts.entries()){const r=records[i]||{},node=document.createElement('div');node.className='record';node.innerHTML=`<div class="record-top"><span>麦克风转写 · 第 ${i+1} 段</span><small>音频 ${escape(r.audio_seconds??'—')} 秒 · 识别 ${escape(r.asr_seconds??'—')} 秒</small></div><p>${escape(text)}</p>`;results.append(node);}if(!t.texts.length)results.insertAdjacentHTML('beforeend','<p class="hint">开始收音后，转写结果会显示在这里。</p>');return html(root);
}
