import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {relativePath,externalLink,vaultSource,projectSource,fetchBytes} from '../solar/static/file-source.js';
import {renderMarkdown} from '../solar/static/markdown.js';
import {parseModel,gltfDocument,MODEL_LIMIT} from '../solar/static/model-parser.js';

assert.equal(relativePath('codex/files/docs/readme.md','../part.stl#view'),'codex/files/part.stl');
assert.equal(relativePath('codex/files/a.md','folder/part%20one.stl'),'codex/files/folder/part one.stl');
for(const value of ['../../../secret','https://example.test','//host/path','C:\\secret','javascript:alert(1)','%2fsecret','x%00y','%zz'])assert.throws(()=>relativePath('user/files/a.md',value));
assert.equal(externalLink('https://example.test/file'),'https://example.test/file');
for(const value of ['javascript:alert(1)','data:text/html,hi','file:///private','relative.md'])assert.equal(externalLink(value),null);
const source=vaultSource({id:'one',path:'codex/files/a.md'},[{id:'two',path:'codex/files/b.pdf'}]);
assert.equal(source.resolve('b.pdf').url,'/api/files/two/');
assert.throws(()=>source.resolve('missing.pdf'));
assert.equal(projectSource('project','docs/a.md').resolve('../b.pdf').url,'/api/projects/project/files/?path=b.pdf');

const oldFetch=globalThis.fetch;
try{
  globalThis.fetch=async()=>new Response(new ReadableStream({start(controller){controller.enqueue(new Uint8Array(8));controller.enqueue(new Uint8Array(8));controller.close();}}));
  await assert.rejects(fetchBytes('/api/files/a/',null,10),/too large/);
  await assert.rejects(fetchBytes('https://example.test/file'),/Invalid/);
}finally{globalThis.fetch=oldFetch;}

class Element {
  constructor(tag){this.tagName=tag;this.children=[];this.listeners={};this.textContent='';}
  set innerHTML(value){throw new Error('Untrusted HTML must never be inserted');}
  append(...children){this.children.push(...children);}
  addEventListener(type,callback){this.listeners[type]=callback;}
}
globalThis.document={createElement:tag=>new Element(tag),createTextNode:text=>({tagName:'#text',textContent:text,children:[]})};
const markdown=await readFile(new URL('./fixtures/preview.md',import.meta.url),'utf8'),opened=[];
const rendered=renderMarkdown(markdown,{openFile:href=>opened.push(href)});
function walk(node){return [node,...node.children.flatMap(walk)];}
const nodes=walk(rendered);
for(const tag of ['h1','strong','table','pre','code','input'])assert(nodes.some(node=>node.tagName===tag),`Missing Markdown ${tag}`);
assert(!nodes.some(node=>['img','script','iframe'].includes(node.tagName)));
assert(!nodes.some(node=>String(node.href||'').startsWith('javascript:')));
nodes.find(node=>node.tagName==='button').listeners.click();
assert.equal(opened[0],'triangle.stl');
assert(nodes.some(node=>node.textContent.includes('onerror=')));
assert(walk(renderMarkdown('<script>alert(1)</script>')).some(node=>node.tagName==='code'&&node.textContent.includes('<script>')));
delete globalThis.document;

const ascii=new Uint8Array(await readFile(new URL('./fixtures/triangle.stl',import.meta.url)));
let meshes=await parseModel('stl',ascii);assert.equal(meshes.length,1);assert.equal(meshes[0].position.length,9);
const binary=new Uint8Array(134),view=new DataView(binary.buffer);view.setUint32(80,1,true);view.setFloat32(104,1,true);view.setFloat32(120,1,true);
meshes=await parseModel('stl',binary);assert.equal(meshes[0].position.length,9);
const encode=value=>new TextEncoder().encode(value);
meshes=await parseModel('obj',encode('v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n'));assert.equal(meshes[0].position.length,9);
meshes=await parseModel('ply',encode('ply\nformat ascii 1.0\nelement vertex 3\nproperty float x\nproperty float y\nproperty float z\nelement face 1\nproperty list uchar int vertex_indices\nend_header\n0 0 0\n1 0 0\n0 1 0\n3 0 1 2\n'));assert.equal(meshes[0].index.length,3);
await assert.rejects(parseModel('obj',encode('v NaN 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')),/Invalid/);
await assert.rejects(parseModel('stl',encode('not a model')),/geometry|bounds|offset/i);

// glTF exercises real loader code with buffers, transforms, and forbidden resource handling.
globalThis.ProgressEvent=class ProgressEvent {constructor(type,options){this.type=type;Object.assign(this,options);}};
const vertices=new Uint8Array(new Float32Array([0,0,0,1,0,0,0,1,0]).buffer);
const gltf={asset:{version:'2.0'},buffers:[{byteLength:36,uri:'part.bin'}],bufferViews:[{buffer:0,byteLength:36}],accessors:[{bufferView:0,componentType:5126,count:3,type:'VEC3',min:[0,0,0],max:[1,1,0]}],meshes:[{primitives:[{attributes:{POSITION:0}}]}],nodes:[{mesh:0,translation:[2,3,4]}],scenes:[{nodes:[0]}],scene:0};
meshes=await parseModel('gltf',encode(JSON.stringify(gltf)),{'part.bin':vertices});assert.deepEqual([...meshes[0].position.slice(0,3)],[2,3,4]);
await assert.rejects(parseModel('gltf',encode(JSON.stringify(gltf)),{}),/Missing/);
const compressed=structuredClone(gltf);compressed.extensionsRequired=['KHR_draco_mesh_compression'];assert.throws(()=>gltfDocument(encode(JSON.stringify(compressed))),/unsupported/);
const huge=structuredClone(gltf);huge.accessors[0].count=MODEL_LIMIT*4;assert.throws(()=>gltfDocument(encode(JSON.stringify(huge))),/limit/);
const cyclic=structuredClone(gltf);cyclic.nodes[0].children=[0];assert.throws(()=>gltfDocument(encode(JSON.stringify(cyclic))),/cyclic/);
const embedded=structuredClone(gltf);delete embedded.buffers[0].uri;
let json=JSON.stringify(embedded);while(json.length%4)json+=' ';
const glb=new Uint8Array(12+8+json.length+8+vertices.length),glbView=new DataView(glb.buffer);
glbView.setUint32(0,0x46546c67,true);glbView.setUint32(4,2,true);glbView.setUint32(8,glb.length,true);
glbView.setUint32(12,json.length,true);glbView.setUint32(16,0x4e4f534a,true);glb.set(encode(json),20);
glbView.setUint32(20+json.length,vertices.length,true);glbView.setUint32(24+json.length,0x004e4942,true);glb.set(vertices,28+json.length);
meshes=await parseModel('glb',glb);assert.equal(meshes.length,1);assert.equal(meshes[0].position[0],2);
glbView.setUint32(8,1,true);assert.throws(()=>gltfDocument(glb),/header/);
console.log('Viewer tests passed: Markdown DOM safety, private relative paths, fetch bounds, STL/OBJ/PLY/glTF/GLB parsing, malformed geometry and compressed/oversized model rejection.');
