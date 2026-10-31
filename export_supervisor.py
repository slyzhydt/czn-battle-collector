"""Bound-state export lifecycle. Native release gates remain independent of payload acceptance."""
from dataclasses import dataclass
import math
import json


class OwnershipUnknown(RuntimeError):
    pass


BOOL_FIELDS = ('nativeBusy', 'done', 'fault', 'accepting', 'cAcceptStopped',
               'hostCallPending', 'faultLatched', 'safeToUnload', 'moduleDisposed',
               'controlStateUnknown')
COUNT_FIELDS = ('queued', 'activeCallbacks', 'epoch')


def validate_status(value):
    if type(value) is not dict:
        raise OwnershipUnknown('RPC status is not an object')
    for key in BOOL_FIELDS:
        if type(value.get(key)) is not bool:
            raise OwnershipUnknown('Missing boolean ownership field: ' + key)
    for key in COUNT_FIELDS:
        if type(value.get(key)) is not int or value[key] < (1 if key == 'epoch' else 0):
            raise OwnershipUnknown('Invalid ownership count: ' + key)
    if value['controlStateUnknown']:
        raise OwnershipUnknown('Agent reports unknown native ownership')
    return value


def safe_stopped(value):
    s = validate_status(value)
    return (s['safeToUnload'] and not s['accepting'] and s['cAcceptStopped'] and
            not s['nativeBusy'] and not s['hostCallPending'] and not s['fault'] and
            not s['faultLatched'] and s['queued'] == 0 and s['activeCallbacks'] == 0)


MAX_PAYLOAD = 1048576
SCHEMAS = {'snapshot':'czn-runtime-battle-read-v2', 'config':'czn-pre-solver-config-v1',
           'inventory':'czn-pre-solver-inventory-v1', 'cache':'czn-pre-solver-page-v1'}


def strict_json(text):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Duplicate JSON key')
            value[key] = item
        return value
    def invalid_constant(value):
        raise ValueError('Nonstandard JSON constant: '+value)
    def finite_float(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError('Non-finite JSON number')
        return value
    return json.loads(text, object_pairs_hook=unique, parse_constant=invalid_constant, parse_float=finite_float)


def validate_result(value, request):
    if (type(value) is not dict or value.get('source') != 'game-stack-bound-export' or
            value.get('gameLive') is not True or value.get('request') != request or
            type(request) is not dict or request.get('kind') not in SCHEMAS):
        return False
    v = value.get('v')
    if (type(v) is not list or len(v) != 31 or any(type(x) is not int or x < 0 for x in v) or
            v[0] != 0 or v[25] != 0 or v[30] != 0 or v[9] <= 0 or not 2 <= v[23] <= MAX_PAYLOAD or
            any(v[i] != 1 for i in (8,11,12,13,14,15,16,17,18,19,20,21,22,26,27))):
        return False
    d = value.get('diagnostics')
    if (type(d) is not dict or any(type(d.get(k)) is not int for k in ('rpmOperations','bytesRead','localsCreated','nodeReads','readerError','copyError')) or
            not 1 <= d['rpmOperations'] <= 8192 or not 8 <= d['bytesRead'] <= 262144 or
            d['localsCreated'] != 15 or not 1 <= d['nodeReads'] <= 512 or d['readerError'] != 0 or d['copyError'] != 0):
        return False
    o = value.get('output')
    keys = ('outputCode','complete','operations','bytesRead','segmentsAttempted','segmentsCompleted',
            'windows','bytesWrittenBeforeCommit','firstByteCleared','lastCopyCode','memoryCalls','processCalls')
    if type(o) is not dict or any(type(o.get(k)) is not int or o[k] < 0 for k in keys):
        return False
    segments = (v[23]+1023)//1024
    windows = (v[23]+262143)//262144
    if (o['outputCode'] != 0 or o['complete'] != 1 or o['firstByteCleared'] != 0 or o['lastCopyCode'] != 0 or
            o['bytesRead'] != v[23] or o['bytesWrittenBeforeCommit'] != v[23] or
            any(o[k] != segments for k in ('operations','segmentsAttempted','segmentsCompleted','memoryCalls','processCalls')) or
            o['windows'] != windows or not 1 <= windows <= 4):
        return False
    text = value.get('text')
    if type(text) is not str:
        return False
    try:
        encoded = text.encode('utf-8', errors='strict')
        if len(encoded) != v[23] or b'\0' in encoded:
            return False
        data = strict_json(text)
    except (ValueError, UnicodeError, RecursionError):
        return False
    if (type(data) is not dict or data.get('schema') != SCHEMAS[request['kind']] or
            data.get('captureSetId') != request.get('captureSetId') or data.get('requestId') != request.get('requestId') or
            data.get('policyId') != 'czn-pre-solver-input-policy-v1' or
            any(data.get(k) is not True for k in ('captureComplete','rngUnchanged','cohortUnchanged')) or
            any(data.get(k) != [] for k in ('missingInputs','errors')) or
            type(data.get('serialization')) is not dict or data['serialization'].get('limits') != [] or
            data['serialization'].get('identityMode') != 'shared-and-cyclic' or
            type(data.get('before')) is not dict or type(data.get('after')) is not dict or
            type(data['before'].get('database')) is not dict or type(data['after'].get('database')) is not dict):
        return False
    before_rng = data['before'].get('rng')
    if (type(before_rng) is not list or not before_rng or data.get('rngAfter') != before_rng or
            data['after'].get('rng') != before_rng or
            ('rngBefore' in data and data['rngBefore'] != before_rng)):
        return False
    return True


@dataclass(frozen=True)
class Limits:
    acquisition_seconds: float = 5.0
    drain_seconds: float = 15.0
    interval_seconds: float = 0.05

    def __post_init__(self):
        for x in (self.acquisition_seconds, self.drain_seconds, self.interval_seconds):
            if type(x) not in (int, float) or not math.isfinite(x) or x <= 0:
                raise ValueError('Finite positive lifecycle limit required')
        if self.acquisition_seconds > 10 or self.drain_seconds > 30 or self.interval_seconds > 1:
            raise ValueError('Lifecycle limit exceeds fixed canary scope')


def supervise(rpc, now, pause, alive, expected_request, limits=Limits()):
    """Only release confirmation authorizes the caller to unload/detach.

    rpc must implement bounded synchronous calls. A timeout poisons its transport:
    the caller keeps the session resident until this exact process exits. This
    function never unloads, detaches, terminates or infers completion from messages.
    """
    result = dict(schema='czn-bound-state-export-supervision-v1', releaseConfirmed=False,
                  exportAccepted=False, targetExited=False, requiresProcessExit=False,
                  snapshots=[], calls=[])

    def invoke(method, *args):
        result['calls'].append(method)
        return rpc(method, *args)

    def observe(method='getstatus'):
        status = validate_status(invoke(method))
        result['snapshots'].append(dict(status))
        return status

    def process_alive():
        value = alive()
        if type(value) is not bool:
            raise OwnershipUnknown('Exact-process liveness is not a boolean')
        return value

    try:
        s = observe()
        epoch = s['epoch']
        if (s['moduleDisposed'] or s['nativeBusy'] or s['hostCallPending'] or s['done'] or
                s['fault'] or s['faultLatched'] or s['queued'] or s['activeCallbacks']):
            raise OwnershipUnknown('Unexpected initial ownership state')
        if not process_alive():
            result['targetExited'] = True
            return result
        invoke('start')
        deadline = now() + limits.acquisition_seconds
        while True:
            if not process_alive():
                result['targetExited'] = True
                return result
            s = observe()
            if s['epoch'] != epoch or s['moduleDisposed']:
                raise OwnershipUnknown('Epoch or module changed before stop')
            if s['fault'] or s['faultLatched'] or s['done'] or now() >= deadline:
                break
            pause(limits.interval_seconds)

        # Closing both gates does not cancel an in-flight engine call.
        s = observe('armstop')
        deadline = now() + limits.drain_seconds
        stable = 0
        while True:
            if not process_alive():
                result['targetExited'] = True
                return result
            if s['epoch'] != epoch or s['moduleDisposed']:
                raise OwnershipUnknown('Epoch or module changed while draining')
            if s['fault'] or s['faultLatched']:
                raise OwnershipUnknown('Native transport fault or restore mismatch latched')
            stable = stable + 1 if safe_stopped(s) else 0
            if stable >= 2:
                break
            if now() >= deadline:
                raise OwnershipUnknown('Native call, queued return or callback remains unresolved')
            pause(limits.interval_seconds)
            s = observe()

        # Capture before disposal. A result getter failure also holds the module.
        value = invoke('getresult')
        result['exportResult'] = value
        result['exportAccepted'] = validate_result(value, expected_request)
        released = validate_status(invoke('release', epoch))
        result['releaseReply'] = released
        if released.get('refused') or released['epoch'] != epoch or not released['moduleDisposed'] or not safe_stopped(released):
            raise OwnershipUnknown('Release not confirmed by agent')
        result['releaseConfirmed'] = True
    except Exception as error:
        result['error'] = dict(type=type(error).__name__, message=str(error))
        result['requiresProcessExit'] = not result['releaseConfirmed']
    return result
