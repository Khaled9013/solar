let attachSlashMenu;
const $ = (q, root = document) => root.querySelector(q);
const $$ = (q, root = document) => [...root.querySelectorAll(q)];
const state = {user:null, csrf:$("meta[name=csrf-token]").content, providers:[], projects:[], modelCatalogs:{}, chats:[], agents:[], skills:[], schedules:[], runs:[], page:"overview", chat:null, cursor:0, events:[], timer:null, socket:null, terminal:null, desktopSocket:null, desktopTimer:null, fileTree:null, modalLocked:false};
const page = $("#page");
const labels = {overview:"Overview", chats:"Sessions", projects:"Projects", chat:"Sessions", agents:"Agents", schedules:"Automations", skills:"Skills", runs:"Activity", desktop:"Remote desktop", terminal:"PowerShell", providers:"Providers", users:"Users", account:"Account"};
const activeStatuses = ["queued","running","cancelling"];
function el(tag, cls="", value) { const node=document.createElement(tag); if(cls) node.className=cls; if(value!==undefined) node.textContent=String(value); return node; }
function append(parent, ...nodes) { for(const node of nodes.flat()) if(node) parent.append(node); return parent; }
function button(label, fn, cls="button") { const node=el("button",cls,label); node.type="button"; node.addEventListener("click",async e=>{try{await fn(e);}catch(error){toast(error.message,true);}}); return node; }
function link(label, href, cls="") {const node=el("a",cls,label); node.href=href; return node;}
function badge(value) {return el("span",`status ${value}`,value.replaceAll("_"," "));}
function toast(message,error=false) {const node=el("div",`toast${error?" error":""}`,message);$("#toasts").append(node);setTimeout(()=>node.remove(),error?9000:5000);}
function date(value,short=false) {return value ? new Date(value).toLocaleString(undefined,short?{month:"short",day:"numeric"}:{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"}):"—";}
function bytes(value) {if(value<1024)return `${value} B`; if(value<1024**2)return `${(value/1024).toFixed(1)} KB`; return `${(value/1024**2).toFixed(1)} MB`;}
function providerName(id) {return state.providers.find(p=>p.id===id)?.name || "Provider";}
function agentName(id) {return state.agents.find(a=>a.id===id)?.name || "No agent role";}
async function api(path, method="GET", data, raw=false) {
  const options={method,credentials:"same-origin",headers:{"X-CSRFToken":state.csrf,"Accept":"application/json"},cache:"no-store"};
  if(data instanceof FormData)options.body=data;
  else if(data!==undefined){options.headers["Content-Type"]="application/json";options.body=JSON.stringify(data);}
  const response=await fetch(`/api/${path}`,options);
  let result;try{result=await response.json();}catch{throw new Error(`Server returned ${response.status}. Check that Solar is still running.`);}
  if(result.csrf)state.csrf=result.csrf;
  if(!response.ok){if(response.status===401 && !path.startsWith("login"))showLogin();throw new Error(result.error||`Request failed (${response.status})`);}
  return result;
}
function showLogin(){clearInterval(state.timer);stopConnections();state.user=null;$("#boot").hidden=true;$("#app").hidden=true;$("#login-view").hidden=false;$("#login-form input").focus();}
async function loadData(){const [providers,chats,agents,skills,schedules,runs,projects]=await Promise.all([api("providers/"),api("chats/"),api("agents/"),api("skills/"),api("schedules/"),api("runs/"),api("projects/")]);Object.assign(state,{projects:projects.projects,providers:providers.providers,chats:chats.chats,agents:agents.agents,skills:skills.skills,schedules:schedules.schedules,runs:runs.runs,origins:providers.allowed_origins});state.chatTotal=chats.total;$("#session-count").textContent=state.chatTotal||"";}
async function enter(user){state.user=user;$("#boot").hidden=true;$("#login-view").hidden=true;$("#app").hidden=false;$("#user-name").textContent=user.username;$("#user-avatar").textContent=user.username.slice(0,1).toUpperCase();$("#user-role").textContent=user.is_admin?"Administrator":"Personal workspace";$("#users-nav").hidden=!user.is_admin;$("#terminal-nav").hidden=!user.is_admin;applyNavigationPreference();$("#projects-nav").hidden=!user.host_access;if(user.must_change_password){page.replaceChildren();passwordForm(true);return;}await loadData();await navigate();}
$("#login-form").addEventListener("submit",async e=>{e.preventDefault();const submit=$("button",e.target);submit.disabled=true;$("#login-error").textContent="";try{const data=Object.fromEntries(new FormData(e.target));const result=await api("login/","POST",data);e.target.reset();await enter(result.user);}catch(error){$("#login-error").textContent=error.message;}finally{submit.disabled=false;}});
$("#logout").addEventListener("click",async()=>{try{await api("logout/","POST",{});showLogin();}catch(e){toast(e.message,true);}});
$("#new-session").addEventListener("click",()=>chatForm());
$("#account-button").addEventListener("click",()=>location.hash="account");
$("#refresh").addEventListener("click",async()=>{try{await loadData();await navigate();toast("Workspace refreshed");}catch(e){toast(e.message,true);}});
$("#mobile-menu").addEventListener("click",()=>{
  if(innerWidth<=650){const open=$("#sidebar").classList.toggle("mobile-open");$("#mobile-menu").setAttribute("aria-expanded",String(open));}
  else {setPanelPreference("navigation-collapsed",!$("#app").classList.contains("navigation-collapsed"));applyNavigationPreference();}
});
document.addEventListener("keydown",e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==="k"&&state.user&&!state.user.must_change_password){e.preventDefault();chatForm();}});
window.addEventListener("hashchange",()=>navigate().catch(e=>toast(e.message,true)));
function stopConnections(){state.viewerClose?.();state.terminalCleanup?.();state.terminalCleanup=null;state.socket?.close();state.socket=null;state.terminal?.dispose();state.terminal=null;state.desktopSocket?.close();state.desktopSocket=null;clearInterval(state.desktopTimer);state.desktopTimer=null;}
async function navigate(){if(!state.user||state.user.must_change_password)return;clearInterval(state.timer);stopConnections();state.chat=null;$("#sidebar").classList.remove("mobile-open");const [route,id]=(location.hash.slice(1)||"overview").split("/");state.page=labels[route]?route:"overview";$("#page-label").textContent=labels[state.page];$$('[data-page]').forEach(n=>n.classList.toggle("active",n.dataset.page===(state.page==="chat"?"chats":state.page)));page.className="page";page.replaceChildren();const generation=location.hash;try{if(state.page==="chat"){await renderChat(id);return;}const renderers={overview:renderOverview,chats:renderChats,projects:renderProjects,agents:renderAgents,schedules:renderSchedules,skills:renderSkills,runs:renderRuns,desktop:renderDesktop,terminal:renderAdminTerminal,providers:renderProviders,users:renderUsers,account:renderAccount};await renderers[state.page]();}catch(error){if(location.hash===generation){page.replaceChildren(empty("Something needs attention",error.message,"Try again",()=>navigate()));}}}
function heading(eyebrow,title,description,action){const head=el("div","page-head"),copy=el("div");append(copy,el("span","eyebrow",eyebrow),el("h1","",title),el("p","",description));append(head,copy,action);return head;}
function empty(title,description,actionLabel,action){const node=el("div","empty");append(node,el("div","empty-symbol","✧"),el("h2","",title),el("p","",description),actionLabel?button(actionLabel,action,"button primary"):null);return node;}
function section(title,actionLabel,action){const wrapper=el("section"),head=el("div","section-head");append(head,el("h3","",title),actionLabel?button(actionLabel,action,"text-button"):null);wrapper.append(head);return wrapper;}
function sessionRow(chat){const row=el("div","list-row interactive");row.tabIndex=0;row.setAttribute("role","link");row.addEventListener("click",()=>location.hash=`chat/${chat.id}`);row.addEventListener("keydown",e=>{if(e.key==="Enter")location.hash=`chat/${chat.id}`;});const copy=append(el("div","row-copy"),el("strong","",chat.title),el("small","",`${chat.provider_name} · ${chat.model||"Default model"}`));append(row,el("span","row-icon",chat.provider_kind==="codex"?"✳":chat.provider_kind==="claude"?"✴":"⌘"),copy,append(el("div","row-trailing"),chat.active_run?badge(chat.active_run.status):el("span","date",date(chat.updated_at,true)),el("span","","↗")));return row;}
function renderOverview(){const hour=new Date().getHours();append(page,heading("YOUR CONTROL ROOM",`${hour<12?"Good morning":hour<18?"Good afternoon":"Good evening"}, ${state.user.username}.`,"Everything you need to keep good work moving.",el("div","page-date",new Date().toLocaleDateString(undefined,{weekday:"long",month:"long",day:"numeric"}))));const stats=el("div","stats");for(const [name,count,foot,icon] of [["Sessions",state.chatTotal??state.chats.length,"Your private conversations","◰"],["Active runs",state.runs.filter(r=>activeStatuses.includes(r.status)).length,"Working on this machine","≋"],["Agent roles",state.agents.length,"Ready when you are","◇"],["Automations",state.schedules.filter(s=>s.enabled).length,"Scheduled and enabled","◷"]]){const card=el("div","stat");append(card,append(el("div","stat-top"),el("span","",name),el("span","stat-icon",icon)),el("strong","stat-value",count),el("span","stat-foot",foot));stats.append(card);}page.append(stats);const grid=el("div","overview-grid"),left=el("div"),right=el("div");const sessions=section("Pick up where you left off","All sessions ↗",()=>location.hash="chats");const list=el("div","panel");if(state.chats.length)state.chats.slice(0,5).forEach(c=>list.append(sessionRow(c)));else list.append(empty("A fresh start","Your conversations, files, and agent outputs will appear here.","Start a session",()=>chatForm()));sessions.append(list);left.append(sessions);const launch=el("div","launch-card");append(launch,el("span","eyebrow","MAKE SPACE FOR DEEP WORK"),el("span","launch-sun","✳"),el("h3","","A capable teammate.\nA workspace of its own."),el("p","","Start with Codex, Claude, or a local model. Every session gets its own files and history."),button("Create a session ↗",()=>chatForm(),"button primary small"));left.append(launch);const upcoming=section("On the horizon","Manage ↗",()=>location.hash="schedules"),tasks=el("div","panel");const scheduled=state.schedules.filter(s=>s.enabled).sort((a,b)=>new Date(a.next_run)-new Date(b.next_run)).slice(0,3);if(scheduled.length)scheduled.forEach(s=>tasks.append(append(el("div","list-row"),el("span","row-icon","◷"),append(el("div","row-copy"),el("strong","",s.name),el("small","",date(s.next_run))),badge("scheduled"))));else tasks.append(append(el("div","panel-body"),el("h3","","Let the routine run itself."),el("p","muted small","Set a task, pick a rhythm, and find each task’s results in its continuing session."),button("＋ Add automation",()=>scheduleForm(),"button ghost small")));upcoming.append(tasks);right.append(upcoming);const providers=section("Your providers","Configure ↗",()=>location.hash="providers"),providerPanel=el("div","panel");state.providers.slice(0,4).forEach(p=>providerPanel.append(append(el("div","list-row"),el("span","row-icon",p.kind==="codex"?"✳":p.kind==="claude"?"✴":"⌘"),append(el("div","row-copy"),el("strong","",p.name),el("small","",p.kind==="openai"?"OpenAI compatible API":"Native CLI + structured chat")),el("span","muted","↗"))));providers.append(providerPanel);providers.style.marginTop="24px";right.append(providers);append(grid,left,right);page.append(grid);}
async function renderChats() {
  append(page,heading("CONVERSATIONS","Your sessions","An independent workspace for every conversation.",button("＋ New session",()=>chatForm(),"button primary")));
  const toolbar=el("div","toolbar"),search=el("input");search.placeholder="Search all your sessions…";search.setAttribute("aria-label","Search sessions");
  const tabs=el("div","tabs"),active=button("Active",()=>show(false),"active"),archived=button("Archived",()=>show(true));
  append(tabs,active,archived);append(toolbar,search,tabs);page.append(toolbar);
  const list=el("div","panel"),more=button("Load more sessions",()=>load(false),"button ghost");
  more.style.marginTop="18px";append(page,list,more);
  let isArchived=false,offset=0,items=[],generation=0,searchTimer;
  async function load(reset=true){
    const request=++generation;more.disabled=true;
    if(reset){offset=0;items=[];}
    const data=await api(`chats/?archived=${isArchived}&offset=${offset}&q=${encodeURIComponent(search.value)}`);
    if(request!==generation||state.page!=="chats")return;
    items.push(...data.chats);offset=data.next_offset;more.hidden=offset===null;more.disabled=false;list.replaceChildren();
    if(!items.length)list.append(empty("No sessions here yet","Start a conversation, or try another search.","Start a session",()=>chatForm()));
    else items.forEach(chat=>list.append(sessionRow(chat)));
  }
  async function show(archived){isArchived=archived;active.classList.toggle("active",!archived);tabs.lastChild.classList.toggle("active",archived);await load();}
  search.addEventListener("input",()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>load().catch(e=>toast(e.message,true)),250);});
  await load();
}

// Dialog fields are built with DOM methods; user and model strings never become HTML.
const modal=$("#modal");$("#modal-close").addEventListener("click",()=>{if(!state.modalLocked)modal.close();});modal.addEventListener("cancel",e=>{if(state.modalLocked)e.preventDefault();});modal.addEventListener("close",()=>{state.modalLocked=false;$("#modal-close").hidden=false;});
function openModal(title,content,eyebrow="WORKSPACE",locked=false){$("#modal-title").textContent=title;$("#modal-eyebrow").textContent=eyebrow;$("#modal-body").replaceChildren(content);state.modalLocked=locked;$("#modal-close").hidden=locked;if(!modal.open)modal.showModal();return content;}
function formField(spec){const label=el("label",spec.full?"full":"",spec.label);let control;if(spec.type==="textarea"){control=el("textarea");control.rows=spec.rows||5;}else if(spec.type==="select"||spec.type==="multiple"){control=el("select");if(spec.type==="multiple")control.multiple=true;for(const [value,name] of spec.options||[]){const option=el("option","",name);option.value=value;control.append(option);}}else{control=el("input");control.type=spec.type||"text";}control.name=spec.name;control.required=!!spec.required;if(spec.type==="checkbox"){label.classList.add("check-label");control.checked=!!spec.value;label.prepend(control);}else{if(spec.type==="multiple")$$('option',control).forEach(o=>o.selected=(spec.value||[]).includes(o.value));else control.value=spec.value??"";label.append(control);}if(spec.placeholder)control.placeholder=spec.placeholder;if(spec.min!==undefined)control.min=spec.min;if(spec.max!==undefined)control.max=spec.max;if(spec.autocomplete)control.autocomplete=spec.autocomplete;if(spec.help)label.append(el("small","",spec.help));return label;}
function formModal(title,fields,onSave,{description="",submit="Save",locked=false,afterBuild=null}={}){const form=el("form");if(description)form.append(el("p","notice",description));const grid=el("div","form-grid");fields.forEach(f=>grid.append(formField(f)));form.append(grid);const error=el("p","form-error");error.setAttribute("role","alert");form.append(error);const actions=el("div","form-actions");if(!locked)actions.append(button("Cancel",()=>modal.close(),"button ghost"));const save=el("button","button primary",submit);save.type="submit";actions.append(save);form.append(actions);form.addEventListener("submit",async e=>{e.preventDefault();save.disabled=true;error.textContent="";const data={};for(const field of fields){const input=form.elements[field.name];data[field.name]=field.type==="checkbox"?input.checked:field.type==="multiple"?[...input.selectedOptions].map(o=>o.value):input.value;}try{await onSave(data);if(!state.modalLocked||!state.user?.must_change_password)modal.close();}catch(err){error.textContent=err.message;}finally{save.disabled=false;}});openModal(title,form,"WORKSPACE",locked);if(afterBuild)afterBuild(form);return form;}
function providerOptions(){return state.providers.map(p=>[p.id,p.name]);}function agentOptions(){return [["","No agent role"],...state.agents.map(a=>[a.id,a.name])];}function permissionOptions(){return [["read-only","Read only"],["workspace-write","Workspace write"],...(state.user.host_access?[["danger-full-access","Full host access (no provider sandbox)"]]:[])];}function effortOptions(){return ["minimal","low","medium","high","xhigh","max","ultra"].map(s=>[s,s[0].toUpperCase()+s.slice(1)]);}
function sessionFields(chat={}, scheduled=false) {
  return [
    {name:"provider_id",label:"Provider",type:"select",options:providerOptions(),value:chat.provider_id||state.providers[0]?.id,required:true},
    {name:"model",label:"Model",value:chat.model||"",placeholder:"Provider default or model ID",help:"Use any model your provider/account supports."},
    {name:"agent_id",label:"Agent role",type:"select",options:agentOptions(),value:chat.agent_id||""},
    {name:"effort",label:"Reasoning effort",type:"select",options:effortOptions(),value:chat.effort||"medium"},
    {name:"project_id",label:"Workspace / output folder on this PC",type:"select",options:[["","Private session folder"],...state.projects.map(p=>[p.id,`${p.name} — ${p.path}`])],value:chat.project_id||"",full:true,help:"Browse the remote PC to choose an existing folder or create a new one. Your session settings stay here while you choose."},
    {name:"permission",label:"Sandbox permissions",type:"select",options:permissionOptions(),value:chat.permission||"read-only",full:true,help:"The selected agent role can further restrict this setting. Full host access allows commands to modify the PC outside the workspace."},
    {name:"approval",label:"Approval policy",type:"select",options:[["default","Default: structured never asks; native asks when needed"],...(!scheduled?[["on-request","Ask when needed — interactive terminal"]]:[]),["never","Never ask — obey selected sandbox"],["auto-review","Codex automatic approval review (workspace write)"]],value:chat.approval||(scheduled?"never":"default"),full:true,help:"Never ask does not bypass the sandbox. Interactive approval opens the native terminal."},
    {name:"allow_shell",label:"Enable command execution",type:"checkbox",value:chat.allow_shell??true,full:true},
    {name:"allow_network",label:"Enable network tools / sandbox network access",type:"checkbox",value:chat.allow_network??false,full:true},
    {name:"windows_sandbox",label:"Codex Windows sandbox",type:"select",options:[["unelevated","Unelevated (no administrator setup)"],["elevated","Elevated (requires Codex sandbox setup on PC)"]],value:chat.windows_sandbox||"unelevated",full:true,help:"Used only on Windows. A missing elevated sandbox setup can prevent command execution."}
  ];
}
async function modelCatalog(id) {
  if(!state.modelCatalogs[id])state.modelCatalogs[id]=await api(`providers/${id}/models/`);
  return state.modelCatalogs[id];
}
function modelAutocomplete(form) {
  installWorkspacePicker(form);
  const input=form.elements.model, select=form.elements.provider_id;
  if(!input||!select)return;
  const list=el("datalist");list.id="model-suggestions";form.append(list);input.setAttribute("list",list.id);
  let catalog={};
  function availability() {
    const provider=state.providers.find(p=>p.id===select.value), kind=provider?.kind;
    const supported=catalog.capabilities?.find(m=>m.id===(input.value||provider?.model))?.efforts;
    for(const option of form.elements.effort.options)option.disabled=(kind==="claude"&&["minimal","ultra"].includes(option.value)) || (supported?.length>0&&!supported.includes(option.value));
    if(form.elements.effort.selectedOptions[0]?.disabled)form.elements.effort.value=supported?.includes("medium")?"medium":supported?.[0]||"medium";
    for(const option of form.elements.approval.options)option.disabled=(kind==="openai"&&!["default","never"].includes(option.value))||(option.value==="auto-review"&&kind!=="codex");
    if(form.elements.approval.selectedOptions[0]?.disabled)form.elements.approval.value="default";
    form.elements.windows_sandbox.closest("label").hidden=kind!=="codex"||!state.user.host_access;
    form.elements.allow_network.closest("label").hidden=!state.user.host_access;
    form.elements.project_id.closest("label").hidden=!state.user.host_access;
  }
  async function load() {
    catalog={};availability();const id=select.value;const result=await modelCatalog(id);
    if(select.value!==id||!form.isConnected)return;catalog=result;list.replaceChildren();
    result.models.forEach(model=>{const option=el("option");option.value=model;list.append(option);});availability();
  }
  select.addEventListener("change",()=>{input.value=state.providers.find(p=>p.id===select.value)?.model||"";load().catch(()=>{});});
  input.addEventListener("input",availability);load().catch(()=>{});
}
// Remote folder selection keeps the parent session/automation form mounted.
function normalizedFolderPath(path) {
  const value=String(path).replace(/[\\/]+$/,"");
  return /^[a-z]:[\\/]/i.test(path)?value.replaceAll("\\","/").toLowerCase():value;
}
function folderChildPath(parent,name) {
  if(!name.trim()||name.trim()==="."||name.trim()===".."||/[\\/\x00-\x1f]/.test(name))throw new Error("Enter a folder name, without a path.");
  return parent.replace(/[\\/]+$/,"")+(/^[a-z]:/i.test(parent)?"\\":"/")+name.trim();
}
async function registerWorkspaceFolder(path,create=false) {
  const match=projects=>projects.find(project=>normalizedFolderPath(project.path)===normalizedFolderPath(path));
  if(!create){const existing=match(state.projects);if(existing)return existing;}
  const name=path.replace(/[\\/]+$/,"").split(/[\\/]/).pop().slice(0,160);
  let project;
  try {project=await api("projects/","POST",{name,path,create});}
  catch(error) {
    // Another session may already have registered the same folder. Reuse its
    // owner-scoped registration instead of leaving the picker on a conflict.
    state.projects=(await api("projects/")).projects;
    project=match(state.projects);if(!project)throw error;
  }
  if(!state.projects.some(item=>item.id===project.id))state.projects.push(project);
  return project;
}
function installWorkspacePicker(form) {
  const select=form.elements.project_id;
  if(!select||!state.user.host_access||select.dataset.pickerReady)return;
  select.dataset.pickerReady="true";
  const label=select.closest("label"),actions=el("div","workspace-picker-actions"),selectedPath=el("small","workspace-selected-path"),writeNotice=el("div","workspace-write-notice");
  selectedPath.setAttribute("aria-live","polite");
  const browse=button("Browse folders…",()=>choose(),"button ghost workspace-browse");
  browse.setAttribute("aria-haspopup","dialog");
  const enableWrite=button("Enable workspace writes",()=>{
    form.elements.permission.value="workspace-write";
    form.elements.permission.dispatchEvent(new Event("change",{bubbles:true}));
    describe();
  },"text-button");
  append(writeNotice,el("span","","This session is read only. Enable workspace writes to create or edit files in the selected folder."),enableWrite);
  append(actions,browse);append(label,actions,selectedPath,writeNotice);
  let previous=select.value;
  function options(value) {
    select.replaceChildren();
    for(const [id,name] of [["","Private session folder"],...state.projects.map(p=>[p.id,`${p.name} — ${p.path}`]),["__browse__","Choose another folder…"]]){
      const option=el("option","",name);option.value=id;select.append(option);
    }
    select.value=value;previous=value;
  }
  function describe() {
    const project=state.projects.find(item=>item.id===select.value);
    selectedPath.textContent=project?`Selected folder: ${project.path}`:"";
    selectedPath.hidden=!project;
    writeNotice.hidden=!project||form.elements.permission?.value!=="read-only";
  }
  function choose() {
    openWorkspaceFolderDialog(state.projects.find(item=>item.id===select.value)?.path||"",project=>{
      if(!form.isConnected)return;
      options(project.id);describe();
      select.dispatchEvent(new Event("change",{bubbles:true}));
    },browse);
  }
  select.addEventListener("change",()=>{
    if(select.value==="__browse__"){select.value=previous;choose();}
    else {previous=select.value;describe();}
  });
  form.elements.permission?.addEventListener("change",describe);
  options(previous);describe();
}
function openWorkspaceFolderDialog(initialPath,onSelect,returnFocus) {
  const dialog=el("dialog","remote-folder-dialog");
  dialog.setAttribute("aria-labelledby","remote-folder-title");
  const title=el("h2","","Choose a folder on this PC");title.id="remote-folder-title";
  const close=button("×",()=>dialog.close(),"icon-button");close.setAttribute("aria-label","Close folder picker");
  const head=append(el("div","modal-head"),append(el("div"),el("span","eyebrow","REMOTE WORKSPACE"),title),close);
  const help=el("p","muted small","Browse this remote PC, paste a folder path, or create a folder for this session’s work and output files.");
  const pathLabel=el("label","","Folder path on the remote PC"),pathInput=el("input");
  pathInput.type="text";pathInput.name="remote_folder_path";pathInput.placeholder="Enter an absolute folder path";pathInput.autocomplete="off";pathInput.maxLength=4096;
  const go=button("Go",()=>load(pathInput.value.trim()),"button ghost");
  append(pathLabel,append(el("div","folder-path-entry"),pathInput,go));
  const toolbar=el("div","folder-dialog-toolbar"),list=el("div","remote-folder-list"),error=el("p","form-error"),chosen=el("p","folder-current-path");
  list.setAttribute("aria-label","Folders on the remote PC");list.setAttribute("role","group");
  error.setAttribute("role","alert");chosen.setAttribute("aria-live","polite");
  let current=null,request=0,committing=false;
  const roots=button("Drives & home",()=>load(""),"button ghost small");
  const up=button("↑ Parent",()=>load(current?.parent||""),"button ghost small");
  const newFolder=button("New folder",()=>{createBox.hidden=false;folderName.focus();},"button ghost small");
  append(toolbar,roots,up,newFolder);
  const createBox=el("div","folder-create"),folderLabel=el("label","","New folder name"),folderName=el("input");
  createBox.hidden=true;folderName.name="remote_new_folder";folderName.placeholder="my-project";folderName.autocomplete="off";folderName.maxLength=255;folderLabel.append(folderName);
  const create=button("Create & use folder",()=>commit(true),"button primary small");
  append(createBox,folderLabel,append(el("div","folder-create-actions"),button("Cancel",()=>{createBox.hidden=true;},"button ghost small"),create));
  const cancel=button("Cancel",()=>dialog.close(),"button ghost"),use=button("Use this folder",()=>commit(false),"button primary");
  use.disabled=true;up.disabled=true;newFolder.disabled=true;
  const footer=append(el("div","form-actions"),cancel,use);
  append(dialog,head,help,pathLabel,toolbar,chosen,list,createBox,error,footer);
  dialog.addEventListener("cancel",event=>{if(committing)event.preventDefault();event.stopPropagation();});
  dialog.addEventListener("close",()=>{request++;dialog.remove();if(returnFocus?.isConnected)returnFocus.focus();});
  pathInput.addEventListener("input",()=>{use.disabled=true;newFolder.disabled=true;createBox.hidden=true;});
  pathInput.addEventListener("keydown",event=>{if(event.key==="Enter"&&!event.isComposing){event.preventDefault();load(pathInput.value.trim());}});
  folderName.addEventListener("keydown",event=>{if(event.key==="Enter"&&!event.isComposing){event.preventDefault();commit(true);}});
  document.body.append(dialog);dialog.showModal();pathInput.value=initialPath;load(initialPath);
  async function load(path) {
    if(committing)return;
    const generation=++request;
    error.textContent="";current=null;chosen.textContent="";use.disabled=true;up.disabled=true;newFolder.disabled=true;createBox.hidden=true;
    list.replaceChildren(el("p","muted small","Loading folders…"));list.setAttribute("aria-busy","true");
    try {
      const data=await api(`workspace-folders/?path=${encodeURIComponent(path)}`);
      if(!dialog.open||generation!==request)return;
      current=data;pathInput.value=data.path;chosen.textContent=data.path||"Choose a drive or your home folder.";list.replaceChildren();
      up.disabled=!data.parent;newFolder.disabled=!data.path;
      use.disabled=!data.path||data.path==="/"||/^[a-z]:[\\/]?$/i.test(data.path);
      for(const root of data.roots||[])list.append(button(`▱ ${root}`,()=>load(root),"folder-choice"));
      for(const child of data.directories)list.append(button(`▱ ${child.name}`,()=>load(child.path),"folder-choice"));
      if(data.path&&!data.directories.length)list.append(el("p","muted small","This folder has no subfolders. You can use it as your workspace."));
    } catch(failure) {
      if(!dialog.open||generation!==request)return;
      list.replaceChildren();error.textContent=failure.message;
    } finally {if(generation===request)list.setAttribute("aria-busy","false");}
  }
  async function commit(createNew) {
    if(committing||!current?.path)return;
    let path;
    try {path=createNew?folderChildPath(current.path,folderName.value):current.path;}
    catch(failure){error.textContent=failure.message;return;}
    committing=true;error.textContent="";
    const controls=$$("button,input",dialog),disabled=controls.map(control=>control.disabled);
    controls.forEach(control=>control.disabled=true);
    try {
      const project=await registerWorkspaceFolder(path,createNew);
      onSelect(project);dialog.close();
    } catch(failure) {error.textContent=failure.message;}
    finally {committing=false;controls.forEach((control,index)=>control.disabled=disabled[index]);}
  }
  return dialog;
}
// End remote folder selection.
function chatForm(chat=null){const existing=!!chat?.id;if(!state.user||state.user.must_change_password)return;formModal(existing?"Session settings":"Start something good",[{name:"title",label:"Session name",value:chat?.title||"",placeholder:"What are we working on?",required:true,full:true},...sessionFields(chat||{}),{name:"skills",label:"Skills",type:"multiple",options:state.skills.map(s=>[s.id,s.name]),value:chat?.skills||[],full:true,help:"Hold Ctrl / Command to select multiple skills."}],async data=>{const result=await api(existing?`chats/${chat.id}/`:"chats/",existing?"PATCH":"POST",data);await loadData();if(location.hash===`#chat/${result.id}`)await renderChat(result.id);else location.hash=`chat/${result.id}`;},{submit:existing?"Save settings":"Create session ↗",description:"Each session has its own encrypted history and file vault. Host CLI providers require host execution permission.",afterBuild:modelAutocomplete});}
function passwordForm(required=false){formModal(required?"Choose your own password":"Change password",[{name:"current_password",label:required?"Temporary password":"Current password",type:"password",required:true,full:true,autocomplete:"current-password"},{name:"password",label:"New password",type:"password",required:true,full:true,autocomplete:"new-password",help:"At least 12 characters; use a unique passphrase."},{name:"confirm",label:"Repeat new password",type:"password",required:true,full:true,autocomplete:"new-password"}],async data=>{if(data.password!==data.confirm)throw new Error("The new passwords do not match");const result=await api("password/","POST",data);state.modalLocked=false;modal.close();toast("Password updated");await enter(result.user);},{locked:required,description:required?"Your temporary password must be replaced before accessing the workspace.":"Other sign-in sessions will be invalidated.",submit:"Update password"});}

function renderAgents(){page.append(heading("YOUR TEAM","A role for every kind of work","Give an agent a purpose, a set of skills, and clear boundaries.",button("＋ Create agent",()=>agentForm(),"button primary")));if(!state.agents.length){page.append(empty("Build your first teammate","Define a reviewer, an embedded debugger, a model maker, or any role your work needs.","Create an agent",()=>agentForm()));return;}const grid=el("div","card-grid");state.agents.forEach(a=>{const card=el("div","resource-card");append(card,append(el("div","resource-top"),el("span","row-icon","◇"),badge(a.permission)),el("h3","",a.name),el("p","",a.role),append(el("div","card-footer"),el("span","chip",`${a.skills.length} skills`),el("span","chip",`${a.delegates.length} delegates`),button("Edit",()=>agentForm(a),"button small ghost")));grid.append(card);});page.append(grid);}
function agentForm(agent=null){const a=agent||{};formModal(agent?"Edit agent":"Create an agent",[{name:"name",label:"Name",value:a.name,required:true},{name:"role",label:"Role",value:a.role,placeholder:"Embedded firmware debugger",required:true},{name:"instructions",label:"Instructions",type:"textarea",value:a.instructions,required:true,full:true,rows:7,placeholder:"Describe the agent's responsibilities and how it should work…"},{name:"permission",label:"File permissions",type:"select",options:permissionOptions(),value:a.permission||"read-only",full:true},{name:"allow_shell",label:"Allow shell tools (trusted host access)",type:"checkbox",value:a.allow_shell,full:true},{name:"allow_network",label:"Allow network tools",type:"checkbox",value:a.allow_network,full:true},{name:"skills",label:"Enabled skills",type:"multiple",options:state.skills.map(s=>[s.id,s.name]),value:a.skills||[],full:true},{name:"delegates",label:"Agents this role may use",type:"multiple",options:state.agents.filter(item=>item.id!==a.id).map(item=>[item.id,item.name]),value:a.delegates||[],full:true,help:"API delegation is enforced by Solar. Native CLI roles use the provider's permission model."}],async data=>{await api(agent?`agents/${agent.id}/`:"agents/",agent?"PATCH":"POST",data);await loadData();renderCurrent();toast("Agent saved");},{description:"Skills and instructions guide behavior. Host access grants the provider your PC account's privileges; grant it only to people you trust."});}
function renderCurrent(){navigate().catch(e=>toast(e.message,true));}
function renderSkills(){const actions=append(el("div","composer-buttons"),button("Import skill",()=>importSkill(),"button ghost"),button("＋ Create skill",()=>skillForm(),"button primary"));page.append(heading("REUSABLE KNOW-HOW","Skills that make the work yours","Write instructions once. Bring them into the sessions that need them.",actions));page.append(el("p","notice","Import SKILL.md or a ZIP containing one skill and its resources. Imported code is stored without being executed. Review a skill before enabling it."));if(!state.skills.length){page.append(empty("A library with room to grow","Add a workflow for hardware debugging, a 3D modeling process, or your team's coding conventions.","Write a skill",()=>skillForm()));return;}const grid=el("div","card-grid");state.skills.forEach(s=>{const card=el("div","resource-card");append(card,append(el("div","resource-top"),el("span","row-icon","✧"),el("span","muted small",s.has_bundle?"BUNDLE":"MARKDOWN")),el("h3","",s.name),el("p","",s.description),append(el("div","card-footer"),button("Edit skill",()=>skillForm(s),"button ghost small"),link("Download",`/api/skills/${s.id}/?download=1`,"button small ghost"),button("Remove",()=>confirmAction("Remove skill?","This removes it from your library and agent selections.",async()=>{await api(`skills/${s.id}/`,"DELETE");await loadData();renderCurrent();}),"text-button")));grid.append(card);});page.append(grid);}
function skillForm(skill=null){const s=skill||{};formModal(skill?"Edit skill":"Create a skill",[{name:"name",label:"Name",value:s.name,required:true},{name:"description",label:"Short description",value:s.description},{name:"content",label:"SKILL.md",type:"textarea",value:s.content||"---\nname: my-skill\ndescription: When and how this skill should be used\n---\n\n# Instructions\n\n",required:true,full:true,rows:14}],async data=>{await api(skill?`skills/${skill.id}/`:"skills/",skill?"PATCH":"POST",data);await loadData();renderCurrent();toast("Skill saved");});}
function importSkill(){const input=el("input");input.type="file";input.accept=".md,.zip";input.addEventListener("change",async()=>{if(!input.files[0])return;try{const data=new FormData();data.append("file",input.files[0]);await api("skills/","POST",data);await loadData();renderCurrent();toast("Skill imported. Review it before enabling.");}catch(e){toast(e.message,true);}});input.click();}
function confirmAction(title,description,action){const content=append(el("div"),el("p","muted",description),append(el("div","form-actions"),button("Cancel",()=>modal.close(),"button ghost"),button("Confirm",async()=>{await action();modal.close();},"button danger")));openModal(title,content);}

function scheduleSummary(s){if(s.kind==="once")return "One time";if(s.kind==="cron")return `${s.cron} · ${s.timezone}`;const n=s.interval_seconds;return n%86400===0?`Every ${n/86400} day${n===86400?"":"s"}`:n%3600===0?`Every ${n/3600} hour${n===3600?"":"s"}`:`Every ${n/60} minutes`;}
function renderSchedules(){page.append(heading("WORK ON A RHYTHM","Your automations","One continuing session per task, with every run and result kept together.",button("＋ Add automation",()=>scheduleForm(),"button primary")));if(!state.schedules.length){page.append(empty("Set it in motion","Run a task every hour, every three days, or on a custom cron schedule. Set a run limit or an end date.","Create an automation",()=>scheduleForm()));return;}const grid=el("div","card-grid");state.schedules.forEach(s=>{const card=el("div","resource-card");append(card,append(el("div","resource-top"),el("span","row-icon","◷"),badge(s.enabled?"enabled":"disabled")),el("h3","",s.name),el("p","",s.prompt.slice(0,180)),el("div","chip",scheduleSummary(s)),el("p","small muted",`Next: ${s.enabled?date(s.next_run):"Paused / complete"}\nRuns: ${s.run_count}${s.max_runs?` / ${s.max_runs}`:""}${s.ends_at?` · Ends ${date(s.ends_at,true)}`:""}`));const footer=el("div","card-footer");append(footer,button("Edit",()=>scheduleForm(s),"button small ghost"),button(s.enabled?"Pause":"Resume",async()=>{await api(`schedules/${s.id}/`,"PATCH",{enabled:!s.enabled});await loadData();renderCurrent();},"button small ghost"),s.chat_id?link("Open session",`#chat/${s.chat_id}`,"button small ghost"):null,button("History",()=>scheduleHistory(s),"text-button"),button("Remove",()=>confirmAction("Remove automation?","Future runs will stop. Existing chats and results stay in your workspace.",async()=>{await api(`schedules/${s.id}/`,"DELETE");await loadData();renderCurrent();}),"text-button"));card.append(footer);grid.append(card);});page.append(grid);}
function localDatetime(value){const date=value?new Date(value):new Date(Date.now()+60000);return new Date(date-date.getTimezoneOffset()*60000).toISOString().slice(0,16);}
function scheduleForm(schedule=null){const s=schedule||{};const unit=(s.interval_seconds||3600)%86400===0?86400:(s.interval_seconds||3600)%3600===0?3600:60;formModal(schedule?"Edit automation":"Put a task on repeat",[{name:"name",label:"Task name",value:s.name,required:true,full:true},{name:"prompt",label:"What should the agent do?",type:"textarea",value:s.prompt,required:true,full:true,rows:5},...sessionFields(s,true),{name:"kind",label:"Schedule type",type:"select",options:[["interval","Every interval"],["cron","Cron expression"],["once","One time"]],value:s.kind||"interval"},{name:"timezone",label:"Timezone",value:s.timezone||Intl.DateTimeFormat().resolvedOptions().timeZone||"UTC",required:true},{name:"every",label:"Repeat every",type:"number",min:1,max:525600,value:(s.interval_seconds||3600)/unit},{name:"unit",label:"Unit",type:"select",options:[["60","Minutes"],["3600","Hours"],["86400","Days"]],value:String(unit)},{name:"cron",label:"Cron expression",value:s.cron||"0 * * * *",placeholder:"0 */3 * * *",help:"Minute · hour · day · month · weekday",full:true},{name:"starts_at",label:"Start at (your local time)",type:"datetime-local",value:localDatetime(s.starts_at),required:true},{name:"ends_at",label:"End at (optional)",type:"datetime-local",value:s.ends_at?localDatetime(s.ends_at):""},{name:"duration_months",label:"Or run for this many months",type:"number",min:1,max:120,value:"",placeholder:"3"},{name:"max_runs",label:"Maximum runs (optional)",type:"number",min:1,max:100000,value:s.max_runs||"",placeholder:"No count limit"},{name:"enabled",label:"Enable automation",type:"checkbox",value:s.enabled??true,full:true}],async data=>{data.interval_seconds=Number(data.every)*Number(data.unit);data.starts_at=new Date(data.starts_at).toISOString();data.ends_at=data.ends_at?new Date(data.ends_at).toISOString():null;data.max_runs=data.max_runs?Number(data.max_runs):null;if(data.duration_months)data.duration_months=Number(data.duration_months);else delete data.duration_months;delete data.every;delete data.unit;await api(schedule?`schedules/${schedule.id}/`:"schedules/",schedule?"PATCH":"POST",data);await loadData();renderCurrent();toast("Automation saved");},{description:"Solar must be running at the scheduled time. Missed intervals coalesce into one run after restart; a task never overlaps itself.",submit:"Save automation",afterBuild:form=>{modelAutocomplete(form);function visibility(){const kind=form.elements.kind.value;for(const name of ["every","unit"])form.elements[name].closest("label").hidden=kind!=="interval";form.elements.cron.closest("label").hidden=kind!=="cron";}form.elements.kind.addEventListener("change",visibility);visibility();}});}
function scheduleHistory(s){const content=el("div");if(!s.history.length)content.append(el("p","muted","No runs yet."));for(const entry of s.history){const row=el("div","list-row");append(row,append(el("div","row-copy"),el("strong","",date(entry.due_at)),el("small","",entry.run?.error||"")),entry.run?badge(entry.run.status):badge("removed"),entry.run?link("Open ↗",`#chat/${entry.run.chat_id}`,"text-button"):null);row.addEventListener("click",()=>modal.close());content.append(row);}openModal("Automation history",content);}
function renderProviders(){page.append(heading("CHOOSE YOUR ENGINE","Your providers","Connect the coding tools you use, or a model running locally.",button("＋ Add provider",()=>providerForm(),"button primary")));page.append(el("p","notice","Codex and Claude use their installed CLI and existing host login when host access is granted. API providers keep their own encrypted keys. Model availability is determined by your provider and account."));const grid=el("div","card-grid");state.providers.forEach(p=>{const card=el("div","resource-card");append(card,append(el("div","resource-top"),el("span","row-icon",p.kind==="codex"?"✳":p.kind==="claude"?"✴":"⌘"),badge(p.kind==="openai"?"api":"native")),el("h3","",p.name),el("p","",p.kind==="openai"?p.base_url:`${p.kind==="codex"?"Codex CLI":"Claude Code"} on this PC`),append(el("div","card-footer"),button("Configure",()=>providerForm(p),"button ghost small"),button("Models",()=>showModels(p),"button ghost small"),p.has_api_key?el("span","chip","Key saved"):null));grid.append(card);});page.append(grid);}
function providerForm(provider=null){const p=provider||{};const fields=[{name:"name",label:"Display name",value:p.name,required:true},{name:"kind",label:"Type",type:"select",value:p.kind||"openai",options:[["openai","OpenAI compatible API"],["codex","Codex CLI"],["claude","Claude Code"]]},{name:"base_url",label:"API base URL",value:p.base_url||"http://127.0.0.1:11434/v1",full:true,help:`Allowed origins: ${(state.origins||[]).join(", ")}. Edit solar.conf on the PC to add one.`},{name:"model",label:"Default model",value:p.model,full:true,placeholder:"Use the provider default, or enter a model ID"},{name:"api_key",label:p.has_api_key?"Replace API key (leave empty to keep)":"API key (optional)",type:"password",full:true,autocomplete:"new-password"},{name:"tools",label:"Model supports file tools and delegation",type:"checkbox",value:p.tools??true,full:true},{name:"vision",label:"Model supports image attachments",type:"checkbox",value:p.vision,full:true},{name:"reasoning_effort",label:"API accepts reasoning_effort",type:"checkbox",value:p.reasoning_effort,full:true},{name:"clear_api_key",label:"Clear saved key and provider login",type:"checkbox",value:false,full:true}];formModal(provider?"Configure provider":"Add a provider",fields,async data=>{await api(provider?`providers/${provider.id}/`:"providers/",provider?"PATCH":"POST",data);await loadData();renderCurrent();toast("Provider saved");},{afterBuild:form=>{form.elements.kind.disabled=!!provider;function visibility(){const isApi=form.elements.kind.value==="openai";for(const name of ["base_url","tools","vision","reasoning_effort"])form.elements[name].closest("label").hidden=!isApi;}form.elements.kind.addEventListener("change",visibility);visibility();}});}
async function showModels(provider){const content=el("div","loading","Loading available models…");openModal(`${provider.name} models`,content);const result=await api(`providers/${provider.id}/models/`);content.className="";content.replaceChildren();if(!result.models.length)content.append(el("p","muted",result.error||"No model catalog is available. Enter the model ID in session settings or use /model inside Native terminal."));else result.models.forEach(m=>content.append(el("pre","",m)));}
async function renderRuns(){state.runs=(await api("runs/")).runs;page.append(heading("IN FLIGHT & FINISHED","Activity","Follow your runs, open their sessions, or stop work in progress."));const table=el("table"),head=el("tr");["Provider / session","State","Mode","Started",""].forEach(h=>head.append(el("th","",h)));table.append(append(el("thead"),head));const tbody=el("tbody");state.runs.forEach(r=>{const chat=state.chats.find(c=>c.id===r.chat_id);const tr=el("tr");append(tr,append(el("td"),link(chat?.title||"Open session",`#chat/${r.chat_id}`)),append(el("td"),badge(r.status)),el("td","muted",r.mode),el("td","muted",date(r.started_at||r.created_at)),append(el("td"),activeStatuses.includes(r.status)?button("Stop",async()=>{await api(`runs/${r.id}/stop/`,"POST",{});renderCurrent();},"button small danger"):link("View ↗",`#chat/${r.chat_id}`,"text-button")));tbody.append(tr);});table.append(tbody);if(state.runs.length)page.append(append(el("div","table-wrap"),table));else page.append(empty("All quiet here","Run a session or an automation to see its progress here."));}
async function renderUsers(){if(!state.user.is_admin){page.append(empty("Administrator access required","Only administrators can manage user accounts."));return;}const data=await api("users/");page.append(heading("ACCESS & PRIVACY","Workspace users","Accounts have separate conversations, files, provider keys, and agent libraries.",button("＋ Add user",()=>userForm(),"button primary")));page.append(el("p","notice warning","Host execution and remote desktop grant access to this PC's operating-system account. Grant them only to trusted operators. Users without these permissions can use API models with files confined to their own chats."));const table=el("table"),head=el("tr");["Username","Role","Access","Status",""].forEach(t=>head.append(el("th","",t)));table.append(append(el("thead"),head));const tbody=el("tbody");data.users.forEach(u=>{const tr=el("tr"),access=el("div","permission-list");if(u.host_access)access.append(el("span","chip","Host execution"));if(u.remote_access)access.append(el("span","chip","Remote desktop"));if(!u.host_access&&!u.remote_access)access.append(el("span","muted","API workspace"));append(tr,el("td","",u.username),el("td","",u.is_admin?"Administrator":"Member"),append(el("td"),access),append(el("td"),badge(u.is_active?"active":"disabled")),append(el("td"),button("Manage",()=>userForm(u),"button small ghost")));tbody.append(tr);});table.append(tbody);page.append(append(el("div","table-wrap"),table));}
function userForm(user=null) {
  const u=user||{};
  formModal(user?`Manage ${u.username}`:"Add a user",[
    {name:"username",label:"Username",value:u.username,required:!user,full:true},
    {name:"password",label:user?"Reset password (leave empty to keep)":"Temporary password",type:"password",required:!user,full:true,autocomplete:"new-password",help:"At least 12 characters. A change is required on the next sign-in."},
    {name:"is_admin",label:"Administrator — can manage accounts",type:"checkbox",value:u.is_admin,full:true},
    {name:"host_access",label:"Trusted host execution — runs Codex / Claude on the PC",type:"checkbox",value:u.host_access,full:true},
    {name:"remote_access",label:"Trusted remote desktop control",type:"checkbox",value:u.remote_access,full:true},
    {name:"is_active",label:"Account enabled",type:"checkbox",value:u.is_active??true,full:true},
    {name:"current_password",label:"Your current password",type:"password",required:true,full:true,autocomplete:"current-password"}
  ],async data=>{await api(user?`users/${u.id}/`:"users/",user?"PATCH":"POST",data);await renderReplace(renderUsers);toast(user?"User updated":"User created");},{
    description:"Administrator status permits account management; chat access through Solar remains scoped to the signed-in account.",
    afterBuild:form=>{
      if(!user)return;
      form.elements.username.disabled=true;
      if(user.id===state.user.id)return;
      $(".form-actions",form).prepend(button("Remove account",()=>{
        const current_password=form.elements.current_password.value;
        if(!current_password)throw new Error("Enter your current password first");
        confirmAction("Remove account access?","This disables the account and removes its password and host permissions. Its encrypted records remain retained for recovery.",async()=>{
          await api(`users/${user.id}/`,"DELETE",{current_password}); await renderReplace(renderUsers); toast("Account access removed");
        });
      },"button danger"));
    }
  });
}
async function renderReplace(fn){page.replaceChildren();await fn();}
function renderAccount(){page.append(heading("YOUR SPACE","Account","Manage your sign-in and review your permissions."));const panel=el("div","panel panel-body account-panel");for(const [name,value] of [["Username",state.user.username],["Account role",state.user.is_admin?"Administrator":"Member"],["Host execution",state.user.host_access?"Enabled":"Disabled"],["Remote desktop",state.user.remote_access?"Enabled":"Disabled"]])panel.append(append(el("div","split-info"),el("span","",name),el("span","",value)));const actions=el("div","form-actions");actions.append(button("Change password",()=>passwordForm(),"button primary"));panel.append(actions);page.append(panel);}

// Chat and file browser.
async function renderChat(id){clearInterval(state.timer);state.terminalCleanup?.();state.terminalCleanup=null;state.socket?.close();state.socket=null;state.terminal?.dispose();state.terminal=null;page.className="page chat-page";page.replaceChildren(el("div","loading","Opening session…"));const chat=await api(`chats/${id}/`);if(state.page!=="chat"||location.hash.slice(1)!==`chat/${id}`)return;state.chat=chat;state.events=[];state.cursor=0;page.replaceChildren();const layout=el("div","chat-layout"),main=el("div","chat-main"),header=el("div","chat-header"),title=append(el("div"),el("h2","",chat.title),el("span","muted",`${chat.provider_name} · ${chat.model||"Provider default"}`));const actions=el("div","chat-header-actions");append(actions,button("Settings",()=>chatForm(state.chat),"button small ghost"),button("Files",()=>toggleFilesPanel(),"button small ghost files-toggle"));append(header,title,actions);main.append(header);const subbar=el("div","chat-subbar");append(subbar,el("span","chip",chat.effort+" reasoning"),el("span","chip",chat.effective_permission||chat.permission),el("span","chip",`Approval: ${chat.approval}`),chat.project_name?el("span","chip",chat.project_name):null,el("span","chip",agentName(chat.agent_id)));if(chat.provider_kind!=="openai"&&state.user.host_access)subbar.append(button("Native terminal ↗",()=>openNative(),"button ghost small"));subbar.append(button(chat.archived?"Restore":"Archive",async()=>{await api(`chats/${chat.id}/`,"PATCH",{archived:!chat.archived});await loadData();await renderChat(chat.id);},"text-button"));main.append(subbar);const messages=el("div","messages");messages.id="messages";main.append(messages);const composerWrap=el("div","composer-wrap"),composer=el("form","composer"),textarea=el("textarea");textarea.name="message";textarea.placeholder="Describe what you want to build, or type / for commands…";textarea.setAttribute("aria-label","Message your agent");textarea.rows=2;textarea.maxLength=100000;textarea.disabled=chat.archived;const menu=el("div","slash-menu");menu.hidden=true;append(composer,menu,textarea);const slash=attachSlashMenu(textarea,menu,slashArguments);modelCatalog(chat.provider_id).then(()=>{if(textarea.value.startsWith("/"))slash.update();}).catch(()=>{});const footer=el("div","composer-footer");append(footer,el("span","composer-hint","↵ send · Shift ↵ newline"));const controls=el("div","composer-buttons");append(controls,button("＋ Files",()=>chooseUpload(false),"text-button"));const stop=button("Stop",async()=>{if(state.chat.active_run)await api(`runs/${state.chat.active_run.id}/stop/`,"POST",{});},"button small danger");stop.id="stop-run";const send=el("button","button primary send-button","Send ↑");send.type="submit";send.id="send-message";append(controls,stop,send);footer.append(controls);composer.append(footer);append(composerWrap,composer,el("p","composer-note","Encrypted in your workspace · Your provider processes the messages and files you send."));main.append(composerWrap);composer.addEventListener("submit",async e=>{e.preventDefault();const value=textarea.value.trim();if(!value)return;send.disabled=true;try{const result=await api(`chats/${chat.id}/messages/`,"POST",{message:value});textarea.value="";slash.close();if(result.notice)toast(result.notice);if(result.chat){await renderChat(chat.id);return;}if(result.native){state.chat.active_run=result.run;await openNative(false);return;}await pollChat();}catch(error){toast(error.message,true);}finally{send.disabled=!!state.chat?.active_run;}});textarea.addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey&&!e.isComposing){e.preventDefault();composer.requestSubmit();}});const files=el("aside","file-panel"+(panelPreference("files-closed",innerWidth<900)?" closed":""));files.id="files-panel";files.setAttribute("aria-label","Chat files");append(files,append(el("div","file-panel-head"),el("strong","","Session files"),el("p","",chat.directory)));const fileActions=el("div","file-actions");append(fileActions,button("↑ Files",()=>chooseUpload(false),"button ghost small"),button("↑ Folder",()=>chooseUpload(true),"button ghost small"),link("↓ ZIP",`/api/chats/${chat.id}/export/`,"button ghost small"));files.append(fileActions);const tree=el("div","file-tree");tree.id="file-tree";append(files,tree,el("div","file-foot","▣ Encrypted at rest\nDrop files or folders into this session."));append(layout,main,files);page.append(layout);if(chat.project_id&&state.user.host_access)installProjectPane(files,chat);installPanelControls(files);for(const name of ["dragenter","dragover"])layout.addEventListener(name,e=>{e.preventDefault();layout.classList.add("drop-zone");});layout.addEventListener("dragleave",e=>{if(!layout.contains(e.relatedTarget))layout.classList.remove("drop-zone");});layout.addEventListener("drop",async e=>{e.preventDefault();layout.classList.remove("drop-zone");try{const files=await droppedFiles(e.dataTransfer);await upload(files);}catch(error){toast(error.message,true);}});await Promise.all([pollChat(),loadFiles()]);state.timer=setInterval(()=>pollChat().catch(e=>toast(e.message,true)),1500);textarea.focus();}
let polling=false;
async function pollChat() {
  const chat=state.chat;if(!chat||polling)return;polling=true;
  try{
    let result,changed=false;
    do{
      result=await api(`chats/${chat.id}/events/?after=${state.cursor}`);
      if(state.chat?.id!==chat.id)return;
      changed ||= result.events.length>0;state.events.push(...result.events);state.cursor=result.cursor;
    }while(result.has_more);
    const previous=state.chat.active_run;
    const statusChanged=previous?.id!==result.active_run?.id||previous?.status!==result.active_run?.status;
    state.chat.active_run=result.active_run;
    const stop=$("#stop-run"),send=$("#send-message");
    if(stop)stop.hidden=!result.active_run;if(send)send.disabled=!!result.active_run||chat.archived;
    if(!state.terminal&&(changed||statusChanged||!$("#messages")?.children.length))drawMessages();
    if(previous&&!result.active_run)await loadFiles();
  }finally{polling=false;}
}
function renderMessageText(node,text) {
  node.replaceChildren();
  function inline(value) {
    const pattern=/(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\([^\s)]+\))/g;
    let offset=0;
    for(const match of value.matchAll(pattern)){
      node.append(document.createTextNode(value.slice(offset,match.index)));
      const token=match[0];
      if(token.startsWith("`"))node.append(el("code","",token.slice(1,-1)));
      else if(token.startsWith("**"))node.append(el("strong","",token.slice(2,-2)));
      else {
        const split=token.indexOf("]("),href=token.slice(split+2,-1),label=token.slice(1,split);
        if(/^https?:\/\//i.test(href)){const anchor=link(label,href,"message-link");anchor.target="_blank";anchor.rel="noopener noreferrer";node.append(anchor);}
        else {const chatId=state.chat?.id;node.append(button(label,()=>openChatFile(href,chatId),"text-button message-link"));}
      }
      offset=match.index+token.length;
    }
    node.append(document.createTextNode(value.slice(offset)));
  }
  const value=String(text),fence=/```[^\n]*\n([\s\S]*?)(?:```|$)/g;let offset=0;
  for(const match of value.matchAll(fence)){inline(value.slice(offset,match.index));node.append(el("pre","",match[1]));offset=match.index+match[0].length;}
  inline(value.slice(offset));
}

function panelPreference(name,fallback=false){try{const value=localStorage.getItem(`solar.panels.${state.user?.id}.${name}`);return value===null?fallback:value==="true";}catch{return fallback;}}
function setPanelPreference(name,value){try{localStorage.setItem(`solar.panels.${state.user?.id}.${name}`,String(value));}catch{}}
function applyNavigationPreference(){
  const collapsed=panelPreference("navigation-collapsed");$("#app").classList.toggle("navigation-collapsed",collapsed);
  $("#mobile-menu").setAttribute("aria-controls","sidebar");$("#mobile-menu").setAttribute("aria-expanded",String(innerWidth<=650?$("#sidebar").classList.contains("mobile-open"):!collapsed));
  $("#mobile-menu").title=collapsed?"Show navigation":"Collapse navigation";
}
function toggleFilesPanel(closed){
  const panel=$("#files-panel");if(!panel)return;
  const value=closed??!panel.classList.contains("closed");panel.classList.toggle("closed",value);setPanelPreference("files-closed",value);
  const toggle=$(".files-toggle");toggle?.setAttribute("aria-expanded",String(!value));toggle?.setAttribute("aria-controls","files-panel");
}
function installPanelControls(panel){
  const controls=el("div","panel-controls"),expand=button("Expand",()=>{
    const wide=panel.classList.toggle("wide-panel");setPanelPreference("files-wide",wide);expand.textContent=wide?"Restore width":"Expand";expand.setAttribute("aria-pressed",String(wide));
  },"button ghost small");
  const wide=panelPreference("files-wide");panel.classList.toggle("wide-panel",wide);expand.textContent=wide?"Restore width":"Expand";expand.setAttribute("aria-pressed",String(wide));
  append(controls,el("strong","small","Files"),expand,button("Collapse",()=>toggleFilesPanel(true),"button ghost small"));panel.prepend(controls);
  const toggle=$(".files-toggle");toggle?.setAttribute("aria-expanded",String(!panel.classList.contains("closed")));toggle?.setAttribute("aria-controls","files-panel");
}
async function projectFileDialog(projectId,path,size){
  const userId=state.user?.id,route=location.hash;
  const [{openFileViewer,closeFileViewer},{projectSource}]=await Promise.all([import("/static/file-viewer.js"),import("/static/file-source.js")]);
  if(!userId||state.user?.id!==userId||location.hash!==route)return;
  state.viewerClose=closeFileViewer;await openFileViewer(projectSource(projectId,path,size));
}
async function openChatFile(href,chatId){
  if(!chatId||state.chat?.id!==chatId)return;
  await loadFiles();if(state.chat?.id!==chatId)return;
  let path;try{path=decodeURIComponent(href.split("#")[0]).replaceAll("\\","/");}catch{throw new Error("Invalid file link");}
  const files=state.fileTree?.files||[];
  const exact=files.find(file=>file.path===path.replace(/^\.\//,""));
  if(exact){await fileDialog(exact);return;}
  if(!/^(?:[a-z]:\/|sandbox:)/i.test(path)&&/^[a-z][a-z\d+.-]*:/i.test(path))throw new Error("Only workspace file links can be previewed");
  const project=state.projects.find(item=>item.id===state.chat.project_id);
  if(project){
    const root=project.path.replaceAll("\\","/").replace(/\/$/,"");
    if(path.toLowerCase().startsWith(root.toLowerCase()+"/"))path=path.slice(root.length+1);
    if(!/^(?:[a-z][a-z\d+.-]*:|\/)/i.test(path)){await projectFileDialog(project.id,path);return;}
  }
  const matches=files.filter(file=>file.path.endsWith("/"+path.replace(/^\.\//,""))||file.path.split("/").pop()===path.split("/").pop());
  if(matches.length===1){await fileDialog(matches[0]);return;}
  throw new Error(matches.length>1?"Several files have this name. Choose the file in the Files panel.":"This file is not available yet. Refresh the Files panel after the run finishes.");
}
async function renderAdminTerminal(){
  if(!state.user.is_admin){page.append(empty("Administrator access required","Only administrators can open the PC terminal."));return;}
  page.append(heading("THIS MACHINE","PowerShell","Open an interactive terminal on your PC."));
  if(!state.user.host_access){page.append(empty("Host execution is disabled","Enable trusted host execution for this administrator under Users to use PowerShell."));return;}
  page.append(el("p","notice","Commands run as the PC account running Solar. Navigation keeps the session available for reconnecting. Stop, sign out, or the idle timeout ends the terminal."));
  const marker=el("div");page.append(marker);
  const {mountAdminTerminal}=await import("/static/admin-terminal.js");
  const userId=state.user?.id;
  const isCurrent=()=>state.user?.id===userId&&state.user?.is_admin&&state.page==="terminal"&&marker.isConnected;
  if(!isCurrent())return;
  const cleanup=await mountAdminTerminal(marker,{api,isCurrent});if(isCurrent())state.terminalCleanup=cleanup;else cleanup();
}
function drawMessages(){const target=$("#messages");if(!target)return;const nearBottom=target.scrollHeight-target.scrollTop-target.clientHeight<140;const oldTop=target.scrollTop;target.replaceChildren();if(!state.events.length){const welcome=el("div","chat-empty");append(welcome,el("span","solar-mark","✳"),el("h2","","What are we working on?"),el("p","","Send a task, add your files, and give your agent room to work. The results will stay right here."));target.append(welcome);return;}const outputs=new Map(),toolGroups=new Map();for(const event of state.events){if(event.kind==="terminal"){
      let details=toolGroups.get("native-"+event.run_id);
      if(!details){details=el("details","tool-log");append(details,el("summary","","Native session transcript"),el("pre"));toolGroups.set("native-"+event.run_id,details);target.append(details);}
      const output=$("pre",details);output.textContent=(output.textContent+event.content.replace(/\x1B\[[0-?]*[ -/]*[@-~]/g,"").replace(/\x1B\][^\x07]*(?:\x07|\x1B\\)/g,"")).slice(-100000);continue;
    }if(event.kind==="activity"&&event.role==="tool"){let details=toolGroups.get(event.run_id);if(!details){details=el("details","tool-log");append(details,el("summary","","Agent activity"),el("pre"));toolGroups.set(event.run_id,details);target.append(details);}$("pre",details).append(document.createTextNode(event.content+"\n"));continue;}if(event.role==="assistant"){let item=outputs.get(event.run_id);if(!item){const node=messageNode("assistant",event.created_at);item={node,text:"",final:false};outputs.set(event.run_id,item);target.append(node);}if(event.kind==="message"){item.text=event.content;item.final=true;}else if(event.kind==="delta"&&!item.final)item.text+=event.content;renderMessageText($(".message-body",item.node),item.text);continue;}const node=messageNode(event.kind==="error"?"error":event.role,event.created_at);renderMessageText($(".message-body",node),event.content);target.append(node);}if(state.chat.active_run?.mode!=="native"&&state.chat.active_run){const pending=el("div","tool-log",`◌ ${state.chat.active_run.status==="queued"?"Waiting for a worker…":"Agent is working…"}`);target.append(pending);}if(nearBottom)target.scrollTop=target.scrollHeight;else target.scrollTop=oldTop;}
function messageNode(role,when){const node=el("article",`message ${role}`);const name=role==="user"?"You":role==="assistant"?state.chat.provider_name:role==="error"?"Needs attention":"Solar";append(node,append(el("div","message-heading"),el("span","",role==="assistant"?"✳":"◦"),el("span","",name),el("small","",date(when))),el("div","message-body"));return node;}
async function loadFiles(){const chat=state.chat;if(!chat)return;const data=await api(`chats/${chat.id}/files/`);if(state.chat?.id!==chat.id)return;state.fileTree=data;const target=$("#file-tree");if(!target)return;target.replaceChildren();const root={folders:new Map(),files:[]};function folder(path){let node=root;for(const part of path.split("/")){if(!node.folders.has(part))node.folders.set(part,{folders:new Map(),files:[]});node=node.folders.get(part);}return node;}data.folders.forEach(folder);data.files.forEach(file=>{const parts=file.path.split("/");parts.pop();folder(parts.join("/")).files.push(file);});function draw(node,parent,prefix=""){for(const [name,child] of [...node.folders].sort(([a],[b])=>a.localeCompare(b))){const path=prefix?`${prefix}/${name}`:name;const details=el("details","folder");details.open=true;const summary=el("summary","",`▱ ${name}`);const download=link("↓",`/api/chats/${chat.id}/export/?prefix=${encodeURIComponent(path)}`,"folder-download");download.title="Download folder as ZIP";download.addEventListener("click",e=>e.stopPropagation());summary.append(download);details.append(summary);draw(child,details,path);parent.append(details);}for(const file of node.files.sort((a,b)=>a.path.localeCompare(b.path))){const row=el("div","tree-file");const open=button(file.path.split("/").pop(),()=>fileDialog(file),"");open.title=file.path;append(row,el("span","","·"),open,el("span","tree-size",bytes(file.size)),link("↓",`/api/files/${file.id}/`));parent.append(row);}}draw(root,target);for(const [name,kind] of [["chat_history.txt","history"],["references.csv","references"]]){const row=el("div","tree-file");append(row,el("span","","·"),link(name,`/api/chats/${chat.id}/export/?kind=${kind}`));target.append(row);}}
function chooseUpload(folder){if(!state.chat)return;const input=el("input");input.type="file";input.multiple=true;if(folder){input.setAttribute("webkitdirectory","");input.setAttribute("directory","");}input.addEventListener("change",()=>upload([...input.files].map(f=>({file:f,path:f.webkitRelativePath||f.name}))).catch(e=>toast(e.message,true)));input.click();}
async function upload(files){if(!state.chat||!files.length)return;if(files.length>200)throw new Error("Upload at most 200 files at a time");const chatId=state.chat.id,data=new FormData();let total=0;for(const item of files){total+=item.file.size;data.append("files",item.file,item.file.name);data.append("paths",item.path);}if(total>95*1024**2)throw new Error("Upload batches must be smaller than 95 MB");toast(`Uploading ${files.length} file${files.length===1?"":"s"}…`);await api(`chats/${chatId}/files/`,"POST",data);if(state.chat?.id===chatId)await loadFiles();toast("Files encrypted and saved");}
async function droppedFiles(transfer){const results=[];async function walk(entry,prefix=""){if(results.length>=200)throw new Error("Drop at most 200 files at a time");if(entry.isFile){const file=await new Promise((resolve,reject)=>entry.file(resolve,reject));results.push({file,path:prefix+file.name});}else if(entry.isDirectory){const reader=entry.createReader();while(true){const entries=await new Promise((resolve,reject)=>reader.readEntries(resolve,reject));if(!entries.length)break;for(const child of entries)await walk(child,prefix+entry.name+"/");}}}const entries=[...transfer.items].map(i=>i.webkitGetAsEntry?.()).filter(Boolean);if(entries.length){for(const entry of entries)await walk(entry);}else for(const file of transfer.files)results.push({file,path:file.name});return results;}
async function fileDialog(file){
  const chatId=state.chat?.id;
  const [{openFileViewer,closeFileViewer},{vaultSource}]=await Promise.all([import("/static/file-viewer.js"),import("/static/file-source.js")]);
  if(!state.user||state.chat?.id!==chatId)return;
  state.viewerClose=closeFileViewer;
  const actions=[];
  if(file.path.toLowerCase().endsWith(".zip"))actions.push({label:"Extract into files",run:async()=>{try{const result=await api(`files/${file.id}/extract/`,"POST",{});await loadFiles();toast(`Extracted ${result.extracted} files`);}catch(error){toast(error.message,true);}}});
  actions.push({label:"Delete file",className:"button small danger",run:()=>confirmAction("Delete file?","This removes the encrypted copy from this session.",async()=>{await api(`files/${file.id}/`,"DELETE");await loadFiles();})});
  await openFileViewer(vaultSource(file,state.fileTree?.files||[file]),{actions});
}
async function openNative(start=true) {
  const chat=state.chat;if(!chat||state.nativeOpening||state.terminal)return;
  state.nativeOpening=true;
  try {
    if(start&&!chat.active_run){const result=await api(`chats/${chat.id}/native/`,"POST",{});chat.active_run=result.run;}
    if(state.chat?.id!==chat.id)return;
    if(chat.active_run?.mode!=="native")throw new Error("Stop the structured run before opening a native terminal");
    const runId=chat.active_run.id;
    const {default:xterm}=await import("/static/vendor/xterm.js");
    await document.fonts.ready;
    if(state.chat?.id!==chat.id)return;
    const target=$("#messages"),composer=$(".composer-wrap"),main=$(".chat-main");
    target.hidden=true;composer.hidden=true;
    const bar=el("div","native-bar"),status=el("span","terminal-status","Connecting to native terminal…"),actions=el("div","terminal-actions");
    const container=el("div","terminal-container");container.id="native-container";
    container.setAttribute("aria-label","Interactive provider terminal");append(bar,status,actions);target.before(bar,container);
    const term=new xterm.Terminal({cursorBlink:true,fontSize:14,lineHeight:1.15,letterSpacing:0,
      fontFamily:'Consolas, "Cascadia Mono", "Liberation Mono", monospace',scrollback:5000,
      theme:{background:"#111315",foreground:"#e6e9df",cursor:"#c5f277"},allowProposedApi:false});
    const fit=new xterm.FitAddon();term.loadAddon(fit);term.open(container);state.terminal=term;
    let ws=null,resizeFrame=0,disposed=false,ended=false,lastSize="";
    const send=data=>{if(ws?.readyState===WebSocket.OPEN)ws.send(JSON.stringify(data));};
    function size(force=false) {
      if(disposed||!container.isConnected||container.clientWidth<20||container.clientHeight<20)return;
      const dimensions=fit.proposeDimensions();if(!dimensions)return;
      const cols=Math.max(20,Math.min(300,dimensions.cols)),rows=Math.max(5,Math.min(150,dimensions.rows));
      if(term.cols!==cols||term.rows!==rows)term.resize(cols,rows);
      const key=`${cols}x${rows}`;
      if(force||key!==lastSize){send({type:"resize",cols,rows});lastSize=key;}
    }
    function scheduleSize(){cancelAnimationFrame(resizeFrame);resizeFrame=requestAnimationFrame(()=>size());}
    const observer=new ResizeObserver(scheduleSize);observer.observe(container);
    const onData=term.onData(data=>send({type:"input",data}));
    const cleanup=()=>{
      if(disposed)return;disposed=true;observer.disconnect();cancelAnimationFrame(resizeFrame);onData.dispose();
      ws?.close();term.dispose();container.remove();bar.remove();main.classList.remove("terminal-expanded");
      target.hidden=false;composer.hidden=false;
      if(state.terminal===term){state.terminal=null;state.socket=null;state.terminalCleanup=null;}
    };
    state.terminalCleanup=cleanup;
    append(actions,
      button("A−",()=>{term.options.fontSize=Math.max(10,term.options.fontSize-1);size(true);},"button small ghost"),
      button("A+",()=>{term.options.fontSize=Math.min(24,term.options.fontSize+1);size(true);},"button small ghost"),
      button("Expand",e=>{const expanded=main.classList.toggle("terminal-expanded");e.currentTarget.textContent=expanded?"Restore":"Expand";scheduleSize();},"button small ghost"),
      button("Back to chat",()=>{cleanup();drawMessages();},"text-button"),
      button("Stop session",async()=>{if(chat.active_run)await api(`runs/${chat.active_run.id}/stop/`,"POST",{});},"button danger small"));
    await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    if(disposed)return;size();
    ws=new WebSocket(`${location.protocol==="https:"?"wss":"ws"}://${location.host}/ws/terminal/${runId}/`);state.socket=ws;
    ws.addEventListener("open",()=>{size(true);status.textContent="Native provider terminal · / opens provider commands";term.focus();});
    ws.addEventListener("message",event=>{
      if(disposed)return;
      const message=JSON.parse(event.data);
      if(message.type==="output")term.write(message.data);
      else if(message.type==="exit"){ended=true;status.textContent="Session ended · return to chat for results";}
    });
    ws.addEventListener("close",()=>{if(!disposed&&!ended)status.textContent="Disconnected · return to chat and reopen to reconnect";});
    ws.addEventListener("error",()=>{if(!disposed)status.textContent="Connection failed · check the server and reconnect";});
  } finally {state.nativeOpening=false;}
}

async function renderDesktop() {
  page.append(heading("YOUR PC, WITH YOU","Remote desktop","Start a private screen stream only when you need it."));
  if(!state.user.remote_access){page.append(empty("Remote desktop is not enabled","An administrator can grant this account remote desktop permission."));return;}
  page.append(el("p","notice warning","This controls the PC's interactive desktop, including other open applications. The stream starts only when you press Run, and stops on disconnect or timeout. Secure Windows screens such as UAC and sign-in cannot be controlled."));
  const panel=el("div","desktop-panel"),toolbar=el("div","desktop-toolbar"),label=el("span","connection-label","○ Desktop is stopped");
  const controlLabel=el("label","check-label"),control=el("input");control.type="checkbox";
  append(controlLabel,control,document.createTextNode("Keyboard & mouse"));
  const monitor=el("select");monitor.setAttribute("aria-label","Remote display");monitor.style.width="145px";
  for(const [id,name] of [["1","Primary display"],["0","All displays"]]){const option=el("option","",name);option.value=id;monitor.append(option);}
  const actions=el("div","composer-buttons");
  const start=button("▶ Run",()=>startDesktop(),"button primary small");
  const stop=button("■ Stop",async()=>{await api("desktop/","DELETE");state.desktopSocket?.close();},"button danger small");
  stop.disabled=true;append(actions,monitor,start,stop);append(toolbar,label,controlLabel,actions);panel.append(toolbar);
  const screen=el("div","desktop-screen");screen.tabIndex=0;
  screen.setAttribute("aria-label","Remote desktop screen. Enable control to send keyboard input.");
  screen.append(empty("Your desktop stays private","Press Run to begin streaming this PC's display."));panel.append(screen);
  const footer=el("div","desktop-footer"),typeInput=el("input");
  typeInput.placeholder="Type text on the remote PC…";typeInput.setAttribute("aria-label","Text to type on remote PC");
  append(footer,typeInput,button("Send text",()=>{send({type:"text",text:typeInput.value});typeInput.value="";},"button ghost small"),button("Full screen",()=>screen.requestFullscreen(),"button ghost small"));
  panel.append(footer);page.append(panel);
  let image=null,oldUrl=null,lastMove=0;
  function send(data){
    if(!control.checked){toast("Enable keyboard & mouse to control the PC",true);return;}
    if(state.desktopSocket?.readyState===WebSocket.OPEN)state.desktopSocket.send(JSON.stringify(data));
  }
  async function startDesktop(){
    start.disabled=true;monitor.disabled=true;
    try{
      const result=await api("desktop/","POST",{monitor:Number(monitor.value)});
      if(state.page!=="desktop"||!page.contains(panel)){await api("desktop/","DELETE");return;}
      if(!result.lease_id)throw new Error(result.error||"Could not start the desktop");
      const ws=new WebSocket(`${location.protocol==="https:"?"wss":"ws"}://${location.host}/ws/desktop/${result.lease_id}/`);
      state.desktopSocket=ws;ws.binaryType="blob";
      ws.addEventListener("open",()=>{
        label.textContent="● Streaming · private connection";stop.disabled=false;
        ws.send(JSON.stringify({type:"heartbeat"}));clearInterval(state.desktopTimer);
        state.desktopTimer=setInterval(()=>{if(ws.readyState===WebSocket.OPEN)ws.send(JSON.stringify({type:"heartbeat"}));},4000);
      });
      ws.addEventListener("message",event=>{
        if(!(event.data instanceof Blob))return;
        if(!image){image=el("img");image.alt="Remote PC display";image.draggable=false;screen.replaceChildren(image);}
        const url=URL.createObjectURL(event.data);image.src=url;if(oldUrl)URL.revokeObjectURL(oldUrl);oldUrl=url;
      });
      ws.addEventListener("close",async()=>{
        clearInterval(state.desktopTimer);state.desktopTimer=null;start.disabled=false;monitor.disabled=false;stop.disabled=true;
        control.checked=false;label.textContent="○ Desktop is stopped";if(oldUrl)URL.revokeObjectURL(oldUrl);image=null;
        screen.replaceChildren(empty("Stream stopped","Press Run to reconnect. Screen capture ends with the session."));
        try{const status=await api("desktop/");if(status.error)toast(status.error,true);}catch{}
      });
    }catch(e){start.disabled=false;monitor.disabled=false;toast(e.message,true);}
  }
  function position(e){
    if(!image?.naturalWidth)return null;
    const rect=image.getBoundingClientRect();
    const scale=Math.min(rect.width/image.naturalWidth,rect.height/image.naturalHeight);
    const width=image.naturalWidth*scale,height=image.naturalHeight*scale;
    const x=(e.clientX-rect.left-(rect.width-width)/2)/width;
    const y=(e.clientY-rect.top-(rect.height-height)/2)/height;
    return {x:Math.max(0,Math.min(1,x)),y:Math.max(0,Math.min(1,y))};
  }
  screen.addEventListener("pointermove",e=>{
    if(!control.checked||performance.now()-lastMove<60)return;
    lastMove=performance.now();const point=position(e);if(point)send({type:"move",...point});
  });
  screen.addEventListener("pointerdown",e=>{
    screen.focus();if(!control.checked)return;e.preventDefault();
    const point=position(e);if(!point)return;screen.setPointerCapture(e.pointerId);
    send({type:"button",down:true,button:e.button===2?"right":e.button===1?"middle":"left",...point});
  });
  screen.addEventListener("pointerup",e=>{
    if(!control.checked)return;const point=position(e);if(!point)return;
    send({type:"button",down:false,button:e.button===2?"right":e.button===1?"middle":"left",...point});
    if(screen.hasPointerCapture(e.pointerId))screen.releasePointerCapture(e.pointerId);
  });
  screen.addEventListener("contextmenu",e=>{if(control.checked)e.preventDefault();});
  function releaseButtons(){const socket=state.desktopSocket;if(socket?.readyState===WebSocket.OPEN)socket.send(JSON.stringify({type:"release"}));}
  screen.addEventListener("pointercancel",releaseButtons);
  screen.addEventListener("lostpointercapture",releaseButtons);
  screen.addEventListener("blur",releaseButtons);
  control.addEventListener("change",()=>{if(!control.checked)releaseButtons();});
  screen.addEventListener("wheel",e=>{if(!control.checked)return;e.preventDefault();send({type:"scroll",amount:e.deltaY>0?-3:3});},{passive:false});
  screen.addEventListener("keydown",e=>{
    if(!control.checked||["Control","Alt","Shift","Meta"].includes(e.key))return;e.preventDefault();
    send({type:"key",key:e.key,ctrl:e.ctrlKey,alt:e.altKey,shift:e.shiftKey,meta:e.metaKey});
  });
}

window.addEventListener("beforeunload",()=>{state.desktopSocket?.close();});
try {
  const result=await api("me/");
  if((result.workspace_controls_version||0)<3) {
    $("#boot").replaceChildren(append(el("div","update-notice"),el("h2","","Solar update ready"),el("p","","Restart the Solar server on this PC with uv run solar, then reload this page. The restart applies the project and automation database updates.")));
  } else {
    ({attachSlashMenu}=await import("/static/slash.js?v=2"));
    if(result.user)await enter(result.user);else showLogin();
  }
} catch(error) {$("#boot").textContent="Solar could not be reached. Reload this page after starting the server.";toast(error.message,true);}

function slashArguments(name) {
  const items=values=>values.map(value=>({value,label:value}));
  switch(name) {
    case "/effort": return items(effortOptions().map(([v])=>v));
    case "/permissions": return permissionOptions().map(([value,label])=>({value,label}));
    case "/approvals": return items(["default","on-request","never","auto-review"]);
    case "/workspace": return [{value:"none",label:"Private session folder"},...state.projects.map(p=>({value:p.id,label:p.name,description:p.path}))];
    case "/agent": return [{value:"none",label:"No agent role"},...state.agents.map(a=>({value:a.id,label:a.name,description:a.role}))];
    case "/skill": return state.skills.map(s=>({value:s.id,label:s.name,description:s.description}));
    case "/model": return items(state.modelCatalogs[state.chat?.provider_id]?.models||[]);
    default:return [];
  }
}
function renderProjects() {
  page.append(heading("ON THIS PC","Your projects","Choose a real project folder and use it across sessions and automations.",state.user.host_access?button("Add project",()=>projectForm(),"button primary"):null));
  if(!state.user.host_access){page.append(empty("Host access required","An administrator can grant trusted host execution to use folders on this PC."));return;}
  page.append(el("p","notice","Linked folders are edited directly on the PC. Solar encrypts their registration and your chat history; existing project files remain in their original location and format."));
  if(!state.projects.length){page.append(empty("Bring an existing project","Select its folder on this PC, or create a new folder.","Add project",()=>projectForm()));return;}
  const grid=el("div","card-grid");
  for(const project of state.projects){
    const card=el("div","resource-card");
    append(card,el("h3","",project.name),el("p","project-path",project.path),append(el("div","card-footer"),
      button("New session",()=>chatForm({project_id:project.id}),"button small primary"),
      button("Rename",()=>projectForm(project),"button small ghost"),
      button("Unregister",()=>confirmAction("Unregister project?","The folder and all its files stay on the PC. First unassign it from sessions and automations.",async()=>{await api(`projects/${project.id}/`,"DELETE");await loadData();renderCurrent();}),"text-button")));
    grid.append(card);
  }page.append(grid);
}
function projectForm(project=null) {
  const fields=[{name:"name",label:"Project name",value:project?.name,required:true,full:true}];
  if(!project)fields.push({name:"path",label:"Absolute folder path on the remote PC",required:true,full:true,placeholder:"D:\\projects\\my-project"},{name:"create",label:"Create this folder if it does not exist (parent must exist)",type:"checkbox",full:true});
  formModal(project?"Rename project":"Register a project",fields,async data=>{
    await api(project?`projects/${project.id}/`:"projects/",project?"PATCH":"POST",data);await loadData();renderCurrent();toast("Project saved");
  },{submit:"Save project",afterBuild:form=>{
    if(project)return;
    const picker=el("div","folder-picker full"),pathInput=form.elements.path;
    const browse=button("Browse folders on this PC",()=>show(pathInput.value),"button ghost small");
    const list=el("div","folder-picker-list");append(picker,browse,list);$(".form-grid",form).append(picker);
    async function show(path="") {
      list.replaceChildren(el("span","muted","Loading folders…"));
      try{
        const data=await api(`workspace-folders/?path=${encodeURIComponent(path)}`);list.replaceChildren();
        if(data.path){list.append(el("div","project-path",data.path));list.append(button("Use this folder",()=>{pathInput.value=data.path;list.replaceChildren();},"button small primary"));}
        if(data.parent)list.append(button("↑ Parent folder",()=>show(data.parent),"folder-choice"));
        for(const root of data.roots||[])list.append(button(root,()=>show(root),"folder-choice"));
        for(const dir of data.directories)list.append(button(`▱ ${dir.name}`,()=>show(dir.path),"folder-choice"));
        if(data.path&&!data.directories.length)list.append(el("p","muted small","No child folders."));
      }catch(error){list.replaceChildren(el("p","form-error",error.message),button("Show drives",()=>show(),"button ghost small"));}
    }
  }});
}
function installProjectPane(panel,chat) {
  const original=[...panel.children],tabs=el("div","tabs workspace-tabs"),workspace=el("div","project-pane");
  workspace.hidden=true;panel.prepend(tabs);panel.append(workspace);
  const privateButton=button("Chat files",()=>switchTab(false),"active"),projectButton=button("Project",()=>switchTab(true));
  append(tabs,privateButton,projectButton);let current="";
  async function switchTab(project) {
    original.forEach(node=>node.hidden=project);workspace.hidden=!project;
    privateButton.classList.toggle("active",!project);projectButton.classList.toggle("active",project);
    if(project)await show(current);
  }
  async function show(relative="") {
    const result=await api(`projects/${chat.project_id}/tree/?path=${encodeURIComponent(relative)}`);
    if(!panel.isConnected)return;current=relative;workspace.replaceChildren();
    append(workspace,el("strong","",result.project.name),el("p","project-path small",result.project.path),el("p","muted small",relative||"Project root"));
    const actions=el("div","file-actions");
    if(relative)actions.append(button("↑ Parent",()=>show(relative.split("/").slice(0,-1).join("/")),"button ghost small"));
    append(actions,button("Refresh",()=>show(current),"button ghost small"),button("Upload",()=>choose(false),"button ghost small"),button("Folder",()=>choose(true),"button ghost small"));workspace.append(actions);
    for(const entry of result.entries){const row=el("div","tree-file");
      if(entry.directory)row.append(button(`▱ ${entry.name}`,()=>show(entry.path),"folder-choice"));
      else append(row,button(entry.name,()=>projectFileDialog(chat.project_id,entry.path,entry.size),""),el("span","tree-size",bytes(entry.size)),link("↓",`/api/projects/${chat.project_id}/files/?path=${encodeURIComponent(entry.path)}`));
      workspace.append(row);
    }
    if(!result.entries.length)workspace.append(el("p","muted small","This folder is empty."));
    workspace.append(el("p","file-foot","These are live project files on the PC. Uploads add new files; existing files are never overwritten."));
  }
  function choose(folder){const input=el("input");input.type="file";input.multiple=true;if(folder)input.setAttribute("webkitdirectory","");
    input.addEventListener("change",async()=>{try{const data=new FormData();for(const file of input.files){data.append("files",file);data.append("paths",[current,file.webkitRelativePath||file.name].filter(Boolean).join("/"));}await api(`projects/${chat.project_id}/files/`,"POST",data);await show(current);toast("Project files uploaded");}catch(error){toast(error.message,true);}});input.click();
  }
}
