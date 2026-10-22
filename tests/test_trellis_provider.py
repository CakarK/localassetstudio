"""Backend dispatch/safety contracts, no models loaded or inference performed."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from ai3d_manager.common import ProductionError
from ai3d_manager import geometry_providers as backend
from ai3d_manager.worker import Worker, settings_for


class TrellisProviderTests(unittest.TestCase):
    def test_axes_conversion_roundtrip_and_orientation(self):
        import numpy as np
        from ai3d_manager.trellis_axes import native_to_gltf,gltf_to_native
        original=np.array([[1.,2.,3.],[0.,0.,1.],[0.,1.,0.]])
        converted=native_to_gltf(original)
        np.testing.assert_array_equal(converted,[[1,3,-2],[0,1,0],[0,0,-1]])
        np.testing.assert_array_equal(gltf_to_native(converted),original)
        np.testing.assert_array_equal(original,[[1,2,3],[0,0,1],[0,1,0]])

    def test_default_provider_is_trellis(self):
        self.assertEqual(backend.provider({}),'trellis2')

    def test_invalid_provider_rejected(self):
        with self.assertRaises(ValueError):backend.provider({'geometry_provider':'typo'})

    def test_unreal_open_blocks_without_killing_it(self):
        process=SimpleNamespace(pid=31332,info={'name':'UnrealEditor.exe'})
        with patch('psutil.process_iter',return_value=[process]):
            with self.assertRaises(ProductionError) as result:backend.ensure_unreal_closed()
        self.assertEqual(result.exception.category,'UnrealEditorOpen')

    def test_unreal_cmd_also_blocks(self):
        process=SimpleNamespace(pid=42,info={'name':'UnrealEditor-Cmd.exe'})
        with patch('psutil.process_iter',return_value=[process]):
            with self.assertRaises(ProductionError):backend.ensure_unreal_closed()

    def test_closed_editor_allows_preflight(self):
        with patch('psutil.process_iter',return_value=[]):backend.ensure_unreal_closed()

    def test_trellis_gpu_dispatch_does_not_submit_comfy_graph(self):
        worker=Worker(Mock(),allow_gpu=True)
        with patch.object(backend,'execute',return_value={'geometry':'output'}) as execute:
            result=worker.gpu_stage('GeometryGenerated',{'id':'attempt'},{'geometry_provider':'trellis2'})
        self.assertEqual(result,{'geometry':'output'})
        execute.assert_called_once()

    def test_pbr_is_a_separate_stage(self):
        worker=Worker(Mock(),allow_gpu=True)
        with patch.object(backend,'execute',return_value={'pbr':'output'}) as execute:
            worker.gpu_stage('PBRGenerated',{'id':'attempt'},{'geometry_provider':'trellis2'})
        self.assertEqual(execute.call_args.args[1],'PBRGenerated')

    def test_trellis_optimization_uses_native_backend(self):
        worker=Worker(Mock(),allow_gpu=True)
        with patch.object(backend,'execute',return_value={'optimized':'output'}) as execute:
            result=worker.execute_stage('GeometryOptimized',{'id':'a','job_id':'j','work_dir':'.'}, {},{'geometry_provider':'trellis2'})
        self.assertEqual(result,{'optimized':'output'})
        self.assertEqual(execute.call_args.args[1],'GeometryOptimized')

    def test_repeated_inference_is_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            work=Path(temp);(work/'inference-started.json').write_text('{}')
            worker=Mock(allow_gpu=True)
            worker.db.attempts.return_value=[{'stage_name':'GeometryGenerated','work_dir':temp}]
            with patch.object(backend,'availability',return_value=True),patch.object(backend,'ensure_unreal_closed'),\
                 patch('ai3d_manager.quality_vlm.check_vlm_reservation'),patch('ai3d_manager.processes.run_process') as run:
                with self.assertRaises(ProductionError) as result:
                    backend.execute(worker,'GeometryGenerated',{'work_dir':temp,'job_id':'j','id':'a'},{'comfy_url':'http://localhost'})
            self.assertTrue(result.exception.uncertain)
            run.assert_not_called()

    def test_missing_runtime_never_falls_back(self):
        with patch.object(backend,'availability',return_value=False):
            with self.assertRaises(ProductionError) as result:
                backend.execute(Mock(),'GeometryGenerated',{'work_dir':'unused'}, {})
        self.assertEqual(result.exception.category,'ProviderUnavailable')
