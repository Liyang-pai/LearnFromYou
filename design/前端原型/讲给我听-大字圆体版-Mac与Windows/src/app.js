// 静态原型状态与页面导航；所有动作仅操作内存和本地DOM，不访问业务服务。
'use strict';
(() => {
  const screens=window.PROTOTYPE_SCREENS, frame=document.getElementById('canvas');
  const scene=document.getElementById('scene'), viewport=document.getElementById('viewport');
  let screen='01', doc, toastTimer;
  const state={lesson:{topic:'',grade:'由 AI 判断',subject:'由 AI 判断',level:'普通水平',type:'新课',points:'',profile:'',textbook:''},session:{active:false,phase:'ready',mic:false,muted:false,tts:true,captions:true,sidebarCollapsed:false,messages:[],elapsed:0},settings:{allowStudentSpeech:true,tts:true,captions:true,review:true},dirty:false};
  function toast(text){const el=document.getElementById('hostToast');el.textContent=text;el.classList.add('visible');clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.remove('visible'),2800);}
  function fit(){const scale=Math.min((viewport.clientWidth-20)/1600,(viewport.clientHeight-18)/1000);frame.style.transform=`scale(${scale})`;frame.style.left=(viewport.clientWidth-1600*scale)/2+'px';frame.style.top=(viewport.clientHeight-1000*scale)/2+'px';doc?.documentElement.classList.toggle('narrow-settings',viewport.clientWidth<1100);}
  function show(id){if(!screens[id])id='01';screen=id;scene.value=id;history.replaceState(null,'','#s='+id);render();}
  function buttons(text){return [...doc.querySelectorAll('button')].filter(b=>b.textContent.trim()===text);}
  function on(text,fn){for(const b of buttons(text)){b.dataset.bound='true';b.addEventListener('click',fn);}}
  function modalClose(){show(state.session.active?'08':'03');}
  function render(){
    const item=screens[screen];
    frame.onload=()=>{doc=frame.contentDocument;hydrate();fit();document.getElementById('previewStatus').textContent=item.name.replaceAll('-',' / ');};
    frame.srcdoc=`<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><base href="${new URL('.',location.href).href}"><style>${item.style}\n${interactionStyle}</style><link rel="stylesheet" href="src/theme.css?v=20261018-detail-debug-tabs"></head><body>${item.body}</body></html>`;
    fit();
  }
  function hydrate(){
    // 共用外壳：品牌归入侧栏，课堂操作保留在内容区。
    const topbar=doc.querySelector('.topbar'), sidebar=doc.querySelector('.sidebar');
    if(topbar && sidebar){
      const brand=topbar.querySelector('.brand');
      brand.querySelector('.top-divider')?.remove();
      brand.querySelector('.top-label')?.remove();
      brand.querySelector('.brand-en').textContent='LearnFromYou';
      sidebar.prepend(brand);
      const actions=doc.querySelector('.page-head .row-actions');
      if(actions)for(const el of topbar.querySelectorAll('.top-status > *')){
        if(el.matches('button') || el.querySelector('b')){el.classList.add('session-action');actions.append(el);}
      }
      topbar.remove();
    }
    const setupForm=doc.querySelector('.profile-form');
    if(setupForm){
      for(const badge of doc.querySelectorAll('.profile-form > .card-head > .badge,.profile-card > .card-head > .badge'))badge.remove();
      const heading=setupForm.querySelector('.card-head');
      const generate=[...setupForm.children].find(el=>el.matches('button.btn.full'));
      if(heading && generate){
        const scroll=doc.createElement('div');scroll.className='setup-scroll';
        scroll.tabIndex=0;scroll.setAttribute('role','region');scroll.setAttribute('aria-label','教学设置表单');
        while(heading.nextElementSibling && heading.nextElementSibling!==generate)scroll.append(heading.nextElementSibling);
        heading.after(scroll);
        const gradeSubject=scroll.querySelector('.form-pair');
        if(gradeSubject)for(const row of [...scroll.querySelectorAll('.segment-row')])gradeSubject.before(row);
        const footer=doc.createElement('div');footer.className='setup-actions';
        while(generate.nextElementSibling)footer.append(generate.nextElementSibling);
        footer.prepend(generate);setupForm.append(footer);
      }
    }
    const navIcons={
      '首页':'<path d="m3 10 9-7 9 7"/><path d="M5 9v12h14V9M9 21v-8h6v8"/>',
      '创建试讲':'<path d="M12 5v14M5 12h14"/>',
      '我的教室':'<rect x="3" y="3" width="18" height="14" rx="1"/><path d="m8 21 4-4 4 4"/>',
      '设置':'<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68h.08A1.65 1.65 0 0 0 10 3.17V3a2 2 0 0 1 4 0v.09A1.65 1.65 0 0 0 15 4.6a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.32 9v.08A1.65 1.65 0 0 0 20.83 10H21a2 2 0 0 1 0 4h-.09A1.65 1.65 0 0 0 19.4 15z"/>',
      '调试前端':'<path d="m7 7-5 5 5 5m10-10 5 5-5 5M14 4l-4 16"/>'
    };
    for(const nav of doc.querySelectorAll('.sidebar .nav-item')){
      const icon=nav.querySelector('svg'), paths=navIcons[nav.textContent.trim()];
      if(icon && paths){icon.innerHTML=paths;icon.setAttribute('stroke-width','1.8');icon.setAttribute('aria-hidden','true');}
    }
    for(const nav of doc.querySelectorAll('.nav-item')){
      nav.tabIndex=0;nav.setAttribute('role','button');
      const label=nav.textContent.trim();const act=()=>show(label==='首页'?'01':label==='创建试讲'?(state.lesson.profile?'03':'02'):label==='我的教室'?(state.session.active?'08':'04'):label==='设置'?'22':'29');
      nav.addEventListener('click',act);nav.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();act();}});
    }
    for(const button of doc.querySelectorAll('button'))button.type='button';
    on('创建一次试讲',()=>show('02'));on('创建试讲',()=>show('05'));on('查看课堂详情',()=>show('10'));on('返回教室',()=>show(state.session.active?'08':'04'));on('返回课堂',()=>show('08'));
    [...doc.querySelectorAll('.settings-tabs span')].forEach((el,i)=>el.onclick=()=>show(['22','24'][i]));
    [...doc.querySelectorAll('.main>.detail-tabs span')].forEach((el,i)=>el.onclick=()=>show((['29','30'].includes(screen)?['29','30']:['10','12','11','13'])[i]));
    for(const el of doc.querySelectorAll('.top-status>span'))if(el.textContent.includes('本机服务'))el.textContent='静态模拟 · 无服务连接';
    doc.addEventListener('keydown',e=>{if(e.key==='Escape')modalClose();});
    for(const close of doc.querySelectorAll('.modal-header>.icon')){close.setAttribute('role','button');close.tabIndex=0;close.setAttribute('aria-label','关闭');close.onclick=modalClose;}
    for(const b of doc.querySelectorAll('button'))if(!b.dataset.bound)b.addEventListener('click',()=>toast('静态演示：'+b.textContent.trim()));
  }
  const interactionStyle=`button,.nav-item,.settings-tabs span,.detail-tabs span,.segments span,.toggle{cursor:pointer}button:focus-visible,[role=button]:focus-visible,input:focus-visible,textarea:focus-visible,select:focus-visible{outline:2px solid #69a8ff;outline-offset:3px}.btn:hover{filter:brightness(.98)}input.input,select.input,textarea.input{width:100%;font:inherit;font-size:13px;color:#284c77;background:#fff}textarea.input{resize:none;line-height:1.8}.prototype-note{padding:10px 16px;background:#eaf5ff;border-radius:9px;color:#7498c0;font-size:11px;margin-bottom:12px}.prototype-toggle{border:0;padding:3px}.prototype-record{font-size:12px;line-height:1.8}.prototype-file-image{display:block;width:100%;height:330px;object-fit:contain;border-radius:12px;background:#f4f9ff}.prototype-modal-text{white-space:pre-wrap;max-height:460px;overflow:auto;font-size:12px;line-height:1.9;color:#6b85a6}.prototype-sim-tag{font-size:9px;color:#8ba5c4}.model-meta-link{font-size:10px;color:#739bd0;text-decoration:underline}.profile-manual{display:block;width:100%;height:260px;resize:vertical;padding:17px;border:1px solid #d8e6f7;border-radius:10px;font:13px/1.8 inherit}.chat-input textarea{min-height:42px;resize:none} .modal-header>[role=button]{cursor:pointer}`;
  for(const [id,item] of Object.entries(screens)){const option=document.createElement('option');option.value=id;option.textContent=item.name;scene.append(option);}
  scene.onchange=()=>show(scene.value);
  document.getElementById('reset').onclick=()=>{location.hash='';location.reload();};
  document.getElementById('simulate').onclick=()=>show(screen==='06'?'07':screen==='07'?'08':'06');
  document.getElementById('fault').onclick=()=>show('34');
  window.addEventListener('resize',fit);show(new URLSearchParams(location.hash.slice(1)).get('s')||'01');
  window.prototypeApp={show,get screen(){return screen;},get state(){return state;}};
})();
