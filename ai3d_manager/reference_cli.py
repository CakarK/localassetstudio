from .quality_store import QualityStore
from .reference_candidates import ReferenceService,comparison,select_reference

def add_parser(sub):
    reference=sub.add_parser('reference').add_subparsers(dest='action',required=True)
    for action in ('generate','evaluate','compare','select'):
        p=reference.add_parser(action);p.add_argument('--asset',required=True)
        if action=='generate':p.add_argument('--count',type=int,default=3)

def execute(args,db):
    store=QualityStore(args.quality_db,args.quality_root)
    try:
        if args.action=='generate':return ReferenceService(db,store).generate(args.asset,args.count)
        if args.action=='evaluate':return ReferenceService(db,store).evaluate(args.asset)
        if args.action=='compare':return comparison(db,store,args.asset)
        return select_reference(db,store,args.asset)
    finally:store.close()
