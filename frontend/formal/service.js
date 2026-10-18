// 正式前端业务状态与协议投影；不把识别片段或候选回复作为对话。
export const emptySession = () => ({id:'',lesson:{},status:'尚未创建课堂',starting:false,active:false,ending:false,ended:false,
  muted:false,tts:true,mic:false,sidebar:true,dialogue:[],raw:[],review:{understood:[],tentative:[],unclear:[],questions:[]},
  image:null,pendingImage:null,uploading:false,uncertain:false,draft:'',pending:null,error:'',retryable:false,startedAt:null,
  createError:'',recovery:{resume:null,knowledge:{}},batchRetryPending:false,batchRetryId:null,batchRetryError:''});
export const state = {
  health:{}, settings:{tts:true,asr_review:true,model_configured:false}, settingsDraft:null,
  setup:{topic:'',points:'',grade:'',subject:'',student_level:'normal',lesson_type:'new',textbook:'',student_profile:''},
  profileBusy:false, session:emptySession(), history:{items:[],total:0,indexing:false,offset:0,loading:false,error:''},
  detail:{id:'',items:[],warnings:[],review:{},before:null}, report:{status:'idle'}, asr:{models:[]},
  test:{active:false,starting:false,texts:[],status:''},notice:''
};
export async function api(path, method='GET', body) {
  const response = await fetch(path,{method,cache:'no-store',headers:body && !(body instanceof Blob) ? {'Content-Type':'application/json'} : {},
    body: body === undefined ? undefined : body instanceof Blob ? body : JSON.stringify(body)});
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(typeof result.detail === 'string' ? result.detail : `请求失败（${response.status}），请检查输入或稍后重试`);
    error.status=response.status; throw error;
  }
  return result;
}
export function recoveryTasks(s) {
  return [s.recovery.resume,...Object.values(s.recovery.knowledge)].filter(Boolean);
}
export function retryPending(s) {
  return s.batchRetryPending || recoveryTasks(s).some(task=>task.status==='queued' || task.status==='running' && task.error);
}
export function canRetry(s) {
  return s.active && !s.ending && !retryPending(s) && (s.retryable ||
    Object.values(s.recovery.knowledge).some(task=>task.status==='failed' && task.retryable) ||
    !s.muted && s.recovery.resume?.status==='failed' && s.recovery.resume.retryable);
}
export function retrySession(s, send) {
  if(!canRetry(s))return false;
  const tasks=recoveryTasks(s).filter(task=>task.status==='failed' && task.retryable && (task!==s.recovery.resume || !s.muted));
  // Lock before sending; a successful send only queues work, it does not resolve it.
  s.batchRetryPending=!!s.retryable;
  s.batchRetryError=s.batchRetryPending?s.error:'';
  s.batchRetryId=s.dialogue.find(row=>row.processing_status==='failed')?.batch_id ?? null;
  for(const task of tasks)task.status='queued';
  try { send({type:'retry'}); }
  catch(error) {s.batchRetryPending=false;s.batchRetryId=null;s.batchRetryError='';for(const task of tasks)task.status='failed';throw error;}
  return true;
}
function reconcileReviews(s, snapshot) {
  if(!Array.isArray(snapshot?.pending_reviews))return;
  const pending=new Set(snapshot.pending_reviews.map(row=>row.knowledge_id));
  for(const id of Object.keys(s.recovery.knowledge))if(!pending.has(id))delete s.recovery.knowledge[id];
}
export function applyEvent(s, event) {
  const d=event.data || {};
  if(typeof event.elapsed==='number')s.elapsed=event.elapsed;
  switch (event.type) {
    case 'ready': Object.assign(s,{id:d.session_id,lesson:d.lesson || s.lesson,active:true,starting:false,ended:false,
      muted:d.muted,error:'',createError:'',retryable:false,batchRetryPending:false,batchRetryId:null,batchRetryError:'',
      recovery:{resume:null,knowledge:{}},status:'课堂已就绪，点击开启麦克风或输入讲授',startedAt:Date.now()}); break;
    case 'status': s.status=d.message; break;
    case 'transcript': s.raw.push({...d,time:event.time,elapsed:event.elapsed}); break;
    case 'review_completed': s.raw.push(event); break;
    case 'teacher_batch': {
      const row = s.dialogue.find(item => item.batch_id === d.batch_id);
      if (row) Object.assign(row,d); else s.dialogue.push({...d,role:'teacher'});
      if(s.batchRetryPending && (!s.batchRetryId || s.batchRetryId===d.batch_id) && ['processed','failed'].includes(d.processing_status)) {
        s.batchRetryPending=false;s.batchRetryId=null;
        if(d.processing_status==='processed' && s.error===s.batchRetryError){s.error='';s.retryable=false;}
        s.batchRetryError='';
      }
      s.status=d.processing_status==='submitted' ? '学生正在处理讲授内容' : d.processing_status==='failed' ? '本批讲授处理失败，可重试' : '本批讲授已处理，继续听课';
      break;
    }
    case 'teacher_input_accepted':
      if (s.pending?.id === d.client_message_id) { if (s.draft === s.pending.text) s.draft=''; s.pending=null; }
      break;
    case 'reply': s.dialogue.push({role:'student',text:d.text,time:event.time,elapsed:event.elapsed}); s.lastReply=d.text; s.status='学生已回应，你可以继续讲授'; break;
    case 'student_view': s.review=d; break;
    case 'mute': {
      const changed=s.muted!==d.muted;s.muted=d.muted;
      if(changed && s.recovery.resume) s.recovery.resume.status=d.muted?'paused':'queued';
      s.status=d.muted ? '学生发言已暂停，继续听课与理解' : '学生发言已恢复'; break;
    }
    case 'resume_response': {
      if(d.status==='failed')s.recovery.resume={status:'failed',error:d.message || '恢复回应失败',retryable:!!d.retryable};
      else if(d.status==='running')s.recovery.resume={...s.recovery.resume,status:s.muted?'paused':'running'};
      else if(['generated','skipped','stale'].includes(d.status))s.recovery.resume=null;
      break;
    }
    case 'knowledge_review': {
      for(const id of d.targets || []) {
        if(d.status==='completed')delete s.recovery.knowledge[id];
        else if(d.status==='failed')s.recovery.knowledge[id]={status:'failed',error:d.message || '知识复核失败',retryable:!!d.retryable};
        else if(d.status==='running')s.recovery.knowledge[id]={...s.recovery.knowledge[id],status:'running'};
        else if(d.status==='stale' && s.recovery.knowledge[id])s.recovery.knowledge[id].status='queued';
      }
      reconcileReviews(s,d.state);break;
    }
    case 'state': reconcileReviews(s,d.state);break;
    case 'audio': s.mic=Boolean(d.recording); break;
    case 'speech': s.speaking=Boolean(d.speaking); break;
    case 'speech_stopped': s.speaking=false; break;
    case 'warning': s.error=d.message; break;
    case 'error':
      s.error=d.message; s.retryable=Boolean(d.retryable);
      if(d.code==='model_failure'){s.batchRetryPending=false;s.batchRetryId=null;s.batchRetryError='';}
      if (s.pending && (!d.client_message_id || d.client_message_id===s.pending.id)) s.pending=null;
      if(s.starting) { s.starting=false; s.active=false;s.createError=d.message;s.retryable=false; s.status='创建失败，内容已保留，可重试'; }
      break;
    case 'finished': s.active=false; s.ending=false; s.ended=true; s.mic=false; s.status='本次试讲已结束';
      if(d.unprocessed_sources?.length) s.error='有部分讲授未处理，原文已保存在课堂详情中。'; break;
  }
}
