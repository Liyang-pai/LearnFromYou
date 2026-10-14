// 验证五状态真实页面的依据、依赖、冲突、失效历史与复核提示，所有服务使用离线模型。
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');

async function main() {
  const reserve = net.createServer();
  await new Promise((resolve,reject) => {reserve.once('error',reject); reserve.listen(0,'127.0.0.1',resolve);});
  const port = reserve.address().port;
  await new Promise(resolve => reserve.close(resolve));
  const root = path.resolve(__dirname,'..');
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(),'lfy-recovery-'));
  const python = process.env.LFY_TEST_PYTHON || path.join(root, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
  const server = spawn(python,[path.join(__dirname,'question_recovery_server.py'),'--port',String(port),'--root',temporary],
    {cwd:root,stdio:['ignore','pipe','pipe'],env:{...process.env,DEEPSEEK_API_KEY:'',Deepseek_API:''}});
  let stderr = '', browser;
  server.stderr.on('data',data => stderr += data);
  try {
    const base = `http://127.0.0.1:${port}`;
    let ready = false;
    for (let i=0;i<100;i++) {
      if (server.exitCode !== null) throw new Error(stderr);
      try {if ((await fetch(base)).ok) {ready=true;break;}} catch {}
      await new Promise(resolve => setTimeout(resolve,100));
    }
    assert(ready,stderr || '测试服务未启动');
    browser = await chromium.launch({headless:true,...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {})});
    const page = await browser.newPage();
    const errors=[]; page.on('pageerror',error => errors.push(error.message));
    await page.goto(base);
    const fixture = {
      schema_version:2, version:7,
      knowledge:[
        {id:'k1',text:'完整理解的规则',status:'understood',sources:['t1'],reason:'教师完整解释',revision:1,conditions:'整数'},
        {id:'k2',text:'依赖待确认前提',status:'tentative',sources:['t2'],reason:'省略步骤可补出',revision:2,dependencies:[{id:'k1',revision:1}]},
        {id:'k3',text:'尚未理解图像',status:'unclear',sources:['t3'],reason:'缺少坐标基础',revision:1},
        {id:'k4',text:'同条件矛盾',status:'conflict',sources:['t4'],reason:'与画像已有知识不同',revision:2},
        {id:'k5',text:'旧规则已撤回',status:'invalid',sources:['t5'],reason:'教师明确纠正',revision:2,correction_sources:['t6']}
      ],open_questions:[],recent_events:[],
      pending_reviews:[{knowledge_id:'k2',revision:2,status:'failed',reason:'前提变化',error:'可重试'}],
      conflicts:[{id:'c1',left_id:'p1',right_id:'k4',conditions:'同一条件',reason:'双方互相矛盾',status:'open',sources:['p1','t4'],resolution_sources:[]}],
      knowledge_history:[{version:7,previous:{id:'k5',revision:1,status:'understood',text:'过去理解的旧规则'},replacement_id:'k5',invalidated:true,change_reason:'教师纠正'}]
    };
    await page.evaluate(state => handle({type:'state',elapsed:1,data:{state,note:'五状态展示'}}),fixture);
    await page.locator('[data-tab=state]').click();
    const cards = page.locator('#stateCards');
    for (const label of ['课堂推断理解','推断待确认','理解障碍','矛盾待解决','已失效','待复核 · 可重试','双方矛盾部分暂停使用']) {
      assert((await cards.innerText()).includes(label),label);
    }
    for (const detail of await cards.locator('details').all()) await detail.evaluate(el => el.open=true);
    const text=await cards.innerText();
    for (const value of ['k1（修订 1）','纠正来源：t6','此版本已失效','p1 ↔ k4']) assert(text.includes(value),value);
    await page.evaluate(state => handle({type:'knowledge_review',data:{status:'failed',targets:['k2'],state,message:'模拟复核失败',retryable:true}}),fixture);
    assert(await page.locator('#retry').isVisible());
    assert((await page.locator('#errorText').innerText()).includes('课堂可继续'));
    await page.addStyleTag({content:'.pane{height:auto;min-height:320px}'});
    await page.locator('.debug').screenshot({path:path.join(temporary,'knowledge-machine.png')});
    // 旧协议没有新增字段时仍可展示，不对历史 conflict 做改写。
    await page.evaluate(() => showState({version:1,knowledge:[{id:'old',text:'旧认知',status:'conflict',sources:['t1']}],questions:[]}));
    assert((await cards.innerText()).includes('矛盾待解决'));
    assert.deepEqual(errors,[]);
    console.log('PASS five-state UI: reasons, dependencies, conflicts, invalid history, retry, legacy records');
    console.log('Screenshot: '+path.join(temporary,'knowledge-machine.png'));
  } finally {
    if (browser) await browser.close();
    const exited = new Promise(resolve => {if(server.exitCode !== null) resolve(); else server.once('exit',resolve);});
    server.kill(); await exited;
  }
}
main().catch(error => {console.error(error);process.exitCode=1;});
