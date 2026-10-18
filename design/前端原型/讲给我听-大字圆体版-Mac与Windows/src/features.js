// 静态原型的本地图片、设置、识别/下载演示、详情和导出；全部使用模拟结果。
'use strict';
(() => {
  const api=window.prototypeInteractions,{app,state,route,toast,popup,download,addEvent,close}=api;
  const escape=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let d,downloadToken=0,asrToken=0,imageJob=0,previewFocus,focusImagePicker=false;
  const modelNames=['Paraformer 中文完整版','SenseVoice Small','Whisper Small','Whisper Large-v3 Turbo'];
  Object.assign(state,{image:null,imageDraft:null,uploadFailure:false,model:modelNames[0],installed:[...modelNames.slice(0,2)],modelDownload:null,asr:{running:false,records:[]},connection:{text:'未测试',image:'未测试'},reviewDone:false,debugTab:'学生状态',eventFilter:'全部',modelMaintenance:{},vadReady:false});
  state.savedSettings={...state.settings};
  const btn=(text,action,primary=false)=>`<button class="btn ${primary?'primary':''}" data-action="${action}">${escape(text)}</button>`;
  const rerender=()=>app.show(app.screen);
  async function addImage(file){
    if(!state.session.active)return toast('请先创建模拟课堂，再选择教学截图。');
    if(!file)return;const token=++imageJob;if(file.size>5*1024*1024||!['image/png','image/jpeg','image/webp'].includes(file.type))return toast('请选择不超过 5 MiB 的 PNG、JPEG 或 WebP 图片。');
    const url=URL.createObjectURL(file),image=new Image();image.src=url;
    try{await image.decode();}catch{URL.revokeObjectURL(url);if(token===imageJob&&state.session.active){state.uploadFailure=true;rerender();toast('图片无法读取，当前截图仍保留，请重新选择。');}return;}
    if(token!==imageJob||!state.session.active){URL.revokeObjectURL(url);return;}
    if(state.faultNext==='image'){state.faultNext=null;URL.revokeObjectURL(url);state.uploadFailure=true;rerender();return;}
    if(state.image?.url)URL.revokeObjectURL(state.image.url);
    state.image={name:file.name,size:file.size,type:file.type,url};state.imageDraft=null;state.uploadFailure=false;addEvent('模拟截图已添加',file.name);rerender();
  }
  function pickImage(){if(!state.session.active)return toast('请先创建模拟课堂。');const input=document.getElementById('imagePicker');input.value='';input.click();}
  document.getElementById('imagePicker').onchange=e=>{const file=e.target.files[0];e.target.value='';addImage(file);};
  function resetImage(){
    imageJob++;focusImagePicker=false;if(state.image?.url)URL.revokeObjectURL(state.image.url);state.image=null;state.imageDraft=null;state.uploadFailure=false;
  }
  function removeImage(){
    resetImage();state.popup=null;focusImagePicker=true;addEvent('模拟截图已移除');rerender();
  }
  function closeImagePreview(){
    state.popup=null;d.querySelector('.image-lightbox')?.remove();const background=d.querySelector('.app');if(background)background.inert=false;
    if(previewFocus?.isConnected)previewFocus.focus();else d.querySelector('[data-action="view-image"]')?.focus();
  }
  function showImagePreview(){
    if(!state.image?.url||d.querySelector('.image-lightbox'))return;
    previewFocus=d.activeElement;state.popup={type:'image-preview'};
    const backdrop=d.createElement('div');backdrop.className='image-lightbox';
    backdrop.innerHTML=`<section class="image-preview" role="dialog" aria-modal="true" aria-label="教学截图放大预览"><div class="image-preview-head"><span>${escape(state.image.name)}</span><button type="button" class="image-preview-close" data-action="close-image-preview" aria-label="关闭图片预览">×</button></div><img alt="放大的教学截图"></section>`;
    backdrop.querySelector('img').src=state.image.url;backdrop.addEventListener('click',e=>{if(e.target===backdrop)closeImagePreview();});
    backdrop.addEventListener('keydown',e=>{if(e.key==='Tab'){e.preventDefault();backdrop.querySelector('button').focus();}});
    d.body.append(backdrop);const background=d.querySelector('.app');if(background)background.inert=true;backdrop.querySelector('button').focus();
  }
  function setSettingsFields(){
    for(const row of d.querySelectorAll('.setting-form-row')){
      const label=row.querySelector('label')?.textContent.trim();const map={'模型服务':'service','接口地址':'baseURL','API Key':'demoKey','文字模型':'textModel','图片模型':'imageModel'},key=map[label];const old=row.querySelector('.input');if(!key||!old)continue;
      const input=d.createElement(key==='service'?'select':'input');input.className='input';input.setAttribute('aria-label',label);
      if(key==='service'){for(const value of ['DeepSeek','自定义兼容接口']){const option=d.createElement('option');option.textContent=value;option.value=value;input.append(option);}}
      if(key==='demoKey'){input.type='password';input.placeholder='演示占位，请勿填写真实密钥';input.autocomplete='off';input.value=state.settings.demoKey||'';}
      else input.value=state.settings[key];
      old.replaceWith(input);
      input.addEventListener('input',()=>{state.settings[key]=input.value;state.dirty=true;if(key==='demoKey'){state.connection.text='未测试';state.connection.image='未测试';const result=d.querySelector('[data-connection-status]');if(result)result.textContent='尚未测试';}const status=d.querySelector('.savebar>span');if(status)status.textContent='有未保存的模拟更改';});
      if(key==='service')input.addEventListener('change',()=>route(input.value==='DeepSeek'?'22':'23',true));
    }
    const connection=d.querySelector('[data-connection-status]');if(connection)connection.textContent=state.connection.text==='模拟通过'&&state.connection.image==='模拟通过'?'模拟连接成功':'尚未测试';
    const label=d.querySelector('.savebar>span');if(label)label.textContent=state.dirty?'有未保存的模拟更改':'模拟设置已保存';
    const lines=d.querySelectorAll('.status-line');if(lines.length>=3&&['22','23'].includes(app.screen)){lines[0].querySelector('.badge').textContent=state.dirty?'未保存':'已保存';lines[1].querySelector('.badge').textContent=state.connection.text;lines[2].querySelector('.badge').textContent=state.connection.image;}
  }
  function saveSettings(){
    if(!state.settings.baseURL.trim()||!state.settings.textModel.trim()){toast('请填入示例接口地址与文字模型名。');return;}
    state.savedSettings={...state.settings};state.dirty=false;state.popup=null;const next=state.pendingRoute;state.pendingRoute=null;route(next||'22',true);toast('模拟设置已保存到内存；没有写文件或发送连接请求。');
  }
  function startDownload(name){
    if(state.session.active)return toast('请先结束模拟课堂，再演示模型下载或切换。');
    if(state.modelDownload)return toast('已有模拟下载进行中，可先取消。');
    state.modelDownload={name,pct:0};const token=++downloadToken;route('25',true);
    function tick(){if(token!==downloadToken||!state.modelDownload)return;state.modelDownload.pct+=20;if(state.modelDownload.pct>=100){state.installed.push(name);state.modelDownload=null;addEvent('模拟模型下载完成',name);if(['24','25'].includes(app.screen))route('24',true);toast('模拟下载完成；没有下载真实模型。');return;}if(['24','25'].includes(app.screen))rerender();setTimeout(tick,350);}setTimeout(tick,350);
  }
  function hydrateModels(){
    for(const card of d.querySelectorAll('.model-card')){
      const name=card.querySelector('h3')?.textContent.trim();if(!name||!modelNames.includes(name))continue;
      const selected=state.model===name,installed=state.installed.includes(name),active=state.modelDownload?.name===name;
      card.classList.toggle('selected',selected);const footer=card.querySelector('.model-footer');if(!footer)continue;
      footer.replaceChildren();const status=d.createElement('span');status.className='hint';status.textContent=active?'模拟进度 '+state.modelDownload.pct+'%':selected?'正在使用 · 模拟':installed?'已安装 · 模拟':'未安装 · 模拟';
      const action=d.createElement('button');action.className='btn'+(selected?' soft':'');action.textContent=active?'取消下载':selected?'已选择':installed?'选择使用':'下载模型';action.dataset.model=name;footer.append(status,action);
      const progress=card.querySelector('.progress');if(progress){progress.style.display=active?'':'none';if(active)progress.querySelector('i').style.width=state.modelDownload.pct+'%';}
      if(!card.querySelector('[data-action="model-details"]')){const details=d.createElement('button');details.className='btn ghost model-meta-link';details.style.height='24px';details.textContent='详情 / 来源与许可';details.dataset.action='model-details';details.dataset.model=name;card.append(details);}
    }
  }
  function startAsr(){
    if(state.session.active)return toast('请先结束正在进行的模拟课堂。');
    state.asr.running=true;const token=++asrToken;rerender();
    const lines=['我们今天来认识惯性。','物体具有保持原有运动状态的性质。','公交车突然刹车时，人会向前倾。'];
    function append(index){if(token!==asrToken||!state.asr.running||index>=lines.length)return;state.asr.records.push({text:lines[index],seconds:0.54,index:state.asr.records.length+1});if(app.screen==='26')rerender();setTimeout(()=>append(index+1),650);}setTimeout(()=>append(0),550);
  }
  function hydrateAsr(){
    if(app.screen!=='26')return;
    const start=[...d.querySelectorAll('button')].find(b=>['结束录音','开始录音测试'].includes(b.textContent.trim()));if(start){start.textContent=state.asr.running?'结束录音':'开始录音测试';start.dataset.action='asr-toggle';}
    const records=d.querySelectorAll('.main .two-col>.card:last-child .record');for(const record of records)record.remove();
    const holder=d.querySelector('.main .two-col>.card:last-child');if(holder){for(const record of state.asr.records){const row=d.createElement('div');row.className='record';const heading=d.createElement('div');heading.className='record-top';heading.textContent='第 '+record.index+' 段 · 模拟识别 '+record.seconds+'秒';const text=d.createElement('p');text.textContent=record.text;row.append(heading,text);holder.append(row);}if(!state.asr.records.length){const empty=d.createElement('p');empty.className='hint';empty.textContent='尚无模拟转写。点击“开始录音测试”演示。';holder.append(empty);}}
    const values=d.querySelectorAll('.stats-strip b');if(values.length>=3){values[0].textContent=api.formatTime(state.asr.records.length*8);values[1].textContent=state.asr.records.length;values[2].textContent=(state.asr.records.length*.54).toFixed(2)+' s';}
  }
  function hydrateImage(){
    const composer=d.querySelector('.chat-input');
    if(composer){
      composer.classList.add('screenshot-composer');
      const line=d.createElement('div');line.className='composer-line';
      const input=composer.querySelector('textarea'),send=composer.querySelector('button');
      const add=d.createElement('button');add.type='button';add.className='add-shot';add.dataset.action='pick-image';add.setAttribute('aria-label','添加或更换截图');add.title='添加或更换截图';add.textContent='＋';
      line.append(input,add,send);const card=d.createElement('div');card.className='composer-card';
      if(state.image?.url){
        const chip=d.createElement('div');chip.className='screenshot-chip';
        chip.innerHTML=`<button type="button" class="screenshot-thumb" data-action="view-image" aria-label="放大预览教学截图"><img alt="当前教学截图"></button><div class="chip-copy"><div class="chip-name">${escape(state.image.name)}</div><div class="chip-tip">当前教学截图 · 点击放大</div></div><button type="button" class="remove-shot" data-action="remove-image" aria-label="移除截图" title="移除截图">×</button>`;
        chip.querySelector('img').src=state.image.url;card.append(chip);
      }
      card.append(line);const hint=d.createElement('div');hint.className='composer-help';hint.textContent='支持 Ctrl+V 粘贴截图';composer.replaceChildren(card,hint);
      if(state.uploadFailure){const error=d.createElement('div');error.className='composer-image-error';error.setAttribute('role','alert');error.innerHTML='图片未能添加，当前截图仍保留。 <button type="button" data-action="pick-image">重新选择</button>';composer.append(error);}
      if(focusImagePicker){add.focus();focusImagePicker=false;}
    }
    if(state.popup?.type==='image-preview')showImagePreview();
  }
  function hydrateClassroomReview(){
    const topic=state.lesson.topic||'惯性的概念';
    const demo=topic.includes('惯性')&&!state.session.active&&!state.records.length&&!state.events.length&&state.report==='idle';
    const examples=[{time:'00:52',kind:'语音讲解',raw:'物体具有保持原有运动状态的性质。'},{time:'01:18',kind:'文字补充',raw:'这里说的是一种性质，不是让物体继续运动的一种力。'},{time:'02:06',kind:'语音讲解',raw:'公交车突然刹车时，人会向前倾。车停了，人仍有保持原来运动状态的性质。'},{time:'03:12',kind:'文字补充',raw:'我们再想一想：原来静止的物体，会不会也有这种性质？'}];
    const records=demo?examples:state.records,summary=d.querySelectorAll('.review-summary strong');
    summary[0].textContent=topic;summary[1].textContent=demo?'03:24':api.formatTime(state.session.elapsed);summary[2].textContent=records.length+' 段';
    if(demo)d.querySelector('.page-head p').textContent+='（示例内容）';
    const holder=d.querySelector('[data-review-records]');
    if(!records.length){const empty=d.createElement('p');empty.className='hint';empty.textContent='还没有讲解记录。返回教室开始讲解后，可以在这里回看。';holder.append(empty);}
    for(const record of records){
      const row=d.createElement('article');row.className='review-record';
      row.innerHTML=`<div class="review-record-head"><time>${escape(record.time)}</time><span>${escape(record.kind||'讲解')}</span></div><p>${escape(record.adopted||record.raw)}</p>`;
      if(record.kind!=='文字补充'){const original=d.createElement('details');original.className='review-original';original.innerHTML='<summary>查看识别原文</summary><p>'+escape(record.raw)+'</p>';row.append(original);}
      holder.append(row);
    }
    if(!demo){
      const questions=[...new Set(replySummary())],cards=d.querySelectorAll('.understanding-card');
      cards[0].innerHTML='<h3>小纸目前的理解</h3><p>暂无可核对的理解记录。</p>';
      cards[1].innerHTML='<h3>还没有弄清的地方</h3><p>'+(questions.length?'目前有 '+questions.length+' 条课堂提问，等待进一步讲解与确认。':'暂未记录到需要进一步确认的疑问。')+'</p>';
      cards[2].innerHTML='<h3>小纸提出的问题</h3>'+(questions.length?questions.map(text=>'<p class="understanding-quote">“'+escape(text)+'”</p>').join(''):'<p>暂无课堂提问。</p>');
    }
  }
  function hydrateDetails(){
    if(app.screen==='10')hydrateClassroomReview();
    for(const el of d.querySelectorAll('.knowledge-card'))if(el.textContent.includes('知识修订历史'))el.dataset.action='knowledge-history';
    for(const el of d.querySelectorAll('.blue-text'))if(el.textContent.includes('展开全部')){el.dataset.action='full-request';el.tabIndex=0;el.setAttribute('role','button');}
    if(['11','12','13','39'].includes(app.screen)&&state.records.length){const stat=d.querySelectorAll('.stats-strip b');if(stat.length>=3){stat[0].textContent=state.records.length;stat[1].textContent=state.records.length;stat[2].textContent=state.records.length+' 次';}}
    if(app.screen==='13'&&state.events.length){const column=d.querySelector('.main>.two-col>section');if(column){column.querySelectorAll('.activity-row').forEach(e=>e.remove());for(const item of state.events.slice(0,6)){const row=d.createElement('div');row.className='activity-row';row.innerHTML=`<span class="time">${escape(item.time)}</span><i class="timeline-dot"></i><div><strong>${escape(item.title)}</strong><p>${escape(item.detail)}</p></div>`;column.append(row);}}}
    if(state.reviewDone&&['11','40'].includes(app.screen)){for(const notice of d.querySelectorAll('.notice.amber')){notice.className='notice green';notice.textContent='模拟复核已完成；未请求模型。';}}
    if(['19','20','21'].includes(app.screen)){const p=d.querySelector('.page-head p');if(p)p.textContent=(state.lesson.topic||'惯性的概念')+' / 示例报告 · 不是真实评价';}
  }
  function action(name,el){
    if(name==='history-all'){
      const cards=[...d.querySelectorAll('.recent-lesson')].map(card=>card.outerHTML).join('');
      popup('试讲历史','<div class="history-list">'+cards+'</div>');return;
    }
    if(name==='history-preview'){
      const records=[['惯性的概念','初中 · 物理','昨天 19:23'],['一次函数','初中 · 数学','10月14日'],['链表的基本操作','大学 · 数据结构','10月10日']];
      const record=records[Number(el.dataset.history)];if(!record)return;
      popup(record[0],'<p>'+escape(record[1])+'</p><p class="hint">讲解时间：'+escape(record[2])+'</p><p class="hint">静态原型示例记录。</p>');return;
    }

    if(name.startsWith('fault-')){
      state.popup=null;const kind=name.slice(6);
      if(kind==='disconnect'){api.seedLesson();state.session.active=true;state.session.phase='disconnected';route('35',true);return;}
      state.faultNext=kind;
      if(kind==='profile'){api.seedLesson();api.generate(true);return;}
      if(kind==='report'){api.finish();return;}
      if(kind==='reply'){api.seedLesson();state.session.active=true;api.sendText('请重新解释本次概念。');return;}
      if(kind==='mic'){api.seedLesson();state.session.active=true;state.returnScreen='05';route('36',true);return;}
      if(kind==='image'){state.faultNext=null;state.uploadFailure=true;api.seedLesson();state.session.active=true;route(api.classScreen(),true);return;}
    }
    if(name==='pick-image'){pickImage();return;}
    if(name==='model-details'){state.detailsModel=el?.dataset.model||state.model;route('38',true);return;}
    if(name==='view-image'){showImagePreview();return;}
    if(name==='close-image-preview'){closeImagePreview();return;}
    if(name==='remove-image'){removeImage();return;}
    if(name==='debug-requests'){route('12');return;}
    if(name==='debug-process'){route('13');return;}
    if(name==='debug-knowledge'){route('11');return;}
    if(name==='fingerprint'){popup('模拟运行信息','<div class="prototype-modal-text">模式：独立静态原型\n规则版本：static-prototype-20261017\n数据来源：当前浏览器内存\n后端进程：无\n外部请求：0</div>');return;}
    if(name==='event-filter-all'||name==='event-filter-errors'){state.eventFilter=name==='event-filter-all'?'全部':'异常';showEvents();return;}
    if(name==='asr-toggle'){if(state.asr.running){state.asr.running=false;asrToken++;rerender();toast('模拟录音已结束，转写结果保留。');}else startAsr();return;}
    if(name==='save-api-key'){close();toast('本原型不保存密钥；无需填写真实凭据。');return;}
    if(name==='knowledge-history'){route('40');return;}
    if(name==='full-request'){popup('完整模拟请求正文',`<div class="prototype-modal-text">${escape(JSON.stringify({说明:'这是固定模拟数据，不是向模型发送的真实请求',教学主题:state.lesson.topic||'惯性的概念',画像:state.lesson.profile||'示例画像',课堂讲授:state.session.messages.filter(m=>m.role==='teacher').map(m=>m.text),学生返回:replySummary(),状态:state.session.phase},null,2))}</div>`);return;}
    if(name==='restore-connection'){state.session.phase='ready';state.faultNext=null;state.popup=null;route('05',true);toast('已恢复模拟连接，没有连接真实后端。');return;}
    if(name==='confirm-remove-image'){removeImage();return;}
  }
  function replySummary(){return state.session.messages.filter(m=>m.role==='student').map(m=>m.text);}
  function showEvents(){
    const examples=[{time:'03:24',title:'学生实际发出提问',detail:'固定演示：静止的物体也有惯性吗？'},{time:'03:22',title:'模型结果已自动修复',detail:'固定演示：首次校验失败，重试后恢复。'},{time:'03:19',title:'学生认知已更新',detail:'固定演示：形成一条新的理解。'},{time:'02:58',title:'旧候选已取消',detail:'固定演示：教师补充了新内容。'}];
    const events=state.events.length?state.events:examples,filtered=state.eventFilter==='异常'?events.filter(e=>/失败|错误|取消|修复|中断/.test(e.title+e.detail)):events;
    const content='<p class="hint">'+(state.events.length?'当前模拟课堂':'固定演示数据')+' · '+escape(state.eventFilter)+' · '+filtered.length+' 条</p><div style="max-height:350px;overflow:auto">'+(filtered.map(e=>'<div class="record"><strong>'+escape(e.time+' · '+e.title)+'</strong><p>'+escape(e.detail)+'</p></div>').join('')||'<p>当前没有符合条件的事件。</p>')+'</div>';
    popup('课堂事件筛选',content,btn('全部事件','event-filter-all')+btn('只看异常','event-filter-errors')+btn('关闭','close'));
  }
  function hydrateDebug(){
    if(app.screen!=='29')return;
    const fields=d.querySelectorAll('.debug-grid>.card:first-child .input');
    for(const [index,key,label] of [[0,'topic','教学主题'],[1,'grade','年级 / 学科'],[2,'profile','学生画像']]){const old=fields[index];if(!old)continue;const input=d.createElement(key==='profile'?'textarea':'input');input.className=old.className;input.dataset.field=key;input.setAttribute('aria-label',label);input.value=state.lesson[key]||'';input.placeholder=key==='topic'?'输入主题，生成画像后创建试讲':'填写'+label;input.maxLength=3000;old.replaceWith(input);}
    const labels={'开启麦克风':state.session.mic?'暂停麦克风':'开启麦克风','学生静音':state.session.muted?'恢复学生发言':'学生静音','语音播报':state.session.tts?'关闭语音播报':'语音播报'};
    for(const button of d.querySelectorAll('button')){const text=button.textContent.trim();if(labels[text]){const label=button.querySelector('span')||button;label.textContent=labels[text];button.setAttribute('aria-pressed',String(text==='学生静音'?state.session.muted:text==='语音播报'?state.session.tts:state.session.mic));if(text==='语音播报')button.disabled=state.session.muted;}}
    const badge=d.querySelector('.debug-grid>.card:last-child .card-head>.badge');if(badge)badge.textContent=state.session.active?'模拟课堂 · '+(state.session.mic?'收音中':'已创建'):'尚未创建模拟课堂';
    const tabs=d.querySelectorAll('.debug-grid .detail-tabs span');for(const tab of tabs)tab.classList.toggle('active',tab.textContent.trim()===state.debugTab);
    const content={'转写与片段':{说明:'模拟转写与教学片段',records:state.records},'模型输入':{说明:'模拟请求，不会发送',topic:state.lesson.topic,profile:state.lesson.profile,messages:state.session.messages},'学生状态':{说明:'仅调试页展示内部关联',knowledge:{id:'k1',sources:['t1','t2'],status:'understood'},muted:state.session.muted},'模型与事件':{说明:'模拟模型处理事件',events:state.events}};
    const console=d.querySelector('.debug-console');if(console){console.textContent=JSON.stringify(content[state.debugTab]||content['学生状态'],null,2);console.style.cssText='max-height:290px;overflow:auto;white-space:pre-wrap';}
    if(console){const links=d.createElement('div');links.className='row-actions';links.style.marginTop='12px';links.innerHTML=btn('认知详情','debug-knowledge')+btn('模型请求与结果','debug-requests')+btn('处理过程','debug-process');console.after(links);}
    const hint=d.querySelector('.debug-grid>.card:last-child .card-head p');if(hint)hint.textContent='本地模拟控制台；与原产品调试服务隔离。';
  }
  // 来源及固定版本抄录自产品 asr/catalog.py；只展示地址，不发起外部请求。
  const modelMetadata={
    'Paraformer 中文完整版':{repo:'csukuangfj/sherpa-onnx-paraformer-zh-2024-03-09',revision:'906992d326ebf0c5171cde675aa0902be9e5bc6c',directory:'paraformer-zh-fp32',files:'model.onnx · tokens.txt',family:'FP32 / Paraformer'},
    'SenseVoice Small':{repo:'csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17',revision:'2365baeacb507f821a0c8120fcee3d484dba7a07',directory:'sensevoice-small-int8',files:'model.int8.onnx · tokens.txt',family:'INT8 / SenseVoice'},
    'Whisper Small':{repo:'csukuangfj/sherpa-onnx-whisper-small',revision:'8f3c18b358db4d1f2fc1eae49d75cd20989e4309',directory:'whisper-small-int8',files:'small-encoder.int8.onnx · small-decoder.int8.onnx · small-tokens.txt',family:'INT8 / Whisper'},
    'Whisper Large-v3 Turbo':{repo:'csukuangfj/sherpa-onnx-whisper-turbo',revision:'2ca6ff69fc878651b770880507669577ac41c2ff',directory:'whisper-turbo-int8',files:'turbo-encoder.int8.onnx · turbo-decoder.int8.onnx · turbo-tokens.txt',family:'INT8 / Whisper'}
  };
  function showModelMetadata(title){
    const name=state.detailsModel||state.model,meta=modelMetadata[name],license=name.startsWith('Whisper')?'https://github.com/openai/whisper/blob/main/LICENSE':'https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE';
    popup(title,'<h3>'+escape(name)+'</h3><p class="hint">来自产品固定版本清单，地址可选择复制；原型不联网打开网页，未下载或验证模型文件。</p><label>模型来源</label><input class="input" readonly aria-label="模型来源地址" value="https://huggingface.co/'+escape(meta.repo)+'"><p>固定版本：<code style="overflow-wrap:anywhere">'+escape(meta.revision)+'</code></p><label>清单中的许可文件地址</label><input class="input" readonly aria-label="许可地址" value="'+escape(license)+'"><p class="hint">许可条件请以对应上游文件为准。</p>');
  }
  function maintainModel(operation){
    if(state.session.active||state.asr.running)return toast('请先结束模拟课堂或识别测试，再进行模型维护。');
    const name=state.detailsModel||state.model;state.modelMaintenance[name]=operation+'中';
    popup(operation+'（模拟）','<p role="status">正在'+escape(operation)+'，请稍候…</p>');
    setTimeout(()=>{if(state.modelMaintenance[name]!==operation+'中')return;state.modelMaintenance[name]=operation+'完成';if(operation==='准备 VAD')state.vadReady=true;addEvent('模拟'+operation+'完成',name);if(app.screen==='38'){hydrateMaintenance();popup(operation+'（模拟）','<p role="status">'+escape(operation)+'完成。'+(operation==='准备 VAD'?'模拟 VAD 已就绪。':'模拟文件检查已通过。')+'</p><p class="hint">仅演示状态变化，未读写或下载真实模型文件。</p>');}},650);
  }
  function hydrateMaintenance(){
    if(app.screen!=='38')return;const name=state.detailsModel||state.model,meta=modelMetadata[name];
    const rows=d.querySelectorAll('.main>.settings-layout>.card:first-child .version-table strong');
    if(rows.length>=6){rows[0].textContent=meta.family;rows[2].textContent='asr/models/'+meta.directory+'/';rows[3].textContent=meta.files;rows[4].textContent='Silero VAD · '+(state.vadReady?'已准备 · 模拟':'未准备 · 模拟');rows[5].textContent=meta.revision;rows[5].style.overflowWrap='anywhere';rows[1].textContent='以产品清单为准 · 未下载';}
    const notice=d.querySelector('.main .notice strong');if(notice)notice.textContent=state.modelMaintenance[name]||'模型状态演示 · 不检查本机真实文件';
  }
  function click(el,text){
    if(text==='返回调试前端'){route('29',true);return true;}
    if(app.screen==='29'&&el.matches('.debug-grid .detail-tabs span')){state.debugTab=text;rerender();return true;}
    if(text==='全部事件'){state.eventFilter='全部';showEvents();return true;}
    if(text==='学生静音'||text==='恢复学生发言'){state.session.muted=!state.session.muted;addEvent(state.session.muted?'关闭学生发言':'恢复学生发言','静态模拟');rerender();return true;}
    if(text==='语音播报'||text==='关闭语音播报'){if(!state.session.muted)state.session.tts=!state.session.tts;rerender();return true;}
    if(text==='确认教学截图'){pickImage();return true;}
    if(text==='上传截图'||text==='重新选择'||text==='更换'){pickImage();return true;}
    if(text==='放大查看'){showImagePreview();return true;}
    if(text==='移除'){popup('移除当前教学截图？','<p>只移除当前页面中的引用，原文件和对话不会改变。</p>',btn('取消','close')+btn('确认移除','confirm-remove-image',true));return true;}
    if(text==='核对当前图'){showImagePreview();return true;}
    if(text==='取消'||text==='关闭'){close();return true;}
    if(text==='保存设置'||text==='保存后离开'){saveSettings();return true;}
    if(text==='取消更改'){Object.assign(state.settings,state.savedSettings);state.dirty=false;rerender();toast('已恢复上次保存的模拟设置。');return true;}
    if(text==='继续编辑'){state.pendingRoute=null;route('23',true);return true;}
    if(text==='更换密钥'){popup('密钥占位说明','<p class="hint">本原型不连接模型，也不需要真实密钥。正式实现时，密钥仅交给后端保存。</p>',btn('知道了','close'));return true;}
    if(text==='测试连接'){if(!state.settings.demoKey?.trim()){toast('请先填写演示 API Key。');d.querySelector('[aria-label="API Key"]')?.focus();return true;}state.connection.text='模拟通过';state.connection.image='模拟通过';rerender();toast('模拟连接成功，未发送真实请求。');return true;}
    if(text==='测试文字连接'||text==='测试看图连接'){const key=text.includes('看图')?'image':'text';state.connection[key]='模拟通过';rerender();toast('模拟连接通过。未发送请求、未产生模型用量。');return true;}
    if(['下载模型','选择使用','已选择','取消下载'].includes(text)&&el.closest('.model-card')){const name=el.dataset.model||el.closest('.model-card').querySelector('h3').textContent.trim();if(text==='取消下载'){downloadToken++;state.modelDownload=null;route('24',true);toast('已取消模拟下载。');}else if(text==='下载模型')startDownload(name);else if(state.session.active)toast('请先结束模拟课堂。');else{state.model=name;rerender();toast('已选择模拟模型：'+name);}return true;}
    if(text==='校验 / 修复'||text==='准备 VAD'){maintainModel(text);return true;}
    if(text==='重新下载'){startDownload('Whisper Small');return true;}
    if(text==='打开文件夹'||text==='打开模型文件夹'){popup('模拟模型目录','<div class="prototype-modal-text">asr/models/\n├─ paraformer-zh-fp32/\n├─ sensevoice-small-int8/\n└─ silero_vad.onnx\n\n仅展示目录约定，不打开或修改本机文件夹。</div>');return true;}
    if(text==='模型来源'||text==='权重许可'){showModelMetadata(text);return true;}
    if(text==='进入'||text==='开始录音测试'||text==='开始收音测试'){if(state.session.active){toast('请先结束模拟课堂，再进行独立测试。');return true;}route('26',true);return true;}
    if(text==='结束录音'){state.asr.running=false;asrToken++;rerender();return true;}
    if(text==='清空结果'){state.asr.records=[];rerender();return true;}
    if(text==='导出结果'){download('模拟ASR结果.json',JSON.stringify({说明:'固定模拟结果，未采集音频',model:state.model,records:state.asr.records},null,2));return true;}
    if(text==='导出 Markdown'||text==='导出报告'){const content=d.querySelector('.report-main')?.innerText||'本次概念讲解与改进练习。';download('模拟教学报告.md','# '+(state.lesson.topic||'惯性的概念')+'\n\n> 静态模拟报告，不是真实教学评价。\n\n'+content,'text/markdown;charset=utf-8');return true;}
    if(text==='导出记录'&&app.screen==='10'){download('模拟课堂回顾.md','# '+(state.lesson.topic||'惯性的概念')+'\n\n> 静态原型记录，不是真实课堂分析。\n\n'+d.querySelector('.review-grid').innerText,'text/markdown;charset=utf-8');return true;}
    if(text==='导出日志'||text==='导出记录'||text==='导出本次记录'){download(text==='导出日志'?'模拟课堂日志.json':'模拟课堂记录.json',JSON.stringify({说明:'静态模拟记录，无内部关联编号',lesson:state.lesson,messages:state.session.messages,records:state.records,events:state.events},null,2));return true;}
    if(text==='导出脱敏诊断'){download('模拟诊断.json',JSON.stringify({说明:'静态原型诊断，无真实进程或凭据',version:'static-prototype-20261017',externalRequests:0,phase:state.session.phase},null,2));return true;}
    if(text==='重试复核'){state.reviewDone=true;addEvent('模拟知识复核完成');rerender();toast('模拟复核完成，没有请求模型。');return true;}
    if(text==='刷新状态'){rerender();toast('已刷新当前内存中的模拟状态。');return true;}
    if(text==='返回语音设置'||text==='返回模型列表'){route('24',true);return true;}
    if(text==='返回认知与问题'){route('11');return true;}
    if(text==='模型返回'){route('39');return true;}
    if(text==='请求内容'){route('12');return true;}
    if(text==='校验与恢复'){route('13');return true;}
    if(text==='查看错误'){popup('模拟下载错误','<p>示例：网络连接中断。当前模型不受影响，可重新下载。本原型没有真实下载任务。</p>');return true;}
    if(text==='查看'||text==='打开 /debug'){route(text==='查看'?'10':'29',true);return true;}
    if(text==='查看原始状态 JSON'){popup('调试界面 · 示例状态','<div class="prototype-modal-text">仅在调试示意中保留内部结构：\nknowledge: { id: "k1", sources: ["t1"], status: "understood" }\n\n这里不读取产品的真实状态。</div>');return true;}
    if(text==='试讲课堂'||text==='ASR 模型'){route(text==='试讲课堂'?'29':'30',true);return true;}
    if(text==='生成教学报告'||text==='查看报告'){state.session.active?route('18'):api.finish();return true;}
    if(text==='重新查看'){route(state.report==='failed'?'21':'20');return true;}
    if(text==='查看完整指纹'){action('fingerprint');return true;}
    if(text==='开始音频测试'||text==='停止上传'){toast('上传录音只在调试示意中保留，静态原型不读取或发送录音。');return true;}
    return false;
  }
  const subjectIcons={'语文':'chinese','数学':'math','英语':'english','物理':'physics','化学':'chemistry','生物':'biology','历史':'history','地理':'geography','道德与法治':'civics','科学':'science','信息科技':'computing'};
  function hydrateHistoryIcons(){
    for(const card of d.querySelectorAll('.recent-lesson')){
      const subject=card.dataset.subject||card.querySelector('.recent-copy > span')?.textContent.split('·').at(-1).trim()||'';
      const key=subjectIcons[subject]||(['数据结构','计算机科学','计算机','编程','信息技术'].includes(subject)?'computing':'other');
      const holder=card.querySelector('.recent-icon');if(!holder)continue;
      const image=d.createElement('img');image.src='assets/subjects/'+key+'.svg';image.alt='';image.width=28;image.height=28;
      holder.dataset.subjectIcon=key;holder.title=key==='other'?'其他（自定义）':subject;holder.replaceChildren(image);
    }
  }
  function hydrate(document){
    d=document;hydrateHistoryIcons();setSettingsFields();hydrateModels();hydrateAsr();hydrateImage();hydrateDetails();hydrateDebug();hydrateMaintenance();
    for(const avatar of d.querySelectorAll('.chat-row:not(.teacher) .chat-avatar')){avatar.replaceChildren();avatar.style.background='transparent';const image=d.createElement('img');image.src='assets/小纸-用户原图.png';image.alt='小纸';image.style.cssText='width:32px;height:32px;object-fit:contain';avatar.append(image);}
    if(app.screen==='35'&&state.session.phase==='disconnected'){const target=d.querySelector('.stage-center');if(target){const b=d.createElement('button');b.className='btn soft';b.textContent='恢复模拟连接';b.dataset.action='restore-connection';target.append(b);}}
    d.addEventListener('paste',e=>{if(app.screen!=='29'&&!e.target.closest('.chat-input'))return;const file=[...(e.clipboardData?.files||[])].find(f=>f.type.startsWith('image/'))||[...(e.clipboardData?.items||[])].find(item=>item.type.startsWith('image/'))?.getAsFile();if(file){e.preventDefault();addImage(file);}});
    if(app.screen==='38'&&state.detailsModel){const h=d.querySelector('.main .card h2');if(h)h.textContent=state.detailsModel;}
    for(const value of d.querySelectorAll('.version-table strong')){if(value.textContent.includes('示例进程'))value.textContent='静态原型 · 无后台进程';if(value.textContent.includes('完整指纹')){const b=d.createElement('button');b.className='btn ghost';b.dataset.action='fingerprint';b.textContent='查看完整指纹';value.replaceChildren(b);}}
    const modal=d.querySelector('.modal');if(modal){modal.setAttribute('role','dialog');modal.setAttribute('aria-modal','true');}
  }
  window.prototypeExtensions={hydrate,click,action,resetImage,closeImagePreview};
})();
