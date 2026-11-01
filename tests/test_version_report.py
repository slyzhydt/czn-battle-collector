"""Version mismatch diagnostics must report both files, without a live game."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import hashlib
import json
import unittest

from version_report import build_report, inspect_candidate, display_path, write_report


class VersionReportChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.exe=self.root/'game.exe';self.exe.write_bytes(b'game-test')
        self.manifest=self.root/'manifest';self.manifest.write_bytes(b'resources-test')
        self.profile={'id':'synthetic-test','targetName':'game.exe','resourceManifestRelative':'manifest',
                      'targetSHA256':hashlib.sha256(b'game-test').hexdigest(),
                      'resourceManifestSHA256':hashlib.sha256(b'resources-test').hexdigest()}

    def test_matching_files(self):
        report=build_report(self.profile,self.exe)
        self.assertTrue(report['compatibleWithCurrentProfile'])
        self.assertFalse(report['gameAttached'])

    def test_executable_mismatch_still_checks_manifest(self):
        self.exe.write_bytes(b'changed-game')
        report=build_report(self.profile,self.exe)
        self.assertFalse(report['compatibleWithCurrentProfile'])
        candidate=report['candidates'][0]
        self.assertEqual(candidate['executable']['status'],'mismatch')
        self.assertEqual(candidate['resourceManifest']['status'],'match')
        self.assertEqual(candidate['executable']['actualSHA256'],hashlib.sha256(b'changed-game').hexdigest())

    def test_missing_resource_is_reported_even_with_wrong_executable(self):
        self.exe.write_bytes(b'changed-game');self.manifest.unlink()
        candidate=inspect_candidate(self.exe,self.profile)
        self.assertEqual(candidate['executable']['status'],'mismatch')
        self.assertEqual(candidate['resourceManifest']['status'],'missing')

    def test_access_denied_preserves_other_file_result(self):
        original=Path.open
        def open_file(path,*args,**kwargs):
            if path==self.exe:raise PermissionError('synthetic')
            return original(path,*args,**kwargs)
        with patch.object(Path,'open',open_file):
            candidate=inspect_candidate(self.exe,self.profile)
        self.assertEqual(candidate['executable']['status'],'access-denied')
        self.assertEqual(candidate['resourceManifest']['status'],'match')

    def test_multiple_processes_enumerated_once(self):
        processes=[SimpleNamespace(info={'pid':i,'name':'game.exe'},exe=lambda:str(self.exe))for i in (1,2)]
        calls=[]
        def iterator(fields):calls.append(fields);return iter(processes)
        fake=SimpleNamespace(process_iter=iterator,NoSuchProcess=type('Gone',(Exception,),{}),AccessDenied=type('Denied',(Exception,),{}))
        with patch.dict('sys.modules',{'psutil':fake}):report=build_report(self.profile)
        self.assertEqual(len(calls),1)
        self.assertEqual(report['matchingProcessCount'],2)
        self.assertEqual(len(report['candidates']),1)
        self.assertFalse(report['compatibleWithCurrentProfile'])

    def test_user_home_is_redacted(self):
        with patch.dict('os.environ',{'USERPROFILE':str(self.root)}):
            self.assertEqual(display_path(self.exe),'%USERPROFILE%'+str(self.exe)[len(str(self.root)):])

    def test_saved_report_includes_differences(self):
        (self.root/'profile.json').write_text(json.dumps(self.profile),encoding='utf8')
        self.manifest.write_bytes(b'changed-resources')
        destination=self.root/'logs/version-check.json'
        with patch('version_report.HERE',self.root),patch('builtins.print'):
            write_report(self.exe,destination)
        result=json.loads(destination.read_text(encoding='utf8'))
        self.assertEqual(result['candidates'][0]['resourceManifest']['status'],'mismatch')
        self.assertFalse(result['gameFilesModified'])
        self.assertFalse(result['gameAttached'])


if __name__=='__main__':unittest.main()
