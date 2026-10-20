from .common import AI
from .quality_store import QualityStore


def add_parser(sub):
    p=sub.add_parser('image-models').add_subparsers(dest='action',required=True)
    p.add_parser('list');p.add_parser('verify')
    b=p.add_parser('benchmark');b.add_argument('--asset',required=True);b.add_argument('--profile',choices=['controlled','model-optimized'],default='controlled')
    p.add_parser('compare').add_argument('--asset',required=True)
    p.add_parser('recommend').add_argument('--category',required=True)
    p.add_parser('optimized')
    p.add_parser('hunyuan-test')


def execute(args,db):
    from .image_providers import load_registry,ImageGenerationProvider
    from .image_benchmark import ImageBenchmarkService,compare_references,recommend
    store=QualityStore(args.quality_db,args.quality_root)
    try:
        if args.action=='list':return load_registry()
        if args.action=='verify':
            import requests
            result=[]
            for ident,p in load_registry()['providers'].items():
                if p.get('research_only'):result.append({'provider_id':ident,'status':'BLOCKED','reason':'Research-only; model files not installed and no runnable workflow claimed'});continue
                try:
                    response=requests.get(p['comfy_url']+'/object_info',timeout=20);response.raise_for_status()
                    result.append(ImageGenerationProvider(ident).verify(response.json()))
                except Exception as e:result.append({'provider_id':ident,'status':'BLOCKED','error':str(e)})
            enabled=set(load_registry()['benchmark_providers'])
            available=all(r['status']=='PASS' for r in result if r['provider_id'] in enabled)
            return {'providers':result,'status':'PASS' if available else 'BLOCKED','note':'Overall PASS covers enabled providers only; research-only BLOCKED entries are not asserted runnable'}
        service=ImageBenchmarkService(db,store)
        if args.action=='benchmark':return service.run(args.asset,profile=args.profile)
        if args.action=='compare':return compare_references(db,store,args.asset)
        if args.action=='recommend':return recommend(db,store,args.category)
        if args.action=='optimized':return service.optimized()
        return service.shapes()
    finally:store.close()
