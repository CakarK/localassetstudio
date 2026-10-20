"""Immutable source/parser cache; reuse only exact, source-validated criteria."""
import json
from pathlib import Path
from .common import AI,atomic_json,digest,file_info
from .quality_requirements import requirements,VERSION

class CriteriaCache:
    def __init__(self,root=None):self.root=Path(root or AI/'State/criteria-cache');self.root.mkdir(parents=True,exist_ok=True)
    def key(self,asset):
        return digest({'source':requirements(asset)['source_sha256'],'parser_version':VERSION,
                       'parser_sha256':file_info(Path(__file__).with_name('quality_requirements.py'))['sha256']})
    def validate(self,asset,interpretation):
        if interpretation['version']!=VERSION or interpretation['source_sha256']!=requirements(asset)['source_sha256']:raise ValueError('Criteria source/parser changed')
        if interpretation['interpretation_sha256']!=digest(interpretation['requirements']):raise ValueError('Criteria content hash mismatch')
        for r in interpretation['requirements']:
            s=r['source'];text=asset[s['field']]
            if text[s['start']:s['end']]!=s['quote']:raise ValueError('Criteria source quote mismatch')
        return interpretation
    def get(self,asset):
        path=self.root/(self.key(asset)+'.json')
        if not path.exists():return None
        return self.validate(asset,json.loads(path.read_text('utf-8'))['interpretation'])
    def put(self,asset,interpretation,origin):
        self.validate(asset,interpretation);path=self.root/(self.key(asset)+'.json')
        if not path.exists():atomic_json(path,{'cache_key':self.key(asset),'interpretation':interpretation,'origin':origin})
        return interpretation
    def find_existing(self,asset,store):
        cached=self.get(asset)
        if cached:return cached,'cache_hit'
        for row in reversed(store.evaluations(asset['asset_id'])):
            path=Path(row['work_dir'])/'requirements.json'
            if not path.exists():continue
            try:value=self.validate(asset,json.loads(path.read_text('utf-8')))
            except (ValueError,KeyError):continue
            return self.put(asset,value,{'evaluation_id':row['id'],'source_file':file_info(path)}),'imported_existing_evaluation'
        return None,'miss'
