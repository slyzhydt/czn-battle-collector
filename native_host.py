"""Fixed existing-cache exporter. No battle commands, network interception, or loaders."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import queue
import re
import signal
import threading
import time

from export_supervisor import OwnershipUnknown, supervise, strict_json

HERE = Path(__file__).resolve().parent


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def profile():
    return json.loads((HERE/'profile.json').read_text(encoding='utf-8'))


def prepare_source(identity, request):
    p = profile()
    if request['kind'] not in ('snapshot', 'config') or request['allowResourceQueries'] is not False:
        raise ValueError('采集范围仅限当前战斗与已加载配置')
    if sha(HERE/'agent-template.js') != p['templateSHA256']:
        raise ValueError('采集模板已改变，请使用原始工具包')
    source = (HERE/'agent-template.js').read_text(encoding='utf-8-sig')
    def literal(name):
        match = re.search(r'^const '+name+r'=(.*);$', source, re.M)
        return json.loads(match[1])
    old_request, expression = literal('gameCaptureRequest'), literal('gameCaptureExpression')
    old_json = json.dumps(old_request, separators=(',', ':'))
    if expression.count(old_json) != 1:
        raise ValueError('模板请求绑定不唯一')
    encode = lambda value: json.dumps(value, ensure_ascii=True, separators=(',', ':'))
    expression = expression.replace(old_json, encode(request))
    replacements = {'gameIdentity': {'pid': identity['pid'], 'name': p['targetName'], 'path': identity['path'].replace('\\', '/')},
                    'gameCaptureRequest': request, 'gameCaptureExpression': expression}
    for name, value in replacements.items():
        source, count = re.subn(r'^const '+name+r'=(.*);$', lambda _: 'const '+name+'='+encode(value)+';', source, flags=re.M)
        if count != 1:
            raise ValueError('模板字段不唯一: '+name)
    return source


def discover():
    import psutil
    p = profile()
    found = []
    for process in psutil.process_iter(['pid', 'name']):
        if (process.info['name'] or '').lower() == p['targetName']:
            try:
                found.append({'pid': process.pid, 'created': process.create_time(), 'path': process.exe()})
            except psutil.AccessDenied as error:
                raise PermissionError('无法读取游戏身份。请关闭本工具，再右键“启动采集.cmd”以管理员身份运行') from error
            except psutil.NoSuchProcess:
                continue
    if len(found) != 1:
        raise ValueError('需要恰好一个正在运行的受支持游戏进程；当前找到 '+str(len(found))+' 个')
    item = found[0]
    if sha(item['path']) != p['targetSHA256']:
        raise ValueError('游戏程序版本与已核对版本不同，未连接游戏。请把检查报告交给工具维护者')
    manifest = Path(item['path']).parent / p['resourceManifestRelative']
    if not manifest.is_file() or sha(manifest) != p['resourceManifestSHA256']:
        raise ValueError('游戏资源版本不同，未连接游戏。需要先适配此版本')
    return item


def same_process(identity):
    import psutil
    try:
        process = psutil.Process(identity['pid'])
        return process.create_time() == identity['created'] and process.is_running()
    except psutil.NoSuchProcess:
        return False
    except psutil.Error as error:
        raise OwnershipUnknown('无法确认原游戏进程是否退出: '+str(error)) from error


class BoundedRPC:
    def __init__(self, script, timeout=1.5):
        self.script, self.timeout, self.poisoned = script, timeout, False
        self.jobs = queue.Queue()
        threading.Thread(target=self.worker, daemon=True, name='bounded-capture-rpc').start()

    def worker(self):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            method, args, event, reply = job
            try:
                function = {'_load': self.script.load, '_unload': self.script.unload}.get(method)
                reply.append((True, function() if function else getattr(self.script.exports_sync, method)(*args)))
            except Exception as error:
                reply.append((False, error))
            finally:
                event.set()

    def __call__(self, method, *args):
        if self.poisoned:
            raise OwnershipUnknown('通信超时后不重复调用')
        if method not in ('_load', '_unload', 'getstatus', 'start', 'armstop', 'getresult', 'release'):
            raise ValueError('不支持的读取指令')
        event, reply = threading.Event(), []
        self.jobs.put((method, args, event, reply))
        if not event.wait(self.timeout):
            self.poisoned = True
            raise OwnershipUnknown('等待回执超时: '+method)
        ok, value = reply[0]
        if not ok:
            self.poisoned = True
            raise OwnershipUnknown('读取指令失败: '+method+': '+str(value))
        return value

    def close(self):
        self.jobs.put(None)


def capture(folder, identity, capture_id, request_id, kind, emit=print):
    import frida
    import psutil
    p = profile()
    if frida.__version__ != p['fridaVersion']:
        raise ValueError('采集依赖版本不匹配')
    request = dict(kind=kind, captureSetId=capture_id, requestId=request_id, cacheRoot='module:GameDataStore',
                   executionRoot='context:exchange', allowAuditedDbAccessor=False, allowAuditedHostGetter=True,
                   allowResourceQueries=False)
    source = prepare_source(identity, request)
    folder.mkdir(parents=True, exist_ok=False)
    record = dict(schema='czn-portable-capture-receipt-v1', request=request, startedUtc=datetime.now(timezone.utc).isoformat(),
                  identity=identity, profileId=p['id'], sourceSHA256=hashlib.sha256(source.encode()).hexdigest(),
                  attached=False, scriptLoadAttempted=False, loaded=False, releaseConfirmed=False,
                  unloaded=False, detached=False, exportAccepted=False, messages=[])
    session = script = rpc = None
    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, lambda signum, frame: record.update(interruptRequested=signum))
    def progress(text):
        record['phase'] = text
        save(folder/'status.json', {k:v for k,v in record.items() if k not in ('supervision','messages')})
        emit(text, flush=True)
    def alive():
        return same_process(identity)
    try:
        if not alive():
            raise ValueError('游戏已退出或重启，未连接')
        progress('正在连接当前游戏进程……')
        session = frida.get_local_device().attach(identity['pid'])
        record['attached'] = True
        if not alive() or Path(psutil.Process(identity['pid']).exe()).resolve() != Path(identity['path']).resolve():
            raise ValueError('连接前后游戏身份改变，未加载读取代码')
        script = session.create_script(source)
        script.on('message', lambda message, data: record['messages'].append(message) if len(record['messages']) < 32 else None)
        rpc = BoundedRPC(script)
        record['scriptLoadAttempted'] = True
        rpc('_load')
        record['loaded'] = True
        progress('正在读取；请暂时不要操作游戏……')
        result = supervise(rpc, time.monotonic, time.sleep, alive, request)
        record['supervision'] = result
        record['releaseConfirmed'] = result['releaseConfirmed']
        record['exportAccepted'] = result['exportAccepted'] and result['releaseConfirmed']
        record['targetExited'] = result['targetExited']
    except Exception as error:
        record['error'] = {'type': type(error).__name__, 'message': str(error)}
    # An unresolved engine call may still reference code owned by this host.
    # Retain it until exact process exit; never force unload or kill the game.
    if record['scriptLoadAttempted'] and not record['releaseConfirmed'] and not record.get('targetExited'):
        try:
            progress('读取未取得恢复回执。请保持本窗口打开，正常退出游戏；退出后会保存错误记录。')
        except Exception:
            pass
        while True:
            try:
                if not alive():
                    record['targetExited'] = True
                    break
            except Exception as error:
                record['holdError'] = str(error)
            time.sleep(.25)
    elif record['releaseConfirmed']:
        try:
            rpc('_unload')
            record['unloaded'] = True
        except Exception as error:
            record['unloadError'] = str(error)
    if session is not None:
        try:
            session.detach()
            record['detached'] = True
        except Exception as error:
            record['detachError'] = str(error)
    if rpc is not None:
        rpc.close()
    for signum, handler in previous.items():
        signal.signal(signum, handler)
    accepted = all(record[k] for k in ('exportAccepted', 'releaseConfirmed', 'unloaded', 'detached'))
    record['exportAccepted'] = accepted
    if accepted:
        payload = strict_json(record['supervision']['exportResult']['text'])
        save(folder/'data.json', payload)
        record['dataSHA256'] = sha(folder/'data.json')
    record['finishedUtc'] = datetime.now(timezone.utc).isoformat()
    record['phase'] = 'finished-cleanly' if accepted else 'failed-or-incomplete'
    save(folder/'receipt.json', record)
    progress('已完成并断开。' if accepted else '本次未取得完整数据；错误记录已保留。')
    if not accepted:
        raise RuntimeError('本次采集未通过；请保留 data 目录内的记录，不要当作训练数据提交')
    return payload
