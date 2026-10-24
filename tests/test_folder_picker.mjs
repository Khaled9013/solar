// Exercise the actual folder picker controller with a small DOM/event harness.
// Browser acceptance separately checks native dialogs, focus and visual layout.
import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import vm from "node:vm";
const source=await readFile(new URL("../solar/static/app.js",import.meta.url),"utf8");
const controller=source.slice(source.indexOf("// Remote folder selection"),source.indexOf("// End remote folder selection."));
assert(controller.includes("function openWorkspaceFolderDialog"));
class Element extends EventTarget {
  constructor(tag){super();this.tagName=tag;this.children=[];this.dataset={};this.attributes={};this.value="";this.textContent="";this.disabled=false;this.hidden=false;this.isConnected=true;}
  append(...nodes){for(const node of nodes){node.parent=this;this.children.push(node);}}
  replaceChildren(...nodes){this.children=[];this.append(...nodes);}
  closest(tag){return this.tagName===tag?this:this.parent?.closest(tag);}
  setAttribute(name,value){this.attributes[name]=value;}
  showModal(){this.open=true;}
  close(){this.open=false;this.dispatchEvent(new Event("close"));}
  remove(){this.isConnected=false;if(this.parent)this.parent.children=this.parent.children.filter(node=>node!==this);}
  focus(){this.focused=true;}
  click(){if(!this.disabled)this.dispatchEvent(new Event("click"));}
}
const body=new Element("body");
const all=(root)=>root.children.flatMap(node=>[node,...all(node)]);
const el=(tag,cls="",text="")=>{const node=new Element(tag);node.className=cls;node.textContent=text;return node;};
const append=(parent,...nodes)=>{parent.append(...nodes);return parent;};
const errors=[];
const button=(text,handler,cls="")=>{const node=el("button",cls,text);node.addEventListener("click",async event=>{try{await handler(event);}catch(error){errors.push(error);}});return node;};
const state={user:{host_access:true},projects:[]};
const requests=[];let conflict=false;
const registered=[];
const api=async(path,method="GET",data)=>{
  requests.push({path,method,data});
  if(path.startsWith("workspace-folders/")){
    const pathName=new URL("http://test/"+path).searchParams.get("path");
    if(pathName==="D:\\missing")throw new Error("Directory does not exist");
    return pathName?{path:pathName,parent:"D:\\",directories:[{name:"existing",path:"D:\\work\\existing"}]}:{path:"",parent:null,roots:["D:\\"],directories:[]};
  }
  if(path==="projects/"&&method==="GET")return {projects:[...registered]};
  if(path==="projects/"&&method==="POST"){
    const project={id:`project-${registered.length}`,name:data.name,path:data.path};registered.push(project);
    if(conflict){conflict=false;throw new Error("This record already exists");}
    return project;
  }
  throw new Error("Unexpected request "+path);
};
const context={state,api,el,append,button,Event,document:{body},$$:(selector,root)=>all(root).filter(node=>selector.split(",").includes(node.tagName))};
vm.createContext(context);vm.runInContext(controller,context);
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const hidden=node=>node.hidden||(node.parent?hidden(node.parent):false);
const find=(root,text)=>all(root).find(node=>node.tagName==="button"&&node.textContent===text&&!hidden(node));
const field=(root,name)=>all(root).find(node=>node.name===name);
function makeForm(){
  const form=el("form"),label=el("label"),project=el("select"),permission=el("select");
  project.name="project_id";permission.name="permission";permission.value="read-only";
  form.elements={project_id:project,permission,title:{value:"My unsaved task"},effort:{value:"xhigh"}};
  label.append(project);form.append(label,permission);body.append(form);return form;
}
const form=makeForm();context.installWorkspacePicker(form);
assert(form.elements.project_id.children.some(option=>option.value==="__browse__"));
find(form,"Browse folders…").click();await settle();
let dialog=all(body).find(node=>node.tagName==="dialog");
assert(dialog.open);assert.equal(form.elements.title.value,"My unsaved task");
assert(find(dialog,"Use this folder").disabled);
find(dialog,"▱ D:\\").click();await settle();
assert(find(dialog,"Use this folder").disabled,"Drive root must not be a workspace");
field(dialog,"remote_folder_path").value="D:\\work";find(dialog,"Go").click();await settle();
find(dialog,"New folder").click();field(dialog,"remote_new_folder").value="results";
find(dialog,"Create & use folder").click();await settle();
assert.equal(dialog.open,false);
assert.equal(form.elements.project_id.value,"project-0");
assert.equal(state.projects[0].path,"D:\\work\\results");
assert.equal(requests.find(item=>item.method==="POST").data.create,true);
assert.equal(form.elements.title.value,"My unsaved task");assert.equal(form.elements.effort.value,"xhigh");
assert.equal(form.elements.permission.value,"read-only","Selecting a folder must not silently expand permissions");
find(form,"Enable workspace writes").click();assert.equal(form.elements.permission.value,"workspace-write");
find(form,"Browse folders…").click();await settle();dialog=all(body).find(node=>node.tagName==="dialog");
field(dialog,"remote_folder_path").value="D:\\missing";find(dialog,"Go").click();await settle();
assert(all(dialog).some(node=>node.textContent==="Directory does not exist"));
assert(find(dialog,"Use this folder").disabled);
find(dialog,"Cancel").click();assert.equal(dialog.open,false);assert.equal(form.elements.project_id.value,"project-0");
assert.equal(form.elements.title.value,"My unsaved task");
// A registered folder can be picked again without duplicate registration.
const before=requests.filter(item=>item.method==="POST").length;
find(form,"Browse folders…").click();await settle();dialog=all(body).find(node=>node.tagName==="dialog");
find(dialog,"Use this folder").click();await settle();
assert.equal(requests.filter(item=>item.method==="POST").length,before);
assert.equal(form.elements.project_id.value,"project-0");
// Registrations created by another tab are reused after an owner-scoped refresh.
conflict=true;const reused=await context.registerWorkspaceFolder("D:\\work\\another");
assert.equal(reused.id,"project-1");
assert.equal(context.normalizedFolderPath("d:/WORK/results/"),context.normalizedFolderPath("D:\\work\\results"));
assert.equal(context.folderChildPath("/work","output"),"/work/output");
for(const name of ["..","../escape","a/b","a\\b",""]){assert.throws(()=>context.folderChildPath("D:\\work",name));}
const ordinary=makeForm();state.user.host_access=false;context.installWorkspacePicker(ordinary);
assert.equal(find(ordinary,"Browse folders…"),undefined);
assert.equal(errors.length,0);
console.log("Folder picker passed: browse/create/select, draft preservation, cancellation, read-only control, duplicate reuse, validation and host-only visibility.");
