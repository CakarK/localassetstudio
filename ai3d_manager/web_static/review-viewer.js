import * as THREE from './vendor/three/three.module.js';
import {OrbitControls} from './vendor/three/OrbitControls.js';
import {GLTFLoader} from './vendor/three/GLTFLoader.js';
import {RoomEnvironment} from './vendor/three/RoomEnvironment.js';

function disposeObject(root){
  const materials=new Set(),textures=new Set();
  root.traverse(o=>{if(o.geometry)o.geometry.dispose();for(const m of Array.isArray(o.material)?o.material:[o.material])if(m)materials.add(m)});
  for(const m of materials){for(const value of Object.values(m))if(value?.isTexture)textures.add(value);m.dispose()}
  for(const texture of textures){texture.dispose();texture.source?.data?.close?.()}
}

export class ReviewViewer{
  constructor(container){
    this.container=container;this.sequence=0;this.loadedSha=null;this.dirty=true;this.root=null;this.wire=false;this.isClay=false;
    this.scene=new THREE.Scene();this.camera=new THREE.PerspectiveCamera(38,1,.01,10000);
    this.renderer=new THREE.WebGLRenderer({antialias:true,alpha:true,powerPreference:'default'});
    this.renderer.setPixelRatio(Math.min(devicePixelRatio,2));this.renderer.outputColorSpace=THREE.SRGBColorSpace;
    this.renderer.toneMapping=THREE.ACESFilmicToneMapping;this.renderer.toneMappingExposure=1.15;
    this.renderer.domElement.setAttribute('aria-hidden','true');container.prepend(this.renderer.domElement);
    this.controls=new OrbitControls(this.camera,this.renderer.domElement);this.controls.enableDamping=!matchMedia('(prefers-reduced-motion: reduce)').matches;
    this.controls.addEventListener('change',()=>{this.dirty=true});
    this.scene.add(new THREE.HemisphereLight(0xeaf0df,0x657257,1.5));
    const key=new THREE.DirectionalLight(0xfff4df,2.2);key.position.set(4,8,6);this.scene.add(key);
    const fill=new THREE.DirectionalLight(0xdde8ff,1.1);fill.position.set(-5,3,-4);this.scene.add(fill);
    const pmrem=new THREE.PMREMGenerator(this.renderer),room=new RoomEnvironment();
    this.environment=pmrem.fromScene(room,.04);this.scene.environment=this.environment.texture;room.dispose();pmrem.dispose();
    this.clayMaterial=new THREE.MeshStandardMaterial({color:0xb8c2ac,roughness:.85,metalness:0});
    this.resizeObserver=new ResizeObserver(()=>this.resize());this.resizeObserver.observe(container);this.resize();
    const tick=()=>{this.frame=requestAnimationFrame(tick);if(document.hidden)return;this.controls.update();if(this.dirty){this.renderer.render(this.scene,this.camera);this.dirty=false}};tick();
    container.addEventListener('webglcontextlost',()=>{this.loadedSha=null;container.dispatchEvent(new CustomEvent('viewer-error',{detail:'3D görüntüleme bağlantısı kesildi. Sayfayı yenileyin.'}))});
  }
  resize(){const w=Math.max(1,this.container.clientWidth),h=Math.max(1,this.container.clientHeight);this.camera.aspect=w/h;this.camera.updateProjectionMatrix();this.renderer.setSize(w,h,false);this.dirty=true}
  async load(model){
    const sequence=++this.sequence;this.loadedSha=null;this.controller?.abort();this.controller=new AbortController();this.clear();
    const response=await fetch(model.url,{signal:this.controller.signal});if(!response.ok){let message='Model dosyası yüklenemedi.';try{message=(await response.json()).error||message}catch{}throw Error(message)}
    const bytes=await response.arrayBuffer();if(sequence!==this.sequence)return null;
    const manager=new THREE.LoadingManager(),resourceErrors=[];manager.onError=url=>resourceErrors.push(url);manager.setURLModifier(url=>{if(url.startsWith('blob:')||url.startsWith('data:')||new URL(url,location.href).origin===location.origin)return url;throw Error('Model dış sunucudan dosya istiyor; yükleme engellendi.')});
    const gltf=await new GLTFLoader(manager).parseAsync(bytes,location.origin+'/');
    const warnings=resourceErrors.length?['Bazı kaplamalar yüklenemedi. Geometriyi inceleyip onaylayabilirsiniz; kaplama eksikliği ayrıca korunur.']:[];
    if(sequence!==this.sequence){disposeObject(gltf.scene);return null}
    this.root=gltf.scene;this.originals=new Map();let triangles=0;
    this.root.traverse(o=>{if(o.isMesh){this.originals.set(o,o.material);triangles+=(o.geometry.index?.count??o.geometry.attributes.position.count)/3}});
    const bounds=new THREE.Box3().setFromObject(this.root);if(bounds.isEmpty()){disposeObject(this.root);this.root=null;throw Error('Modelde görüntülenebilir geometri yok.')}
    const size=bounds.getSize(new THREE.Vector3()),center=bounds.getCenter(new THREE.Vector3());this.root.position.sub(center);
    this.radius=Math.max(size.length()/2,.001);this.scene.add(this.root);this.controls.minDistance=this.radius*.04;this.controls.maxDistance=this.radius*25;
    this.camera.near=this.radius/1000;this.camera.far=this.radius*100;this.camera.updateProjectionMatrix();this.applyMaterials();this.fit('iso');this.loadedSha=model.sha256;
    return {triangles:Math.round(triangles),dimensions:[size.x,size.y,size.z],warnings};
  }
  clear(){if(this.root){for(const [mesh,material] of this.originals||[])mesh.material=material;this.scene.remove(this.root);disposeObject(this.root);this.root=null}this.dirty=true}
  fit(view='iso'){
    if(!this.root)return;
    const vertical=THREE.MathUtils.degToRad(this.camera.fov)/2,horizontal=Math.atan(Math.tan(vertical)*this.camera.aspect);
    const distance=this.radius/Math.sin(Math.min(vertical,horizontal))*1.12;
    const direction=view==='front'?new THREE.Vector3(0,.12,1):view==='top'?new THREE.Vector3(0,1,.0001):new THREE.Vector3(1,.85,1);
    this.controls.target.set(0,0,0);this.camera.position.copy(direction.normalize().multiplyScalar(distance));this.controls.update();this.dirty=true;
  }
  applyMaterials(){if(!this.root)return;for(const [mesh,original] of this.originals){mesh.material=this.isClay?this.clayMaterial:original;for(const material of Array.isArray(mesh.material)?mesh.material:[mesh.material]){material.wireframe=this.wire;material.needsUpdate=true}}this.dirty=true}
  wireframe(value){this.wire=value;this.applyMaterials()}
  clay(value){this.isClay=value;this.applyMaterials()}
  rotate(value){this.controls.autoRotate=value;this.controls.autoRotateSpeed=.7;this.dirty=true}
  dispose(){++this.sequence;this.controller?.abort();cancelAnimationFrame(this.frame);this.resizeObserver.disconnect();this.controls.dispose();this.clear();this.clayMaterial.dispose();this.environment.dispose();this.renderer.dispose()}
}
