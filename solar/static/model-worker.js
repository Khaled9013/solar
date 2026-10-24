import {parseModel} from './model-parser.js';
self.onmessage=async({data})=>{
  try {const meshes=await parseModel(data.format,new Uint8Array(data.bytes),data.resources);
    self.postMessage({meshes},meshes.flatMap(mesh=>[mesh.position.buffer,...(mesh.index?[mesh.index.buffer]:[])]));
  }catch(error){self.postMessage({error:error.message||'Could not parse this model'});}
};
