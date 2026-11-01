"""Wave transitions may change battle_res_id without creating a new encounter."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from domain import pair_info
from test_collector import snapshot


def waves():
    start=snapshot()
    start['worktables']['BattleInitWorktable']['data']['battle_init']['battle_res_id']='wave-a'
    start['worktables']['BattleWorktable']['data']['battle'].update(
        first_unit_id=33,last_unit_id=43,last_action_id=18,
        battle_step_mode={'type':'BATTLE_STEP_WAVE_MODE','battle_ids':['wave-a','wave-b'],'cur':0})
    start['worktables']['BattleStageWorktable']={'data':{'battle_stage':{
        'stage_id':0,'ingame_content_config_id':'story', 'story_simulation_info':{'story_map_node_id':'node-a'}}}}
    end=deepcopy(start)
    end['worktables']['BattleInitWorktable']['data']['battle_init']['battle_res_id']='wave-b'
    battle=end['worktables']['BattleWorktable']['data']['battle']
    battle.update(turn=4,last_unit_id=50,last_action_id=306,game_result='BATTLE_RESULT_TYPE_REWARD',
                  user_actions=[{'type':'card_use','card_id':1},{'type':'turn_over'}])
    battle['battle_step_mode']['cur']=2
    return start,end


class WavePairChecks(unittest.TestCase):
    def test_final_wave_has_terminal_cursor_past_last_index(self):
        start,end=waves();result=pair_info(start,end)
        self.assertTrue(result['paired'])
        self.assertEqual(result['waveProgression']['toIndex'],2)
        self.assertEqual(result['recordedActions'],2)
        self.assertFalse(result['trainingReady'])

    def test_mid_battle_next_wave_checkpoint(self):
        start,end=waves();b=end['worktables']['BattleWorktable']['data']['battle']
        b['battle_step_mode']['cur']=1;b['game_result']='BATTLE_RESULT_TYPE_NONE'
        self.assertFalse(pair_info(start,end)['terminalObserved'])

    def test_changed_id_without_wave_evidence_still_rejected(self):
        start,end=waves();start['worktables']['BattleWorktable']['data']['battle'].pop('battle_step_mode')
        with self.assertRaises(ValueError):pair_info(start,end)

    def test_different_wave_list_rejected(self):
        start,end=waves();end['worktables']['BattleWorktable']['data']['battle']['battle_step_mode']['battle_ids'][0]='other'
        with self.assertRaises(ValueError):pair_info(start,end)

    def test_id_outside_declared_wave_rejected(self):
        start,end=waves();end['worktables']['BattleInitWorktable']['data']['battle_init']['battle_res_id']='other'
        with self.assertRaises(ValueError):pair_info(start,end)

    def test_different_seed_rejected_even_with_matching_waves(self):
        start,end=waves();end['worktables']['BattleInitWorktable']['data']['battle_init']['seed']+=1
        with self.assertRaises(ValueError):pair_info(start,end)

    def test_new_instance_or_counter_reset_rejected(self):
        for field,value in [('first_unit_id',34),('last_unit_id',2),('last_action_id',1)]:
            start,end=waves();end['worktables']['BattleWorktable']['data']['battle'][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):pair_info(start,end)

    def test_changed_scene_rejected(self):
        start,end=waves();end['worktables']['BattleStageWorktable']['data']['battle_stage']['story_simulation_info']['story_map_node_id']='node-b'
        with self.assertRaises(ValueError):pair_info(start,end)

    def test_exhausted_cursor_without_result_rejected(self):
        start,end=waves();end['worktables']['BattleWorktable']['data']['battle']['game_result']='BATTLE_RESULT_TYPE_NONE'
        with self.assertRaises(ValueError):pair_info(start,end)

    def test_wave_cursor_out_of_bounds_rejected(self):
        start,end=waves();end['worktables']['BattleWorktable']['data']['battle']['battle_step_mode']['cur']=3
        with self.assertRaises(ValueError):pair_info(start,end)


class RecoveryChecks(unittest.TestCase):
    def test_recovery_cli_does_not_require_game_or_frida(self):
        import collector
        with TemporaryDirectory()as tmp:
            root=Path(tmp);folder=root/'data/test/ending-test';folder.mkdir(parents=True)
            (folder/'data.json').write_text('{}');(folder.parent/'manifest.json').write_text('{}')
            with patch('collector.HERE',root),patch('collector.DATA',root/'data'),patch('collector.export_pair',return_value=(root/'exports/result.zip',{'recordedActions':2}))as export,patch('collector.discover',side_effect=AssertionError('no game lookup')),patch('collector.capture',side_effect=AssertionError('no capture')),patch('collector.check_dependencies',side_effect=AssertionError('no Frida needed')),patch('sys.argv',['collector.py','--recover-exports']),patch('builtins.print'):
                collector.main()
                export.assert_called_once_with(folder.parent,folder)

    def test_recovery_never_overwrites_existing_archive(self):
        import collector
        with TemporaryDirectory()as tmp:
            root=Path(tmp);folder=root/'data/test/ending-test';folder.mkdir(parents=True)
            (folder/'data.json').write_text('{}');(folder.parent/'manifest.json').write_text('{}')
            (root/'exports').mkdir();archive=root/'exports/test-ending-test.zip';archive.write_bytes(b'existing')
            with patch('collector.HERE',root),patch('collector.DATA',root/'data'),patch('collector.export_pair',side_effect=AssertionError('do not overwrite')),patch('builtins.print'):
                self.assertEqual(collector.recover_exports()['existing'],1)
            self.assertEqual(archive.read_bytes(),b'existing')


if __name__=='__main__':unittest.main()
