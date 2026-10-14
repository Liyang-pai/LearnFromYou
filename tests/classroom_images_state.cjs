'use strict';
// Actual controller, a tiny DOM substitute; no browser, network or model calls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) nodes.set(id, {
    value:'', textContent:'', disabled:false, src:'',
    classList:{add(){},remove(){},toggle(){}},
    addEventListener(){}, setAttribute(){}, removeAttribute(){},
    querySelector(){return node(id+'-caption');}
  });
  return nodes.get(id);
}
const revoked=[];
const ctx=vm.createContext({document:{getElementById:node},URL:{
  createObjectURL(){return 'blob:draft';}, revokeObjectURL(url){revoked.push(url);}
}});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../frontend/classroom-images.js'),'utf8'),ctx);
const Input=vm.runInContext('ClassroomImageInput',ctx);
const sid='012345abcdef';
const input=new Input({session:()=>sid});
input.setCurrent({id:'confirmed',url:`/api/sessions/${sid}/images/confirmed`});
input.select({name:'old.png',type:'image/png',size:30});
assert.equal(node('imageUpload').disabled,false);
input.select({name:'bad.svg',type:'image/svg+xml',size:20});
assert.equal(input.file,null,'invalid replacement must remove the old pending draft');
assert.equal(input.previewURL,null);
assert.equal(node('imageUpload').disabled,true);
assert.equal(input.current.id,'confirmed','confirmed teaching material must survive a bad draft');
assert.deepEqual(revoked,['blob:draft']);
input.select({name:'new.png',type:'image/png',size:30});
input.select({name:'huge.png',type:'image/png',size:6*1024*1024});
assert.equal(input.file,null);
assert.equal(node('imageUpload').disabled,true);
assert.equal(input.current.id,'confirmed');
console.log('Invalid format/oversize clear drafts and preserve confirmed material.');
