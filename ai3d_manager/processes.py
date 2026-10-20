import os
import subprocess
import sys
from pathlib import Path
from .common import ROOT, ProductionError

def run_process(command,directory,timeout=300,db=None,attempt=None,label='process'):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    env=os.environ.copy();env['PYTHONUTF8']='1';env['PYTHONIOENCODING']='utf-8'
    flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    with (directory/(label+'.stdout.log')).open('w',encoding='utf-8') as stdout, (directory/(label+'.stderr.log')).open('w',encoding='utf-8') as stderr:
        try:
            process=subprocess.Popen([str(x) for x in command],cwd=ROOT,stdout=stdout,stderr=stderr,env=env,creationflags=flags)
        except OSError as exc:raise ProductionError('BlenderExecution' if 'blender' in str(command[0]).lower() else 'ExportFailure',str(exc)) from exc
        if db and attempt:db.attempt_update(attempt['id'],process_id=process.pid)
        try:code=process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            # Only the process tree launched for this attempt may be terminated.
            import psutil
            try:
                owned=psutil.Process(process.pid)
                for child in owned.children(recursive=True):child.kill()
                owned.kill()
            except psutil.NoSuchProcess:pass
            process.wait()
            raise ProductionError('BlenderExecution','Owned subprocess timed out; logs preserved.') from exc
    if code:
        category='BlenderExecution' if 'blender' in str(command[0]).lower() else 'ExportFailure'
        tail=(directory/(label+'.stderr.log')).read_text('utf-8',errors='replace')[-3500:]
        output=(directory/(label+'.stdout.log')).read_text('utf-8',errors='replace')[-3500:]
        raise ProductionError(category,f'Exit code {code}: {tail}\n{output}')
    return code
