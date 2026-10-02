#!/usr/bin/python3
"""Exercise the actual opened-PCM guard and parser under ASan/UBSan."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile

REPOSITORY=Path(__file__).resolve().parents[2]
PATCH=REPOSITORY/'install/patches/owntone-29.3-pcm-identity.patch'


def image(name,after=True):
    block=PATCH.read_text().split(f'diff --git a/{name} b/{name}\n',1)[1].split('\ndiff --git ',1)[0]
    lines,active=[],False
    for line in block.splitlines():
        if line.startswith('@@'):
            active=True
            lines.append('')
        elif active and line.startswith(('+' if after else '-',' ')):
            lines.append(line[1:])
    return '\n'.join(lines)+'\n'


def function(source,name):
    found=re.search(rf'(?m)^{name}\([^\n]*\)\n\{{',source)
    if found is None:
        raise ValueError(f'Missing actual function {name}')
    start=source.rfind('\n',0,found.start()-1)+1
    end=source.index('{',found.start())+1
    depth=1
    while depth:
        depth+=(source[end]=='{')-(source[end]=='}')
        end+=1
    result=source[start:end]+'\n'
    if result.lstrip().startswith(name+'('):
        result='static int\n'+result
    return result


def run_tests(*,source=None,compiler=None,sanitize=True):
    compiler=compiler or shutil.which('clang') or shutil.which('cc')
    if compiler is None:
        raise RuntimeError('A C compiler is required for PCM hardware identity tests')
    found=subprocess.run(['pkg-config','--cflags','--libs','json-c'],capture_output=True,text=True,timeout=10)
    if found.returncode:
        raise RuntimeError('PCM hardware identity tests require libjson-c development files and pkg-config')
    libs=shlex.split(found.stdout)
    flags=['-std=c11','-D_GNU_SOURCE','-Wall','-Wextra','-Werror','-Wno-unused-function','-O1']
    if sanitize:
        flags+=['-fsanitize=address,undefined','-fno-sanitize-recover=all']
    environment={**os.environ,'ASAN_OPTIONS':'detect_leaks=0:halt_on_error=1','UBSAN_OPTIONS':'halt_on_error=1'}
    fixture=(REPOSITORY/'tests/native/test_pcm_identity.c').read_text()
    with tempfile.TemporaryDirectory(prefix='shiri-pcm-identity-') as temporary:
        directory=Path(temporary)
        header=(source/'src/outputs/pcm_identity.h').read_text() if source else image('src/outputs/pcm_identity.h')
        patched=(source/'src/outputs/alsa.c').read_text() if source else image('src/outputs/alsa.c')
        (directory/'pcm_identity.h').write_text(header)
        unit=directory/'check.c'
        target=directory/'check'
        results=[]
        for before in (False,True):
            body=function(image('src/outputs/alsa.c',False) if before else patched,'pcm_open')
            unit.write_text(fixture.replace('/* @ACTUAL_PCM_OPEN@ */',body))
            subprocess.run([compiler,*flags,*(['-DPREIMAGE'] if before else []),'-I',str(directory),str(unit),*libs,'-o',str(target)],check=True,capture_output=True,text=True,timeout=30)
            result=subprocess.run([str(target)],env=environment,capture_output=True,text=True,timeout=30,cwd=directory)
            if before:
                if result.returncode!=1 or 'wrong physical PCM' not in result.stderr:
                    raise RuntimeError('Actual preimage wrong-PCM identity defect was not reproduced')
            else:
                result.check_returncode()
                results.append(result.stdout.strip())
    return {'ok':True,'sanitized':sanitize,'upstream_wrong_pcm_reproduced':True,'results':results}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path)
    parser.add_argument('--compiler')
    parser.add_argument('--no-sanitizers',action='store_true')
    args=parser.parse_args()
    try:
        print(json.dumps(run_tests(source=args.source,compiler=args.compiler,sanitize=not args.no_sanitizers),indent=2))
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f'PCM identity sanitizer check failed: {exc.stderr or exc.stdout}') from None


if __name__=='__main__':
    main()
