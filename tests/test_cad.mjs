// Requires the pinned runtime installed by `uv run solar viewer-assets`.
import assert from 'node:assert/strict';
import {access,readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
const wasm=new URL('../solar/static/vendor/occt-import-js.wasm',import.meta.url);
try{await access(wasm);}catch{console.error('CAD acceptance is pending: install the runtime with uv run solar viewer-assets first.');process.exit(2);}
const require=createRequire(import.meta.url),factory=require('../solar/static/vendor/occt-import-js.js');
const occt=await factory({locateFile:()=>fileURLToPath(wasm)});
const bytes=new Uint8Array(await readFile(new URL('./fixtures/cube.step',import.meta.url)));
const result=occt.ReadStepFile(bytes,{linearUnit:'millimeter',linearDeflectionType:'bounding_box_ratio',linearDeflection:.002,angularDeflection:.5});
assert(result.success);assert(result.meshes.length>0);
let vertices=0,faces=0;
for(const mesh of result.meshes){const points=mesh.attributes.position.array;assert(points.length>0);assert.equal(points.length%3,0);assert(points.every(Number.isFinite));assert(mesh.index.array.every(value=>value>=0&&value<points.length/3));vertices+=points.length/3;faces+=mesh.index.array.length/3;}
assert(faces>=12);console.log(`CAD parser passed: STEP cube, ${vertices} vertices, ${faces} triangles.`);
