"""Release integrity and removal contracts; no inference or downloads."""
import hashlib
import json
import unittest
import zipfile
import importlib.util
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
RUNTIME=ROOT/'AI3D/TrellisRuntime'

class TrellisReleaseTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','Windows double-click contracts')
    def test_install_bat_executes_local_setup_and_propagates_failure(self):
        with tempfile.TemporaryDirectory(prefix='LAS click ') as temp:
            directory=Path(temp);bat=directory/'Install-LocalAssetStudio.bat'
            shutil.copy2(ROOT/bat.name,bat)
            script=directory/'Install-LocalAssetStudio.ps1'
            script.write_text("Set-Content -LiteralPath (Join-Path $PSScriptRoot 'installed.marker') -Value 'PASS'\nexit 0\n",encoding='utf-8')
            run=subprocess.run(['cmd.exe','/d','/c',str(bat)],capture_output=True,text=True,timeout=30,errors='replace')
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            self.assertTrue((directory/'installed.marker').is_file())
            script.write_text('exit 7\n',encoding='utf-8')
            run=subprocess.run(['cmd.exe','/d','/c',str(bat)],input='\n',capture_output=True,text=True,timeout=30,errors='replace')
            self.assertEqual(run.returncode,1,run.stdout+run.stderr)

    @unittest.skipUnless(os.name=='nt','Windows double-click contracts')
    def test_start_bat_installs_when_missing_then_uses_installed_start(self):
        with tempfile.TemporaryDirectory(prefix='LAS click ') as temp:
            directory=Path(temp);bat=directory/'Start-LocalAssetStudio.bat';shutil.copy2(ROOT/bat.name,bat)
            (directory/'Install-LocalAssetStudio.bat').write_text('@echo off\necho PASS>"%~dp0install.marker"\nexit /b 0\n',encoding='ascii')
            run=subprocess.run(['cmd.exe','/d','/c',str(bat)],capture_output=True,text=True,timeout=30,errors='replace')
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            self.assertTrue((directory/'install.marker').exists())
            state=directory/'AI3D/State';state.mkdir(parents=True);(state/'install-verification.json').write_text('{}')
            (directory/'Start-LocalAssetStudio.ps1').write_text("Set-Content -LiteralPath (Join-Path $PSScriptRoot 'start.marker') -Value 'PASS'\n",encoding='utf-8')
            run=subprocess.run(['cmd.exe','/d','/c',str(bat)],capture_output=True,text=True,timeout=30,errors='replace')
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            self.assertTrue((directory/'start.marker').exists())

    @unittest.skipUnless(os.name=='nt','Windows bootstrap migration contract')
    def test_bootstrap_never_dispatches_an_old_hunyuan_installer(self):
        with tempfile.TemporaryDirectory(prefix='LAS bootstrap ') as temp:
            old=Path(temp)/'studio';old.mkdir();(old/'VERSION').write_text('0.2.0')
            (old/'user-asset.fixture').write_bytes(b'preserve')
            (old/'Install-LocalAssetStudio.ps1').write_text('throw "Old installer must not run"')
            new=Path(str(old)+'-v0.3.0');new.mkdir();(new/'VERSION').write_text('0.3.0')
            (new/'Install-LocalAssetStudio.ps1').write_text("Set-Content -LiteralPath (Join-Path $PSScriptRoot 'new.marker') -Value 'PASS'\n",encoding='utf-8')
            run=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(ROOT/'install.ps1'),'-InstallDirectory',str(old),'-NoStart','-SourceRef','v0.3.0'],capture_output=True,text=True,timeout=30,errors='replace')
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            self.assertTrue((new/'new.marker').exists())
            self.assertEqual((old/'user-asset.fixture').read_bytes(),b'preserve')

    def access_helper(self):
        spec=importlib.util.spec_from_file_location('las_access',RUNTIME/'check_access.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module

    def test_approved_access_does_not_prompt_or_download(self):
        calls=[]
        def unexpected(*args):raise AssertionError('Unexpected login/browser')
        value=self.access_helper().require_access('https://example.invalid/model',lambda:'fixture-token',unexpected,
            lambda url,**kwargs:calls.append((url,kwargs['token'])),unexpected)
        self.assertEqual(calls,[('https://example.invalid/model','fixture-token')])
        self.assertTrue(value['gated_access_verified']);self.assertFalse(value['model_downloaded'])

    def test_pending_gated_access_stops_instead_of_bypassing(self):
        def denied(*args,**kwargs):raise RuntimeError('fixture access denied')
        with self.assertRaisesRegex(RuntimeError,'access denied'):
            self.access_helper().require_access('https://example.invalid/model',lambda:'fixture-token',denied,denied,denied)

    def test_missing_account_opens_terms_then_logs_in_locally(self):
        tokens=iter([None,'fixture-token']);events=[]
        self.access_helper().require_access('https://example.invalid/model',lambda:next(tokens),lambda:events.append('login'),
            lambda *args,**kwargs:events.append('metadata'),lambda page:events.append('terms'))
        self.assertEqual(events,['terms','login','metadata'])

    def test_double_click_launchers_are_present_and_bootstrap_is_versioned(self):
        installer=(ROOT/'Install-LocalAssetStudio.bat').read_text('utf-8')
        self.assertIn('v0.3.0/install.ps1',installer)
        self.assertIn('Install-LocalAssetStudio.ps1',installer)
        self.assertTrue((ROOT/'Start-LocalAssetStudio.bat').is_file())
        script=(ROOT/'Install-LocalAssetStudio.ps1').read_text('utf-8')
        self.assertLess(script.index('check_access.py'),script.index('[2/6]'))

    def test_native_wheels_are_pinned_and_have_compiled_modules(self):
        manifest=json.loads((RUNTIME/'wheels/manifest.json').read_text('utf-8'))
        self.assertEqual(len(manifest['files']),3)
        for spec in manifest['files']:
            path=RUNTIME/'wheels'/spec['file']
            self.assertEqual(path.stat().st_size,spec['size'])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),spec['sha256'])
            with zipfile.ZipFile(path) as archive:
                self.assertTrue(any(n.endswith('.pyd') for n in archive.namelist()))
                self.assertTrue(any(n.endswith('.dist-info/RECORD') for n in archive.namelist()))

    def test_corresponding_sources_include_native_and_eigen_licenses(self):
        archive=RUNTIME/'native-sources.zip'
        digest=(RUNTIME/'native-sources.sha256').read_text('ascii').split()[0]
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(),digest)
        with zipfile.ZipFile(archive) as z:
            names=z.namelist()
            self.assertIn('CuMesh/LICENSE',names)
            self.assertIn('FlexGEMM/LICENSE',names)
            self.assertTrue(any('eigen/COPYING.MPL2' in n for n in names))

    def test_installer_never_downloads_removed_geometry_models(self):
        lock=json.loads((ROOT/'AI3D/install.lock.json').read_text('utf-8'))
        self.assertEqual(lock['geometry_provider'],'trellis2')
        self.assertEqual(lock['huggingface_cache'],[])
        self.assertNotIn('hunyuan',json.dumps(lock).lower())
        self.assertNotIn('dinov2',json.dumps(lock).lower())
        self.assertEqual(lock['tools']['blender']['version'],'5.2.2')

    def test_model_download_lock_is_independent_of_partial_receipts(self):
        lock=json.loads((RUNTIME/'model-files.lock.json').read_text('utf-8'))
        self.assertTrue(any(f['model'].startswith('facebook/dinov3') for f in lock['files']))
        self.assertTrue(any(f['model']=='microsoft/TRELLIS.2-4B' for f in lock['files']))
        script=(RUNTIME/'download_models.py').read_text('utf-8')
        self.assertIn('expected_files',script)
        self.assertIn('model-files.lock.json',script)
        for spec in lock['files']:
            self.assertEqual(len(spec['sha256']),64)
            self.assertFalse(Path(spec['path']).is_absolute())
