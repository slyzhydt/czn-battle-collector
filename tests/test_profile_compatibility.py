"""Only complete reviewed file combinations may select a capture profile."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import hashlib
import unittest

from version_report import inspect_candidate, select_candidate_profile, known_profiles
from native_host import active_profile

sha=lambda b:hashlib.sha256(b).hexdigest()


class ProfileCompatibilityChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.exe=self.root/'game.exe'
        self.manifest=self.root/'manifest';self.chunk=self.root/'code.ssrc'
        self.profile={'id':'old','targetName':'game.exe','resourceManifestRelative':'manifest',
            'targetSHA256':sha(b'old-exe'),'resourceManifestSHA256':sha(b'old-manifest'),
            'scriptChunkRelative':'code.ssrc','scriptChunkSHA256':sha(b'old-code'),'templateSHA256':'unchanged',
            'compatibleProfiles':[{'id':'new','targetSHA256':sha(b'new-exe'),
                'resourceManifestSHA256':sha(b'new-manifest'),'scriptChunkSHA256':sha(b'new-code'),
                'liveAcceptance':False}]}

    def install(self,exe,manifest,code):
        self.exe.write_bytes(exe);self.manifest.write_bytes(manifest);self.chunk.write_bytes(code)

    def select(self):
        candidate=inspect_candidate(self.exe,self.profile)
        selected=select_candidate_profile(self.exe,self.profile,candidate)
        return selected,candidate

    def test_old_profile_retained(self):
        self.install(b'old-exe',b'old-manifest',b'old-code')
        selected,candidate=self.select()
        self.assertEqual(selected['id'],'old')
        self.assertEqual(candidate['scriptChunk']['status'],'match')

    def test_new_profile_reports_its_actual_file_identities(self):
        self.install(b'new-exe',b'new-manifest',b'new-code')
        selected,candidate=self.select()
        self.assertEqual(selected['id'],'new')
        self.assertEqual(selected['targetSHA256'],sha(b'new-exe'))
        self.assertNotIn('compatibleProfiles',selected)
        self.assertEqual(candidate['executable']['status'],'match')
        self.assertEqual(candidate['resourceManifest']['expectedSHA256'],sha(b'new-manifest'))
        self.assertFalse(candidate['runtimeVerified'])

    def test_mixed_old_and_new_files_rejected(self):
        self.install(b'new-exe',b'old-manifest',b'new-code')
        selected,candidate=self.select()
        self.assertIsNone(selected)
        self.assertEqual(candidate['executable']['status'],'match')
        self.assertEqual(candidate['resourceManifest']['status'],'mismatch')

    def test_unreviewed_code_rejected_even_when_program_and_manifest_match(self):
        self.install(b'new-exe',b'new-manifest',b'unknown-code')
        selected,candidate=self.select()
        self.assertIsNone(selected)
        self.assertEqual(candidate['scriptChunk']['status'],'mismatch')

    def test_missing_code_rejected(self):
        self.install(b'new-exe',b'new-manifest',b'new-code');self.chunk.unlink()
        selected,candidate=self.select()
        self.assertIsNone(selected)
        self.assertEqual(candidate['scriptChunk']['status'],'missing')

    def test_unknown_executable_rejected(self):
        self.install(b'unknown-exe',b'new-manifest',b'new-code')
        self.assertIsNone(self.select()[0])

    def test_active_metadata_uses_selected_profile(self):
        with patch('native_host.profile',lambda:self.profile):
            self.assertEqual(active_profile({'profileId':'new'})['resourceManifestSHA256'],sha(b'new-manifest'))
            with self.assertRaises(ValueError):active_profile({'profileId':'unknown'})

    def test_cross_version_ending_stops_before_capture(self):
        import collector
        with patch('collector.read',return_value={'profile':self.profile}),patch('collector.discover',return_value={'profileId':'new'}),patch('collector.active_profile',return_value=known_profiles(self.profile)[1]),patch('collector.capture')as capture:
            with self.assertRaises(ValueError):collector.ending(self.root)
            capture.assert_not_called()


if __name__=='__main__':unittest.main()
