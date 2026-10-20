"""No inference: distinguish an asynchronous unload from persistent residency."""
import unittest
from unittest.mock import Mock,patch
from ai3d_manager import image_providers as providers
from ai3d_manager.common import ProductionError

def response(value):
    result=Mock();result.json.return_value=value;return result

class GPUHandoffTests(unittest.TestCase):
    def test_translation_unload_handoff_waits_without_unloading(self):
        models={'models':[{'name':'qwen3:4b','size_vram':0}]}
        with patch.object(providers,'check_vlm_reservation'),patch.object(providers.requests,'get',side_effect=[response(models),response(models),response({'models':[]})]) as get,patch.object(providers.requests,'post') as post,patch.object(providers.time,'sleep') as sleep:
            providers.wait_for_ollama_idle(polls=4,interval=.5)
        self.assertEqual(get.call_count,3);self.assertEqual(sleep.call_count,2);post.assert_not_called()

    def test_foreign_resident_model_remains_protected(self):
        with patch.object(providers,'check_vlm_reservation'),patch.object(providers.requests,'get',return_value=response({'models':[{'name':'other-active-model','size_vram':18000000000}]})) as get,patch.object(providers.requests,'post') as post,patch.object(providers.time,'sleep'):
            with self.assertRaises(ProductionError) as caught:providers.wait_for_ollama_idle(polls=3,interval=.5)
        self.assertEqual(caught.exception.category,'GPUBusy');self.assertTrue(caught.exception.transient)
        self.assertIn('other-active-model',str(caught.exception));self.assertEqual(get.call_count,3);post.assert_not_called()

    def test_reservation_appearing_during_wait_still_blocks(self):
        with patch.object(providers,'check_vlm_reservation',side_effect=[None,ProductionError('VLMRecoveryRequired','reserved')]),patch.object(providers.requests,'get',return_value=response({'models':[{'name':'qwen3:4b'}]})),patch.object(providers.requests,'post') as post,patch.object(providers.time,'sleep'):
            with self.assertRaises(ProductionError) as caught:providers.wait_for_ollama_idle(polls=4)
        self.assertEqual(caught.exception.category,'VLMRecoveryRequired');post.assert_not_called()

    def test_gpu_preflight_checks_vram_and_both_queues_after_handoff(self):
        replies=[response({'models':[{'name':'qwen3:4b','size_vram':0}]}),response({'models':[]}),response({'queue_running':[],'queue_pending':[]}),response({'queue_running':[],'queue_pending':[]})]
        with patch.object(providers,'check_vlm_reservation'),patch.object(providers.requests,'get',side_effect=replies) as get,patch.object(providers.requests,'post',return_value=response({})) as post,patch.object(providers.time,'sleep'),patch.object(providers,'gpu_sample',return_value={'free_mib':20000}):
            sample=providers.gpu_preflight('http://127.0.0.1:8191',12000)
        self.assertEqual(sample['free_mib'],20000)
        self.assertEqual({c.args[0] for c in get.call_args_list if c.args[0].endswith('/queue')},{'http://127.0.0.1:8190/queue','http://127.0.0.1:8191/queue'})
        self.assertEqual({c.args[0] for c in post.call_args_list},{'http://127.0.0.1:8190/free','http://127.0.0.1:8191/free'})

    def test_busy_ollama_never_triggers_comfy_cleanup(self):
        with patch.object(providers,'wait_for_ollama_idle',side_effect=ProductionError('GPUBusy','busy')),patch.object(providers.requests,'post') as post,patch.object(providers,'gpu_sample') as sample:
            with self.assertRaises(ProductionError):providers.gpu_preflight('http://127.0.0.1:8191')
        post.assert_not_called();sample.assert_not_called()

if __name__=='__main__':unittest.main()
