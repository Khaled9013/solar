import * as THREE from './vendor/three.module.js';
import {OrbitControls} from './vendor/OrbitControls.js';
import {extension,fetchBytes,PREVIEW_LIMIT} from './file-source.js';
import {gltfDocument} from './model-parser.js';

export const MODEL_FORMATS=['step','stp','iges','igs','brep','brp','stl','obj','ply','glb','gltf'];
const CAD=['step','stp','iges','igs','brep','brp'];
function node(tag,cls,text){const item=document.createElement(tag);item.className=cls||'';if(text)item.textContent=text;return item;}
export async function modelViewer(host,source,bytes,signal) {
  const format=extension(source.path),resources=Object.create(null);
  let total=bytes.length;
  if(['glb','gltf'].includes(format)){
    const {json}=gltfDocument(bytes);
    for(const buffer of json.buffers||[])if(buffer.uri&&!buffer.uri.startsWith('data:')&&!resources[buffer.uri]){
      const related=source.resolve(buffer.uri),data=await fetchBytes(related.url,signal,PREVIEW_LIMIT-total);total+=data.length;resources[buffer.uri]=data;
    }
  }
  if(CAD.includes(format)){
    const available=await fetch('/static/vendor/occt-import-js.wasm',{method:'HEAD',signal,cache:'no-store'});
    if(!available.ok)throw new Error("CAD preview runtime is not installed. On the PC, restart Solar or run 'uv run solar viewer-assets' while Solar is stopped, then restart it.");
  }
  signal.throwIfAborted();
  const worker=new Worker(CAD.includes(format)?'/static/cad-worker.js':'/static/model-worker.js',CAD.includes(format)?{}:{type:'module'});
  let meshes;
  try {
    meshes=await new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{cleanup();reject(new Error('Model preview took too long. Export a simpler mesh and try again.'));},45000);
      function cleanup(){clearTimeout(timer);signal.removeEventListener('abort',cancel);worker.terminate();}
      function cancel(){cleanup();reject(new DOMException('Closed','AbortError'));}
      signal.addEventListener('abort',cancel,{once:true});
      worker.onmessage=event=>{cleanup();event.data.error?reject(new Error(event.data.error)):resolve(event.data.meshes);};
      worker.onerror=()=>{cleanup();reject(new Error('The model parser could not start or ran out of memory'));};
      worker.postMessage({format,bytes:bytes.buffer,resources},[bytes.buffer,...Object.values(resources).map(data=>data.buffer)]);
    });
  }finally{worker.terminate();}
  signal.throwIfAborted();host.replaceChildren();
  const toolbar=node('div','viewer-toolbar'),stage=node('div','model-stage'),info=node('div','viewer-caption');
  host.append(toolbar,stage,info);
  const renderer=new THREE.WebGLRenderer({antialias:true,alpha:false});
  renderer.setPixelRatio(Math.min(devicePixelRatio||1,2));renderer.setClearColor(0x11161c);stage.append(renderer.domElement);
  renderer.domElement.setAttribute('aria-label','3D model. Drag to orbit, wheel or pinch to zoom, right-drag to pan.');renderer.domElement.tabIndex=0;
  const scene=new THREE.Scene(),camera=new THREE.PerspectiveCamera(45,1,.01,10000),group=new THREE.Group();scene.add(group);
  const materials=[];let triangles=0,vertices=0;
  for(const packed of meshes){
    const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.BufferAttribute(packed.position,3));
    if(packed.index)geometry.setIndex(new THREE.BufferAttribute(packed.index,1));
    vertices+=packed.position.length/3;const color=new THREE.Color().fromArray(packed.color);
    let object,material;
    if(packed.mode==='points'){material=new THREE.PointsMaterial({color,size:.003,sizeAttenuation:true});object=new THREE.Points(geometry,material);}
    else if(packed.mode==='lines'){material=new THREE.LineBasicMaterial({color});object=new THREE.LineSegments(geometry,material);}
    else {geometry.computeVertexNormals();material=new THREE.MeshStandardMaterial({color,metalness:.12,roughness:.65,side:THREE.DoubleSide});object=new THREE.Mesh(geometry,material);triangles+=(packed.index?.length||packed.position.length/3)/3;}
    materials.push(material);group.add(object);
  }
  const box=new THREE.Box3().setFromObject(group),center=box.getCenter(new THREE.Vector3()),size=box.getSize(new THREE.Vector3()),span=Math.max(size.x,size.y,size.z);
  if(!Number.isFinite(span)||span<=0){renderer.dispose();throw new Error('This model has no measurable dimensions');}
  // Normalize the scene to avoid clipping for CAD units ranging from microns to meters.
  group.position.copy(center).multiplyScalar(-1/span);group.scale.setScalar(1/span);
  for(const material of materials)if(material.isPointsMaterial)material.size=.004;
  scene.add(new THREE.HemisphereLight(0xdce9ff,0x374154,3));const light=new THREE.DirectionalLight(0xffffff,3);light.position.set(3,5,4);scene.add(light);
  const controls=new OrbitControls(camera,renderer.domElement);controls.enableDamping=false;controls.minDistance=.05;controls.maxDistance=100;controls.target.set(0,0,0);
  let disposed=false;
  function draw(){if(!disposed)renderer.render(scene,camera);}
  function resize(){if(disposed)return;const width=Math.max(1,stage.clientWidth),height=Math.max(1,stage.clientHeight);renderer.setSize(width,height);camera.aspect=width/height;camera.updateProjectionMatrix();draw();}
  function view(direction){const distance=1.8/Math.min(1,camera.aspect);camera.position.copy(direction).normalize().multiplyScalar(distance);camera.near=.001;camera.far=200;camera.updateProjectionMatrix();controls.target.set(0,0,0);controls.update();draw();}
  function action(label,fn){const button=node('button','button small ghost',label);button.type='button';button.addEventListener('click',fn);toolbar.append(button);return button;}
  action('Fit model',()=>view(new THREE.Vector3(1, .7, 1)));
  action('Front',()=>view(new THREE.Vector3(0,0,1)));action('Top',()=>view(new THREE.Vector3(0,1,.0001)));action('Right',()=>view(new THREE.Vector3(1,0,0)));
  const wire=action('Wireframe',()=>{const enabled=wire.getAttribute('aria-pressed')!=='true';wire.setAttribute('aria-pressed',String(enabled));materials.forEach(material=>{if(material.isMeshStandardMaterial)material.wireframe=enabled;});draw();});wire.setAttribute('aria-pressed','false');
  info.textContent=`${Math.round(triangles).toLocaleString()} triangles · ${vertices.toLocaleString()} vertices · ${size.toArray().map(value=>Number(value.toPrecision(5))).join(' × ')} ${['step','stp','iges','igs'].includes(format)?'mm':'model units'} · Drag to orbit, scroll to zoom, right-drag to pan. Geometry preview; textures and animations are omitted.`;
  controls.addEventListener('change',draw);const observer=new ResizeObserver(resize);observer.observe(stage);resize();view(new THREE.Vector3(1,.7,1));
  function dispose(){if(disposed)return;disposed=true;observer.disconnect();controls.dispose();group.traverse(object=>object.geometry?.dispose());materials.forEach(material=>material.dispose());renderer.dispose();renderer.forceContextLoss();}
  signal.addEventListener('abort',dispose,{once:true});
  renderer.domElement.addEventListener('webglcontextlost',event=>{event.preventDefault();if(!disposed)info.textContent='The graphics context was lost. Close and reopen the preview.';});
  return dispose;
}
