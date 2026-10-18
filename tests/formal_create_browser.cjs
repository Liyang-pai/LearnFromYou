'use strict';
// 离线加载真实入口和页面模块，覆盖缺少 ASR/API 时的导航及创建页状态。
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '../frontend');
const base = 'http://127.0.0.1:18979';

(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.CHROME_PATH});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    const errors = [], unexpected = [];
    page.on('pageerror', error => errors.push(error.message));
    let asr = false, configured = false, renders = 0;
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin !== base || route.request().method() !== 'GET') {
        unexpected.push(route.request().url()); return route.abort();
      }
      if (url.pathname === '/api/health') return route.fulfill({json:{asr_ready:asr,model_configured:configured}});
      if (url.pathname === '/api/settings') return route.fulfill({json:{tts:true,asr_review:true,model_configured:configured}});
      if (url.pathname === '/api/history') return route.fulfill({json:{items:[],total:0,indexing:false}});
      const relative = url.pathname.startsWith('/static/') ? decodeURIComponent(url.pathname.slice(8)) : 'formal/index.html';
      const file = path.resolve(root, relative);
      assert.ok(file.startsWith(root + path.sep));
      const contentType = {'.html':'text/html','.js':'text/javascript','.css':'text/css','.png':'image/png','.svg':'image/svg+xml','.woff2':'font/woff2'}[path.extname(file)];
      return route.fulfill({body:await fs.readFile(file),contentType});
    });

    for (asr of [false,true]) for (configured of [false,true]) {
      const label = `ASR=${asr}, API=${configured}`;
      await page.goto(base + '/create');
      await page.locator('#setup-form').waitFor();
      for (const variant of ['empty','profile','generating','failed']) {
        await page.evaluate(async variant => {
          const {state} = await import('/static/formal/service.js');
          const {create} = await import('/static/formal/prototype.js');
          state.setup.topic = '导航回归主题';
          state.setup.student_profile = variant === 'empty' ? '' : '知道运动与静止，尚未学习惯性。';
          state.profileBusy = variant === 'generating';
          state.profileError = variant === 'failed' ? '离线模拟画像生成失败' : '';
          document.querySelector('#view').innerHTML = create(state);
        }, variant);
        assert.deepEqual(await page.locator('.sidebar .nav-item').allTextContents(),
          ['首页','创建试讲','我的教室','设置','调试前端'], `${label}, ${variant}: 导航必须完整`);
        assert.equal(await page.locator('.sidebar .active').getAttribute('href'), '/create');
        const change = page.locator('#setup-form [data-action="open-asr"]');
        assert.equal(await change.count(), 1, `${label}, ${variant}: 更换模型入口必须保留`);
        const status = change.locator('..');
        assert.match(await status.textContent(), asr ? /语音识别已就绪/ : /语音识别待准备/);
        assert.equal(await status.locator('svg').count(), 1, '更新状态文字不能破坏图标');
        assert.equal(await page.locator('#setup-topic').inputValue(), '导航回归主题');
        renders++;
      }
      // 使用真实控制器切页，验证缺少配置时仍能进入设置、首页和返回创建页。
      await page.evaluate(async () => {
        const {state} = await import('/static/formal/service.js');
        state.profileBusy = false; state.profileError = '';
      });
      for (const href of ['/settings','/','/create']) {
        await page.locator(`.sidebar a[href="${href}"]`).click();
        await page.waitForURL(base + href);
        assert.equal(await page.locator('.sidebar .active').getAttribute('href'), href);
      }
      assert.equal(await page.locator('#setup-topic').inputValue(), '导航回归主题');
    }
    assert.deepEqual(errors, [], '浏览器不应出现脚本错误');
    assert.deepEqual(unexpected, [], '回归不得访问外网或调用模型');
    console.log(`formal create: ${renders} render cases and 4 navigation round trips passed (offline)`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
