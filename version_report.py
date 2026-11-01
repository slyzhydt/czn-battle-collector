"""File-only version diagnostics. Never attaches to a process or imports Frida."""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
import sys

HERE = Path(__file__).resolve().parent


def display_path(path):
    value = str(path)
    home = os.environ.get('USERPROFILE')
    if home and value.lower().startswith(home.lower().rstrip('\\/')+os.sep):
        value = '%USERPROFILE%'+value[len(home):]
    return value


def inspect_file(path, expected):
    item = {'path':display_path(path), 'expectedSHA256':expected}
    try:
        with Path(path).open('rb') as stream:
            before = os.fstat(stream.fileno())
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            after = os.fstat(stream.fileno())
        item.update(actualSHA256=digest, bytes=after.st_size,
                    modifiedUtc=datetime.fromtimestamp(after.st_mtime,timezone.utc).isoformat())
        stable = (before.st_size,before.st_mtime_ns)==(after.st_size,after.st_mtime_ns)
        item['status'] = ('match' if digest==expected else 'mismatch') if stable else 'changed-during-read'
    except FileNotFoundError:
        item['status'] = 'missing'
    except PermissionError:
        item.update(status='access-denied',error='文件无读取权限')
    except OSError as error:
        item.update(status='read-error',errorType=type(error).__name__,errorNumber=error.errno)
    return item


def inspect_candidate(path, profile):
    exe = Path(path)
    return {'executable':inspect_file(exe,profile['targetSHA256']),
            'resourceManifest':inspect_file(exe.parent/profile['resourceManifestRelative'],profile['resourceManifestSHA256'])}


def known_profiles(root):
    base={key:value for key,value in root.items() if key!='compatibleProfiles'}
    return [base]+[{**base,**entry} for entry in root.get('compatibleProfiles',[])]


def select_candidate_profile(path, root, candidate):
    """An exact reviewed file tuple is required; similar native code alone never admits it."""
    variants=known_profiles(root)
    exe_hash=candidate['executable'].get('actualSHA256')
    manifest_hash=candidate['resourceManifest'].get('actualSHA256')
    nearest=next((v for v in variants if v['targetSHA256']==exe_hash),variants[0])
    for key,field in [('executable','targetSHA256'),('resourceManifest','resourceManifestSHA256')]:
        item=candidate[key];item['expectedSHA256']=nearest[field]
        if item['status'] in ('match','mismatch'):
            item['status']='match' if item['actualSHA256']==nearest[field] else 'mismatch'
    matches=[v for v in variants if v['targetSHA256']==exe_hash and v['resourceManifestSHA256']==manifest_hash]
    if len(matches)>1:raise ValueError('兼容配置存在重复的程序/资源组合')
    candidate['matchedProfileId']=None
    if len(matches)!=1 or any(candidate[k]['status']!='match' for k in ('executable','resourceManifest')):
        return None
    selected=matches[0]
    if 'scriptChunkRelative' in selected:
        chunk=inspect_file(Path(path).parent/selected['scriptChunkRelative'],selected['scriptChunkSHA256'])
        candidate['scriptChunk']=chunk
        if chunk['status']!='match':return None
    candidate['matchedProfileId']=selected['id']
    candidate['runtimeVerified']=selected.get('liveAcceptance',False)
    candidate['runtimeReadObserved']=selected.get('runtimeReadObserved',False)
    return selected


def build_report(profile, executable=None):
    report = {'schema':'czn-version-check-v1','createdUtc':datetime.now(timezone.utc).isoformat(),
              'profileId':profile['id'],'targetName':profile['targetName'],
              'gameAttached':False,'gameMemoryRead':False,'gameFilesModified':False,
              'pathPrivacy':'USERPROFILE prefix replaced; other game-folder names retained',
              'processErrors':[],'candidates':[]}
    paths = []
    if executable is not None:
        report['discovery']='user-selected-file'
        paths.append(Path(executable))
    else:
        import psutil
        report['discovery']='exact-process-name'
        report['matchingProcessCount']=0
        for process in psutil.process_iter(['pid','name']):
            if (process.info['name'] or '').lower()!=profile['targetName'].lower():
                continue
            report['matchingProcessCount']+=1
            try:
                path = Path(process.exe())
                if path not in paths:paths.append(path)
            except psutil.NoSuchProcess:
                report['processErrors'].append({'status':'exited-before-inspection'})
            except psutil.AccessDenied:
                report['processErrors'].append({'status':'process-access-denied'})
    selected=[]
    for path in paths:
        candidate=inspect_candidate(path,profile)
        selected.append(select_candidate_profile(path,profile,candidate))
        report['candidates'].append(candidate)
    unique_process = executable is not None or report.get('matchingProcessCount')==1
    files_match = len(selected)==1 and selected[0] is not None
    report['compatibleWithCurrentProfile']=bool(unique_process and files_match and not report['processErrors'])
    report['interpretation']='Hashes identify file differences; they do not identify whether the cause is an update, channel, hotfix or local modification.'
    return report


def render(report, destination):
    statuses={'match':'匹配','mismatch':'不匹配','missing':'文件不存在','access-denied':'没有读取权限',
              'read-error':'文件读取失败','changed-during-read':'读取时文件发生变化'}
    lines=['版本检查报告已保存：'+str(destination), '检查仅读取文件信息，未连接游戏。']
    if not report['candidates']:
        lines.append('未找到可读取的目标程序。请正常启动游戏停在主界面后再运行；不需要进入战斗。')
    for index,candidate in enumerate(report['candidates'],1):
        for key,label in [('executable','游戏程序'),('resourceManifest','资源清单'),('scriptChunk','主脚本包')]:
            if key not in candidate:continue
            value=candidate[key]
            lines.extend([f"{index}. {label}：{statuses[value['status']]}", '   文件：'+value['path']])
            if value['status']=='mismatch':
                lines.extend(['   预期 SHA256：'+value['expectedSHA256'],'   实际 SHA256：'+value['actualSHA256']])
        if candidate.get('matchedProfileId'):
            lines.append('   已匹配兼容配置：'+candidate['matchedProfileId'])
            if candidate.get('runtimeReadObserved') is True:
                lines.append('   读取通道已有实际采集记录；每份数据仍需离线重放核对。')
            elif candidate.get('runtimeVerified') is not True:
                lines.append('   此配置仍需首场实际采集确认；文件匹配不等于已完成实机验证。')
    if report.get('matchingProcessCount',1)!=1 and report['candidates']:
        lines.append('找到的游戏进程数不是 1，请关闭多开的客户端后再检查。')
    if report['processErrors']:
        lines.append('部分游戏进程信息无法读取，详情已写进报告；权限不足时可右键检查脚本，以管理员身份运行。')
    lines.append('把这个 JSON 文件交给维护者即可，不需要发送游戏程序。报告中的文件差异尚不能确定具体原因。')
    return '\n'.join(lines)


def write_report(executable=None, output=None):
    profile=json.loads((HERE/'profile.json').read_text(encoding='utf-8'))
    report=build_report(profile,executable)
    destination=Path(output) if output else HERE/'logs/version-check.json'
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(render(report,destination))
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe',type=Path,help='Optional exact game executable; can inspect while game is closed')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    try:
        write_report(args.exe,args.output)
    except Exception as error:
        print('无法生成检查报告：'+str(error))
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
