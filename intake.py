"""Inspect collected ZIPs without extracting or training. Offline analysis only."""
from pathlib import Path
import argparse
import hashlib
import json
import zipfile
from domain import opening_info, pair_info, validate_opening_config
from export_supervisor import strict_json

NAMES = {'manifest.json','opening.json','config.json','ending.json'}


def inspect(path):
    with zipfile.ZipFile(path) as archive:
        info = archive.infolist()
        if len(info)!=4 or {i.filename for i in info}!=NAMES or sum(i.file_size for i in info)>32*1024*1024:
            raise ValueError('采集包文件集合或体积不符合格式')
        raw = {name:archive.read(name) for name in NAMES}
    values = {name:strict_json(value.decode('utf-8')) for name,value in raw.items()}
    m = values['manifest.json']
    if m.get('schema')!='czn-training-capture-v1':
        raise ValueError('不支持的采集包版本')
    for name in NAMES-{'manifest.json'}:
        if hashlib.sha256(raw[name]).hexdigest()!=m['files'][name]:
            raise ValueError('文件哈希不匹配: '+name)
        receipt = m['receipts'][name]
        if not all(receipt.get(k) is True for k in ('releaseConfirmed','unloaded','detached','exportAccepted')) or receipt['dataSHA256']!=m['files'][name]:
            raise ValueError('回执不完整: '+name)
    opening, config, ending = [values[n] for n in ('opening.json','config.json','ending.json')]
    computed = opening_info(opening, m['profile'])
    if any(m[k]!=computed[k] for k in ('groupKey','split','openingSemanticHash')):
        raise ValueError('开场分组信息不匹配')
    validate_opening_config(opening, config)
    pair = pair_info(opening, ending)
    return {'file':str(path), 'captureId':m['id'], 'anonymousOperator':m['anonymousOperator'],
            **computed, 'declaredMode':m['declaredMode'], **pair,
            'trustedSource':False, 'trainingReady':False,
            'nextGate':'trusted-profile / exact-resource-coverage / full-action-replay / outcome-RNG-comparison'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('files', nargs='+', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    results, groups = [], {}
    for path in args.files:
        try:
            record = inspect(path)
            group = groups.setdefault(record['groupKey'], [])
            record['sameOpeningGroupAlreadySeen'] = bool(group)
            group.append(record['captureId'])
        except Exception as error:
            record = {'file':str(path), 'rejected':True, 'reason':str(error), 'trainingReady':False}
        results.append(record)
    args.output.write_text(json.dumps({'schema':'czn-raw-intake-v1','files':results,'uniqueGroups':len(groups),
                                      'trainingAdmitted':0},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f'检查 {len(results)} 个包，独立分组 {len(groups)} 个；尚未自动纳入训练。')


if __name__=='__main__':
    main()
