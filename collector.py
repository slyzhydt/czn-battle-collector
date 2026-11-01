"""Interactive, offline-only, two-stop battle collector."""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
import sys
import uuid
import zipfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE/'vendor'))
from native_host import capture, discover, profile, active_profile, save, sha
from domain import opening_info, pair_info, validate_opening_config
from version_report import write_report

DATA = HERE/'data'
VERSION = '0.1.3'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def local_operator():
    path = DATA/'anonymous-operator.json'
    if not path.exists():
        DATA.mkdir(exist_ok=True)
        save(path, {'id': uuid.uuid4().hex})
    return read(path)['id']


def new_opening(mode, team, locale):
    identity = discover()
    selected=active_profile(identity)
    capture_id = uuid.uuid4().hex
    folder = DATA/capture_id
    folder.mkdir(parents=True)
    meta = dict(schema='czn-training-capture-v1', id=capture_id, anonymousOperator=local_operator(),
                startedUtc=datetime.now(timezone.utc).isoformat(), declaredMode=mode, declaredTeam=team,
                locale=locale, profile=selected, state='opening-in-progress', trainingReady=False)
    save(folder/'manifest.json', meta)
    a = capture(folder/'opening', identity, capture_id, 'opening', 'snapshot')
    info = opening_info(a, selected)
    c = capture(folder/'config', identity, capture_id, 'config', 'config')
    validate_opening_config(a, c)
    meta.update(info, state='opening-collected')
    save(folder/'manifest.json', meta)
    print('\n开场已保存，读取已断开。现在可以正常打完整场，失败也请保留。')
    print('出现胜利、失败或固定回合结算界面后，先不点继续，回来选 2。')
    print('本场预先归入：'+('留出测试组（不用于训练）' if info['split']=='test' else '训练组'))
    return folder


def export_pair(folder, final_dir):
    meta = read(folder/'manifest.json')
    opening = read(folder/'opening/data.json')
    later = read(final_dir/'data.json')
    summary = pair_info(opening, later)
    files = {'opening.json': folder/'opening/data.json', 'config.json': folder/'config/data.json',
             'ending.json': final_dir/'data.json'}
    receipts = {}
    for name, source in files.items():
        receipt = read(source.parent/'receipt.json')
        if not all(receipt.get(k) is True for k in ('releaseConfirmed','unloaded','detached','exportAccepted')):
            raise ValueError('恢复或断开回执不完整，不能导出为采集包')
        if sha(source) != receipt['dataSHA256']:
            raise ValueError('采集数据与回执哈希不符')
        # Avoid distributing machine paths, PIDs or raw native-address diagnostics.
        receipts[name] = {k:receipt[k] for k in ('sourceSHA256','dataSHA256','startedUtc','finishedUtc',
                                                 'releaseConfirmed','unloaded','detached','exportAccepted')}
    manifest = {**meta, 'state':'paired-terminal' if summary['terminalObserved'] else 'paired-checkpoint',
                'pair':summary, 'files':{k:sha(v) for k,v in files.items()}, 'receipts':receipts,
                'trainingReady':False, 'liveAcceptance':False}
    out = HERE/'exports'
    out.mkdir(exist_ok=True)
    destination = out/(meta['id']+'-'+final_dir.name+'.zip')
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        for name, source in files.items():
            archive.write(source, name)
    if summary['terminalObserved']:
        meta.update(state='paired-terminal', finalCapture=final_dir.name)
        save(folder/'manifest.json', meta)
    return destination, summary


def ending(folder):
    meta = read(folder/'manifest.json')
    identity = discover()
    selected=active_profile(identity)
    for key in ('targetSHA256','resourceManifestSHA256','scriptChunkSHA256'):
        if key in meta['profile'] and selected.get(key)!=meta['profile'][key]:
            raise ValueError('开场与当前客户端版本不同，不能将跨版本数据配成一场')
    label = 'ending-'+uuid.uuid4().hex[:10]
    capture(folder/label, identity, meta['id'], label, 'snapshot')
    try:
        path, summary = export_pair(folder, folder/label)
    except Exception as error:
        save(folder/label/'export-error.json',{'phase':'pairing-or-export','type':type(error).__name__,
             'message':str(error),'recordedDataPreserved':True,'gameAttached':False})
        raise
    print('\n已导出：'+str(path))
    print('记录操作数：'+str(summary['recordedActions']))
    if summary['terminalObserved']:
        print('整场配对完成。可以继续游戏；把这个 ZIP 交给收集人即可。')
    else:
        print('仍未观察到结算，已保存中途数据；结算后可再选 2。')
    print('尚未通过离线重放，不会自动进入训练。')


def recover_exports():
    """Re-export existing captures only; no discovery, Frida, or game reads."""
    recovered,failed,existing=0,0,0
    for manifest in sorted(DATA.glob('*/manifest.json')):
        folder=manifest.parent
        for ending_data in sorted(folder.glob('ending-*/data.json')):
            destination=HERE/'exports'/(folder.name+'-'+ending_data.parent.name+'.zip')
            if destination.exists():
                existing+=1
                continue
            try:
                path,summary=export_pair(folder,ending_data.parent)
                recovered+=1
                print('已恢复导出：'+str(path))
                print('记录操作数：'+str(summary['recordedActions']))
            except Exception as error:
                failed+=1
                save(ending_data.parent/'export-error.json',{'phase':'offline-recovery','type':type(error).__name__,
                     'message':str(error),'recordedDataPreserved':True,'gameAttached':False})
                print('无法恢复 '+folder.name+'：'+str(error))
    result={'recovered':recovered,'failed':failed,'existing':existing,'gameAttached':False}
    print(f'恢复完成：新导出 {recovered} 份，已有 {existing} 份，待处理 {failed} 份。未连接游戏。')
    return result


def choose_pending():
    pending = [p.parent for p in sorted(DATA.glob('*/manifest.json')) if read(p).get('state')=='opening-collected']
    if not pending:
        raise ValueError('没有等待结算的开场，请先选 1')
    if len(pending)==1:
        return pending[0]
    for i, folder in enumerate(pending, 1):
        m = read(folder/'manifest.json')
        print(f"{i}. {m['startedUtc']} / {m['declaredMode']} / {m['declaredTeam']} / {m['id'][:8]}")
    index = int(input('选择本场编号：'))-1
    if not 0 <= index < len(pending):
        raise ValueError('编号不在列表中')
    return pending[index]


def check_dependencies():
    import frida
    import psutil
    if sys.version_info[:2] != (3,11) or frida.__version__ != profile()['fridaVersion']:
        raise ValueError('需要工具包附带的 Python 3.11 和 Frida 17.23.2')
    return {'collectorVersion':VERSION,'python':sys.version.split()[0], 'frida':frida.__version__, 'psutil':psutil.__version__,
            'gameAttached':False, 'profile':profile()['id'], 'liveAcceptance':False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-check', action='store_true', help='只检查工具依赖，不连接游戏')
    parser.add_argument('--recover-exports',action='store_true',help='从已保存数据恢复导出，不连接游戏')
    args = parser.parse_args()
    if args.recover_exports:
        recover_exports()
        return
    if args.self_check:
        print(json.dumps(check_dependencies(), ensure_ascii=False))
        return
    check_dependencies()
    print(f'卡厄思战斗采集 v{VERSION} 兼容试用版\n开场与结算各记录一次；读取期间停手，平时工具不连接游戏。')
    print('只支持已核对的 PC 客户端版本。所有文件留在本机，不自动上传。')
    print('首次请先采一场交回验证，再让大家批量采集。\n')
    while True:
        print('\n1 记录新开场（手牌已出现，尚未手动出牌）\n2 记录结算并导出\n3 检查游戏版本（不连接）\n4 从已有数据恢复导出（不连接游戏）\n0 退出')
        try:
            choice = input('请选择：').strip()
            if choice=='0':
                return
            if choice=='1':
                mode = input('模式名称（例如普通探索、全力战）：').strip()[:80]
                team = input('队伍角色（角色名即可，不要填写账号）：').strip()[:120]
                locale = input('界面语言 [默认繁体中文；可填简体中文/English/其他]：').strip()[:40] or '繁体中文'
                new_opening(mode, team, locale)
            elif choice=='2':
                ending(choose_pending())
            elif choice=='3':
                write_report()
            elif choice=='4':
                recover_exports()
        except (EOFError, KeyboardInterrupt):
            return
        except Exception as error:
            print('\n未完成：'+str(error))
            print('若提示版本不同，选 3 或运行“版本检查.cmd”，将生成 logs/version-check.json。')
            print('已有数据保留在 data 文件夹。读取异常时不要反复重试，先把错误交给维护者。')


if __name__=='__main__':
    main()
