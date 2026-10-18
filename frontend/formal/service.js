// 正式前端业务状态与协议投影；不把识别片段或候选回复作为对话。
export const emptySession = () => ({id:'',lesson:{},status:'尚未创建课堂',starting:false,active:false,ending:false,ended:false,
  muted:false,tts:true,mic:false,sidebar:true,dialogue:[],raw:[],review:{understood:[],tentative:[],unclear:[],questions:[]},
  image:null,pendingImage:null,uploading:false,uncertain:false,draft:'',pending:null,error:'',retryable:false,startedAt:null});
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
export function applyEvent(s, event) {
  const d=event.data || {};
  if(typeof event.elapsed==='number')s.elapsed=event.elapsed;
  switch (event.type) {
    case 'ready': Object.assign(s,{id:d.session_id,lesson:d.lesson || s.lesson,active:true,starting:false,ended:false,
      muted:d.muted,status:'课堂已就绪，点击开启麦克风或输入讲授',startedAt:Date.now()}); break;
    case 'status': s.status=d.message; break;
    case 'transcript': s.raw.push({...d,time:event.time,elapsed:event.elapsed}); break;
    case 'review_completed': s.raw.push(event); break;
    case 'teacher_batch': {
      const row = s.dialogue.find(item => item.batch_id === d.batch_id);
      if (row) Object.assign(row,d); else s.dialogue.push({...d,role:'teacher'});
      s.status=d.processing_status==='submitted' ? '学生正在处理讲授内容' : d.processing_status==='failed' ? '本批讲授处理失败，可重试' : '本批讲授已处理，继续听课';
      break;
    }
    case 'teacher_input_accepted':
      if (s.pending?.id === d.client_message_id) { if (s.draft === s.pending.text) s.draft=''; s.pending=null; }
      break;
    case 'reply': s.dialogue.push({role:'student',text:d.text,time:event.time,elapsed:event.elapsed}); s.lastReply=d.text; s.status='学生已回应，你可以继续讲授'; break;
    case 'student_view': s.review=d; break;
    case 'mute': s.muted=d.muted; s.status=d.muted ? '学生发言已暂停，继续听课与理解' : '学生发言已恢复'; break;
    case 'audio': s.mic=Boolean(d.recording); break;
    case 'speech': s.speaking=Boolean(d.speaking); break;
    case 'speech_stopped': s.speaking=false; break;
    case 'warning': s.error=d.message; break;
    case 'error':
      s.error=d.message; s.retryable=Boolean(d.retryable);
      if (s.pending && (!d.client_message_id || d.client_message_id===s.pending.id)) s.pending=null;
      if(s.starting) { s.starting=false; s.active=false; s.status='创建失败，内容已保留，可重试'; }
      break;
    case 'finished': s.active=false; s.ending=false; s.ended=true; s.mic=false; s.status='本次试讲已结束';
      if(d.unprocessed_sources?.length) s.error='有部分讲授未处理，原文已保存在课堂详情中。'; break;
  }
}
