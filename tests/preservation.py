"""Keep historical row equality strict while admitting only declared Phase3.3 jobs."""
import json
from ai3d_manager.quality_store import QualityStore

def historical_rows(db,table,old):
    rows=[dict(r) for r in db.db.execute('SELECT * FROM '+table)]
    keys=lambda r:tuple(r[k] for k in ('asset_id','variant')) if table=='AssetVariants' else r.get('id',r.get('key'))
    old_keys={keys(r) for r in old};added=[r for r in rows if keys(r) not in old_keys]
    if added:
        store=QualityStore()
        try:
            allowed={r[0] for r in store.db.execute('SELECT job_id FROM ReferenceCandidates')}|{r[0] for r in store.db.execute('SELECT job_id FROM ControlledGenerations WHERE job_id IS NOT NULL')}
            allowed|={r[0] for r in store.db.execute('SELECT job_id FROM ImageReferences WHERE job_id IS NOT NULL')}|{r[0] for r in store.db.execute('SELECT job_id FROM ImageShapeRuns')}
            allowed_variants={(r['asset_id'],r['variant']) for r in db.db.execute('SELECT id,asset_id,variant FROM GenerationJobs') if r['id'] in allowed}
            stage_ids={r['id'] for r in db.db.execute('SELECT id,job_id FROM JobStages') if r['job_id'] in allowed}
            for row in added:
                if table=='GenerationJobs':assert row['id'] in allowed and (json.loads(row['request_json']).get('phase33_set_id') or json.loads(row['request_json']).get('phase34_benchmark_id'))
                elif table in ('JobStages','JobAttempts','JobEvents'):assert row['job_id'] in allowed
                elif table=='JobArtifacts':assert row['stage_id'] in stage_ids
                elif table=='AssetVariants':assert (row['asset_id'],row['variant']) in allowed_variants
                else:raise AssertionError('Unexpected additional historical table row: '+table)
        finally:store.close()
    return [r for r in rows if keys(r) in old_keys]
