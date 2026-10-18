// Keep the stage image connected during transcript/status updates so it never
// needs to be painted as a newly inserted image. Other content is still refreshed.
export function updateClassroomView(view,markup) {
  const template=document.createElement('template');
  template.innerHTML=markup;
  const selector='[data-stage-student]';
  const previous=view.querySelector(selector),next=template.content.querySelector(selector);
  if(!previous || !next || previous.dataset.stageStudent!==next.dataset.stageStudent) {
    view.replaceChildren(template.content);
    return;
  }
  updateBranch(view,template.content,previous,next);
}

function syncAttributes(current,next) {
  for(const {name} of [...current.attributes]) {
    if(!next.hasAttribute(name))current.removeAttribute(name);
  }
  for(const {name,value} of next.attributes) {
    // In particular, do not reassign an unchanged image source.
    if(current.getAttribute(name)!==value)current.setAttribute(name,value);
  }
}

function updateBranch(current,next,image,nextImage) {
  if(current===image) {
    syncAttributes(current,nextImage);
    return;
  }
  const retained=[...current.childNodes].find(node=>node===image || node.contains(image));
  const replacement=[...next.childNodes].find(node=>node===nextImage || node.contains(nextImage));
  // Only the ancestor branch containing the student survives. Never detach it.
  for(const child of [...current.childNodes])if(child!==retained)child.remove();
  let after=false;
  for(const child of [...next.childNodes]) {
    if(child===replacement){after=true;continue;}
    current.insertBefore(child,after?null:retained);
  }
  syncAttributes(retained,replacement);
  updateBranch(retained,replacement,image,nextImage);
}
