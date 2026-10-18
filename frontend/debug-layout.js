// 复用指定原型的调试外壳，仅移动原调试节点，保留其事件、状态与完整诊断能力。
import {template,shell} from './formal/prototype.js';
const legacy=document.querySelector('body>main'),header=document.querySelector('body>header');
if(legacy){
  const root=shell(template('29'),'/debug'),main=root.querySelector('.main'),head=main.querySelector('.page-head');
  main.replaceChildren(head);
  const status=head.querySelector('.row-actions');if(header){for(const node of header.querySelectorAll('a:not(.brand),#health'))status.append(node);header.remove();}
  const version=document.querySelector('#runtimeVersion');if(version)head.after(version);
  const note=legacy.querySelector('.development-note');note?.remove();legacy.querySelector('.intro')?.remove();
  for(const node of [...legacy.children])main.append(node);
  legacy.replaceWith(root);document.body.classList.add('prototype-debug');
  document.querySelector('.workspace')?.classList.add('debug-grid');
  for(const b of document.querySelectorAll('.main button'))b.classList.add('btn');
  for(const input of document.querySelectorAll('.main input:not([type=checkbox]):not([type=radio]):not([type=file]),.main textarea,.main select'))input.classList.add('input');
}
