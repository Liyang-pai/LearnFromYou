// 为设计画面接入内存中的表单、课堂和模拟任务；不采集音频，不访问模型或产品API。
'use strict';
(() => {
  const app=window.prototypeApp, state=app.state, frame=document.getElementById('canvas');
  const blankLesson=JSON.parse(JSON.stringify(state.lesson));
  Object.assign(state,{profileBusy:false,report:'idle',records:[],events:[],draftText:'',popup:null,returnScreen:'01',pendingRoute:null,faultNext:null});
  Object.assign(state.settings,{service:'DeepSeek',baseURL:'https://api.deepseek.com',textModel:'deepseek-chat',imageModel:'deepseek-flash'});
  let d,profileJob=0,replyJob=0,reportJob=0;
  const classScreens=new Set(['05','06','07','08','09','18','34','35','36']);
  const settingsScreens=new Set(['22','23','24','25','37','42']);
  const escape=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const toast=text=>{const el=document.getElementById('hostToast');el.textContent=text;el.classList.add('visible');clearTimeout(state.toastTimer);state.toastTimer=setTimeout(()=>el.classList.remove('visible'),3200);};
  const addEvent=(title,detail='')=>state.events.unshift({time:formatTime(state.session.elapsed),title,detail});
  function formatTime(seconds){return String(Math.floor(seconds/60)).padStart(2,'0')+':'+String(seconds%60).padStart(2,'0');}
  function route(id,force=false){
    if(!force&&state.dirty&&settingsScreens.has(app.screen)&&!settingsScreens.has(id)){state.pendingRoute=id;app.show('37');return;}
    app.show(id);
  }
  function classScreen(){return !state.session.active?'04':state.session.muted?'09':({ready:'05',listening:'06',thinking:'07',question:'08',error:'34',disconnected:'35'})[state.session.phase]||'05';}
  function close(){if(state.popup?.type==='image-preview'){window.prototypeExtensions?.closeImagePreview();return;}if(state.popup){state.popup=null;app.show(app.screen);return;}if(app.screen==='37'){state.pendingRoute=null;route('23',true);return;}route(state.returnScreen||classScreen(),true);}
  function seedLesson(){
    if(!state.lesson.topic)Object.assign(state.lesson,{topic:'惯性的概念',grade:'初中 · 八年级',subject:'物理',points:'惯性的含义；生活中的惯性现象'});
    if(!state.lesson.profile)state.lesson.profile=mockProfile();
  }
  function mockProfile(){const topic=state.lesson.topic||'本次主题';return topic.includes('惯性')?'已有知识：知道静止与运动是不同的状态，能描述常见运动现象。\n待学内容：尚未学习惯性的含义及生活中的惯性现象。\n可能的困难：容易把惯性理解成一种力。\n理解特点：借助生活实例，更容易建立概念联系。':`已有知识：具备${state.lesson.grade==='由 AI 判断'?'本次课程':state.lesson.grade}的一般学习基础。\n待学内容：尚未学习“${topic}”，不默认知道本节答案。\n可能的困难：需要把新概念与具体例子联系起来。\n理解特点：适合先观察例子，再用自己的话总结。\n（以上为静态模拟画像。）`;}
  function button(label,action,primary=false){return `<button class="btn ${primary?'primary':''}" data-action="${action}">${escape(label)}</button>`;}
  function popup(title,content,actions){
    state.popup={title,content,actions};
    const old=d.querySelector('[data-prototype-popup]');if(old)old.remove();
    const backdrop=d.createElement('div');backdrop.className='backdrop';backdrop.dataset.prototypePopup='true';
    backdrop.innerHTML=`<section class="modal small-modal" role="dialog" aria-modal="true" aria-label="${escape(title)}"><div class="modal-header"><div><h2>${escape(title)}</h2><p>静态交互演示 · 所有内容仅保存在当前页面内存中</p></div><button class="btn ghost" data-action="close" aria-label="关闭">×</button></div><div class="modal-body">${content}</div><div class="modal-footer">${actions||button('关闭','close')}</div></section>`;
    d.body.append(backdrop);backdrop.querySelector('input,textarea,button')?.focus();
  }
  function download(name,content,type='application/json'){
    const url=URL.createObjectURL(new Blob([content],{type}));const anchor=document.createElement('a');anchor.href=url;anchor.download=name;document.body.append(anchor);anchor.click();anchor.remove();setTimeout(()=>URL.revokeObjectURL(url),5000);toast('已导出模拟内容，不包含真实课堂数据。');
  }
  function newLesson(){profileJob++;state.profileBusy=false;Object.assign(state.lesson,blankLesson);state.returnScreen='02';route('02');}
  function generate(confirmed=false){
    if(state.profileBusy)return;
    if(!state.lesson.topic.trim()){toast('先填入教学主题，再生成模拟画像。');d.querySelector('[data-field="topic"]')?.focus();return;}
    if(state.lesson.profile&&!confirmed){state.returnScreen=app.screen;route('33');return;}
    state.profileBusy=true;const token=++profileJob;route('31');
    setTimeout(()=>{if(token!==profileJob)return;state.profileBusy=false;
      if(state.faultNext==='profile'){state.faultNext=null;if(app.screen==='31')route('32');toast('模拟画像生成失败，原有内容仍保留。');return;}
      if(state.lesson.grade==='由 AI 判断')state.lesson.grade='初中 · 八年级';if(state.lesson.subject==='由 AI 判断')state.lesson.subject=state.lesson.topic.includes('惯性')?'物理':'综合';
      if(!state.lesson.points)state.lesson.points=state.lesson.topic+'的概念与实例';state.lesson.profile=mockProfile();if(app.screen==='31')route('03');
    },850);
  }
  function startClass(){
    if(!state.lesson.topic.trim()||!state.lesson.profile.trim()){toast('请先填写主题并生成或手写学生画像。');return;}
    window.prototypeExtensions?.resetImage();
    const defaults=state.savedSettings||state.settings;
    const debug=app.screen==='29';replyJob++;Object.assign(state.session,{active:true,phase:'ready',mic:false,muted:!defaults.allowStudentSpeech,tts:defaults.tts,captions:defaults.captions,sidebarCollapsed:false,messages:[],elapsed:0});state.records=[];state.events=[];state.report='idle';state.draftText='';addEvent('模拟课堂已创建','麦克风仍未开启。');route(debug?'29':classScreen());
  }
  function replyText(){return state.lesson.topic.includes('惯性')?'老师，静止的物体也有惯性吗？可以再举个例子吗？':`老师，“${state.lesson.topic}”可以再用一个具体例子解释吗？`;}
  function sendText(text){
    text=(text||'').trim();if(!text||!state.session.active)return;
    state.session.elapsed+=18;state.session.messages.push({role:'teacher',text});state.records.push({time:formatTime(state.session.elapsed),raw:text,adopted:text,kind:'文字补充',review:'文字直通'});state.draftText='';state.session.phase='thinking';addEvent('模拟输入已记录',text);const token=++replyJob;
    if(classScreens.has(app.screen))route('07');
    setTimeout(()=>{if(token!==replyJob||!state.session.active)return;
      if(state.faultNext==='reply'){state.faultNext=null;state.session.phase='error';addEvent('模拟处理失败','待处理内容已保留，可重试。');if(classScreens.has(app.screen))route('34');return;}
      state.session.phase='question';if(!state.session.muted){state.session.messages.push({role:'student',text:replyText()});addEvent('模拟学生回应已发出',replyText());}else addEvent('模拟认知已更新','当前关闭学生发言，因此不追加回应。');
      if(classScreens.has(app.screen))route(classScreen());
    },850);
  }
  function toggleMic(){
    if(!state.session.active)return toast('先创建一次试讲。');
    if(state.faultNext==='mic'){state.faultNext=null;state.returnScreen=classScreen();route('36');return;}
    const debug=app.screen==='29';state.session.mic=!state.session.mic;state.session.phase=state.session.mic?'listening':'ready';addEvent(state.session.mic?'模拟麦克风开启':'模拟麦克风暂停','本原型从未申请麦克风权限。');route(debug?'29':classScreen());toast(state.session.mic?'模拟收音已开启；用“演示下一步”或文字补充继续。':'模拟收音已暂停。');
  }
  function finish(){
    replyJob++;Object.assign(state.session,{active:false,mic:false});state.report='generating';const token=++reportJob;route('19');
    setTimeout(()=>{if(token!==reportJob)return;state.report=state.faultNext==='report'?'failed':'ready';state.faultNext=null;if(['19','21'].includes(app.screen))route(state.report==='ready'?'20':'21');},900);
  }
  function showProfileEditor(){popup('编辑学生画像',`<textarea id="profileEditor" class="profile-manual" maxlength="3000" placeholder="填写学生已有知识、待学内容和可能的困难">${escape(state.lesson.profile)}</textarea>`,button('取消','close')+button('保存画像','save-profile',true));}
  function makeField(old,key,options){
    const field=d.createElement(options?'select':key==='points'?'textarea':'input');field.className=old.className.replace('placeholder','');field.dataset.field=key;
    if(options){for(const value of [...new Set([...options,state.lesson[key]])]){const option=d.createElement('option');option.value=value;option.textContent=value;field.append(option);}}
    field.value=state.lesson[key]||'';field.setAttribute('aria-label',key==='topic'?'教学主题':key==='points'?'准备讲的知识点':key==='grade'?'年级':'学科');if(key==='topic')field.placeholder='例如：惯性的概念';if(key==='points')field.placeholder='选填：本次准备讲的知识点';field.disabled=state.profileBusy;field.maxLength=key==='topic'?500:3000;old.replaceWith(field);
  }
  function syncProfileGenerateButton(){
    const generateButton=[...d.querySelectorAll('.profile-form button')].find(b=>/生成学生画像|正在生成画像/.test(b.textContent));
    if(!generateButton)return;
    const ready=Boolean(state.lesson.topic.trim())&&!state.profileBusy;
    generateButton.disabled=!ready;
    generateButton.classList.toggle('disabled',!ready);
    generateButton.classList.toggle('primary',ready);
    generateButton.classList.remove('soft');
    generateButton.classList.add('profile-generate');
  }
  function bindSetup(){
    const form=d.querySelector('.profile-form');if(!form)return;
    const fields=[['教学主题','topic'],['年级','grade'],['学科','subject'],['准备讲的知识点','points']];
    for(const el of form.querySelectorAll('.field')){const label=el.querySelector('label')?.textContent||'',key=fields.find(([t])=>label.startsWith(t))?.[1];const old=el.querySelector('.input');if(!key||!old)continue;
      makeField(old,key,key==='grade'?['由 AI 判断','小学 · 六年级','初中 · 七年级','初中 · 八年级','初中 · 九年级','高中 · 高一','大学 · 大一','成人零基础','自定义…']:key==='subject'?['由 AI 判断','语文','数学','英语','物理','化学','生物','历史','科学','信息科技','综合','自定义…']:null);
    }
    for(const row of form.querySelectorAll('.segment-row')){const key=row.querySelector('label').textContent==='学生水平'?'level':'type';for(const item of row.querySelectorAll('.segments span')){item.dataset.segment=key;item.classList.toggle('selected',item.textContent===state.lesson[key]);item.tabIndex=0;item.setAttribute('role','button');}}
    const profile=d.querySelector('.profile-text');if(profile){profile.replaceChildren();for(const text of (state.lesson.profile||'尚未填写画像。').split('\n')){const p=d.createElement('p');p.className='profile-block';p.textContent=text;profile.append(p);}}
    const heading=d.querySelector('.profile-card .card-head');if(heading&&!heading.querySelector('[data-action]'))heading.insertAdjacentHTML('beforeend',button(state.lesson.profile?'手动编辑':'手写画像','edit-profile'));
    syncProfileGenerateButton();
    const create=[...d.querySelectorAll('button')].find(b=>b.textContent.includes('确认设定，创建试讲'));if(create){create.disabled=!state.lesson.profile||state.profileBusy;create.classList.toggle('disabled',create.disabled);}
  }
  function bindClass(){
    if(!classScreens.has(app.screen))return;
    const head=d.querySelector('.page-head p');if(head)head.textContent=(state.lesson.topic||'惯性的概念')+' / '+state.lesson.grade+' · '+state.lesson.subject;
    const timer=d.querySelector('.session-action b');if(timer)timer.textContent=formatTime(state.session.elapsed);
    const bubble=d.querySelector('.speech-bubble');if(bubble){bubble.style.display=state.session.captions&&!state.session.muted?'':'none';if(state.session.messages.length){const latest=[...state.session.messages].reverse().find(m=>m.role==='student');if(latest&&state.session.phase==='question')bubble.textContent=latest.text;}}
    bindClassControls();
    const chat=d.querySelector('.chat-body');if(chat){chat.replaceChildren();if(!state.session.messages.length){const empty=d.createElement('div');empty.className='empty';empty.textContent='从你的第一段讲解开始。可以输入文字，或点击“演示下一步”。';chat.append(empty);}for(const message of state.session.messages){const row=d.createElement('div');row.className='chat-row'+(message.role==='teacher'?' teacher':'');const avatar=d.createElement('div');avatar.className='chat-avatar';avatar.textContent=message.role==='teacher'?'我':'AI';const text=d.createElement('div');text.className='chat-text';text.textContent=message.text;row.append(avatar,text);chat.append(row);}chat.style.overflowY='auto';chat.scrollTop=chat.scrollHeight;}
    const old=d.querySelector('.chat-input .input');if(old){const input=d.createElement('textarea');input.className='input';input.dataset.field='draftText';input.placeholder='输入讲解或提问（模拟）…';input.setAttribute('aria-label','讲解或提问');input.maxLength=5000;input.value=state.draftText;old.replaceWith(input);}
  }
  function applyClassSidebar(){
    const collapsed=state.session.sidebarCollapsed,layout=d.querySelector('.class-layout'),panel=layout?.querySelector('.chat-panel');
    if(!layout||!panel)return;
    layout.classList.toggle('sidebar-collapsed',collapsed);panel.id='classroomSidebar';panel.hidden=collapsed;
    const button=d.querySelector('[data-action="toggle-sidebar"]');if(button){button.setAttribute('aria-expanded',String(!collapsed));button.querySelector('span').textContent=collapsed?'展开侧栏':'收起侧栏';button.classList.toggle('is-off',collapsed);}
  }
  function bindClassControls(){
    const dock=d.querySelector('.control-dock');if(!dock)return;
    const paths={mic:'<path d="M9 5a3 3 0 0 1 6 0v7a3 3 0 0 1-6 0zM5 10v2a7 7 0 0 0 14 0v-2M12 19v3m-4 0h8"/>',student:'<path d="M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2h-8l-6 4v-4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z"/><path d="M7 9h10M7 13h6"/>',sound:'<path d="M3 9h4l5-4v14l-5-4H3zM16 8a6 6 0 0 1 0 8M19 5a10 10 0 0 1 0 14"/>',sidebar:'<rect x="2" y="4" width="20" height="16" rx="3"/><path d="M15 4v16m-5-11 3 3-3 3"/>'};
    const control=(key,label,action,on,disabled=false)=>`<button type="button" class="toolbar-control${on?'':' is-off'}" data-action="${action}" ${key==='sidebar'?'aria-controls="classroomSidebar"':`aria-pressed="${on}"`}${disabled?' disabled':''}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[key]}${!on&&key!=='sidebar'?'<path class="icon-slash" d="m3 3 18 18"/>':''}</svg><span>${label}</span></button>`;
    dock.classList.add('classroom-controls');
    dock.innerHTML=control('mic',state.session.mic?'暂停麦克风':'开启麦克风','mic',state.session.mic)+control('student',state.session.muted?'恢复学生发言':'暂停学生发言','toggle-student',!state.session.muted)+control('sound',state.session.muted?'声音播报':state.session.tts?'关闭声音':'开启声音','toggle-sound',state.session.tts&&!state.session.muted,state.session.muted)+control('sidebar','收起侧栏','toggle-sidebar',!state.session.sidebarCollapsed);
    applyClassSidebar();
    const status=d.querySelector('.student-status');if(status&&state.session.muted){status.querySelector('.badge').textContent='暂不发言';status.querySelector('p').textContent='小纸继续听课，暂时不发出回应。';}
    else if(status&&state.session.phase==='question'&&!state.session.tts){status.querySelector('.badge').textContent='文字回应';status.querySelector('p').textContent='小纸照常回复，声音播报已关闭。';}
  }
  function syncToggles(){
    for(const old of [...d.querySelectorAll('.toggle')]){const title=old.closest('.setting-row')?.querySelector('strong')?.textContent||'';const key=title.includes('允许学生')?'allowStudentSpeech':title.includes('播报')?'tts':title.includes('文字显示')?'captions':'review';const on=state.settings[key];const toggle=d.createElement('button');toggle.className='toggle prototype-toggle'+(on?' on':'');toggle.setAttribute('role','switch');toggle.setAttribute('aria-label',title||'语音转文字审核');toggle.setAttribute('aria-checked',String(on));toggle.dataset.toggle=key;toggle.innerHTML='<i></i>';old.replaceWith(toggle);}
  }
  function act(action,el){
    if(action==='close'){close();return;}
    if(action==='edit-profile'){showProfileEditor();return;}
    if(action==='save-profile'){const value=d.getElementById('profileEditor').value.trim();if(!value)return toast('画像不能为空。');state.lesson.profile=value;state.popup=null;route('03');return;}
    if(action==='mic'){if(state.session.active)state.classControlFocus=action;toggleMic();return;}
    if(action==='toggle-sidebar'){state.session.sidebarCollapsed=!state.session.sidebarCollapsed;applyClassSidebar();return;}
    if(action==='toggle-student'){state.session.muted=!state.session.muted;addEvent(state.session.muted?'关闭学生发言':'恢复学生发言','仅本次模拟课堂');state.classControlFocus=action;route(classScreen());return;}
    if(action==='toggle-sound'){if(state.session.muted)return;state.session.tts=!state.session.tts;state.classControlFocus=action;route(app.screen,true);return;}
    if(action==='save-textbook'){state.lesson.textbook=d.getElementById('textbookEditor').value.trim();state.popup=null;route('41');toast('教材版本已写入本次模拟设定。');return;}
    if(action==='save-custom-choice'){const value=d.getElementById('customChoice').value.trim();if(!value)return toast('请填写自定义内容。');state.lesson[state.customField]=value;state.popup=null;route(app.screen,true);return;}
    window.prototypeExtensions?.action(action,el);
  }
  function click(event){
    const el=event.target.closest('button,[data-action],.nav-item,.settings-tabs span,.detail-tabs span,.segments span,.dock-item,.input,.thumb-actions span,.modal-header>.icon,.subtle-link,.chat-tabs span');if(!el)return;
    const text=el.textContent.trim();let handled=true;
    if(el.disabled)return;
    if(el.dataset.action)act(el.dataset.action,el);
    else if(el.dataset.toggle){const key=el.dataset.toggle;state.settings[key]=!state.settings[key];if(settingsScreens.has(app.screen))state.dirty=true;route(app.screen,true);}
    else if(el.dataset.segment){state.lesson[el.dataset.segment]=text;route(app.screen,true);}
    else if(el.matches('.nav-item'))route(text==='首页'?'01':text==='创建试讲'?(state.lesson.profile?'03':'02'):text==='我的教室'?classScreen():text==='设置'?'22':'29');
    else if(el.matches('.settings-tabs span'))route(['22','24'][[...el.parentElement.children].indexOf(el)]);
    else if(el.matches('.main>.detail-tabs span'))route((['29','30'].includes(app.screen)?['29','30']:['10','11','12','13'])[[...el.parentElement.children].indexOf(el)]);
    else if(el.matches('.chat-tabs span'))route(['08','10','11','13'][[...el.parentElement.children].indexOf(el)]);
    else if(['创建一次试讲','再创建一次试讲'].includes(text))newLesson();
    else if(text.includes('生成学生画像')||text==='确认重新生成')generate(text==='确认重新生成');
    else if(text.includes('正在生成画像')||text.includes('正在处理画像'))toast('模拟画像正在生成，请稍候。');
    else if(text.includes('确认设定，创建试讲')||text==='创建试讲')startClass();
    else if(['手写画像','编辑画像'].includes(text))showProfileEditor();
    else if(text==='发送')sendText(state.draftText);
    else if(text==='结束试讲'||text==='生成报告'||text==='报告'){state.returnScreen=classScreen();if(state.session.active)route('18');else if(state.report!=='idle')route(state.report==='ready'?'20':state.report==='failed'?'21':'19');else toast('请先创建一次试讲，再生成报告。');}
    else if(text==='结束并生成报告'||text==='结束并查看报告'||text==='重试生成报告')finish();
    else if(['继续试讲','返回教室','返回课堂'].includes(text))route(classScreen(),true);
    else if(app.screen==='29'&&text==='停止播报'){addEvent('模拟播报已停止');toast('已停止模拟播报。本原型不播放语音。');}
    else if(text==='开启麦克风'||text==='暂停麦克风')toggleMic();
    else if(text==='完成')route(classScreen(),true);
    else if(text==='保留当前画像')close();
    else if(text==='查看课堂详情'||text==='查看课堂记录')route('10');
    else if(text==='更换模型'||text==='检查设置')route('24');
    else if(text.includes('更多设置')||text.startsWith('教材版本：'))popup('更多教学设置',`<label for="textbookEditor">教材版本（选填）</label><input id="textbookEditor" class="input" value="${escape(state.lesson.textbook)}" maxlength="200" placeholder="例如：人教版 · 八年级物理">`,button('取消','close')+button('保存','save-textbook',true));
    else if(text==='重试这一段'){state.session.phase='thinking';sendText(state.session.messages.filter(m=>m.role==='teacher').at(-1)?.text||'重新解释本次概念。');}
    else if(text==='使用文字讲授'){state.session.phase='ready';route('05');}
    else if(text==='重新尝试')toggleMic();
    else handled=extraClick(el,text);
    if(handled){event.preventDefault();event.stopImmediatePropagation();}
  }
  function extraClick(el,text){return window.prototypeExtensions?.click(el,text)||false;}
  function bind(){
    d=frame.contentDocument;if(!d?.body)return;
    bindSetup();bindClass();syncToggles();
    window.prototypeExtensions?.hydrate(d);
    for(const field of d.querySelectorAll('[data-field]')){const key=field.dataset.field,previousValue=state.lesson[key];field.addEventListener('input',()=>{if(key==='draftText')state.draftText=field.value;else state.lesson[key]=field.value;if(key==='topic')syncProfileGenerateButton();});if(field.tagName==='SELECT')field.addEventListener('change',()=>{if(field.value!=='自定义…')return;state.customField=key;state.lesson[key]=previousValue;popup(key==='grade'?'自定义年级':'自定义学科','<input id="customChoice" class="input" maxlength="100" aria-label="自定义内容" placeholder="填写你的教学场景">',button('取消','close')+button('保存','save-custom-choice',true));});}
    d.addEventListener('click',click,true);
    d.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();close();}if(e.key==='Enter'&&!e.shiftKey&&e.target.dataset.field==='draftText'){e.preventDefault();sendText(state.draftText);}if((e.key==='Enter'||e.key===' ')&&e.target.matches('[role=button]')){e.preventDefault();e.stopImmediatePropagation();e.target.dispatchEvent(new d.defaultView.MouseEvent('click',{bubbles:true,cancelable:true}));}},true);
    for(const el of d.querySelectorAll('.settings-tabs span,.detail-tabs span,[data-action],.thumb-actions span:not(:first-child),.dock-item')){if(el.tagName!=='BUTTON'){el.tabIndex=0;el.setAttribute('role','button');}}
    for(const el of d.querySelectorAll('.modal-header>.icon')){el.onclick=null;el.dataset.action='close';}
    d.body.dataset.prototypeReady=app.screen;
    if(state.classControlFocus){d.querySelector(`[data-action="${state.classControlFocus}"]`)?.focus();state.classControlFocus=null;}
  }
  frame.addEventListener('load',bind);
  document.getElementById('simulate').onclick=()=>{if(['19','21'].includes(app.screen)){finish();return;}if(['31','32'].includes(app.screen)){seedLesson();generate(true);return;}if(!state.session.active){seedLesson();startClass();return;}if(state.session.phase==='ready'){toggleMic();return;}if(state.session.phase==='listening'){sendText(state.lesson.topic.includes('惯性')?'物体具有保持原有运动状态的性质，这就是惯性。':'我们先用一个例子解释'+state.lesson.topic+'。');return;}state.session.phase='question';route(classScreen());};
  document.getElementById('fault').onclick=()=>{popup('选择一个异常演示',`<p class="hint">只改变本地模拟状态，不触碰真实服务。</p><div class="prototype-modal-text">${button('画像生成失败','fault-profile')}${button('课堂处理失败','fault-reply')}${button('报告生成失败','fault-report')}${button('截图上传失败','fault-image')}${button('麦克风权限受限','fault-mic')}${button('连接中断','fault-disconnect')}</div>`);};
  const scene=document.getElementById('scene');scene.onchange=()=>{state.popup=null;state.faultNext=null;const id=scene.value;if(!['01','02','04','31'].includes(id)){seedLesson();if(classScreens.has(id)){state.session.active=true;state.session.muted=id==='09';state.session.mic=['06','07','08','09'].includes(id);state.session.phase=({'05':'ready','06':'listening','07':'thinking','08':'question','09':'question','34':'error','35':'disconnected'})[id]||'question';}}state.returnScreen=classScreen();route(id,true);};
  window.prototypeInteractions={app,route,state,toast,popup,download,addEvent,close,seedLesson,formatTime,classScreen,startClass,generate,finish,sendText,get document(){return d;}};
})();
