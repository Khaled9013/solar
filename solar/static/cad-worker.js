// OpenCascade runs off the UI thread and is terminated when a viewer closes.
self.onmessage=async({data})=>{
  try {
    importScripts('./vendor/occt-import-js.js');
    const occt=await occtimportjs({locateFile:name=>new URL(`./vendor/${name}`,self.location.href).href});
    const method=['step','stp'].includes(data.format)?'ReadStepFile':['iges','igs'].includes(data.format)?'ReadIgesFile':'ReadBrepFile';
    const result=occt[method](new Uint8Array(data.bytes),{linearUnit:'millimeter',linearDeflectionType:'bounding_box_ratio',linearDeflection:.002,angularDeflection:.5});
    if(!result.success||!result.meshes?.length)throw new Error('The CAD file could not be tessellated');
    if(result.meshes.length>2000)throw new Error('CAD assembly has too many parts to preview');
    let total=0,indices=0;
    const meshes=result.meshes.map(mesh=>{
      const raw=mesh.attributes?.position?.array,faces=mesh.index?.array;
      if(!raw?.length||raw.length%3||!faces?.length||faces.length%3)throw new Error('Invalid CAD geometry');
      total+=raw.length/3;indices+=faces.length;if(total>2000000||indices>6000000)throw new Error('CAD model exceeds the preview geometry limit');
      if(raw.some(value=>!Number.isFinite(value)||Math.abs(value)>1e15)||faces.some(value=>!Number.isInteger(value)||value<0||value>=raw.length/3))throw new Error('Invalid CAD coordinates');
      return {position:new Float32Array(raw),index:new Uint32Array(faces),color:mesh.color||[.66,.77,.9],mode:'mesh'};
    });
    self.postMessage({meshes},meshes.flatMap(mesh=>[mesh.position.buffer,mesh.index.buffer]));
  } catch(error){self.postMessage({error:error.message||'CAD preview failed. Restart Solar to install its CAD viewer runtime.'});}
};
