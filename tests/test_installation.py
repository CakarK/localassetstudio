"""Installer failure/resume contracts. No external download or inference."""
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from tests.runtime import TEST_ROOT

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'AI3D/Scripts'
spec = importlib.util.spec_from_file_location('las_install_support', SCRIPTS / 'install_support.py')
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.root = Path(self.temp.name)
        self.target = self.root / 'weights.bin'
        self.body = b'actual synthetic fixture payload'
        self.spec = {'size': len(self.body), 'sha256': hashlib.sha256(self.body).hexdigest()}

    def tearDown(self):
        self.temp.cleanup()

    def test_resume_verified_partial_without_duplicate_bytes(self):
        self.target.with_suffix('.bin.partial').write_bytes(self.body[:7])
        def opener(request, timeout):
            self.assertEqual(request.get_header('Range'), 'bytes=7-')
            return Response(self.body[7:], 206, {'Content-Range': f'bytes 7-{len(self.body)-1}/{len(self.body)}'})
        support.download('https://example.invalid/fixture', self.target, self.spec, opener)
        self.assertEqual(self.target.read_bytes(), self.body)

    def test_ignored_range_restarts_partial(self):
        self.target.with_suffix('.bin.partial').write_bytes(self.body[:7])
        support.download('https://example.invalid/fixture', self.target, self.spec,
                         lambda request, timeout: Response(self.body))
        self.assertEqual(self.target.read_bytes(), self.body)

    def test_early_end_of_response_resumes_instead_of_discarding_data(self):
        calls = []
        def opener(request, timeout):
            calls.append(request.get_header('Range'))
            if len(calls) == 1:
                return Response(self.body[:7])
            return Response(self.body[7:], 206, {'Content-Range': f'bytes 7-{len(self.body)-1}/{len(self.body)}'})
        with patch.object(support.time, 'sleep'):
            support.download('https://example.invalid/fixture', self.target, self.spec, opener)
        self.assertEqual(calls, [None, 'bytes=7-'])
        self.assertEqual(self.target.read_bytes(), self.body)

    def test_existing_bad_model_preserved_and_not_downloaded_over(self):
        self.target.write_bytes(b'previous user model')
        with self.assertRaises(ValueError):
            support.download('https://example.invalid/fixture', self.target, self.spec,
                             lambda *a, **kw: self.fail('must not open network'))
        self.assertEqual(self.target.read_bytes(), b'previous user model')

    def test_valid_existing_model_does_not_download_again(self):
        self.target.write_bytes(self.body)
        support.download('https://example.invalid/fixture', self.target, self.spec,
                         lambda *a, **kw: self.fail('must not open network'))

    def test_hash_failure_never_promotes_target(self):
        with self.assertRaises(ValueError):
            support.download('https://example.invalid/fixture', self.target, self.spec,
                             lambda request, timeout: Response(b'x' * len(self.body)))
        self.assertFalse(self.target.exists())
        self.assertTrue(list(self.root.glob('*.bad-*')))

    def test_wrong_content_range_is_rejected(self):
        partial = self.target.with_suffix('.bin.partial')
        partial.write_bytes(self.body[:7])
        with self.assertRaises(ValueError):
            support.download('https://example.invalid/fixture', self.target, self.spec,
                             lambda request, timeout: Response(self.body[7:], 206, {'Content-Range': 'bytes 0-99/100'}))
        self.assertEqual(partial.read_bytes(), self.body[:7])
        self.assertFalse(self.target.exists())

    def test_network_failure_preserves_partial_for_next_run(self):
        partial = self.target.with_suffix('.bin.partial')
        partial.write_bytes(self.body[:7])
        def fail(*a, **kw):
            raise OSError('synthetic connection failure')
        with patch.object(support.time, 'sleep'), self.assertRaises(OSError):
            support.download('https://example.invalid/fixture', self.target, self.spec, fail)
        self.assertEqual(partial.read_bytes(), self.body[:7])

    def test_model_destination_cannot_escape_installation(self):
        for name in ['../other.bin', str(self.root.parent / 'other.bin'), '.']:
            with self.assertRaises(ValueError):
                support.confined(self.root, name)

    def test_changed_public_ollama_manifest_is_not_silently_accepted(self):
        with self.assertRaises(ValueError):
            support.ollama_manifest_matches(b'changed public tag', 'a' * 64)

    def test_existing_different_ollama_model_is_preserved(self):
        with self.assertRaises(ValueError):
            support.installed_ollama_models({'models': [{'name': 'demo:1', 'digest': 'b' * 64}]},
                                           [{'model': 'demo:1', 'sha256': 'a' * 64}])

    def test_installer_model_profile_matches_quality_guard(self):
        lock = json.loads((ROOT / 'AI3D/install.lock.json').read_text('utf-8'))
        quality = json.loads((ROOT / 'AI3D/Config/quality.json').read_text('utf-8'))['quality']
        expected = next(m['sha256'] for m in lock['ollama_models'] if m['model'] == quality['model'])
        self.assertEqual(quality['model_digests'][quality['model']], expected)
        self.assertFalse(quality['allow_automatic_visual_approval'])

    def test_lock_covers_supported_provider_files_and_background_model(self):
        lock = json.loads((ROOT / 'AI3D/install.lock.json').read_text('utf-8'))
        registry = json.loads((ROOT / 'AI3D/Config/image_providers.json').read_text('utf-8'))
        files = {f['path']: f for f in lock['files']}
        for name in ['dreamshaper8', 'qwen-image-2512', 'z-image-turbo']:
            for model in registry['providers'][name]['files']:
                self.assertEqual(files[model['path']]['sha256'], model['sha256'])
        self.assertIn('AI3D/Models/rembg/u2net.onnx', files)
        self.assertGreater(lock['model_download_bytes'], 80_000_000_000)
        for model in lock['huggingface_cache'] + lock['files']:
            self.assertRegex(model['sha256'], r'^[a-f0-9]{64}$')

    def test_portable_blender_path_is_used_without_changing_explicit_override(self):
        from ai3d_manager.worker import settings_for
        asset = {'polygon_target': 1000, 'polygon_range': [500, 1500], 'texture_size': 1024}
        with patch.dict(os.environ, {'LAS_BLENDER_PATH': 'D:/Portable/blender.exe'}):
            self.assertEqual(settings_for(asset)['blender'], 'D:/Portable/blender.exe')
            self.assertEqual(settings_for(asset, {'blender': 'E:/Artist/blender.exe'})['blender'], 'E:/Artist/blender.exe')

    @unittest.skipUnless(os.name == 'nt', 'Windows PowerShell checks')
    def test_plan_is_read_only_and_windows_powershell_parses_every_script(self):
        script = self.root / 'check.ps1'
        script.write_text(r'''param($Root)
$ErrorActionPreference='Stop'
Get-ChildItem -LiteralPath $Root -Recurse -Filter *.ps1 | Where-Object { $_.FullName -notmatch '[\\/]AI3D[\\/](Tools|ComfyUI|ImageComfyUI|\.venv)[\\/]' } | ForEach-Object {
 $errors=$null; $tokens=$null
 $null=[System.Management.Automation.Language.Parser]::ParseFile($_.FullName,[ref]$tokens,[ref]$errors)
 if ($errors.Count) { throw ($errors | Out-String) }
}
& (Join-Path $Root 'Install-LocalAssetStudio.ps1') -Plan
''', 'utf-8')
        result = subprocess.run(['powershell.exe', '-NoProfile', '-File', str(script), str(ROOT)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('no changes or downloads', result.stdout)

    @unittest.skipUnless(os.name == 'nt', 'Windows tool extraction checks')
    def test_interrupted_tool_extraction_resumes_and_unowned_directory_is_preserved(self):
        archive = self.root / 'fixture.zip'
        with zipfile.ZipFile(archive, 'w') as stream:
            stream.writestr('tool.exe', b'fixture executable, never launched')
            stream.writestr('library.txt', b'fixture library')
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        cache = self.root / 'cache';cache.mkdir();shutil.copyfile(archive, cache / (digest + '.zip'))
        tools = self.root / 'tools';incomplete = tools / 'fixture-1';incomplete.mkdir(parents=True)
        (incomplete / '.verified-archive-sha256').write_text(digest)
        (incomplete / 'tool.exe').write_bytes(b'partly extracted')
        spec = {'sha256': digest, 'size': archive.stat().st_size, 'version': '1', 'executable': 'tool.exe', 'url': 'https://example.invalid/never-downloaded'}
        config = self.root / 'spec.json';config.write_text(json.dumps(spec))
        script = self.root / 'extract.ps1'
        script.write_text('''param($Functions,$Config,$Tools,$Cache)
$ErrorActionPreference='Stop'
. $Functions
$spec=Get-Content -LiteralPath $Config -Raw | ConvertFrom-Json
$null=Install-LASTool 'fixture' $spec $Tools $Cache
if (!(Test-Path -LiteralPath (Join-Path $Tools 'fixture-1/library.txt'))) { throw 'incomplete extraction was incorrectly reused' }
New-Item -ItemType Directory -Path (Join-Path $Tools 'foreign-1') | Out-Null
try { $null=Install-LASTool 'foreign' $spec $Tools $Cache; throw 'foreign directory was modified' }
catch { if ($_.Exception.Message -notmatch 'Unowned') { throw } }
''', 'utf-8')
        result = subprocess.run(['powershell.exe', '-NoProfile', '-File', str(script), str(SCRIPTS / 'install_functions.ps1'), str(config), str(tools), str(cache)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((incomplete / 'library.txt').read_bytes(), b'fixture library')

    @unittest.skipUnless(os.name == 'nt' and shutil.which('git'), 'Windows/Git source staging checks')
    def test_source_staging_resumes_but_never_resets_existing_revision(self):
        upstream = self.root / 'upstream';upstream.mkdir()
        def git(*arguments):
            return subprocess.check_output(['git', '-C', str(upstream), *arguments], text=True, stderr=subprocess.STDOUT).strip()
        git('init');(upstream / 'fixture.txt').write_text('first revision')
        git('add', '.');git('-c','user.name=Installer Fixture','-c','user.email=fixture@example.invalid','commit','-m','fixture')
        first = git('rev-parse','HEAD')
        (upstream / 'fixture.txt').write_text('second revision');git('add','.')
        git('-c','user.name=Installer Fixture','-c','user.email=fixture@example.invalid','commit','-m','second fixture')
        second = git('rev-parse','HEAD')
        destination = self.root / 'source with spaces'
        staged = Path(str(destination) + '.installing')
        subprocess.check_call(['git','clone','--no-checkout',str(upstream),str(staged)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        Path(str(staged)+'.owner').write_text(str(upstream)+'@'+first)
        config = self.root / 'source.json';config.write_text(json.dumps({'url':str(upstream),'revision':first}))
        script = self.root / 'source.ps1'
        script.write_text('''param($Functions,$Config,$Destination,$OtherRevision)
$ErrorActionPreference='Stop'
. $Functions
$spec=Get-Content -LiteralPath $Config -Raw | ConvertFrom-Json
$git=(Get-Command git.exe).Source
Install-LASSource $git $spec $Destination
Install-LASSource $git $spec $Destination
$spec.revision=$OtherRevision
try { Install-LASSource $git $spec $Destination; throw 'existing revision was reset' }
catch { if ($_.Exception.Message -notmatch 'Unexpected source revision') { throw } }
''', 'utf-8')
        result = subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(script),str(SCRIPTS/'install_functions.ps1'),str(config),str(destination),second], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        actual = subprocess.check_output(['git','-C',str(destination),'rev-parse','HEAD'],text=True).strip()
        self.assertEqual(actual, first)
        self.assertEqual((destination/'fixture.txt').read_text(),'first revision')


if __name__ == '__main__':
    unittest.main()
