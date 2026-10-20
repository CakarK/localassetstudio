import {ReviewViewer} from './review-viewer.js';

const $=selector=>document.querySelector(selector),el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n};
const names={Pending:'İnceleme bekliyor',Accepted:'Onaylandı',RevisionRequested:'Revizyon istendi',Rejected:'Reddedildi'};
let csrf='',models=[],selected=null,entry=null,viewer=null,loading=false,saving=false,stale=false,sequence=0,timer,toastTimer,revisionPaused=false;
const drafts=new Map();
function toast(text){$('#toast').textContent=text;$('#toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').hidden=true,6500)}
async function api(url,body){const response=await fetch(url,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:body===undefined?undefined:JSON.stringify(body)});const value=await response.json();if(!response.ok){if(response.status===401){$('#review-app').hidden=true;$('#review-login').hidden=false;clearTimeout(timer)}throw Error(value.error||'İstek tamamlanamadı.')}return value}
function modelMessage(title,description=''){const m=$('#viewer-message');m.hidden=false;m.replaceChildren(el('span','viewer-symbol','◇'),el('strong','',title));if(description)m.append(el('p','',description))}
function actions(){const busy=saving||loading||stale||!entry?.can_decide;$('#approve-model').disabled=busy||!entry?.can_accept||viewer?.loadedSha!==entry?.model.sha256;$('#request-revision').disabled=busy;$('#reject-model').disabled=busy;$('#review-note').disabled=saving||!entry;$('#stale-notice').hidden=!stale}
function renderList(){
  const list=$('#model-list');list.replaceChildren();const term=$('#search').value.toLocaleLowerCase('tr'),filter=$('#status-filter').value;
  const visible=models.filter(m=>(m.name+' '+m.asset_id).toLocaleLowerCase('tr').includes(term)&&(filter==='all'||filter==='blocked'&&m.production_state==='Blocked'||m.review_status===filter));
  for(const model of visible){const button=el('button','model-choice');button.type='button';button.setAttribute('aria-pressed',String(model.subject===selected));if(model.thumbnail){const img=el('img');img.src=model.thumbnail;img.alt='';img.loading='lazy';button.append(img)}else button.append(el('span','model-placeholder','◇'));const body=el('span');body.append(el('strong','',model.name),el('small','',model.asset_id+' · '+(model.source==='Sprint 01'?'Sprint 01':model.id.slice(0,6))),el('span','choice-state '+model.review_status.toLowerCase(),(names[model.review_status]||model.review_status)+(model.production_state==='Blocked'?' · Üretim blokeli':'')));button.append(body);button.addEventListener('click',()=>selectModel(model.subject));list.append(button)}
  if(!visible.length)list.append(el('p','rail-empty',models.length?'Bu filtreye uygun model bulunamadı.':'Henüz 3D model oluşmadı. Üretim atölyesinden başlayın.'));
  $('#model-count').textContent=`${models.length} model · ${models.filter(m=>m.review_status==='Pending').length} inceleme bekliyor`;
}
const checkNames={footprint:'Alan ve ölçek',height:'Yükseklik',semantic_prompt_adherence:'Prompt uyumu',artistic_quality:'Sanatsal kalite',quad_retopology:'Quad retopoloji',uv_overlap_and_lightmap:'UV çakışması / lightmap',real_unreal_import:'Gerçek Unreal importu',triangle_budget:'Üçgen bütçesi',uv:'UV verisi',pivot:'Pivot'};
function renderRevision(){
  const panel=$('#revision-production'),task=entry?.revision_production;panel.replaceChildren();panel.hidden=!task;if(!task)return;
  const labels={Pending:'Otomatik üretim bekliyor',Preparing:'Notlara göre prompt hazırlanıyor',Reference:'Yeni referans üretiliyor',Queued:'3D kuyruğunda',Production:'3D model ve kontroller üretiliyor',Done:'Yeni aday incelemeye hazır',Blocked:'Üretim durdu · inceleme gerekli',Cancelled:'Üretim iptal edildi',Superseded:'Daha yeni karar nedeniyle durduruldu'};
  panel.append(el('strong','',labels[task.state]||task.state));
  if(revisionPaused&&!['Done','Blocked','Cancelled','Superseded'].includes(task.state))panel.append(el('p','','Sıra duraklatıldı. Çalışan aşama bitince yeni işlem başlatılmaz.'));
  if(task.error)panel.append(el('p','',task.error));
  if(task.child_job_id){const a=el('a','file-link',task.state==='Done'?'Yeni modeli 3D incele ↗':'Yeni üretimi izle ↗');a.href=task.state==='Done'?'/reviews?job='+encodeURIComponent(task.child_job_id):'/?job='+encodeURIComponent(task.child_job_id);panel.append(a)}
}
function renderDetail(){
  $('#model-title').textContent=entry.name;$('#asset-code').textContent=entry.asset_id+' / '+entry.source;$('#model-source').textContent=entry.model.label;$('#triangles').textContent=entry.triangles?.toLocaleString('tr-TR')??'—';$('#dimensions').textContent=entry.dimensions?entry.dimensions.map(n=>n.toLocaleString('tr-TR',{maximumFractionDigits:2})).join(' × ')+' m':'Ölçü doğrulanmamış';$('#review-state').textContent=names[entry.review_status];
  $('#reference-image').hidden=!entry.reference_url;$('#reference-link').hidden=!entry.reference_url;$('#no-reference').hidden=!!entry.reference_url;if(entry.reference_url){$('#reference-image').src=entry.reference_url;$('#reference-link').href=entry.reference_url}else $('#no-reference').textContent='Kaynak referans dosyası bulunamadı.';
  $('#technical-state').textContent={PASS:'Geçti',WARN:'Uyarılar var',FAIL:'Başarısız'}[entry.technical_status]||'Henüz yok';$('#qwen-score').textContent=entry.visual?.overall_score?.toLocaleString('tr-TR',{maximumFractionDigits:2})??'—';$('#visual-error').textContent=entry.visual?.error?'Görsel rapor tamamlanamadı: '+entry.visual.error.message:'';
  const rawGeometry=['GeometryGenerated','GeometryOptimized'].includes(entry.model.stage),warnings=[];if(rawGeometry)warnings.push('Bu ham geometri; PBR kaplama aşaması henüz tamamlanmadığı için beyaz görünebilir. Renk veya malzeme için önce üretimin kaldığı aşamadan tamamlanması gerekir.');if(entry.production_error)warnings.push('Üretim kaydı: '+entry.production_error);$('#production-warning').hidden=!warnings.length;$('#production-warning').textContent=warnings.join(' ');
  const checks=$('#technical-checks');checks.replaceChildren();for(const check of entry.technical?.checks||[]){const row=el('li'),state={PASS:'Geçti',WARN:'Doğrulanmadı / uyarı',FAIL:'Başarısız'}[check.status]||check.status;row.append(el('b','',(checkNames[check.check]||check.check.replaceAll('_',' '))+' · '+state));if(check.status==='WARN'&&typeof check.detail==='object'&&check.detail?.reason)row.append(el('span','','Kaynak ölçüsü belirtilmemiş.'));checks.append(row)}if(!checks.childElementCount)checks.append(el('li','','Teknik doğrulama henüz tamamlanmamış.'));
  $('#decision-hint').textContent=!entry.can_decide?'Bu model üretimde veya sırada. İşlem bitince karar verebilirsiniz.':'Revizyon ve ret, notunuza göre yeni görsel ve ardından otomatik 3D üretimi başlatır. Önceki model korunur. Son onay yine sizdedir. Teknik uyarılar model onayınızı engellemez.';
  renderRevision();
  const history=$('#history-list');history.replaceChildren();for(const review of entry.history){const row=el('li');row.append(el('strong','',names[review.action]||review.action),el('small','',new Date(review.created_at).toLocaleString('tr-TR')+' · model '+(review.model_sha256?.slice(0,8)||'önceki kayıt')),el('p','',review.note||'Not eklenmedi.'));history.append(row)}if(!entry.history.length)history.append(el('li','','Bu model için henüz karar kaydedilmedi.'));
  const files=$('#review-files');files.replaceChildren();const download=el('a','file-link','GLB indir ↓');download.href=entry.model.url;download.download=entry.asset_id+'.glb';files.append(download);for(const file of entry.files||[]){if(file.name==='model.glb')continue;const a=el('a','file-link',file.name);a.href=file.url;a.target='_blank';a.rel='noopener';files.append(a)}if(entry.download_url){const a=el('a','file-link','Teslim ZIP ↓');a.href=entry.download_url;files.append(a)}if(entry.source==='Web üretimi'){const a=el('a','file-link','Üretim sayfasında aç ↗');a.href='/?job='+encodeURIComponent(entry.id);files.append(a)}
  actions();
}
async function selectModel(subject){
  if(saving)return;if(selected)drafts.set(selected,$('#review-note').value);selected=subject;const seq=++sequence;stale=false;loading=true;entry=null;actions();renderList();$('#decision-receipt').hidden=true;modelMessage('Model yükleniyor…','Gerçek GLB dosyası açılıyor.');
  const url=new URL(location.href);url.search='';url.searchParams.set(subject.startsWith('library:')?'library':'job',subject.split(':').slice(1).join(':'));history.replaceState(null,'',url);localStorage.setItem('rei-review-selected',subject);
  try{const data=await api('/api/model-reviews/'+encodeURIComponent(subject));if(seq!==sequence)return;csrf=data.csrf;entry=data.model;$('#review-note').value=drafts.get(subject)||'';renderDetail();
    if(!viewer)viewer=new ReviewViewer($('#viewer'));
    if(viewer.loadedSha===entry.model.sha256){$('#viewer-message').hidden=true;return}
    const stats=await viewer.load(entry.model);if(seq!==sequence||!stats)return;
    $('#triangles').textContent=stats.triangles.toLocaleString('tr-TR');if(!entry.dimensions)$('#dimensions').textContent=stats.dimensions.map(n=>n.toLocaleString('tr-TR',{maximumFractionDigits:2})).join(' × ')+' · '+(entry.model.stage==='GeometryGenerated'?'ham model birimi':'model birimi');$('#viewer-message').hidden=true;if(stats.warnings?.length){$('#visual-error').textContent=stats.warnings.join(' ');toast(stats.warnings.join(' '))}
    for(const b of document.querySelectorAll('[data-camera]'))b.setAttribute('aria-pressed',String(b.dataset.camera==='iso'));
  }catch(error){if(seq!==sequence)return;if(error.name!=='AbortError'){modelMessage('3D görünüm açılamadı.',error.message);toast(error.message)}}finally{if(seq===sequence){loading=false;actions()}}
}
async function refreshList(){
  clearTimeout(timer);
  try{const data=await api('/api/model-reviews');csrf=data.csrf;models=data.models;revisionPaused=!!data.revision_paused;$('#revision-pause').textContent=revisionPaused?'Üretime devam et':'Üretimi duraklat';$('#boot-status').hidden=true;$('#review-login').hidden=true;$('#review-app').hidden=false;renderList();
    if(entry){const updated=models.find(m=>m.subject===entry.subject);if(!updated||updated.revision!==entry.revision||updated.review_version!==entry.review_version||updated.model.sha256!==entry.model.sha256){stale=true;actions()}else{entry.revision_production=updated.revision_production;renderRevision()}}
    if(!selected&&models.length){const params=new URL(location.href).searchParams;const requested=params.has('job')?'web:'+params.get('job'):params.has('library')?'library:'+params.get('library'):localStorage.getItem('rei-review-selected');await selectModel(models.some(m=>m.subject===requested)?requested:models[0].subject)}
    timer=setTimeout(refreshList,15000);
  }catch(error){$('#boot-status').hidden=true;if($('#review-app').hidden===false){toast(error.message);timer=setTimeout(refreshList,10000)}}
}
async function decide(action){
  if(!entry||saving||loading||stale)return;$('#review-note').required=action!=='Accepted';if(!$('#decision-form').reportValidity())return;
  saving=true;actions();const subject=entry.subject;
  try{const data=await api('/api/model-reviews/'+encodeURIComponent(subject),{action,note:$('#review-note').value,revision:entry.revision,review_version:entry.review_version,model_sha256:entry.model.sha256});entry=data.model;drafts.delete(subject);$('#review-note').value='';stale=false;renderDetail();$('#decision-receipt').hidden=false;$('#decision-receipt').textContent=action==='Accepted'?(entry.acceptance_scope==='model_only'?'Modeli beğendiğiniz kaydedildi. Teknik eksikler ve üretim durumu ayrı tutuldu.':'Model onayınız kaydedildi.'):'Notunuz kaydedildi. Yeni görsel ve otomatik 3D üretim isteği sıraya alındı; önceki model korunuyor.';toast(names[action]);await refreshList()}catch(error){toast(error.message);await refreshList()}finally{saving=false;actions()}
}
$('#login-form').addEventListener('submit',async event=>{event.preventDefault();try{const data=await api('/api/login',{code:$('#access-code').value});csrf=data.csrf;$('#access-code').value='';$('#login-error').textContent='';await refreshList()}catch(error){$('#login-error').textContent=error.message}});
$('#logout').addEventListener('click',async()=>{try{await api('/api/logout',{});location.reload()}catch(error){toast(error.message)}});
$('#search').addEventListener('input',renderList);$('#status-filter').addEventListener('change',renderList);
$('#refresh').addEventListener('click',async()=>{await refreshList();if(selected&&!saving)await selectModel(selected)});$('#reload-model').addEventListener('click',()=>selectModel(selected));
$('#approve-model').addEventListener('click',()=>decide('Accepted'));$('#request-revision').addEventListener('click',()=>decide('RevisionRequested'));$('#reject-model').addEventListener('click',()=>decide('Rejected'));$('#decision-form').addEventListener('submit',event=>event.preventDefault());
$('#revision-pause').addEventListener('click',async()=>{try{await api(revisionPaused?'/api/queue/start':'/api/queue/pause',{});await refreshList();toast(revisionPaused?'Üretim duraklatıldı.':'Üretim devam edecek.')}catch(error){toast(error.message)}});
for(const button of document.querySelectorAll('[data-camera]'))button.addEventListener('click',()=>{viewer?.fit(button.dataset.camera);for(const b of document.querySelectorAll('[data-camera]'))b.setAttribute('aria-pressed',String(b===button))});
for(const [id,method] of [['wireframe','wireframe'],['clay','clay'],['rotate','rotate']])$('#'+id).addEventListener('click',()=>{const button=$('#'+id),value=button.getAttribute('aria-pressed')!=='true';button.setAttribute('aria-pressed',String(value));viewer?.[method](value)});
$('#reset-camera').addEventListener('click',()=>viewer?.fit('iso'));$('#fullscreen').addEventListener('click',async()=>{try{if(document.fullscreenElement)await document.exitFullscreen();else await $('#viewer').requestFullscreen()}catch{toast('Bu tarayıcı tam ekranı desteklemiyor.')}});
$('#viewer').addEventListener('viewer-error',event=>{modelMessage('3D görünüm durdu.',event.detail);actions()});window.addEventListener('pagehide',()=>{clearTimeout(timer);viewer?.dispose()});
window.addEventListener('pageshow',event=>{if(event.persisted)location.reload()});
refreshList();
