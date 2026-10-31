"""Self-contained synthetic fixtures. No game data, Frida attachment or network."""
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import unittest

from domain import opening_info, pair_info, validate_opening_config
from native_host import prepare_source

HERE=Path(__file__).resolve().parents[1]


def snapshot():
    return {'schema':'czn-runtime-battle-read-v2','captureComplete':True,'rngUnchanged':True,'cohortUnchanged':True,
            'errors':[],'missingInputs':[],'serialization':{'limits':[]},'rngBefore':[{'state':123}],
            'after':{'battle':{'turn':1}},
            'runtimeInputs':{'executionProtocol':{'pendingServerSteps':{'available':True,'count':0}}},
            'worktables':{
                'BattleWorktable':{'data':{'battle':{'turn':1,'user_actions':[],'current_state':26,
                    'game_result':'BATTLE_RESULT_TYPE_NONE','chars':[{'res_id':111}]}}},
                'BattleInitWorktable':{'data':{'battle_init':{'seed':123,'battle_res_id':'test-only','enter_time':100}}}}}


class CollectorChecks(unittest.TestCase):
    def setUp(self):
        self.open=snapshot()
        self.profile=json.loads((HERE/'profile.json').read_text(encoding='utf8'))

    def test_template_hash(self):
        self.assertEqual(hashlib.sha256((HERE/'agent-template.js').read_bytes()).hexdigest(),self.profile['templateSHA256'])

    def test_pair_keeps_training_gate_closed(self):
        end=deepcopy(self.open)
        end['worktables']['BattleWorktable']['data']['battle'].update(turn=2,user_actions=[{'type':'turn_over'}],game_result='BATTLE_RESULT_TYPE_STAGE_CLEAR')
        result=pair_info(self.open,end)
        self.assertEqual(result['recordedActions'],1)
        self.assertTrue(result['terminalObserved'])
        self.assertFalse(result['trainingReady'])

    def test_wrong_seed_rejected(self):
        end=deepcopy(self.open);end['worktables']['BattleInitWorktable']['data']['battle_init']['seed']=124
        with self.assertRaises(ValueError):pair_info(self.open,end)

    def test_retry_stays_same_split(self):
        first=opening_info(self.open,self.profile)
        self.open['worktables']['BattleInitWorktable']['data']['battle_init']['enter_time']=200
        second=opening_info(self.open,self.profile)
        self.assertEqual(first['groupKey'],second['groupKey'])
        self.assertEqual(first['split'],second['split'])

    def test_pending_sync_rejected(self):
        config={'schema':'czn-pre-solver-config-v1','captureComplete':True,'before':self.open['after'],
                'after':self.open['after'],'payload':{'enums':{'LogicState':{'MainPhase':25,'MainPhaseAutoAvailable':26}}}}
        validate_opening_config(self.open,config)
        self.open['runtimeInputs']['executionProtocol']['pendingServerSteps']['count']=1
        with self.assertRaises(ValueError):validate_opening_config(self.open,config)

    def test_arbitrary_request_rejected(self):
        with self.assertRaises(ValueError):prepare_source({'pid':1,'path':'not-game'},{'kind':'lookup','allowResourceQueries':False})


if __name__=='__main__':unittest.main()
