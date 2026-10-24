// Starting and stopping are explicit API actions; navigation only disconnects the viewer.
export async function mountAdminTerminal(parent,{api,isCurrent}) {
  const make=(tag,cls,text)=>{const item=document.createElement(tag);item.className=cls||'';if(text)item.textContent=text;return item;};
  const panel=make('section','admin-terminal-panel'),bar=make('div','native-bar'),status=make('span','terminal-status','PowerShell is stopped'),actions=make('div','terminal-actions'),container=make('div','terminal-container');container.setAttribute('aria-label','Administrator PowerShell terminal');
  bar.append(status,actions);panel.append(bar,container);parent.append(panel);
  let disposed=false,term,fit,observer,ws,resizeFrame=0,lastSize='',session=null,onData,busy=false;
  function button(label,callback,cls='button small ghost'){const item=make('button',cls,label);item.type='button';item.addEventListener('click',async()=>{if(busy)return;busy=true;try{await callback();}catch(error){if(!disposed)status.textContent=error.message;}finally{busy=false;}});actions.append(item);return item;}
  const start=button('Start PowerShell',async()=>{start.disabled=true;try{const result=await api('admin-terminal/','POST',{});session=result.terminal;if(!disposed&&isCurrent())await connect();}finally{if(!disposed)start.disabled=!!session?.running;}},'button small primary');
  const reconnect=button('Reconnect',()=>connect());
  const stop=button('Stop',async()=>{const result=await api('admin-terminal/','DELETE');session=result.terminal;ws?.close();if(!disposed){status.textContent='PowerShell stopped';start.disabled=false;stop.disabled=true;reconnect.disabled=true;}},'button small danger');
  button('A−',()=>{if(term){term.options.fontSize=Math.max(10,term.options.fontSize-1);size(true);}});
  button('A+',()=>{if(term){term.options.fontSize=Math.min(24,term.options.fontSize+1);size(true);}});
  const expand=button('Expand',()=>{const expanded=panel.classList.toggle('terminal-expanded');expand.textContent=expanded?'Restore':'Expand';scheduleSize();});
  function send(data){if(ws?.readyState===WebSocket.OPEN)ws.send(JSON.stringify(data));}
  function size(force=false){if(disposed||!fit||!container.isConnected||!container.clientWidth)return;const dims=fit.proposeDimensions();if(!dims)return;const cols=Math.max(20,Math.min(300,dims.cols)),rows=Math.max(5,Math.min(150,dims.rows));if(term.cols!==cols||term.rows!==rows)term.resize(cols,rows);const value=`${cols}x${rows}`;if(force||lastSize!==value){lastSize=value;send({type:'resize',cols,rows});}}
  function scheduleSize(){cancelAnimationFrame(resizeFrame);resizeFrame=requestAnimationFrame(()=>size());}
  async function connect(){
    if(!session?.running||disposed)return;
    start.disabled=true;stop.disabled=false;reconnect.disabled=false;
    if(!term){
      const {default:xterm}=await import('./vendor/xterm.js');await document.fonts.ready;if(disposed||!isCurrent())return;
      term=new xterm.Terminal({cursorBlink:true,fontSize:14,lineHeight:1.15,letterSpacing:0,fontFamily:'Consolas, "Cascadia Mono", monospace',scrollback:5000,theme:{background:'#111315',foreground:'#e6e9df',cursor:'#c5f277'}});
      fit=new xterm.FitAddon();term.loadAddon(fit);container.replaceChildren();term.open(container);observer=new ResizeObserver(scheduleSize);observer.observe(container);onData=term.onData(data=>send({type:'input',data}));
    }
    ws?.close();term.reset();status.textContent='Connecting…';
    const socket=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws/admin-terminal/${session.id}/`);ws=socket;
    socket.addEventListener('open',()=>{if(disposed||ws!==socket)return;size(true);term.focus();status.textContent=`${session.name} · connected`;stop.disabled=false;reconnect.disabled=false;});
    socket.addEventListener('message',event=>{if(disposed||ws!==socket)return;const data=JSON.parse(event.data);if(data.type==='output')term.write(data.data);else if(data.type==='exit'){session.running=false;status.textContent=data.reason||'PowerShell ended';start.disabled=false;stop.disabled=true;reconnect.disabled=true;}});
    socket.addEventListener('close',()=>{if(!disposed&&ws===socket&&session?.running)status.textContent='Disconnected. Reconnect to resume this terminal.';});
    socket.addEventListener('error',()=>{if(!disposed&&ws===socket)status.textContent='Connection failed. Check Solar and reconnect.';});
  }
  function cleanup(){if(disposed)return;disposed=true;ws?.close();observer?.disconnect();cancelAnimationFrame(resizeFrame);onData?.dispose();term?.dispose();panel.remove();}
  try{const result=await api('admin-terminal/');if(!isCurrent()){cleanup();return cleanup;}session=result.terminal;start.disabled=!!session?.running;stop.disabled=reconnect.disabled=!session?.running;if(session?.running)await connect();else container.append(make('p','viewer-loading','Press Start PowerShell to open a terminal on this PC.'));}
  catch(error){status.textContent=error.message;}
  return cleanup;
}
