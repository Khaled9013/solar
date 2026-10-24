import { lexer } from './vendor/marked.esm.js';
import { externalLink } from './file-source.js';

// Render lexer tokens with DOM operations. Uploaded HTML never becomes markup.
export function renderMarkdown(source, {openFile, imageFile}={}) {
  const root=document.createElement('article');root.className='markdown-content';
  let count=0;
  function node(tag,text){const element=document.createElement(tag);if(text!==undefined)element.textContent=text;return element;}
  function tokens(items,parent,depth=0){
    if(depth>40)throw new Error('Markdown nesting is too deep');
    for(const token of items||[]){
      if(++count>50000)throw new Error('Markdown is too complex to preview');
      let child;
      switch(token.type){
        case 'space':continue;
        case 'heading':child=node(`h${Math.min(6,Math.max(1,token.depth))}`);break;
        case 'paragraph':child=node('p');break;
        case 'text':case 'escape':
          if(token.tokens){tokens(token.tokens,parent,depth+1);}else parent.append(document.createTextNode(token.text||''));continue;
        case 'strong':child=node('strong');break;
        case 'em':child=node('em');break;
        case 'del':child=node('del');break;
        case 'codespan':parent.append(node('code',token.text));continue;
        case 'code':child=node('pre');child.append(node('code',token.text));parent.append(child);continue;
        case 'hr':parent.append(node('hr'));continue;
        case 'br':parent.append(node('br'));continue;
        case 'blockquote':child=node('blockquote');break;
        case 'list':
          child=node(token.ordered?'ol':'ul');if(token.ordered&&Number.isInteger(token.start))child.start=token.start;
          for(const item of token.items){const li=node('li');if(item.task){const check=node('input');check.type='checkbox';check.disabled=true;check.checked=item.checked;li.append(check);}tokens(item.tokens,li,depth+1);child.append(li);}parent.append(child);continue;
        case 'table':{
          const wrap=node('div');wrap.className='markdown-table';child=node('table');const head=node('thead'),row=node('tr');
          for(const cell of token.header){const th=node('th');tokens(cell.tokens,th,depth+1);row.append(th);}head.append(row);child.append(head);
          const body=node('tbody');for(const cells of token.rows){const tr=node('tr');for(const cell of cells){const td=node('td');tokens(cell.tokens,td,depth+1);tr.append(td);}body.append(tr);}child.append(body);wrap.append(child);parent.append(wrap);continue;}
        case 'link':{
          const href=externalLink(token.href);child=node(href?'a':'button');
          if(href){child.href=href;child.target='_blank';child.rel='noopener noreferrer';}
          else if(openFile){child.type='button';child.className='text-button';child.addEventListener('click',()=>openFile(token.href));}
          else {child=node('span');}
          break;}
        case 'image':{
          // No tracking pixels or external image requests from model-authored files.
          child=node('span',`[Image: ${token.text||token.href}]`);
          if(imageFile&&!/^(?:[a-z][a-z\d+.-]*:|\/)/i.test(token.href))imageFile(token.href,token.text,child);
          parent.append(child);continue;}
        case 'html':parent.append(node('code',token.text||token.raw));continue;
        default:parent.append(document.createTextNode(token.raw||token.text||''));continue;
      }
      tokens(token.tokens||[{type:'text',text:token.text||''}],child,depth+1);parent.append(child);
    }
  }
  tokens(lexer(source,{gfm:true}),root);return root;
}
