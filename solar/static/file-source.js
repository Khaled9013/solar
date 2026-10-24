// All preview reads stay behind the same ownership checks as downloads.
export const PREVIEW_LIMIT = 50 * 1024 ** 2;
export const TEXT_LIMIT = 2 * 1024 ** 2;
export function extension(path) {return String(path).split('.').pop().toLowerCase();}
export function relativePath(base, href) {
  let value;
  try {value=decodeURIComponent(String(href).split('#')[0]);} catch {throw new Error('Invalid file link');}
  value=value.replaceAll('\\','/');
  if(!value||/^[a-z][a-z\d+.-]*:/i.test(value)||value.startsWith('/')||/[\x00-\x1f?]/.test(value))throw new Error('Only relative workspace file links can be opened');
  const parts=base.split('/').slice(0,-1);
  for(const part of value.split('/')){
    if(part==='..'){if(!parts.length)throw new Error('File link leaves the workspace');parts.pop();}
    else if(part&&part!=='.')parts.push(part);
  }
  return parts.join('/');
}
export function externalLink(href) {
  try {const url=new URL(href);return ['https:','http:','mailto:'].includes(url.protocol)?url.href:null;}catch{return null;}
}
export async function fetchBytes(url, signal, limit=PREVIEW_LIMIT) {
  // Callers only construct API paths. Do not follow a redirect to another origin.
  if(!url.startsWith('/api/'))throw new Error('Invalid preview source');
  const response=await fetch(url,{credentials:'same-origin',cache:'no-store',redirect:'error',signal});
  if(!response.ok){let message=`Could not read file (${response.status})`;try{message=(await response.json()).error||message;}catch{}throw new Error(message);}
  if(Number(response.headers.get('content-length'))>limit){await response.body?.cancel();throw new Error('File is too large to preview; use Download');}
  const reader=response.body.getReader(),chunks=[];let length=0;
  try {while(true){const {value,done}=await reader.read();if(done)break;length+=value.length;if(length>limit)throw new Error('File is too large to preview; use Download');chunks.push(value);}}
  catch(error){await reader.cancel();throw error;}finally{reader.releaseLock();}
  const result=new Uint8Array(length);let offset=0;
  for(const chunk of chunks){result.set(chunk,offset);offset+=chunk.length;}return result;
}
export function vaultSource(file, files) {
  const url=`/api/files/${file.id}/`;
  return {path:file.path,size:file.size,url,download:url,preview:['image/png','image/jpeg','image/webp','image/gif'].includes(file.mime)?`${url}?preview=true`:null,
    resolve(href){const path=relativePath(file.path,href),target=files.find(item=>item.path===path);if(!target)throw new Error(`File is not in this session: ${path}`);return vaultSource(target,files);}};
}
export function projectSource(projectId, path, size) {
  const url=`/api/projects/${encodeURIComponent(projectId)}/files/?path=${encodeURIComponent(path)}`;
  return {path,size,url,download:url,resolve(href){return projectSource(projectId,relativePath(path,href));}};
}
