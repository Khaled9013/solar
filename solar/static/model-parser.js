import * as THREE from './vendor/three.module.js';
import { STLLoader } from './vendor/STLLoader.js';
import { OBJLoader } from './vendor/OBJLoader.js';
import { PLYLoader } from './vendor/PLYLoader.js';
import { GLTFLoader } from './vendor/GLTFLoader.js';

export const MODEL_LIMIT=2000000, MESH_LIMIT=2000;
const decoder=new TextDecoder();
export function gltfDocument(bytes) {
  let json,bin=null;
  if(bytes.length>=12&&new DataView(bytes.buffer,bytes.byteOffset,bytes.byteLength).getUint32(0,true)===0x46546c67){
    const view=new DataView(bytes.buffer,bytes.byteOffset,bytes.byteLength);
    if(view.getUint32(4,true)!==2||view.getUint32(8,true)!==bytes.length)throw new Error('Invalid GLB header');
    let offset=12;
    while(offset+8<=bytes.length){const size=view.getUint32(offset,true),kind=view.getUint32(offset+4,true);offset+=8;
      if(size%4||size>bytes.length-offset)throw new Error('Invalid GLB chunk');
      if(kind===0x4e4f534a){if(json)throw new Error('Duplicate GLB JSON chunk');json=JSON.parse(decoder.decode(bytes.subarray(offset,offset+size)));}
      if(kind===0x004e4942)bin=bytes.slice(offset,offset+size);offset+=size;
    }
    if(offset!==bytes.length)throw new Error('Invalid GLB length');
  } else json=JSON.parse(decoder.decode(bytes));
  if(!json||String(json.asset?.version)!=='2.0')throw new Error('Only glTF 2.0 models are supported');
  for(const name of json.extensionsRequired||[])if(name!=='KHR_mesh_quantization')throw new Error(`This model needs an unsupported extension: ${name}. Export an uncompressed GLB or STL.`);
  if((json.buffers||[]).length>64||(json.nodes||[]).length>10000||(json.accessors||[]).length>20000)throw new Error('Model is too complex to preview');
  const nodes=json.nodes||[],marks=new Uint8Array(nodes.length);let edges=0;
  for(const node of nodes){edges+=(node.children||[]).length;if(edges>20000)throw new Error('Model hierarchy exceeds the preview limit');}
  function visit(index,depth=0){
    if(!Number.isInteger(index)||index<0||index>=nodes.length)throw new Error('Invalid model node');
    if(depth>128||marks[index]===1)throw new Error('Model hierarchy is cyclic or too deep');
    if(marks[index]===2)return;marks[index]=1;
    for(const child of nodes[index].children||[])visit(child,depth+1);marks[index]=2;
  }
  nodes.forEach((_,index)=>visit(index));
  let count=0;
  for(const buffer of json.buffers||[]){if(!Number.isSafeInteger(buffer.byteLength)||buffer.byteLength<0)throw new Error('Invalid model buffer');count+=buffer.byteLength;if(count>75*1024**2)throw new Error('Model buffers exceed the preview limit');
    if(buffer.uri!==undefined&&(typeof buffer.uri!=='string'||buffer.uri.length>100*1024**2))throw new Error('Invalid model buffer URL');}
  for(const accessor of json.accessors||[])if(!Number.isSafeInteger(accessor.count)||accessor.count<0||accessor.count>MODEL_LIMIT*3)throw new Error('Model geometry exceeds the preview limit');
  for(const mesh of json.meshes||[])for(const primitive of mesh.primitives||[])if(primitive.extensions?.KHR_draco_mesh_compression)throw new Error('Draco-compressed meshes are not supported. Export an uncompressed GLB.');
  for(const view of json.bufferViews||[])if(view.extensions?.EXT_meshopt_compression)throw new Error('Meshopt-compressed meshes are not supported. Export an uncompressed GLB.');
  return {json,bin};
}
export function packScene(root) {
  const meshes=[];let vertices=0,indices=0;
  root.updateMatrixWorld(true);
  root.traverse(object=>{
    if(!object.geometry?.attributes.position)return;
    const geometry=object.geometry.clone().applyMatrix4(object.matrixWorld),position=geometry.attributes.position;
    vertices+=position.count;indices+=geometry.index?.count||position.count;
    if(vertices>MODEL_LIMIT||indices>MODEL_LIMIT*3||meshes.length>=MESH_LIMIT)throw new Error('Model exceeds the preview geometry limit');
    if(position.itemSize!==3||!position.count)throw new Error('Invalid model coordinates');
    const array=new Float32Array(position.count*3);
    for(let i=0;i<position.count;i++)for(let axis=0;axis<3;axis++){const value=position.getComponent(i,axis);if(!Number.isFinite(value)||Math.abs(value)>1e15)throw new Error('Invalid or excessively large model coordinates');array[i*3+axis]=value;}
    let index=null;
    if(geometry.index){index=new Uint32Array(geometry.index.count);for(let i=0;i<index.length;i++){const value=geometry.index.getX(i);if(!Number.isInteger(value)||value<0||value>=position.count)throw new Error('Invalid model face index');index[i]=value;}}
    const material=Array.isArray(object.material)?object.material[0]:object.material;
    const color=material?.color?.toArray();
    meshes.push({position:array,index,color:color?.every(Number.isFinite)?color:[.66,.77,.9],mode:object.isPoints?'points':object.isLine?'lines':'mesh'});
    geometry.dispose();
  });
  if(!meshes.length)throw new Error('No visible geometry was found in this model');
  return meshes;
}
export async function parseModel(format,bytes,resources={}) {
  let root;
  const material=()=>new THREE.MeshStandardMaterial({color:0xa7c4e5});
  if(format==='stl')root=new THREE.Mesh(new STLLoader().parse(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength)),material());
  else if(format==='obj')root=new OBJLoader().parse(decoder.decode(bytes));
  else if(format==='ply'){
    const geometry=new PLYLoader().parse(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength));
    root=geometry.index?new THREE.Mesh(geometry,material()):new THREE.Points(geometry,new THREE.PointsMaterial());
  } else if(['glb','gltf'].includes(format)){
    const {json,bin}=gltfDocument(bytes),urls=[];
    // Geometry preview: omit textures, animations and application-specific plugins.
    json.images=[];json.textures=[];json.animations=[];
    json.materials=(json.materials||[]).map(item=>({name:item.name,pbrMetallicRoughness:{baseColorFactor:item.pbrMetallicRoughness?.baseColorFactor,metallicFactor:0,roughnessFactor:.7},doubleSided:true}));
    json.extensionsUsed=[];json.extensionsRequired=[];delete json.extensions;
    try {
      for(let i=0;i<(json.buffers||[]).length;i++){
        const buffer=json.buffers[i];let data;
        if(buffer.uri===undefined&&i===0)data=bin;
        else if(buffer.uri?.startsWith('data:')){
          const match=/^data:application\/(?:octet-stream|gltf-buffer);base64,([a-z\d+/=\s]*)$/i.exec(buffer.uri);
          if(!match||match[1].length>100*1024**2)throw new Error('Invalid embedded model buffer');
          const raw=atob(match[1]);data=Uint8Array.from(raw,char=>char.charCodeAt(0));
        }else data=resources[buffer.uri];
        if(!(data instanceof Uint8Array)||data.length<buffer.byteLength||data.length>75*1024**2)throw new Error('Missing or invalid glTF buffer; upload its .bin files beside the model');
        const url=URL.createObjectURL(new Blob([data]));urls.push(url);buffer.uri=url;
      }
      const manager=new THREE.LoadingManager();manager.setURLModifier(url=>{if(!urls.includes(url))throw new Error('External model resources are blocked');return url;});
      root=(await new GLTFLoader(manager).parseAsync(JSON.stringify(json),'' )).scene;
    } finally {urls.forEach(url=>URL.revokeObjectURL(url));}
  } else throw new Error('Unsupported model format');
  try{return packScene(root);}finally{root.traverse(object=>{object.geometry?.dispose();const materials=Array.isArray(object.material)?object.material:[object.material];materials.forEach(value=>value?.dispose());});}
}
