import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
const source=await readFile(new URL("../solar/static/slash.js",import.meta.url),"utf8");
const {suggestions,nextSelection,attachSlashMenu}=await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const argumentsFor=name=>name==="/effort"?[{value:"medium",label:"Medium"},{value:"max",label:"Max"}]:[];
assert(suggestions("/").length>10);
assert.deepEqual(suggestions("/app").map(s=>s.value),["/approvals "]);
assert.deepEqual(suggestions("/effort ma",argumentsFor).map(s=>s.value),["/effort max"]);
assert.equal(suggestions("Task /effort").length,0);
assert.equal(suggestions("/effort\nmalformed",argumentsFor).length,0);
assert.equal(nextSelection(0,-1,3),2);
assert.equal(nextSelection(2,1,3),0);
assert.equal(nextSelection(0,1,0),-1);

// A minimal DOM event harness exercises the real controller. Browser acceptance
// separately checks rendering, focus and layout in an actual browser engine.
class Element {
  constructor(){this.children=[];this.listeners={};this.attributes={};this.value="";this.hidden=true;this.classes=new Set();this.classList={toggle:(name,yes)=>yes?this.classes.add(name):this.classes.delete(name)};}
  setAttribute(name,value){this.attributes[name]=value;}
  removeAttribute(name){delete this.attributes[name];}
  addEventListener(name,listener){(this.listeners[name]??=[]).push(listener);}
  append(...children){this.children.push(...children);}
  replaceChildren(){this.children=[];}
  focus(){this.focused=true;}
  setSelectionRange(a,b){this.selectionStart=a;this.selectionEnd=b;}
  scrollIntoView(){}
  dispatch(name,properties={}){
    const event={key:"",...properties,preventDefault(){this.defaultPrevented=true;},stopImmediatePropagation(){this.stopped=true;}};
    for(const callback of this.listeners[name]||[]){callback(event);if(event.stopped)break;}
    return event;
  }
}
globalThis.document={createElement:()=>new Element()};
const input=new Element(),menu=new Element();
attachSlashMenu(input,menu,argumentsFor);
let submitted=0;
input.addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey&&!e.isComposing)submitted++;});
input.value="/";input.dispatch("input");
assert.equal(menu.hidden,false);
assert.equal(input.attributes["aria-expanded"],"true");
assert.equal(menu.children[0].attributes["aria-selected"],"true");
input.dispatch("keydown",{key:"ArrowDown"});
assert.equal(menu.children[1].attributes["aria-selected"],"true");
input.dispatch("keydown",{key:"Enter"});
assert.equal(input.value,"/effort ");
assert.equal(submitted,0);
assert.equal(menu.children.length,2);
input.dispatch("keydown",{key:"ArrowUp"});
input.dispatch("keydown",{key:"Tab"});
assert.equal(input.value,"/effort max");
assert.equal(menu.hidden,true);
assert.equal(input.selectionStart,input.value.length);
input.dispatch("keydown",{key:"Enter"});assert.equal(submitted,1);
input.value="/model";input.dispatch("input");
input.dispatch("keydown",{key:"Escape"});assert.equal(menu.hidden,true);
assert.equal(input.attributes["aria-expanded"],"false");
input.value="/unknown";input.dispatch("input");assert.equal(menu.hidden,true);
input.value="/";input.dispatch("input");
assert.equal(input.dispatch("keydown",{key:"Enter",shiftKey:true}).defaultPrevented,undefined);
assert.equal(input.dispatch("keydown",{key:"Enter",isComposing:true}).defaultPrevented,undefined);
input.dispatch("blur");assert.equal(menu.hidden,true);
console.log("Slash menu: matching, argument completion, arrow wrapping, Tab/Enter, Escape, IME and submit ordering passed.");
