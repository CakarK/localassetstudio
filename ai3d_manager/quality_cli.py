import json
from .common import AI, ProductionError
from .quality_store import QualityStore

def add_parser(sub):
    q=sub.add_parser('quality').add_subparsers(dest='action',required=True)
    evaluate=q.add_parser('evaluate');evaluate.add_argument('job_id');evaluate.add_argument('--config');evaluate.add_argument('--reuse-renders')
    for action in ('report','approve','reject','suggest-fixes'):
        p=q.add_parser(action);p.add_argument('job_id')
        if action in ('approve','reject'):p.add_argument('--reason',required=True)
    q.add_parser('compare').add_argument('asset_id')
    q.add_parser('controlled-generate').add_argument('--asset',required=True)
    q.add_parser('reference-fidelity').add_argument('job_id')
    q.add_parser('model-comparison').add_argument('--asset',required=True)
    for action in ('reference-check','geometry-check','full-check','gates-report'):
        p=q.add_parser(action);p.add_argument('job_id')
        if action!='gates-report':p.add_argument('--refresh',action='store_true')
    bench=q.add_parser('benchmark');bench.add_argument('--asset',required=True);bench.add_argument('--model')
    q.add_parser('refine').add_argument('job_id')
    improve=q.add_parser('improve');improve.add_argument('job_id');improve.add_argument('--candidates',type=int,default=2)
    improve.add_argument('--gpu-budget-seconds',type=float,default=900);improve.add_argument('--disk-budget-mb',type=float,default=2048)
    improve.add_argument('--prepare-only',action='store_true')
    q.add_parser('gpu-status')
    q.add_parser('gpu-resolve').add_argument('--reason',required=True)
    retry=q.add_parser('retry');retry.add_argument('job_id');retry.add_argument('--plan');retry.add_argument('--action-id')
    retry.add_argument('--enable-inference',action='store_true');retry.add_argument('--run',action='store_true')
    retry.add_argument('--max-candidates',type=int,default=3);retry.add_argument('--gpu-budget-seconds',type=float,default=600)
    retry.add_argument('--disk-budget-mb',type=float,default=2048)

def execute(args,db):
    from .quality_service import QualityService
    from .quality_planner import compare_candidates,save_plan,enqueue_candidates,run_candidates
    store=QualityStore(args.quality_db,args.quality_root)
    try:
        if args.action=='reference-fidelity':
            from .reference_fidelity import ReferenceFidelityEvaluator
            result=ReferenceFidelityEvaluator(db,store).evaluate(args.job_id)
        elif args.action=='model-comparison':
            from .image_benchmark import model_comparison
            result=model_comparison(db,store,args.asset)
        elif args.action=='controlled-generate':
            from .controlled_generation import ControlledService
            result=ControlledService(db,store).run(args.asset)
        elif args.action in ('reference-check','geometry-check','full-check','gates-report'):
            from .quality_gates import QualityGateService,gate_summary
            service=QualityGateService(db,store)
            if args.action=='gates-report':result={'job_id':args.job_id,**gate_summary(db,store,args.job_id),'reports':{kind:store.gate_report(args.job_id,kind) for kind in ('reference','geometry')},'full_check':store.gate_report(args.job_id,'full')}
            elif args.action=='full-check':result=service.full_check(args.job_id,args.refresh)
            else:result=service.run('reference' if args.action=='reference-check' else 'geometry',args.job_id,refresh=args.refresh)
        elif args.action=='refine':
            from .prompt_refinement import refine
            result=refine(db,store,args.job_id)
        elif args.action=='improve':
            from .quality_improve import improve
            result=improve(db,store,args.job_id,args.candidates,args.gpu_budget_seconds,args.disk_budget_mb,args.prepare_only)
        elif args.action=='benchmark':
            from .quality_benchmark import benchmark
            from .quality_config import load_quality
            result=benchmark(db,store,args.asset,args.model or load_quality()['model'])
        elif args.action in ('gpu-status','gpu-resolve'):
            from .quality_vlm import RESERVATION,resolve_reservation
            if args.action=='gpu-resolve':result=resolve_reservation(args.reason)
            else:result=json.loads(RESERVATION.read_text('utf-8')) if RESERVATION.exists() else {'reservation':'none'}
        elif args.action=='evaluate':
            config=json.loads(__import__('pathlib').Path(args.config).read_text('utf-8-sig')) if args.config else {}
            result=QualityService(db,store).evaluate(args.job_id,config,args.reuse_renders)
        elif args.action=='report':result={**store.report(args.job_id),'manual_actions':store.actions(args.job_id)}
        elif args.action=='compare':result=compare_candidates(db,store,args.asset_id)
        elif args.action in ('approve','reject'):
            report=store.report(args.job_id)
            if args.action=='approve':
                from .quality_gates import gate_summary
                if gate_summary(db,store,args.job_id)['status']!='PASS':raise ProductionError('QualityApprovalBlocked','Current reference and identity gates must PASS; a high VLM score cannot override a gate')
            if args.action=='approve' and (report.get('technical_status')!='TechnicalPassed' or report.get('scores') is None):
                raise ProductionError('QualityApprovalBlocked','Technical FAIL or incomplete visual evidence cannot receive manual visual approval')
            result=store.action(args.job_id,'Approved' if args.action=='approve' else 'Rejected',args.reason)
            result['manual_visual_status']='VisualApproved' if args.action=='approve' else 'VisualRejected'
            result['production_job_status']=db.job(args.job_id)['status']
        elif args.action=='suggest-fixes':result=save_plan(store,store.report(args.job_id))
        else:
            if args.plan:
                row=store.db.execute('SELECT plan_json FROM QualityPlans WHERE id=?',(args.plan,)).fetchone()
                if not row:raise ValueError('Unknown correction plan')
                plan=json.loads(row[0])
                if plan['job_id']!=args.job_id:raise ValueError('Plan belongs to another job')
            else:plan=save_plan(store,store.report(args.job_id))
            result=enqueue_candidates(db,store,plan,{'enabled':args.enable_inference,'max_candidates':args.max_candidates,
                     'max_gpu_seconds':args.gpu_budget_seconds,'max_disk_mb':args.disk_budget_mb},args.action_id)
            if args.run:
                result['execution_results']=run_candidates(db,store,result)
                result['inference_started']=bool(result['execution_results'])
                # The same shadow evaluator is applied to every completed candidate.
                result['evaluations']=[QualityService(db,store).evaluate(j['id']) for j in result['execution_results'] if j['status']=='Completed']
        return result
    finally:store.close()
