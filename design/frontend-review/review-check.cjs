// 复核共享设计目录的数量、图片摘要、相对链接和个人路径；不访问业务服务或网络。
'use strict';
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const sharp = require('sharp');
const root = __dirname;
const digest = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
function walk(dir) {
  return fs.readdirSync(dir, {withFileTypes:true}).flatMap(entry => {
    if (entry.name === 'node_modules' || entry.name === '.git') return [];
    const file = path.join(dir, entry.name);
    return entry.isDirectory() ? walk(file) : [file];
  });
}
(async () => {
  const failures = [], versions = [];
  for (const name of ['v1-placeholder', 'v2-xiaozhi']) {
    const dir = path.join(root, name);
    const manifest = JSON.parse(fs.readFileSync(path.join(dir, 'manifest.json'), 'utf8'));
    const verification = JSON.parse(fs.readFileSync(path.join(dir, 'verification.json'), 'utf8'));
    if (manifest.pages.length !== 42) failures.push(name + ': 单页数量错误');
    if (verification.failures.length || verification.pageErrors.length) failures.push(name + ': 页面验证未通过');
    for (const page of manifest.pages) {
      if (digest(fs.readFileSync(path.join(dir, page.png))) !== page.sha256) failures.push(name + '/' + page.png + ': SHA-256不一致');
      if (page.qa.overflow.length) failures.push(name + '/' + page.png + ': 存在画幅裁切');
    }
    for (let n = 1; n <= 5; n++) await sharp(path.join(dir, 'overview', `总览-${n}.png`)).raw().toBuffer();
    if (manifest.character) {
      const asset = path.join(dir, manifest.character.file);
      if (digest(fs.readFileSync(asset)) !== manifest.character.sha256) failures.push(name + ': 角色原图摘要不符');
      await sharp(asset).raw().toBuffer();
    }
    versions.push({name, pages:manifest.pages.length, overviews:5, pngHashesMatch:true, priorPageVerificationPassed:!verification.failures.length&&!verification.pageErrors.length});
  }
  const files = walk(root);
  const pngCount = files.filter(f => f.endsWith('.png')).length;
  if (pngCount !== 95) failures.push('PNG总数不是95');
  const privatePath = /\b[A-Za-z]:[\\/]|wxid_|AppData[\\/]|\.cache[\\/]codex-runtimes/i;
  const secretPattern = /(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----)/;
  let checkedLinks = 0;
  for (const file of files) {
    if (/\.(?:zip|env)$/i.test(file)) failures.push(path.relative(root,file) + ': 不应提交此类文件');
    if (!/\.(?:md|html|json|cjs)$/i.test(file)) continue;
    const text = fs.readFileSync(file,'utf8');
    // 此检查脚本自身包含匹配式，不把检查规则误当成泄漏内容。
    if (file !== __filename && (privatePath.test(text) || secretPattern.test(text))) failures.push(path.relative(root,file) + ': 疑似个人路径或凭据');
    if (!/\.(?:md|html)$/i.test(file)) continue;
    const links = file.endsWith('.md') ? [...text.matchAll(/\]\(([^)\s]+)\)/g)].map(m=>m[1]) : [...text.matchAll(/(?:href|src)=["']([^"']+)["']/g)].map(m=>m[1]);
    for (const link of links) {
      if (/^(?:https?:|data:|mailto:|#)/i.test(link)) continue;
      const target = decodeURIComponent(link.split('#')[0].split('?')[0]);
      if (target && !fs.existsSync(path.resolve(path.dirname(file),target))) failures.push(path.relative(root,file) + ': 相对链接缺失 ' + target);
      checkedLinks++;
    }
  }
  const report = {verified:'2026-10-17 Asia/Shanghai',scope:'仅共享设计材料；不验证产品功能',versions,pngCount,checkedLinks,privatePathAndCredentialScanPassed:!failures.some(f=>f.includes('凭据')),failures};
  fs.writeFileSync(path.join(root,'review-verification.json'), JSON.stringify(report,null,2),'utf8');
  console.log(JSON.stringify(report,null,2));
  if (failures.length) process.exit(1);
})().catch(error => { console.error(error); process.exit(1); });
