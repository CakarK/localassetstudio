"""Public distribution accepts user content without assuming a game or a catalog."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from ai3d_manager.catalog import DEFAULT_SOURCE, load_catalog
from ai3d_manager.web_assets import asset_from_request
from ai3d_manager.web_policy import reference_prompt
from ai3d_manager.reference_prompts import compile_prompt
from ai3d_manager.quality_requirements import requirements
from ai3d_manager.prompt_refinement import compile_refinement
from tests.runtime import TEST_ROOT


class PublicCatalogTests(unittest.TestCase):
    def test_public_template_has_no_asset_prompts_or_style(self):
        template=json.loads(DEFAULT_SOURCE.read_text('utf-8'))
        self.assertEqual(load_catalog(DEFAULT_SOURCE),[])
        self.assertEqual(template['shared_style'],'')
        self.assertEqual(template['shared_negative'],'')
        self.assertIsNone(template['grid_cell_m'])

    def test_custom_asset_works_with_empty_catalog_and_no_inherited_style(self):
        with patch.dict('os.environ',{'LAS_CATALOG_PATH':str(DEFAULT_SOURCE)}):
            asset=asset_from_request({'source_mode':'custom','name':'Test vase',
                'prompt':'A complete glazed ceramic vase.','prompt_language':'en'})
        self.assertEqual(asset['shared_style'],'')
        self.assertEqual(asset['shared_negative'],'')
        self.assertIsNone(asset['period'])
        self.assertIsNone(asset['grid'])
        self.assertEqual(asset['footprint_m'],[1,1])
        self.assertEqual(asset['source'],{'kind':'web_custom_prompt'})

    def test_custom_style_reaches_reference_without_replacement(self):
        asset=asset_from_request({'source_mode':'custom','name':'Test vase',
            'prompt':'A complete glazed ceramic vase.','prompt_language':'en',
            'shared_style':'Polished porcelain, bright blue palette.',
            'shared_negative':'gold trim','period':'user-defined label'})
        result=reference_prompt({'translated':asset['prompt']},asset,{})
        self.assertIn(asset['shared_style'],result['optimized'])
        self.assertEqual(asset['period'],'user-defined label')
        self.assertEqual(compile_prompt(asset)['shared_style'],asset['shared_style'])
        for rule in requirements(asset)['requirements']:
            source=rule['source']
            self.assertEqual(asset[source['field']][source['start']:source['end']],source['quote'])

    def test_custom_styles_reject_invalid_types(self):
        base={'source_mode':'custom','name':'Test vase','prompt':'A complete glazed ceramic vase.'}
        for value in [{'shared_style':[]},{'shared_negative':{}},{'shared_style':'x'*2001}]:
            with self.assertRaises(ValueError):asset_from_request({**base,**value})

    def test_grid_uses_only_explicit_user_cell_size(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            path=Path(directory)/'catalog.json'
            value={'assets':[{'asset_id':'UserAsset','prompt':'An isolated ceramic vase.',
                'grid':[2,3],'polygon_target':3600}],'grid_cell_m':2.5}
            path.write_text(json.dumps(value),'utf-8')
            asset=load_catalog(path)[0]
            self.assertEqual(asset['footprint_m'],[5,7.5])
            self.assertEqual(asset['polygon_target'],3600)
            self.assertEqual(load_catalog(path,{'UserAsset':{'grid':[3,1]}})[0]['footprint_m'],[7.5,2.5])
            value['grid_cell_m']=None;path.write_text(json.dumps(value),'utf-8')
            with self.assertRaises(ValueError):load_catalog(path)

    def test_refinement_preserves_arbitrary_user_prompt_and_style(self):
        asset=asset_from_request({'source_mode':'custom','name':'Test vase',
            'prompt':'A complete ceramic vase with green glaze.','shared_style':'Studio photograph.',
            'shared_negative':'red glaze'})
        result=compile_refinement(asset,{'job_id':'fixture','evaluation_id':'fixture','findings':[]})
        self.assertEqual(result['refined_prompt'],asset['prompt'])
        self.assertEqual(result['style'],asset['shared_style'])
        self.assertEqual(result['negative'],asset['shared_negative'])
        self.assertFalse(result['inference_started'])

    def test_duplicate_catalog_ids_and_identity_overrides_rejected(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            path=Path(directory)/'catalog.json'
            entry={'asset_id':'UserAsset','prompt':'An isolated ceramic vase.'}
            path.write_text(json.dumps({'assets':[entry,entry]}),'utf-8')
            with self.assertRaises(ValueError):load_catalog(path)
            path.write_text(json.dumps({'assets':[entry]}),'utf-8')
            with self.assertRaises(ValueError):load_catalog(path,{'UserAsset':{'asset_id':'Changed'}})


if __name__=='__main__':unittest.main()
