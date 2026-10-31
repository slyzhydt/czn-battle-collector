"""Build a Windows x64 portable Python bundle from installed, licensed local files."""
from pathlib import Path
import hashlib
import importlib.metadata
import json
import shutil
import sys
import zipfile

HERE=Path(__file__).resolve().parent


def build(destination, frida_root=None):
    if sys.platform!='win32' or sys.version_info[:2]!=(3,11):
        raise RuntimeError('Build requires a complete Windows Python 3.11 installation')
    if importlib.metadata.version('psutil')!='7.1.3':
        raise RuntimeError('Build requires psutil 7.1.3')
    if frida_root is None:
        import frida
        if frida.__version__!='17.23.2':raise RuntimeError('Build requires Frida 17.23.2')
        frida_root=Path(frida.__file__).resolve().parent.parent
    if destination.exists():raise FileExistsError('Use a new release directory')
    destination.mkdir(parents=True)
    app=destination/'battle-collector';app.mkdir()
    for name in ['collector.py','native_host.py','domain.py','export_supervisor.py','intake.py','profile.json','agent-template.js','使用说明.txt','LICENSE']:
        shutil.copy2(HERE/name,app/name)
    runtime=app/'runtime';runtime.mkdir()
    pyhome=Path(sys.executable).parent
    for name in ['python.exe','python3.dll','python311.dll','vcruntime140.dll','vcruntime140_1.dll','LICENSE.txt']:
        shutil.copy2(pyhome/name,runtime/name)
    dlls=runtime/'DLLs';dlls.mkdir()
    for source in (pyhome/'DLLs').iterdir():
        if source.suffix.lower() in ('.dll','.pyd') and source.name not in ('_tkinter.pyd','tcl86t.dll','tk86t.dll'):
            shutil.copy2(source,dlls/source.name)
    # A real isolated Python runtime, not a dependency on the donor's absolute paths.
    with zipfile.ZipFile(runtime/'python311.zip','x',zipfile.ZIP_DEFLATED)as archive:
        for source in (pyhome/'Lib').rglob('*.py'):
            rel=source.relative_to(pyhome/'Lib')
            if any(p in ('site-packages','__pycache__','test','tests','idlelib','tkinter','turtledemo','ensurepip') for p in rel.parts):continue
            archive.write(source,rel.as_posix())
    (runtime/'python311._pth').write_text('python311.zip\nDLLs\n..\n../vendor\n',encoding='ascii')
    vendor=app/'vendor';vendor.mkdir()
    frida=frida_root
    for name in ('frida','frida-17.23.2.dist-info'):
        shutil.copytree(frida/name,vendor/name,ignore=shutil.ignore_patterns('__pycache__'))
    dist=importlib.metadata.distribution('psutil')
    shutil.copytree(dist.locate_file('psutil'),vendor/'psutil',ignore=shutil.ignore_patterns('__pycache__','tests'))
    shutil.copytree(dist._path,vendor/dist._path.name)
    command='@echo off\r\nchcp 65001 >nul\r\n"%~dp0runtime\\python.exe" -B -X utf8 "%~dp0collector.py" %*\r\npause\r\n'
    (app/'启动采集.cmd').write_bytes(command.encode('utf-8'))
    shutil.copy2(HERE/'THIRD_PARTY.txt',app/'THIRD_PARTY.txt')
    manifest={p.relative_to(app).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()for p in app.rglob('*')if p.is_file()}
    (destination/'build-manifest.json').write_text(json.dumps({'files':manifest,'sourcePython':sys.version,'gameAttached':False},indent=2)+'\n',encoding='utf8')
    print(json.dumps({'directory':str(app),'files':len(manifest),'bytes':sum(p.stat().st_size for p in app.rglob('*')if p.is_file())}))
    return app


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('destination',type=Path)
    parser.add_argument('--frida-root',type=Path,help='Optional existing directory containing frida and its dist-info')
    args=parser.parse_args()
    build(args.destination.resolve(),args.frida_root.resolve() if args.frida_root else None)
