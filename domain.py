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
    """Only immutable encounter identity, not turn-varying HP, deck or resource caches."""
    init = table(snapshot, 'BattleInitWorktable')['battle_init']
    keys = ('seed', 'battle_res_id', 'enter_time', 'stage_enter_time', 'first_enter_time', 'spot_id', 'floor', 'area_id')
    if any(k not in init for k in ('seed', 'battle_res_id', 'enter_time')):
        raise ValueError('缺少配对所需的战斗身份；不能猜测为同一场')
    return {k: init[k] for k in keys if k in init}


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
    if identity(opening) != identity(later):
        raise ValueError('这不是开场记录的那一场；不能把两场拼接')
    prefix, trail = a['user_actions'], b['user_actions']
    if trail[:len(prefix)] != prefix or b['turn'] < a['turn']:
        raise ValueError('操作记录或回合发生回退；需作为新采集处理')
    result = b.get('game_result')
    terminal = isinstance(result, str) and result != 'BATTLE_RESULT_TYPE_NONE'
    return {'paired': True, 'terminalObserved': terminal, 'gameResult': result,
            'turn': b['turn'], 'recordedActions': len(trail)-len(prefix),
            'replayVerified': False, 'trainingReady': False,
            'admission': 'pending-offline-replay-and-resource-coverage',
            'damage': b.get('dpt', {}).get('total_damage')}
