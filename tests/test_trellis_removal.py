import unittest
from ai3d_manager.common import ProductionError
from ai3d_manager.geometry_providers import PROVIDERS,provider
from ai3d_manager.worker import settings_for
class TrellisOnlyTests(unittest.TestCase):
    def test_new_settings_are_trellis(self):
        settings=settings_for({'polygon_target':1000,'texture_size':1024})
        self.assertEqual(settings['geometry_provider'],'trellis2')
        self.assertEqual(PROVIDERS,('trellis2',))
    def test_removed_provider_is_never_silently_migrated(self):
        with self.assertRaises(ProductionError) as error:provider({'geometry_provider':'hunyuan3d-2.1'})
        self.assertEqual(error.exception.category,'ProviderRemoved')
