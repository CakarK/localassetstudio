from .common import AI
from .phase35_store import open_state


def add_parser(sub):
    c=sub.add_parser('categories').add_subparsers(dest='action',required=True)
    c.add_parser('list');c.add_parser('recommend').add_argument('--category',required=True)
    for name,actions in [('segmentation',['evaluate','compare']),('refinement',['analyze','plan']),('unreal',['pilot-import','validate'])]:
        p=sub.add_parser(name).add_subparsers(dest='action',required=True)
        for action in actions:
            a=p.add_parser(action);a.add_argument('job_id')
            if name=='segmentation' and action=='evaluate':a.add_argument('--strategy',action='append',choices=['existing-u2net','alpha-preservation','adaptive-border','adaptive-grabcut','local-sam-vit-b'])


def execute(args,source_db):
    if args.command=='categories':
        from .category_pipeline import load_profiles,recommend
        return load_profiles() if args.action=='list' else recommend(args.category)
    db,store=open_state()
    try:
        if args.command=='segmentation':
            from .segmentation import SegmentationService
            service=SegmentationService(source_db,db,store)
            return service.evaluate(args.job_id,args.strategy) if args.action=='evaluate' else service.compare(args.job_id)
        if args.command=='unreal':
            from .unreal_pilot import UnrealPilotService
            service=UnrealPilotService(source_db,store)
            return service.pilot_import(args.job_id) if args.action=='pilot-import' else service.validate(args.job_id)
        from .quality_store import QualityStore
        from .detail_refinement import AssetDetailRefinementPlanner
        quality=QualityStore(args.quality_db,args.quality_root)
        try:
            service=AssetDetailRefinementPlanner(source_db,quality,store)
            return service.analyze(args.job_id) if args.action=='analyze' else service.plan(args.job_id)
        finally:quality.close()
    finally:store.close();db.close()
