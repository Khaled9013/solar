import {extension,fetchBytes,TEXT_LIMIT,PREVIEW_LIMIT} from './file-source.js';
import {renderMarkdown} from './markdown.js';

const MODELS=['step','stp','iges','igs','brep','brp','stl','obj','ply','glb','gltf'];
const IMAGES=['png','jpg','jpeg','gif','webp','bmp'];
const TEXT=['md','markdown','mdown','txt','csv','tsv','json','jsonl','yaml','yml','toml','ini','log','py','js','mjs','ts','tsx','jsx','html','htm','css','xml','svg','c','h','cpp','hpp','cs','rs','go','sh','ps1','bat','sql','scad','gcode'];
let current=null;
function node(tag,cls,text){const item=document.createElement(tag);item.className=cls||'';if(text!==undefined)item.textContent=text;return item;}
function action(label,fn,cls='button small ghost'){const item=node('button',cls,label);item.type='button';item.addEventListener('click',fn);return item;}
export function closeFileViewer(){current?.close();}
export async function openFileViewer(source,{actions=[]}={}) {
  current?.close();
  const dialog=node('dialog','file-viewer'),head=node('div','viewer-head'),copy=node('div'),title=node('h2','',source.path.split('/').pop()),path=node('p','muted small',source.path),buttons=node('div','viewer-head-actions');
  title.id='file-viewer-title';dialog.setAttribute('aria-labelledby',title.id);copy.append(title,path);
  const download=node('a','button small primary','Download ↓');download.href=source.download;
  const expand=action('Expand',()=>{const expanded=dialog.classList.toggle('viewer-expanded');expand.textContent=expanded?'Restore':'Expand';expand.setAttribute('aria-pressed',String(expanded));});expand.setAttribute('aria-pressed','false');
  buttons.append(download,expand,action('Close',()=>dialog.close()));head.append(copy,buttons);
  const body=node('div','viewer-body'),status=node('p','viewer-loading','Loading preview…');status.setAttribute('role','status');body.append(status);dialog.append(head,body);
  if(actions.length){const footer=node('div','viewer-footer');for(const item of actions)footer.append(action(item.label,()=>{dialog.close();item.run();},item.className));dialog.append(footer);}
  const controller=new AbortController(),signal=controller.signal,urls=[];let dispose;
  dialog.addEventListener('close',()=>{controller.abort();dispose?.();urls.forEach(url=>URL.revokeObjectURL(url));dialog.remove();if(current===dialog)current=null;},{once:true});
  document.body.append(dialog);current=dialog;dialog.showModal();
  function imageURL(bytes,type){const url=URL.createObjectURL(new Blob([bytes],{type}));urls.push(url);return url;}
  async function openRelative(href){try{await openFileViewer(source.resolve(href));}catch(error){const warning=node('p','form-error',error.message);body.prepend(warning);}}
  try {
    const format=extension(source.path);
    if(![...TEXT,...MODELS,...IMAGES,'pdf'].includes(format)){
      status.textContent='A preview is not available for this format. Use Download to open it in another application.';return;
    }
    const isText=TEXT.includes(format),isImage=IMAGES.includes(format),limit=isText?TEXT_LIMIT:PREVIEW_LIMIT;
    if(source.size>limit&&!isImage)throw new Error(`This file exceeds the ${limit/1024**2} MB preview limit. Use Download.`);
    const bytes=await fetchBytes(isImage&&source.preview?source.preview:source.url,signal,limit);signal.throwIfAborted();
    if(isImage){
      const img=node('img','viewer-image');img.alt=source.path;img.src=imageURL(bytes,source.preview?'image/jpeg':format==='jpg'?'image/jpeg':`image/${format}`);
      img.addEventListener('error',()=>{body.replaceChildren(node('p','form-error','This image could not be decoded.'));},{once:true});body.replaceChildren(img);
    }else if(format==='pdf')dispose=await pdfViewer(body,bytes,signal);
    else if(MODELS.includes(format)){const {modelViewer}=await import('./model-viewer.js');signal.throwIfAborted();dispose=await modelViewer(body,source,bytes,signal);}
    else {
      const text=new TextDecoder().decode(bytes);if(text.includes('\0'))throw new Error('This is a binary file. Use Download to open it.');
      if(['md','markdown','mdown'].includes(format)){
        let imageCount=0,imageBytes=0;
        const content=renderMarkdown(text,{openFile:openRelative,imageFile:async(href,alt,placeholder)=>{
          if(++imageCount>20)return;
          try {const related=source.resolve(href);if(!IMAGES.includes(extension(related.path)))return;
            const data=await fetchBytes(related.preview||related.url,signal,5*1024**2);imageBytes+=data.length;if(imageBytes>20*1024**2)return;
            signal.throwIfAborted();const img=node('img');img.alt=alt||href;img.src=imageURL(data,related.preview?'image/jpeg':`image/${extension(related.path)==='jpg'?'jpeg':extension(related.path)}`);placeholder.replaceChildren(img);
          }catch{/* An unavailable relative image stays as descriptive text. */}
        }});
        const toolbar=node('div','viewer-toolbar'),raw=node('pre','viewer-text',text);raw.hidden=true;
        const toggle=action('Show source',()=>{raw.hidden=!raw.hidden;content.hidden=!raw.hidden;toggle.textContent=raw.hidden?'Show source':'Rendered view';});toolbar.append(toggle);body.replaceChildren(toolbar,content,raw);
      }else body.replaceChildren(node('pre','viewer-text',text));
    }
  } catch(error){if(!signal.aborted){dispose?.();body.replaceChildren(node('p','viewer-error',error.message||'Preview failed. The original file is still available to download.'));}}
}
async function pdfViewer(host,bytes,signal){
  const pdfjs=await import('./vendor/pdf.min.mjs');signal.throwIfAborted();
  pdfjs.GlobalWorkerOptions.workerSrc='/static/vendor/pdf.worker.min.mjs';
  const loading=pdfjs.getDocument({data:bytes,isEvalSupported:false,enableXfa:false,stopAtErrors:true,
    cMapUrl:'/static/vendor/pdfjs/cmaps/',cMapPacked:true,standardFontDataUrl:'/static/vendor/pdfjs/standard_fonts/',wasmUrl:'/static/vendor/pdfjs/wasm/',maxImageSize:16000000});
  let pdf,renderTask,disposed=false,revision=0,pageNumber=1,zoom=1,fitted=true,timer;
  const dispose=()=>{if(disposed)return;disposed=true;clearTimeout(timer);observer?.disconnect();renderTask?.cancel();loading.destroy();};
  let observer;signal.addEventListener('abort',dispose,{once:true});
  try {pdf=await loading.promise;signal.throwIfAborted();}catch(error){dispose();throw error;}
  const toolbar=node('div','viewer-toolbar'),stage=node('div','pdf-stage'),label=node('span','small'),error=node('p','form-error'),input=node('input','pdf-page-number');input.type='number';input.min=1;input.max=pdf.numPages;input.value=1;input.setAttribute('aria-label','PDF page number');
  const previous=action('← Previous',()=>go(pageNumber-1)),next=action('Next →',()=>go(pageNumber+1));
  const canvas=node('canvas');canvas.setAttribute('aria-label','PDF page preview');stage.append(canvas);
  toolbar.append(previous,input,label,next,action('−',()=>{fitted=false;zoom=Math.max(.25,zoom/1.2);draw();}),action('+',()=>{fitted=false;zoom=Math.min(4,zoom*1.2);draw();}),action('Fit width',()=>{fitted=true;draw();}));
  host.replaceChildren(toolbar,error,stage);input.addEventListener('change',()=>go(Number(input.value)));
  function go(value){pageNumber=Number.isFinite(value)?Math.min(pdf.numPages,Math.max(1,Math.trunc(value))):pageNumber;draw();}
  async function draw(){
    const version=++revision;renderTask?.cancel();error.textContent='';
    try {
      const page=await pdf.getPage(pageNumber);if(disposed||version!==revision)return;
      if(renderTask){try{await renderTask.promise;}catch{}}
      if(disposed||version!==revision)return;
      const initial=page.getViewport({scale:1});
      if(!Number.isFinite(initial.width)||!Number.isFinite(initial.height)||initial.width<=0||initial.height<=0)throw new Error('Invalid PDF page dimensions');
      if(fitted)zoom=Math.min(4,Math.max(.001,(stage.clientWidth-32)/initial.width));
      const viewport=page.getViewport({scale:zoom}),ratio=Math.min(devicePixelRatio||1,2,16384/viewport.width,16384/viewport.height,Math.sqrt(16000000/(viewport.width*viewport.height)));
      canvas.width=Math.max(1,Math.floor(viewport.width*ratio));canvas.height=Math.max(1,Math.floor(viewport.height*ratio));canvas.style.width=`${viewport.width}px`;canvas.style.height=`${viewport.height}px`;
      input.value=pageNumber;label.textContent=`of ${pdf.numPages} · ${Math.round(zoom*100)}%`;previous.disabled=pageNumber===1;next.disabled=pageNumber===pdf.numPages;
      renderTask=page.render({canvasContext:canvas.getContext('2d'),viewport,transform:ratio===1?null:[ratio,0,0,ratio,0,0],annotationMode:pdfjs.AnnotationMode.DISABLE});
      await renderTask.promise;
    }catch(cause){if(!disposed&&version===revision&&cause.name!=='RenderingCancelledException')error.textContent=cause.message||'Could not render this PDF page';}
  }
  let width=0;observer=new ResizeObserver(()=>{if(fitted&&stage.clientWidth!==width){width=stage.clientWidth;clearTimeout(timer);timer=setTimeout(draw,100);}});observer.observe(stage);await draw();return dispose;
}
