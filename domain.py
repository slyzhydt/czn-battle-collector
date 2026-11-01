"""Offline collection pairing. Raw captures are never auto-admitted to training."""
import hashlib
import json


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def table(snapshot, name):
    return snapshot['worktables'][name]['data']


def battle(snapshot):
    return table(snapshot, 'BattleWorktable')['battle']


def validate_snapshot(snapshot):
    if snapshot.get('schema') != 'czn-runtime-battle-read-v2':
        raise ValueError('不是支持的战斗快照')
    if any(snapshot.get(k) is not True for k in ('captureComplete', 'rngUnchanged', 'cohortUnchanged')):
        raise ValueError('读取不完整，或读取期间战斗发生变化')
    if snapshot.get('errors') or snapshot.get('missingInputs') or snapshot['serialization']['limits']:
        raise ValueError('读取触及缺项或大小限制')
    b = battle(snapshot)
    if not isinstance(b.get('user_actions'), list):
        raise ValueError('操作记录不可用')
    return b


def identity(snapshot):
    """Encounter stamps plus current wave ID, which may advance within one battle."""
    init = table(snapshot, 'BattleInitWorktable')['battle_init']
    keys = ('seed', 'battle_res_id', 'enter_time', 'stage_enter_time', 'first_enter_time', 'spot_id', 'floor', 'area_id')
    if any(k not in init for k in ('seed', 'battle_res_id', 'enter_time')):
        raise ValueError('缺少配对所需的战斗身份；不能猜测为同一场')
    return {k: init[k] for k in keys if k in init}


def wave_progression(opening, later):
    """Prove a current resource-ID change follows the captured wave sequence."""
    a,b=battle(opening),battle(later)
    start,end=a.get('battle_step_mode'),b.get('battle_step_mode')
    if not isinstance(start,dict) or not isinstance(end,dict) or start.get('type')!='BATTLE_STEP_WAVE_MODE' or end.get('type')!=start['type']:
        raise ValueError('战斗编号变化，但没有可核对的同场波次信息')
    ids=start.get('battle_ids')
    if not isinstance(ids,list) or not ids or any(not isinstance(x,str) or not x for x in ids) or end.get('battle_ids')!=ids:
        raise ValueError('开场与结算的波次列表不同，不能配对')
    first,last=start.get('cur'),end.get('cur')
    if type(first)is not int or type(last)is not int or not 0<=first<len(ids) or not first<=last<=len(ids):
        raise ValueError('波次索引无效或发生回退，不能配对')
    terminal=isinstance(b.get('game_result'),str) and b['game_result']!='BATTLE_RESULT_TYPE_NONE'
    if last==len(ids) and not terminal:
        raise ValueError('波次标记已结束，但还没有结算结果')
    ia,ib=identity(opening),identity(later)
    if ia['battle_res_id']!=ids[first] or ib['battle_res_id']!=ids[min(last,len(ids)-1)]:
        raise ValueError('当前战斗编号不符合已记录的波次顺序')
    if type(a.get('first_unit_id'))is not int or a['first_unit_id']!=b.get('first_unit_id'):
        raise ValueError('战斗实例起始编号不同，不能当作同场换波')
    for key in ('last_unit_id','last_action_id'):
        if type(a.get(key))is not int or type(b.get(key))is not int or b[key]<a[key]:
            raise ValueError('战斗实例计数回退，不能当作同场换波')
    sa=table(opening,'BattleStageWorktable')['battle_stage']
    sb=table(later,'BattleStageWorktable')['battle_stage']
    for key in ('stage_id','ingame_content_config_id','story_simulation_info'):
        if sa.get(key)!=sb.get(key):raise ValueError('战斗场景身份不同，不能当作同场换波')
    return {'type':start['type'],'battleIds':ids,'fromIndex':first,'toIndex':last,
            'initialBattleId':ia['battle_res_id'],'endingBattleId':ib['battle_res_id']}


def group_key(snapshot, profile):
    """Group retries across operators by seed/encounter/initial party; exclude wall clock."""
    init = table(snapshot, 'BattleInitWorktable')['battle_init']
    return digest({'client': profile['targetSHA256'], 'resources': profile['resourceManifestSHA256'],
                   'seed': init['seed'], 'battle': init['battle_res_id']})


def opening_info(snapshot, profile):
    b = validate_snapshot(snapshot)
    if b['turn'] != 1 or b['user_actions'] or b.get('game_result') != 'BATTLE_RESULT_TYPE_NONE':
        raise ValueError('需要第 1 回合、尚未手动操作的开场；自动开幕效果可以保留')
    ident = identity(snapshot)
    key = group_key(snapshot, profile)
    # Declared before outcome. Central intake further groups identical openings and seeds.
    split = ('test' if int(key[:8], 16) % 10 < 2 else 'train')
    return {'encounterIdentity': ident, 'groupKey': key, 'split': split,
            'splitPolicy': 'version-encounter-seed-v1: sha256 first32bits modulo10 <2 test',
            'openingSemanticHash': digest({'worktables': snapshot['worktables'], 'rng': snapshot['rngBefore']}),
            'turn': b['turn'], 'partyResourceIds': [c.get('res_id') for c in b.get('chars', [])]}


def validate_opening_config(snapshot, config):
    if config.get('schema') != 'czn-pre-solver-config-v1' or config.get('captureComplete') is not True:
        raise ValueError('配置读取不完整')
    if config['before'] != snapshot['after'] or config['after'] != config['before']:
        raise ValueError('开场与配置读取之间发生变化；请停手后重新采集开场')
    states = config['payload']['enums']['LogicState']
    if battle(snapshot)['current_state'] not in (states['MainPhase'], states['MainPhaseAutoAvailable']):
        raise ValueError('开场尚未进入正常出牌阶段，请等动画结束再试')
    pending = snapshot['runtimeInputs']['executionProtocol']['pendingServerSteps']
    if pending.get('available') is not True or pending.get('count') != 0:
        raise ValueError('客户端仍有未完成的同步，请等界面稳定再试')


def pair_info(opening, later):
    a, b = validate_snapshot(opening), validate_snapshot(later)
    ia,ib=identity(opening),identity(later)
    if {k:v for k,v in ia.items()if k!='battle_res_id'}!={k:v for k,v in ib.items()if k!='battle_res_id'}:
        raise ValueError('这不是开场记录的那一场；不能把两场拼接')
    progression=None
    if ia['battle_res_id']!=ib['battle_res_id']:
        progression=wave_progression(opening,later)
    prefix, trail = a['user_actions'], b['user_actions']
    if trail[:len(prefix)] != prefix or b['turn'] < a['turn']:
        raise ValueError('操作记录或回合发生回退；需作为新采集处理')
    result = b.get('game_result')
    terminal = isinstance(result, str) and result != 'BATTLE_RESULT_TYPE_NONE'
    result={'paired': True, 'terminalObserved': terminal, 'gameResult': result,
            'turn': b['turn'], 'recordedActions': len(trail)-len(prefix),
            'replayVerified': False, 'trainingReady': False,
            'admission': 'pending-offline-replay-and-resource-coverage',
            'damage': b.get('dpt', {}).get('total_damage')}
    if progression is not None:result['waveProgression']=progression
    return result
