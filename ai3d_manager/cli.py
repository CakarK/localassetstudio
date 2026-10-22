import argparse
import json
from pathlib import Path
from .catalog import load_catalog, DEFAULT_SOURCE
from .common import ProductionError, intact
from .database import Database
from .worker import Worker, settings_for, reuse_snapshot


def parser():
    p=argparse.ArgumentParser(description='Local Asset Studio asset production manager')
    p.add_argument('--db');p.add_argument('--output-root');p.add_argument('--catalog',default=None);p.add_argument('--overrides')
    p.add_argument('--quality-db');p.add_argument('--quality-root')
    sub=p.add_subparsers(dest='command',required=True)
    catalog=sub.add_parser('catalog').add_subparsers(dest='action',required=True)
    catalog.add_parser('list');catalog.add_parser('show').add_argument('asset_id')
    generate=sub.add_parser('generate');generate.add_argument('--asset',required=True);generate.add_argument('--variant',default='001')
    generate.add_argument('--settings',help='JSON settings file');generate.add_argument('--request',help='Explicit asset request JSON overriding catalog fields')
    generate.add_argument('--from-existing',help='Copy existing real artifacts; no new inference for those stages')
    generate.add_argument('--enqueue-only',action='store_true');generate.add_argument('--new-version',action='store_true')
    batch=sub.add_parser('batch');filters=batch.add_mutually_exclusive_group(required=True)
    filters.add_argument('--all',action='store_true');filters.add_argument('--period');filters.add_argument('--category')
    batch.add_argument('--variant',default='001');batch.add_argument('--settings')
    jobs=sub.add_parser('jobs').add_subparsers(dest='action',required=True);jobs.add_parser('list')
    for name in ('status','history','cancel','approve','retry','resume','resolve'):
        item=jobs.add_parser(name);item.add_argument('job_id')
        if name in ('retry','resume'):item.add_argument('--settings');item.add_argument('--run',action='store_true')
        if name=='resolve':item.add_argument('--abandoned',action='store_true',required=True);item.add_argument('--attempt',required=True)
    queue=sub.add_parser('queue').add_subparsers(dest='action',required=True)
    queue.add_parser('pause');queue.add_parser('resume');run=queue.add_parser('run');run.add_argument('--limit',type=int)
    from .quality_cli import add_parser
    add_parser(sub)
    from .reference_cli import add_parser as add_reference_parser
    add_reference_parser(sub)
    from .image_models_cli import add_parser as add_image_parser
    add_image_parser(sub)
    from .phase35_cli import add_parser as add_phase35_parser
    add_phase35_parser(sub)
    return p


def read_settings(path):return json.loads(Path(path).read_text('utf-8-sig')) if path else {}
def emit(value):print(json.dumps(value,ensure_ascii=False,indent=2))
def compact(job):return {k:job[k] for k in ('id','asset_id','variant','version','status','output_dir','error_json')}


def main(argv=None):
    import sys
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    args=parser().parse_args(argv);db=None
    try:
        db=Database(args.db,args.output_root)
        if args.command in ('categories','segmentation','refinement','unreal'):
            from .phase35_cli import execute as execute_phase35
            result=execute_phase35(args,db);emit(result)
            return 2 if result.get('status') in ('BLOCKED','FAIL','WARN','NeedsReview') else 0
        if args.command=='image-models':
            if args.action in ('benchmark','optimized'):
                known={a['asset_id'] for a in db.assets()}
                missing=[a for a in load_catalog(args.catalog,read_settings(args.overrides)) if a['asset_id'] not in known]
                if missing:db.sync_assets(missing)
            from .image_models_cli import execute as execute_images
            result=execute_images(args,db);emit(result)
            return 2 if result.get('status') in ('BLOCKED','FAIL','NeedsReview') else 0
        if args.command=='reference':
            if args.action=='generate':db.sync_assets(load_catalog(args.catalog,read_settings(args.overrides)))
            from .reference_cli import execute as execute_reference
            result=execute_reference(args,db);emit(result)
            return 2 if result.get('status') in ('FAIL','NeedsReview') else 0
        if args.command=='quality':
            from .quality_cli import execute
            result=execute(args,db);emit(result)
            blocked=args.action in ('reference-check','geometry-check','full-check','controlled-generate','reference-fidelity','model-comparison') and result.get('status') in ('FAIL','Failed','NeedsReview','Cancelled','BLOCKED')
            return 2 if result.get('error') or blocked else 0
        if args.command in ('catalog','generate','batch'):db.sync_assets(load_catalog(args.catalog,read_settings(args.overrides)))
        if args.command=='catalog':
            emit(db.asset(args.asset_id) if args.action=='show' else db.assets());return 0
        if args.command=='generate':
            asset=db.asset(args.asset)
            if args.request:
                request=read_settings(args.request)
                if request.get('asset_id',args.asset)!=args.asset:raise ValueError('Request asset_id must match --asset')
                asset.update(request);asset['request_override_source']=str(Path(args.request).resolve())
            from .category_pipeline import apply_profile
            asset,overrides=apply_profile(asset,read_settings(args.settings))
            settings=settings_for(asset,overrides)
            if args.from_existing:settings['reuse_files']=reuse_snapshot(args.from_existing)
            job,created=db.enqueue(asset,args.variant,settings,args.new_version)
            # A duplicate completed request is a read-only result lookup. Explicit
            # resume audits code fingerprints; changed software must not silently
            # start another inference just because generate was repeated.
            completed_intact=job['status']=='Completed' and all(
                intact(info) for stage in db.stages(job['id']) if stage['status']=='Completed'
                for info in db.artifact_dict(stage['id']).values())
            if not args.enqueue_only and not completed_intact:job=Worker(db,allow_gpu=True).run(job['id'])
            emit({**compact(job),'created':created});return 0 if job['status'] in ('Completed','Pending','Paused') else 2
        if args.command=='batch':
            selected=[a for a in db.assets() if args.all or (args.period and a['period']==args.period) or (args.category and a['category']==args.category)]
            if not selected:raise ValueError('No catalog entries match the filter')
            overrides=read_settings(args.settings);items=[]
            from .category_pipeline import apply_profile
            for asset in selected:
                asset,opts=apply_profile(asset,overrides)
                job,created=db.enqueue(asset,args.variant,settings_for(asset,opts));items.append({**compact(job),'created':created})
            emit({'mode':'enqueue_only','count':len(items),'jobs':items});return 0
        if args.command=='jobs':
            if args.action=='list':emit([compact(j) for j in db.jobs()]);return 0
            ident=args.job_id;job=db.job(ident)
            if args.action=='cancel':db.cancel(ident)
            elif args.action=='approve':db.resume(ident,{'reference_approved':True})
            elif args.action in ('retry','resume'):
                changed=read_settings(args.settings)
                settings_for(json.loads(job['request_json']),changed)
                db.resume(ident,changed)
                if args.run:Worker(db,allow_gpu=True).run(ident)
            elif args.action=='resolve':
                attempt=next((a for a in db.attempts(ident) if a['id']==args.attempt),None)
                if not attempt or attempt['state'] not in ('NeedsReview','Submitting','Submitted','SubmissionUnknown'):raise ValueError('No unresolved attempt with this ID')
                # An explicit operator assertion is required; no automatic inference resubmission.
                from .comfy import ComfyClient
                client=ComfyClient(json.loads(job['settings_json'])['comfy_url'])
                location,pid,history=client.locate(attempt)
                if location!='missing':raise ValueError('Matching ComfyUI entry still exists; resume for recovery, do not abandon')
                db.fail_stage(attempt,{'category':'OperatorAbandoned','message':'Operator explicitly confirmed missing remote attempt abandoned.'})
                db.event(ident,'OperatorAbandoned',{'attempt':attempt['id']});db.resume(ident)
            if args.action in ('history','status'):
                emit({'job':db.job(ident),'stages':db.stages(ident),'attempts':db.attempts(ident)})
            else:emit(compact(db.job(ident)))
            return 2 if db.job(ident)['status'] in ('Failed','NeedsReview') else 0
        if args.command=='queue':
            if args.action in ('pause','resume'):
                db.set_setting('queue_paused',args.action=='pause');emit({'queue_paused':db.setting('queue_paused')});return 0
            count=0;failed=0
            while not db.setting('queue_paused') and (args.limit is None or count<args.limit):
                job=db.next_job()
                if not job:break
                result=Worker(db,allow_gpu=True).run(job['id']);count+=1;failed+=result['status'] in ('Failed','NeedsReview')
                if result['status']=='Paused':break
            emit({'processed':count,'failed_or_review':failed,'queue_paused':db.setting('queue_paused')});return 2 if failed else 0
    except (ValueError,OSError,ProductionError) as exc:
        emit({'error':str(exc),'category':getattr(exc,'category',type(exc).__name__)});return 2
    finally:
        if db:db.close()
