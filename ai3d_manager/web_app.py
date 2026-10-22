"""Authenticated LAN workbench. Comfy/Ollama remain bound to localhost."""
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from flask import Flask, request, session, jsonify, send_file, abort
from werkzeug.security import generate_password_hash, check_password_hash
from .common import AI, ROOT, atomic_json, utcnow
from .catalog import load_catalog
from .worker import settings_for
from .web_assets import asset_from_request
from .web_policy import ground_free_asset,GROUND_NEGATIVE
from . import web_queue,web_revisions,web_production
from .web_control import connect, control, authorize, snapshot, reference_ready, STEPS, LABELS, QUALITY_PATH, QUALITY_ROOT

STATIC=ROOT/'ai3d_manager/web_static'
CONFIG=AI/'State/web-config.json'

def local_addresses():
    addresses={'127.0.0.1'}
    try:addresses.update(a[4][0] for a in socket.getaddrinfo(socket.gethostname(),None,socket.AF_INET))
    except OSError:pass
    return sorted(addresses)

def configuration():
    if CONFIG.exists():return json.loads(CONFIG.read_text('utf-8'))
    code=secrets.token_urlsafe(12)
    cfg={'secret':secrets.token_hex(32),'password_hash':generate_password_hash(code)}
    atomic_json(CONFIG,cfg)
    (AI/'State/web-access-code.txt').write_text(code,encoding='utf-8')
    return cfg

def create_app(test_config=None):
    app=Flask(__name__,static_folder=str(STATIC),static_url_path='/static')
    cfg=configuration() if not test_config else test_config
    app.config.update(SECRET_KEY=cfg['secret'],MAX_CONTENT_LENGTH=24000,
        SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Strict',PERMANENT_SESSION_LIFETIME=43200,
        TRUSTED_HOSTS=['localhost',socket.gethostname(),*local_addresses()])
    if test_config:app.config.update(TESTING=True,TRUSTED_HOSTS=['localhost'])
    failures={};children={};mutex=threading.Lock();design_mutex=threading.Lock()

    @app.before_request
    def guard():
        if request.method not in ('GET','HEAD','OPTIONS'):
            origin=request.headers.get('Origin')
            if origin and urlsplit(origin).netloc!=request.host:abort(403)
            if request.headers.get('Sec-Fetch-Site')=='cross-site':abort(403)
        if request.path.startswith('/api/') or request.path.startswith('/files/'):
            if request.path!='/api/login' and not session.get('authenticated'):abort(401)
            if request.method=='POST' and request.path!='/api/login':
                if not secrets.compare_digest(request.headers.get('X-CSRF-Token',''),session.get('csrf','invalid')):abort(403)

    @app.after_request
    def headers(response):
        response.headers['X-Asset-Studio']='game3dcreator'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        if request.path=='/reviews':
            # GLB images are decoded from local embedded blob/data resources.
            response.headers['Content-Security-Policy']=response.headers['Content-Security-Policy'].replace("connect-src 'self'","connect-src 'self' blob: data:")
        if request.path.startswith(('/api/','/files/')):response.headers['Cache-Control']='no-store'
        return response

    @app.errorhandler(ValueError)
    def bad_value(exc):return jsonify(error=str(exc)),409

    @app.errorhandler(401)
    def unauth(exc):return jsonify(error='Giriş gerekli.'),401

    @app.get('/')
    def index():return send_file(STATIC/'index.html')

    @app.get('/reviews')
    def model_review_page():return send_file(STATIC/'reviews.html')

    @app.get('/queue')
    def production_queue_page():return send_file(STATIC/'queue.html')

    @app.post('/api/login')
    def login():
        ip=request.remote_addr;now=time.monotonic()
        times=[x for x in failures.get(ip,[]) if now-x<60]
        if len(times)>=5:return jsonify(error='Bir dakika bekleyip tekrar deneyin.'),429
        code=(request.get_json(silent=True) or {}).get('code','')
        if not isinstance(code,str) or len(code)>200 or not check_password_hash(cfg['password_hash'],code):
            failures[ip]=times+[now];return jsonify(error='Erişim kodu yanlış.'),401
        failures.pop(ip,None);session.clear();session.permanent=True
        session.update(authenticated=True,csrf=secrets.token_hex(24))
        return jsonify(csrf=session['csrf'])

    @app.post('/api/logout')
    def logout():session.clear();return jsonify(ok=True)

    def reconcile(db):
        import psutil
        for row in db.db.execute("SELECT * FROM WebControls WHERE state='Running'").fetchall():
            proc=children.get(row['job_id'])
            alive=proc is not None and proc.poll() is None
            if not alive and row['pid']:
                try:
                    args=psutil.Process(row['pid']).cmdline()
                    alive='ai3d_manager.web_control' in args and row['job_id'] in args
                except psutil.Error:pass
            # A reservation without PID can be between commit and Popen; never steal it.
            age=time.time()-__import__('datetime').datetime.fromisoformat(row['updated_at']).timestamp()
            if not alive and (row['pid'] or age>60):
                db.db.execute("UPDATE WebControls SET state='Blocked',error=?,pid=NULL WHERE job_id=?",
                    ('İşlem beklenmedik biçimde kapandı. Kayıtlar korundu; otomatik tekrar gönderilmedi.',row['job_id']));db.db.commit()

    def file_url(ident,stage,name):return f'/files/job/{ident}/{stage}/{name}'

    def detail(db,job):
        ident=job['id'];row=control(db,ident);out=snapshot(db,ident)
        artifacts=[]
        for stage,items in out.items():
            for name,info in items.items():
                artifacts.append({'stage':stage,'name':name,'size':info['size'],'suffix':Path(info['path']).suffix.lower(),
                                  'url':file_url(ident,stage,name),'sha256':info['sha256']})
        asset=json.loads(job['request_json'])
        events=[dict(x) for x in db.db.execute('SELECT created_at,event,data_json FROM JobEvents WHERE job_id=? ORDER BY id DESC LIMIT 30',(ident,))]
        reports=[]
        if QUALITY_PATH.exists():
            import sqlite3
            q=sqlite3.connect(QUALITY_PATH.as_uri()+'?mode=ro',uri=True)
            try:
                for table in ('QualityGateReports','ReferenceFidelityReports','QualityEvaluations'):
                    for r in q.execute(f'SELECT report_json FROM {table} WHERE job_id=? ORDER BY created_at',(ident,)):
                        if r[0]:reports.append(json.loads(r[0]))
            finally:q.close()
        return {'id':ident,'asset_id':job['asset_id'],'name':asset.get('name_tr',job['asset_id']),
                'provider':asset.get('image_provider'),'created_at':job['created_at'],'production_status':job['status'],
                'control':row,'reference_ready':reference_ready(db,ident),'artifacts':artifacts,'stages':db.stages(ident),'events':events,'reports':reports,
                'customization':asset.get('web_customization',''),'design':asset.get('web_design',{}),'settings':json.loads(job['settings_json']),
                'source_mode':'custom' if asset.get('web_custom_asset') else 'catalog',
                'asset':{k:asset.get(k) for k in ('name_tr','prompt','prompt_language','phase35_gate_profile','polygon_target','footprint_m','height_m','period','shared_style','user_shared_style','user_shared_negative')}}

    @app.get('/api/state')
    def state():
        db=connect()
        try:
            with mutex:
                reconcile(db);web_queue.settle(db)
            return jsonify(csrf=session['csrf'],addresses=local_addresses(),steps=[{'id':s,'label':l} for s,l in zip(STEPS,LABELS) if s!='ReferenceFidelity'],
                jobs=[detail(db,j) for j in reversed(db.jobs())],library=library(),
                production=web_production.board(db,QUALITY_PATH),
                queue={'running':db.setting('web_queue_running',False),'items':[{k:v for k,v in r.items() if k!='snapshot_json'} for r in web_queue.rows(db)]})
        finally:db.close()

    @app.get('/api/catalog')
    def catalog():
        from .image_providers import load_registry
        source=load_catalog()
        from .geometry_providers import availability
        return jsonify(assets=source,geometry_providers=[{'id':'trellis2','name':'TRELLIS.2 · 512','available':availability()}],providers=[{'id':k,'name':v['name']} for k,v in load_registry()['providers'].items()])

    @app.post('/api/design/compose')
    def compose_design():
        from .web_design import compose
        data=request.get_json()
        asset=next((a for a in load_catalog() if a['asset_id']==data.get('asset_id')),None)
        edits=data.get('customization')
        if not asset or not isinstance(edits,str) or not 1<=len(edits)<=1400:raise ValueError('Geçerli varlık ve en fazla 1400 karakter tasarım notu gerekli.')
        if not design_mutex.acquire(blocking=False):raise ValueError('Başka bir tasarım notu işleniyor; bitmesini bekleyin.')
        try:
            value=compose(asset,edits)
            # A signed token binds the exact reviewed/used prompt to this catalog and edits.
            from itsdangerous import URLSafeTimedSerializer
            token=URLSafeTimedSerializer(cfg['secret'],salt='web-design').dumps(value)
            return jsonify(prompt=value['prompt'],design_token=token)
        except Exception as exc:
            if isinstance(exc,ValueError):raise
            raise ValueError('Yerel tasarım çevirisi tamamlanamadı: '+str(exc)) from exc
        finally:design_mutex.release()

    @app.post('/api/jobs')
    def create_job():
        from .reference_prompts import compile_prompt
        from .image_providers import load_registry
        data=request.get_json();asset=asset_from_request(data)
        provider=data.get('provider','z-image-turbo')
        if data.get('geometry_provider','trellis2')!='trellis2':raise ValueError('Removed 3D provider; TRELLIS.2 is required.')
        if provider not in load_registry()['providers']:raise ValueError('Geçersiz görsel modeli.')
        custom=data.get('customization','')
        if not isinstance(custom,str) or len(custom)>1400:raise ValueError('Tasarım notları en fazla 1400 karakter olmalı.')
        language=data.get('language','tr')
        if language not in ('tr','en'):raise ValueError('Geçersiz dil.')
        seed=data.get('seed',42)
        if type(seed)!=int or not 0<=seed<=2147483647:raise ValueError('Seed 0–2147483647 arasında olmalı.')
        design=data.get('design',{})
        if not isinstance(design,dict) or set(design)-{'roof','door','surroundings','notes','language'} or any(not isinstance(v,str) or len(v)>1400 for v in design.values()):
            raise ValueError('Geçersiz tasarım alanı.')
        composition={'generation_prompt':asset['prompt'],'negative':asset['shared_negative']} if asset.get('web_custom_asset') else compile_prompt(asset)
        composed=None
        if asset.get('web_custom_asset') and custom:raise ValueError('Serbest varlığın bütün ayrıntılarını ana prompt alanına yazın.')
        if custom:
            from itsdangerous import URLSafeTimedSerializer,BadSignature
            try:composed=URLSafeTimedSerializer(cfg['secret'],salt='web-design').loads(data.get('design_token',''),max_age=43200)
            except (BadSignature,TypeError) as exc:raise ValueError('Tasarım prompt’unu önce hazırlayın.') from exc
            if composed['original_catalog_prompt']!=asset['prompt'] or composed['edits']!=custom:raise ValueError('Tasarım notları prompt hazırlandıktan sonra değişmiş.')
            asset={**asset,'catalog_original_prompt':asset['prompt'],'prompt':composed['prompt'],'prompt_is_customized':True}
            composition={**composition,'generation_prompt':composed['prompt']}
        asset=ground_free_asset(asset)
        composition={**composition,'negative':composition['negative']+', '+GROUND_NEGATIVE}
        asset={**asset,'quality_sidecar':{'db':str(QUALITY_PATH),'root':str(QUALITY_ROOT)},'image_provider':provider,
            'web_base_prompt':composition['generation_prompt'],'web_customization':custom,'web_customization_language':language,'web_design':design,
            'web_composition':composed}
        settings=settings_for(asset,{'image_provider':provider,'geometry_provider':'trellis2','trellis_steps':12,'seed':seed,'prompt':composition['generation_prompt'],
            'prompt_language':asset.get('prompt_language','en'),
            'negative':composition['negative'],'max_transient_retries':0,'allow_quality_reduction':False,
            'shape_steps':40,'shape_resolution':384,'subprocess_timeout':600})
        db=connect()
        try:
            db.sync_assets([asset]);job,created=db.enqueue(asset,'web',settings)
            db.db.execute('INSERT OR IGNORE INTO WebControls(job_id,updated_at) VALUES (?,?)',(job['id'],utcnow()));db.db.commit()
            return jsonify(job=detail(db,job),created=created)
        finally:db.close()

    def launch(db,ident,target):
        logdir=AI/'Logs/Web';logdir.mkdir(parents=True,exist_ok=True)
        try:
            with (logdir/(ident+'.log')).open('ab') as log:
                proc=subprocess.Popen([sys.executable,'-m','ai3d_manager.web_control',ident,target],cwd=ROOT,
                    stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),env={**os.environ,'PYTHONUTF8':'1'})
            children[ident]=proc
            db.db.execute('UPDATE WebControls SET pid=? WHERE job_id=? AND state=?',(proc.pid,ident,'Running'));db.db.commit()
        except Exception as exc:
            db.db.execute("UPDATE WebControls SET state='Blocked',error=? WHERE job_id=?",(str(exc),ident));db.db.commit();raise

    @app.post('/api/jobs/<ident>/advance')
    def advance(ident):
        db=connect()
        try:
            with mutex:
                reconcile(db)
                revision=(request.get_json() or {}).get('revision')
                if type(revision)!=int:raise ValueError('Güncel ekran sürümü gerekli.')
                target=authorize(db,ident,revision)
                if target!='UserAccepted':
                    launch(db,ident,target)
            return jsonify(ok=True,target=target)
        finally:db.close()

    @app.post('/api/jobs/<ident>/stop')
    def stop(ident):
        db=connect()
        try:
            with mutex:
                control(db,ident)
                for item in web_queue.rows(db):
                    if item['job_id']==ident and item['state']=='Queued':web_queue.remove(db,item['id'])
                db.cancel(ident)
                db.db.execute("UPDATE WebControls SET state=CASE WHEN state='Running' THEN state ELSE 'Rejected' END,revision=revision+1 WHERE job_id=?",(ident,));db.db.commit()
            return jsonify(ok=True)
        finally:db.close()

    @app.post('/api/queue/add')
    def add_to_queue():
        db=connect()
        try:
            with mutex:ids=web_queue.add(db,(request.get_json() or {}).get('selections'))
            return jsonify(ids=ids)
        finally:db.close()

    @app.post('/api/queue/start')
    def start_queue():
        db=connect()
        try:
            with mutex:
                if not any(r['state']=='Queued' for r in web_queue.rows(db)) and not web_revisions.pending(db):raise ValueError('3D kuyruğuna önce hazır referans seçin.')
                db.set_setting('web_revision_paused',False)
                db.set_setting('web_queue_running',True)
            return jsonify(ok=True)
        finally:db.close()

    @app.post('/api/queue/pause')
    def pause_queue():
        db=connect()
        try:
            with mutex:
                db.set_setting('web_queue_running',False)
                db.set_setting('web_revision_paused',True)
            return jsonify(ok=True)
        finally:db.close()

    @app.post('/api/queue/reorder')
    def reorder_queue():
        db=connect()
        try:
            with mutex:web_queue.reorder(db,(request.get_json() or {}).get('ids'))
            return jsonify(ok=True)
        finally:db.close()

    @app.post('/api/queue/<qid>/remove')
    def remove_queue(qid):
        db=connect()
        try:
            with mutex:web_queue.remove(db,qid)
            return jsonify(ok=True)
        finally:db.close()

    @app.get('/api/production')
    def production_board():
        db=connect()
        try:
            with mutex:
                reconcile(db);web_queue.settle(db);web_revisions.reconcile(db)
                value=web_production.board(db,QUALITY_PATH)
            return jsonify(csrf=session['csrf'],production=value)
        finally:db.close()

    @app.post('/api/production/reorder')
    def reorder_production():
        db=connect()
        try:
            with mutex:web_production.reorder(db,(request.get_json() or {}).get('keys'))
            return jsonify(ok=True)
        finally:db.close()

    @app.post('/api/production/remove')
    def remove_production():
        db=connect()
        try:
            with mutex:web_production.cancel(db,(request.get_json() or {}).get('key'))
            return jsonify(ok=True)
        finally:db.close()

    revision_tick_mutex=threading.Lock()
    def queue_tick():
        # Composition uses the existing CPU translator outside the HTTP mutex.
        # A second tick must not prepare the same review concurrently.
        if not revision_tick_mutex.acquire(blocking=False):return
        db=connect()
        try:
            with mutex:
                reconcile(db);web_queue.settle(db)
                web_revisions.enroll(db);web_revisions.reconcile(db)
                if db.db.execute("SELECT 1 FROM WebControls WHERE state='Running'").fetchone():return
                chosen=next((i for i in web_production.candidates(db) if i['enabled']),None)
                if not chosen:return
                if chosen['queue_id']:
                    ident=web_queue.claim_next(db,[chosen['job_id']] if chosen['kind']=='revision' else (),selected_job=chosen['job_id'])
                    if ident:launch(db,ident,'Production')
                    return
                task=chosen.get('task')
                if not task:return
                if task['child_job_id']:
                    try:web_revisions.advance(db,task,launch)
                    except Exception as exc:web_revisions.update(db,task['review_id'],state='Blocked',error=str(exc))
                    return
                web_revisions.update(db,task['review_id'],state='Preparing')
            try:
                ident=web_revisions.prepare(db,task,AI)
                with mutex:
                    task=dict(db.db.execute('SELECT * FROM WebReviewProductions WHERE review_id=?',(task['review_id'],)).fetchone())
                    if task['state'] not in web_revisions.TERMINAL and web_revisions.current(db,task) and not db.setting('web_revision_paused',False) and not db.db.execute("SELECT 1 FROM WebControls WHERE state='Running'").fetchone():
                        web_revisions.advance(db,task,launch)
            except Exception as exc:
                with mutex:
                    latest=db.db.execute('SELECT state FROM WebReviewProductions WHERE review_id=?',(task['review_id'],)).fetchone()
                    if latest['state'] not in web_revisions.TERMINAL:web_revisions.update(db,task['review_id'],state='Blocked',error=str(exc))
        finally:
            db.close();revision_tick_mutex.release()
    app.extensions['studio_queue_tick']=queue_tick

    @app.post('/api/jobs/<ident>/recover')
    def recover(ident):
        db=connect()
        try:
            with mutex:
                row=control(db,ident);revision=(request.get_json() or {}).get('revision')
                if row['state'] not in ('Blocked','Rejected') or row['revision']!=revision:raise ValueError('Güncel bir durdurulmuş iş seçin.')
                if db.db.execute("SELECT 1 FROM WebControls WHERE state='Running'").fetchone():raise ValueError('Çalışan işlem bitmeli.')
                db.resume(ident)
                db.db.execute("UPDATE WebControls SET state='Waiting',revision=revision+1,error=NULL,updated_at=? WHERE job_id=?",(utcnow(),ident));db.db.commit()
                db.event(ident,'WebRecoveryApproved',{'policy':'Completed artifacts reused; uncertain remote attempts resolved by existing Worker; no automatic dispatch'})
                return jsonify(revision=row['revision']+1)
        finally:db.close()

    def library():
        manifest=AI/'Library/manifest.json'
        if not manifest.exists():return []
        data=json.loads(manifest.read_text('utf-8'))
        return [{'id':k,'name':v['name_tr'],'score':v.get('score'),'triangles':v.get('triangles'),
            'status':v['status'],'technical':v.get('technical_status'),'provider':v.get('provider'),
            'preview':f'/files/library/{k}/preview_isometric.png','reason':v.get('reason',''),
            'files':[{'name':p.name,'url':f'/files/library/{k}/{p.name}'} for p in (AI/'Outputs/Library'/k).glob('*') if p.is_file()]}
            for k,v in data['assets'].items()]

    @app.get('/files/library/<ident>/<name>')
    def library_file(ident,name):
        root=AI/'Outputs/Library'
        allowed={x['id'] for x in library()}
        if ident not in allowed:abort(404)
        path=(root/ident/name).resolve()
        if path.parent!=(root/ident).resolve() or not path.is_file():abort(404)
        return send_file(path,as_attachment=path.suffix not in ('.png','.jpg','.json'))

    def review_subject(db,subject):
        from .web_reviews import web_subject,library_subject,decorate
        if subject.startswith('web:'):
            entry=web_subject(db,subject[4:])
        elif subject.startswith('library:'):
            item=next((item for item in library() if item['id']==subject[8:]),None)
            if not item:raise ValueError('Kütüphane varlığı bulunamadı.')
            entry=library_subject(item,AI/'Outputs/Library')
        else:raise ValueError('Geçersiz model kimliği.')
        return decorate(db,entry)

    def review_payload(entry,full=False):
        result={k:entry[k] for k in ('subject','id','name','asset_id','source','created_at','revision','review_version','review_status',
                'can_accept','can_decide','production_error','thumbnail','reference_url','triangles','dimensions','download_url','revision_production','acceptance_scope')}
        model=entry['model'];info=model['info']
        from urllib.parse import quote
        result['model']={'label':model['label'],'stage':model['stage'],'sha256':info['sha256'],'size':info['size'],
                         'url':'/api/model-reviews/'+quote(entry['subject'],safe='')+'/model'}
        result['technical_status']=(entry['technical'] or {}).get('status')
        result['production_state']=entry['control']['state'] if entry['control'] else None
        if full:
            result['technical']=entry['technical'];result['prompt']=entry['prompt']
            result['history']=[{k:r[k] for k in ('id','action','note','created_at')} | {'model_sha256':r['model'].get('info',{}).get('sha256')} for r in entry['history']]
            visual=None
            if entry['control'] and QUALITY_PATH.exists():
                import sqlite3
                q=sqlite3.connect(QUALITY_PATH.as_uri()+'?mode=ro',uri=True)
                try:
                    row=q.execute('SELECT report_json,error_json FROM QualityEvaluations WHERE job_id=? ORDER BY created_at DESC LIMIT 1',(entry['id'],)).fetchone()
                    if row:visual=json.loads(row[0]) if row[0] else {'error':json.loads(row[1]) if row[1] else None,'scores':None}
                finally:q.close()
            elif not entry['control']:
                path=Path(entry['model']['info']['path']).parent/'visual-validation.json'
                if path.is_file():visual=json.loads(path.read_text('utf-8'))
            result['visual']=visual
            result['files']=[]
            if entry['control']:
                for name in ('model.glb','model.fbx','model.obj','model.blend','albedo.png','roughness.png','metallic.png','lod1.fbx','validation.json'):
                    if name in entry['outputs'].get('Packaged',{}):result['files'].append({'name':name,'url':file_url(entry['id'],'Packaged',name)})
            else:
                item=next((x for x in library() if x['id']==entry['id']),{})
                result['files']=item.get('files',[])
        return result

    @app.get('/api/model-reviews')
    def model_reviews():
        db=connect()
        try:
            result=[]
            for job in reversed(db.jobs()):
                # Production data stays unchanged; reference-only jobs are absent.
                if not db.db.execute('SELECT 1 FROM WebControls WHERE job_id=?',(job['id'],)).fetchone():continue
                try:entry=review_subject(db,'web:'+job['id'])
                except ValueError:continue
                result.append(review_payload(entry))
            for item in library():
                try:entry=review_subject(db,'library:'+item['id'])
                except ValueError:continue
                result.append(review_payload(entry))
            return jsonify(csrf=session['csrf'],models=result,revision_paused=db.setting('web_revision_paused',False))
        finally:db.close()

    @app.get('/api/model-reviews/<subject>')
    def model_review_detail(subject):
        db=connect()
        try:return jsonify(csrf=session['csrf'],model=review_payload(review_subject(db,subject),full=True))
        finally:db.close()

    @app.get('/api/model-reviews/<subject>/model')
    def model_review_file(subject):
        from .common import intact
        db=connect()
        try:
            entry=review_subject(db,subject);info=entry['model']['info']
            if not intact(info):raise ValueError('Model dosyası eksik veya değişmiş; 3D görünüm yüklenmedi.')
            return send_file(info['path'],mimetype='model/gltf-binary',as_attachment=False,conditional=True)
        finally:db.close()

    @app.post('/api/model-reviews/<subject>')
    def model_review_decide(subject):
        from .web_reviews import decide
        db=connect()
        try:
            with mutex:
                entry=review_subject(db,subject)
                ident=decide(db,entry,request.get_json(silent=True))
                return jsonify(review_id=ident,model=review_payload(review_subject(db,subject),full=True))
        finally:db.close()

    @app.get('/files/job/<ident>/<stage>/<path:name>')
    def artifact_file(ident,stage,name):
        db=connect()
        try:
            control(db,ident);info=snapshot(db,ident).get(stage,{}).get(name)
            if not info:abort(404)
            path=Path(info['path']).resolve()
            if not path.is_relative_to(Path(db.job(ident)['output_dir']).resolve()) or not path.is_file():abort(404)
            # companions are only delivered through the existing real package; never arbitrary paths.
            return send_file(path,as_attachment=path.suffix not in ('.png','.jpg','.json'))
        finally:db.close()

    @app.get('/api/jobs/<ident>/download')
    def download(ident):
        import tempfile,zipfile
        from .common import intact
        db=connect()
        try:
            row=control(db,ident)
            if row['cursor']<2:raise ValueError('Teslim paketi ve kalite değerlendirmesi henüz tamamlanmadı.')
            outputs=snapshot(db,ident).get('Packaged',{})
            if not outputs:raise ValueError('Teslim paketi bulunamadı.')
            stream=tempfile.SpooledTemporaryFile(max_size=8*1024*1024,mode='w+b')
            try:
                with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_DEFLATED) as archive:
                    for name,info in outputs.items():
                        if not intact(info):raise ValueError('Teslim dosyasının bütünlüğü bozulmuş: '+name)
                        archive.write(info['path'],name)
                    if QUALITY_PATH.exists():
                        import sqlite3
                        q=sqlite3.connect(QUALITY_PATH.as_uri()+'?mode=ro',uri=True)
                        try:
                            r=q.execute('SELECT report_json FROM QualityEvaluations WHERE job_id=? ORDER BY created_at DESC LIMIT 1',(ident,)).fetchone()
                            if r and r[0]:archive.writestr('visual-validation.json',r[0])
                        finally:q.close()
                    approvals=[dict(r) for r in db.db.execute('SELECT * FROM WebApprovals WHERE job_id=? ORDER BY id',(ident,))]
                    archive.writestr('human-approvals.json',json.dumps(approvals,ensure_ascii=False,indent=2))
                stream.seek(0)
                response=send_file(stream,as_attachment=True,download_name=db.job(ident)['asset_id']+'_'+ident[:8]+'.zip',mimetype='application/zip')
                response.call_on_close(stream.close)
                return response
            except BaseException:stream.close();raise
        finally:db.close()

    return app

def main():
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr,'reconfigure'):sys.stderr.reconfigure(encoding='utf-8')
    import argparse
    from socketserver import ThreadingMixIn
    from wsgiref.simple_server import WSGIServer,WSGIRequestHandler,make_server
    from .common import ProcessLock
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8088);args=parser.parse_args()
    class Server(ThreadingMixIn,WSGIServer):
        daemon_threads=True
        allow_reuse_address=True
    with ProcessLock(AI/'State/web-server.lock'):
        app=create_app();db=connect();db.close()
        def monitor_queue():
            while True:
                try:app.extensions['studio_queue_tick']()
                except Exception as exc:print('Queue monitor:',str(exc),file=sys.stderr,flush=True)
                threading.Event().wait(1)
        threading.Thread(target=monitor_queue,daemon=True,name='3d-production-queue').start()
        print('Game3DCreator • LAN Studio',flush=True)
        for addr in local_addresses():print(f'http://{addr}:{args.port}',flush=True)
        print('Erişim kodu: AI3D/State/web-access-code.txt',flush=True)
        with make_server('0.0.0.0',args.port,app,server_class=Server,handler_class=WSGIRequestHandler) as server:server.serve_forever()

if __name__=='__main__':main()
