// 页面只使用用户指定原型的模板，业务状态由独立服务层持有。
import {home,create,classroom,details,report,settings,asr} from './prototype.js';
export {escape} from './prototype.js';
export function render(state,path='/',query=''){
  if(path==='/' || path==='/history')return home(state,path==='/history');
  if(path==='/create')return create(state);
  if(path==='/classroom')return classroom(state);
  if(path==='/classroom/details')return details(state,false);
  if(path.startsWith('/history/'))return details(state,true);
  if(path.startsWith('/report/'))return report(state,path);
  if(path==='/settings')return settings(state);
  if(path==='/settings/asr')return asr(state,query);
  return home(state);
}
