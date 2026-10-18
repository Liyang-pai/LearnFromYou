import {publicValue,classroomStats,elapsedSeconds,canRetry} from './service.js';
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const time=value=>{const seconds=Math.floor(Math.max(0,value||0));return String(Math.floor(seconds/60)).padStart(2,'0')+':'+String(seconds%60).padStart(2,'0');};
const empty=text=>`<p class="records-empty">${text}</p>`;
const phase={preparation:'课堂范围分析',review_glossary:'术语准备',asr_review:'转写审核',student:'学生认知更新',student_resume:'恢复回应判断',knowledge_review:'知识复核'};
const labels={understood:'课堂推断理解',tentative:'推断待确认',unclear:'理解障碍',conflict:'矛盾待解决',conflicted:'矛盾待解决',invalidated:'已失效',invalid:'已失效',pending:'待回应',asked:'已提问',deferred:'暂缓提问',answered:'已回应',resolved:'已解决',dismissed:'无需回应',open:'待解决'};
const card=(title,text,meta='')=>`<article class="live-record"><div class="live-record-title">${esc(title)}</div><p>${esc(text)}</p>${meta?`<small>${esc(meta)}</small>`:''}</article>`;
const payload=(title,value,key)=>`<details class="live-payload" data-key="${esc(key)}"><summary>${esc(title)}</summary><pre>${esc(typeof publicValue(value)==='string'?publicValue(value):JSON.stringify(publicValue(value),null,2))}</pre></details>`;
export function transcripts(s){
  const rows=(s.records||[]).filter(e=>['transcript','review_completed','segment'].includes(e.type));
  return rows.length?rows.map((r,i)=>{
    const d=r.data,stamp=time(r.elapsed);
    if(r.type==='transcript')return card(stamp+' · '+(d.mode==='microphone'?'语音转写':'转写'),d.text,d.asr_seconds==null?'':`识别 ${d.asr_seconds} 秒`);
    if(r.type==='segment')return card(stamp+' · 教学片段',d.text,d.reason);
    return (d.segments||[]).map((seg,n)=>`<article class="live-record"><div class="live-record-title">${stamp} · 转写审核</div><p>${esc(seg.text??seg.corrected_text??seg.raw_text)}</p><small>${esc(({bypassed:'未开启审核',corrected:'已校正',reviewed:'审核已完成',disabled:'未开启审核',unchanged:'采用原文',fallback:'审核失败，采用原文',failed:'审核失败，采用原文'})[seg.review_status]||seg.review_status||'审核完成')}${seg.review_seconds==null?'':` · ${esc(seg.review_seconds)} 秒`}</small><details data-key="raw-${i}-${n}"><summary>查看识别原文</summary><p>${esc(seg.raw_text)}</p></details></article>`).join('');
  }).join(''):empty('尚无转写与教学分段。开始讲解后在这里查看。');
}
export function cognition(s){
  const v=s.cognition;if(!v)return empty('尚无可核对的认知记录。');
  const groups=[['学生当前认知',v.knowledge],['问题与解答',v.open_questions??v.questions],['教师待回应请求',v.response_requests],['近期课堂事件',v.recent_events]];
  let body=groups.map(([name,items])=>`<section class="live-state-group"><h3>${name}</h3>${items?.length?items.map(item=>{
    const review=(v.pending_reviews||[]).find(r=>r.knowledge_id===item.id);
    return card(labels[item.status]||'课堂记录',item.text,[item.reason,item.conditions,item.attempts==null?'':`已提问 ${item.attempts} 次`,review?'待复核 · '+(review.reason||'暂不作为确定前提'):'',review?.error].filter(Boolean).join(' · '));
  }).join(''):empty('暂无记录。')}</section>`).join('');
  if(v.conflicts?.length)body+='<h3>认知矛盾</h3>'+v.conflicts.map(c=>card(labels[c.status]||'矛盾记录',c.reason,c.resolution)).join('');
  if(v.knowledge_history?.length)body+=`<details class="live-payload" data-key="knowledge-history"><summary>知识修订历史（${v.knowledge_history.length}）</summary>${v.knowledge_history.map(r=>card(labels[r.previous?.status]||'早先的理解',r.previous?.text,[r.change_reason,r.invalidated?'旧版本已失效':''].filter(Boolean).join(' · '))).join('')}</details>`;
  if(v.pending_reviews?.length)body+='<h3>待复核内容</h3>'+v.pending_reviews.map(r=>card(r.status==='failed'?'复核失败':'复核处理中',r.reason,r.error)).join('');
  if(canRetry(s))body+='<button class="btn soft" data-action="retry-class">重试未完成处理与复核</button>';
  return body;
}
export function modelInputs(s){
  const rows=(s.records||[]).filter(e=>e.type==='llm_request').slice().reverse();
  return rows.length?rows.map((r,i)=>payload(`${time(r.elapsed)} · ${phase[r.data.phase]||r.data.phase||'模型请求'} · 第 ${r.data.attempt??1} 次`,r.data.body??r.data,'request-'+i)).join(''):empty('尚无模型调用。');
}
const eventNames={llm_output:'模型输出',candidate:'候选回复 · 尚未发出',reply:'学生实际发言',state:'认知更新与候选',student_validation_error:'模型结果校验',student_recovery:'自动恢复',knowledge_review:'知识复核',resume_response:'恢复回应判断',suppressed:'候选已取消或抑制',invitation_expired:'回应邀请已失效',asr_empty:'未识别到有效语音',question_release:'发言控制',warning:'提示',error:'处理错误',mute:'发言控制',finished:'课堂收尾',review_glossary:'术语准备',status:'处理状态'};
export function outputs(s){
  const rows=(s.records||[]).filter(r=>eventNames[r.type]).slice().reverse();
  return rows.length?rows.map((r,i)=>payload(`${time(r.elapsed)} · ${eventNames[r.type]}${r.data.status?' · '+r.data.status:''}${r.type==='llm_output'&&r.data.seconds!=null?' · '+r.data.seconds+' 秒':''}`,r.type==='llm_output'?(r.data.raw??r.data):r.data,'event-'+i)).join(''):empty('尚无模型输出与处理事件。');
}
export const recordTabs=[['transcript','转写与分段'],['input','LLM 输入'],['student','学生状态'],['output','输出与事件']];
export function tabs(items,selected,action){return `<div class="live-tabs" role="tablist">${items.map(([key,label])=>`<button type="button" role="tab" aria-selected="${key===selected}" id="${action}-${key}" data-action="${action}" data-id="${key}">${label}</button>`).join('')}</div>`;}
export function recordContent(s,tab){return ({transcript:transcripts,input:modelInputs,student:cognition,output:outputs}[tab]||transcripts)(s);}
export function inspector(s,chat,report){
  const tab=s.sidebarTab||'conversation';
  return `<section class="classroom-inspector">${tabs([['conversation','课堂对话'],['transcript','转写'],['student','学生状态']],tab,'sidebar-tab')}<div class="inspector-content" id="sidebar-${tab}" data-scroll role="tabpanel" tabindex="0" aria-label="${tab==='conversation'?'课堂对话':tab==='transcript'?'转写':'学生状态'}">${tab==='conversation'?chat:recordContent(s,tab)}</div>${tab==='conversation'?'':`<div class="inspector-footer"><a href="/classroom/details?tab=${tab}">查看完整${tab==='student'?'学生状态与修订':'转写与分段'} →</a></div>`}</section>`;
}
export function statsMarkup(s){const st=classroomStats(s);return `<div class="stats-strip">${[['转写段数',st.transcripts],['教学片段',st.segments],['认知更新',st.version],['最近模型耗时',st.modelSeconds==null?null:st.modelSeconds+' s'],['最近审核耗时',st.reviewSeconds==null?null:st.reviewSeconds+' s']].map(([label,v])=>`<div class="stat"><span>${label}</span><b>${esc(v??'—')}</b></div>`).join('')}</div>`;}
export function liveDetail(s){
  const tab=s.detailsTab||'transcript';
  return `<div class="page-head"><div><div class="eyebrow">我的教室 / 课堂详情</div><h1>课堂详情</h1><p>${esc(s.lesson?.topic||'当前没有课堂')} / 本次课堂的输入、理解与回应</p></div><div class="row-actions"><button class="btn" data-action="back-class">返回教室</button><button class="btn" data-action="export-session"${s.id?'':' disabled'}>导出记录</button></div></div>${statsMarkup(s)}${tabs(recordTabs,tab,'details-tab')}<section class="card live-detail" id="details-${tab}" data-scroll role="tabpanel" tabindex="0" aria-label="${recordTabs.find(r=>r[0]===tab)?.[1]||'课堂记录'}">${s.id?recordContent(s,tab):empty('当前没有可查看的课堂。请返回教室创建课堂；刷新后不能恢复此前课堂连接。')}</section>`;
}
