"""Bundle a UCRT64 build, recursively resolving PE imports without system DLLs."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--ucrt-root', type=Path, default=Path('C:/msys64/ucrt64'))
    parser.add_argument('--objdump', default='C:/msys64/ucrt64/bin/objdump.exe')
    args = parser.parse_args()
    output = ROOT / 'dist' / 'ai-rdp-windows-x86_64'
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    for name in ('ai_rdp', 'native', 'tests', 'scripts', 'examples'):
        shutil.copytree(ROOT / name, output / name,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in ('README.md', 'VALIDATION.md', 'CHANGELOG.md', 'LICENSE', 'CMakeLists.txt', 'requirements.txt', 'requirements.lock'):
        shutil.copy2(ROOT / name, output / name)
    binary = output / 'bin' / 'windows-x86_64'
    binary.mkdir(parents=True)
    search = [ROOT / 'build' / 'deps' / 'bin', args.ucrt_root / 'bin']
    libraries = {p.name.lower(): p for directory in reversed(search) for p in directory.glob('*.dll')}
    system = Path('C:/Windows/System32')
    pending = [ROOT / 'build' / 'native' / 'ai-rdp-native.exe']
    # OpenSSL loads providers dynamically, so they do not appear in PE imports.
    provider_source = args.ucrt_root / 'lib' / 'ossl-modules'
    if not (provider_source / 'legacy.dll').is_file():
        raise RuntimeError('OpenSSL legacy provider is missing')
    provider_output = binary / 'ossl-modules'
    provider_output.mkdir()
    for provider in provider_source.glob('*.dll'):
        shutil.copy2(provider, provider_output / provider.name)
    copied = set()
    while pending:
        source = pending.pop()
        name = source.name.lower()
        if name in copied:
            continue
        copied.add(name)
        shutil.copy2(source, binary / source.name)
        pe = subprocess.check_output([args.objdump, '-p', str(source)], text=True, encoding='utf-8')
        for dependency in re.findall(r'DLL Name:\s*(\S+)', pe):
            lower = dependency.lower()
            if lower in libraries:
                pending.append(libraries[lower])
            elif lower.startswith(('api-ms-win-', 'ext-ms-win-')) or (system / dependency).is_file():
                continue
            else:
                raise RuntimeError(f'Unresolved runtime DLL: {dependency} (required by {source.name})')
    notices = output / 'THIRD_PARTY_LICENSES'
    notices.mkdir()
    for name in ('LICENSE', 'NOTICE'):
        src = ROOT / 'third_party' / 'FreeRDP' / name
        if src.is_file():
            shutil.copy2(src, notices / f'FreeRDP-{name}')
    # Include the toolchain package license texts, including runtime exceptions.
    shutil.copytree(args.ucrt_root / 'share' / 'licenses', notices / 'MSYS2-UCRT64')
    (notices / 'SOURCES.txt').write_text(
        'FreeRDP 3.32.2: https://github.com/FreeRDP/FreeRDP/tree/97eff6ddfbbfaa8e42a126d362686bd001ff8bdc\n'
        'Runtime packages: https://packages.msys2.org/ (UCRT64)\n'
        'GCC sources and runtime exception: https://gcc.gnu.org/\n'
        'OpenSSL: https://www.openssl.org/\n', encoding='utf-8')
    manifest = {
        'platform': 'windows-x86_64',
        'freerdp_commit': '97eff6ddfbbfaa8e42a126d362686bd001ff8bdc',
        'compiler': subprocess.check_output([str(args.ucrt_root / 'bin' / 'g++.exe'), '--version'],
                                            text=True, encoding='utf-8').splitlines()[0],
        'sha256': {p.relative_to(binary).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in sorted(binary.rglob('*')) if p.is_file()},
    }
    (output / 'build-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    archive = output.with_suffix('.zip')
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as zip:
        for path in sorted(output.rglob('*')):
            if path.is_file():
                zip.write(path, path.relative_to(output.parent))
    print(archive)
    print('SHA256:', hashlib.sha256(archive.read_bytes()).hexdigest())


if __name__ == '__main__':
    main()
