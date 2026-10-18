// 报告衔接回归：结束后自动生成，历史只读，失败不自动循环调用提供方。
import {readFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
import vm from 'node:vm';
const business=await readFile(new URL('../frontend/formal/service.js',import.meta.url),'utf8');
const {state,applyEvent}=await import('data:text/javascript;base64,'+Buffer.from(business).toString('base64'));
let source=await readFile(new URL('../frontend/formal/app.js',import.meta.url),'utf8');
source=source.replace(/^import .*;$/gm,'').replace('void boot();','');
const requests=[],timers=[],statuses=[];
const node={addEventListener(){},querySelectorAll:()=>[],querySelector:()=>node,focus(){},innerHTML:''};
const location={pathname:'/',search:'',protocol:'http:',host:'localhost',href:'http://localhost/'};
const context=vm.createContext({state,applyEvent,api(url,method='GET'){return new Promise((resolve,reject)=>requests.push({url,method,resolve,reject}));},
  Microphone:class{async stop(){}},MicrophoneWaveform:class{bind(){} sync(){}},prepareReplySound(){},playReplySound(){},
  updateClassroomView(){},render(){statuses.push(state.report.loading?'generating':state.report.status);return '';},document:{querySelector:()=>node,querySelectorAll:()=>[],addEventListener(){},activeElement:null},
  location,history:{pushState(_,__,path){location.pathname=path;}},window:{addEventListener(){},scrollTo(){}},
  setTimeout(fn){timers.push(fn);return timers.length;},clearTimeout(){},console});
vm.runInContext(source+';this.subject={classEvent,navigate,loadReport};',context);
const {classEvent,navigate,loadReport}=context.subject;
async function settle(){for(let i=0;i<20;i++)await Promise.resolve();}
state.session.id='aaaaaaaaaaaa';state.session.active=true;
classEvent({type:'finished',data:{}});
assert.equal(location.pathname,'/report/aaaaaaaaaaaa');assert.equal(requests[0].method,'GET');
requests[0].resolve({session_id:state.session.id,status:'idle'});await settle();
assert.equal(state.report.status,'generating','注册任务前的 idle 不能显示成未生成');
assert.equal(requests.length,2,'结束课堂的 idle 自动提交生成，无需再点击');assert.equal(requests[1].method,'POST');
requests[1].resolve({session_id:state.session.id,status:'generating',progress:'正在分析'});await settle();
assert.ok(!statuses.includes('idle'),'衔接期间不能闪现未生成页面');
const poll=loadReport(state.session.id);requests[2].resolve({session_id:state.session.id,status:'ready',report:{}});await poll;
assert.equal(state.report.status,'ready');
const historical=navigate('/report/bbbbbbbbbbbb');requests[3].resolve({session_id:'bbbbbbbbbbbb',status:'idle'});await historical;
assert.equal(requests.length,4,'打开旧课堂不能自动生成报告');
state.session.id='cccccccccccc';state.session.active=true;
classEvent({type:'finished',data:{}});requests[4].resolve({session_id:state.session.id,status:'failed',error:'真实生成失败'});await settle();
assert.equal(state.report.status,'failed');assert.equal(requests.length,5,'失败必须等待用户重试，不能自动循环');
console.log('formal report: automatic handoff, idle gap, polling, history read-only and failure guards passed');
