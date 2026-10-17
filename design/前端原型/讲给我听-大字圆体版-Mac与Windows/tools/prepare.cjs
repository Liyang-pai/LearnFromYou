// 从已验收的设计HTML提取本地模板与素材，不修改原始设计或产品仓库。
'use strict';
const fs=require('fs'),path=require('path');
const root=path.resolve(__dirname,'..');
const source=process.argv[2];
if(!source)throw new Error('请传入设计交付目录');
const manifest=JSON.parse(fs.readFileSync(path.join(source,'manifest.json'),'utf8'));
const screens={};
for(const page of manifest.pages){
  const html=fs.readFileSync(path.join(source,'html',page.name+'.html'),'utf8');
  screens[page.name.slice(0,2)]={name:page.name,category:page.category,style:html.match(/<style>([\s\S]*?)<\/style>/)[1],body:html.match(/<body>([\s\S]*?)<\/body>/)[1].replaceAll('../assets/','assets/')};
}
fs.mkdirSync(path.join(root,'assets'),{recursive:true});
fs.copyFileSync(path.join(source,manifest.character.file),path.join(root,'assets','小纸-用户原图.png'));
fs.writeFileSync(path.join(root,'src','screens.js'),'// 来自42张设计画面的本地模板；只读数据，无业务连接。\nwindow.PROTOTYPE_SCREENS='+JSON.stringify(screens)+';\n','utf8');
console.log('Prepared '+Object.keys(screens).length+' local screens and original character.');
