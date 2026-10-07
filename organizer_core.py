"""Local-only inventory, Git coverage, verified sandbox copy and reversible moves."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

STATE = '.organizer-state'
OUTPUT = '_정리결과'
SKIP_CONTENT = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', 'dist', 'build', '.next'}
GROUPS = ['프로젝트', '학습', '문서', '이미지·미디어', '보관', '검토 필요']


def linked(path: Path) -> bool:
    s = path.lstat()
    return stat.S_ISLNK(s.st_mode) or bool(getattr(s, 'st_file_attributes', 0) & 1024)


def safe_path(root: Path, relative: str, must_exist=True) -> Path:
    if not relative or Path(relative).is_absolute() or re.search(r'[:*?"<>|]', relative):
        raise ValueError('상대 경로만 사용할 수 있습니다.')
    parts = relative.replace('\\', '/').split('/')
    if any(p in ('', '.', '..') or p.rstrip(' .') != p for p in parts):
        raise ValueError('잘못된 경로입니다.')
    if any(re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', p, re.I) for p in parts):
        raise ValueError('Windows 예약 이름입니다.')
    root = root.absolute()
    if linked(root):
        raise ValueError('선택한 폴더 자체가 링크입니다.')
    current = root
    for part in parts:
        current = current / part
        if os.path.lexists(current) and linked(current):
            raise ValueError('링크·정션 경로는 변경할 수 없습니다.')
    if not current.resolve().is_relative_to(root.resolve()):
        raise ValueError('선택한 폴더 밖 경로입니다.')
    if must_exist and not current.exists():
        raise ValueError('대상이 없어졌습니다. 다시 조사하세요.')
    return current


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + '.' + uuid.uuid4().hex + '.tmp')
    with tmp.open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def register_folder(root: Path):
    """Register the selected directory in place; never copy or move its contents."""
    root = Path(root).absolute()
    if not root.is_dir():
        raise ValueError('일반 폴더를 선택하세요.')
    for parent in (root, *root.parents):
        if linked(parent):
            raise ValueError('링크·정션 경로는 사용할 수 없습니다.')
    manifest = safe_path(root, STATE + '/manifest.json', False)
    if manifest.exists():
        return read_manifest(root)
    meta = root / STATE
    if meta.exists():
        raise ValueError('기존 관리 폴더에 등록 정보가 없습니다. 기존 기록을 보존하고 확인하세요.')
    meta.mkdir()
    data = {'version': 2, 'mode': 'in_place', 'destination': str(root),
            'status': 'ready', 'issues': [], 'created': time.time()}
    write_json(manifest, data)
    return data


def read_manifest(root: Path):
    path = safe_path(root, STATE + '/manifest.json')
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('status') not in ('ready', 'ready_with_skips'):
        raise ValueError('아직 준비되지 않은 폴더입니다. 등록 정보를 확인하세요.')
    if Path(data['destination']).resolve() != root.resolve():
        raise ValueError('등록된 폴더 위치와 다릅니다.')
    if data.get('version') == 2 and data.get('mode') == 'in_place':
        return data
    if data.get('version') != 1:
        raise ValueError('지원하지 않는 폴더 등록 형식입니다.')
    source = Path(data['source']).resolve()
    if root.resolve().is_relative_to(source) or source.is_relative_to(root.resolve()):
        raise ValueError('원본과 시험 공간은 서로 독립된 경로여야 합니다.')
    return data


def note_identity(root: Path, relative: str):
    """Stable for same-volume rename, including moves of an item's parent."""
    path = safe_path(root, relative)
    if any(part.casefold() in (STATE.casefold(), '.git') for part in Path(relative).parts):
        raise ValueError('관리 폴더에는 메모를 작성할 수 없습니다.')
    s = path.stat()
    if not s.st_ino:
        raise ValueError('이 파일시스템에서는 항목 메모 식별을 지원하지 않습니다.')
    birth = getattr(s, 'st_birthtime_ns', s.st_ctime_ns if os.name == 'nt' else '')
    return hashlib.sha256(f'{s.st_dev}:{s.st_ino}:{birth}'.encode()).hexdigest()


def read_notes(root: Path):
    read_manifest(root)
    path = safe_path(root, STATE + '/notes.json', False)
    if not path.exists(): return {}
    if path.stat().st_size > 8_000_000:
        raise ValueError('메모 저장소 크기 한도를 초과했습니다.')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or any(not re.fullmatch('[a-f0-9]{64}', k) or
                not isinstance(v, str) or len(v) > 2000 for k, v in data.items()):
            raise ValueError('메모 저장소 형식 오류')
        return data
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('메모 저장소를 읽을 수 없습니다. 기존 파일을 보존하고 복구하세요.') from exc


def save_note(root: Path, relative: str, text: str):
    if not isinstance(text, str) or len(text) > 2000:
        raise ValueError('메모는 2,000자까지 작성할 수 있습니다.')
    notes = read_notes(root); identity = note_identity(root, relative)
    if text.strip(): notes[identity] = text.strip()
    else: notes.pop(identity, None)
    if len(json.dumps(notes, ensure_ascii=False).encode('utf-8')) > 8_000_000:
        raise ValueError('메모 저장소 크기 한도를 초과했습니다.')
    write_json(safe_path(root, STATE + '/notes.json', False), notes)
    return notes


def file_hash(path: Path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def copy_io_path(path: Path) -> Path:
    """Extended paths for copying; keep ordinary paths in manifests and UI."""
    if os.name == 'nt':
        value = str(path.absolute())
        if not value.startswith('\\\\?\\'):
            return Path('\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value)
    return path


def _copy_one(task):
    src, dst, rel, resume = task
    src, dst = copy_io_path(src), copy_io_path(dst)
    try:
        if linked(src): raise OSError('source_link')
        before = src.stat()
        h = hashlib.sha256()
        if dst.exists():
            if not resume or linked(dst) or not dst.is_file(): raise OSError('existing_destination')
            with src.open('rb') as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b''): h.update(chunk)
        else:
            with src.open('rb') as f, dst.open('xb') as out:
                for chunk in iter(lambda: f.read(1024 * 1024), b''):
                    out.write(chunk); h.update(chunk)
        after = src.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns): raise OSError('source_changed')
        if file_hash(dst) != h.hexdigest(): raise OSError('hash_mismatch')
        shutil.copystat(src, dst, follow_symlinks=False)
        return {'path': rel, 'size': before.st_size, 'sha256': h.hexdigest()}
    except OSError:
        return {'path': rel, 'reason': '복사/검증 실패 또는 원본 변경'}


def copy_sandbox(source: Path, destination: Path, progress=lambda text: None, cancel=None, exclude=(), resume=False):
    """No overwrites, no link traversal; per-file verification, not a live-volume snapshot."""
    source, destination = source.absolute(), destination.absolute()
    if not source.is_dir() or linked(source):
        raise ValueError('일반 원본 폴더를 선택하세요.')
    if destination.resolve().is_relative_to(source.resolve()) or source.resolve().is_relative_to(destination.resolve()):
        raise ValueError('원본 밖의 독립된 복사 위치를 선택하세요.')
    for p in (destination, *destination.parents):
        if p.exists() and linked(p):
            raise ValueError('복사 목적지에 링크·정션이 포함되어 있습니다.')
    if destination.exists() and any(destination.iterdir()):
        if not resume:
            raise ValueError('복사 위치는 비어 있어야 합니다. 기존 파일을 덮어쓰지 않습니다.')
        old = json.loads((destination / STATE / 'manifest.json').read_text(encoding='utf-8'))
        if Path(old['source']).resolve() != source.resolve() or old['status'] not in ('copying', 'incomplete'):
            raise ValueError('동일 원본의 중단된 복사만 재개할 수 있습니다.')
    if (source / STATE).exists() or (source / OUTPUT).exists():
        raise ValueError('원본에 앱 예약 폴더가 있습니다. 다른 범위를 선택하세요.')
    destination.mkdir(parents=True, exist_ok=True)
    meta = destination / STATE
    meta.mkdir(exist_ok=resume)
    manifest = {'version': 1, 'source': str(source), 'destination': str(destination),
                'status': 'copying', 'copied_files': 0, 'copied_bytes': 0, 'issues': [],
                'excluded': list(exclude), 'verification': 'SHA-256 per copied file; not an atomic snapshot', 'created': time.time()}
    write_json(meta / 'manifest.json', manifest)
    ledger = (meta / 'copy-files.jsonl').open('w', encoding='utf-8')
    stack = [source]
    last_update = time.monotonic()
    pending = []
    pool = ThreadPoolExecutor(max_workers=12)
    def flush():
        nonlocal last_update
        required = sum(copy_io_path(t[0]).stat().st_size for t in pending if not copy_io_path(t[1]).exists())
        if required + 2 * 1024**3 > shutil.disk_usage(destination).free:
            raise ValueError('여유 공간 2GB를 확보할 수 없어 복사를 중단했습니다.')
        for row in pool.map(_copy_one, pending):
            if 'reason' in row:
                manifest['issues'].append(row)
            else:
                ledger.write(json.dumps(row, ensure_ascii=False) + '\n')
                manifest['copied_files'] += 1
                manifest['copied_bytes'] += row['size']
        pending.clear()
        if time.monotonic() - last_update > 5:
            progress(f"{manifest['copied_files']:,}개 / {manifest['copied_bytes']/1024**3:.2f}GB 복사·검증")
            write_json(meta / 'manifest.json', manifest)
            ledger.flush()
            last_update = time.monotonic()
    try:
        while stack:
            directory = stack.pop()
            if cancel and cancel.is_set():
                raise InterruptedError('복사가 취소되었습니다. 부분 복사본은 남겨두었습니다.')
            try:
                with os.scandir(copy_io_path(directory)) as it:
                    entries = list(it)
            except OSError:
                manifest['issues'].append({'path': directory.relative_to(source).as_posix(), 'reason': '읽기 불가'})
                continue
            for entry in entries:
                src = directory / entry.name
                rel = src.relative_to(source).as_posix()
                dst = destination / rel
                if any(rel == x or rel.startswith(x + '/') for x in exclude):
                    manifest['issues'].append({'path': rel, 'reason': '사용자 지정 제외'})
                    continue
                if cancel and cancel.is_set():
                    raise InterruptedError('복사가 취소되었습니다.')
                try:
                    if linked(copy_io_path(src)):
                        manifest['issues'].append({'path': rel, 'reason': '링크·정션 제외'})
                        continue
                    if copy_io_path(src).is_dir():
                        if copy_io_path(dst).exists() and linked(copy_io_path(dst)): raise OSError('destination_link')
                        copy_io_path(dst).mkdir(exist_ok=resume)
                        stack.append(src)
                        continue
                    if not copy_io_path(src).is_file():
                        manifest['issues'].append({'path': rel, 'reason': '일반 파일 아님'})
                        continue
                    pending.append((src, dst, rel, resume))
                    if len(pending) >= 128: flush()
                except OSError:
                    manifest['issues'].append({'path': rel, 'reason': '복사/검증 실패 또는 원본 변경'})
        flush()
        manifest['status'] = 'ready_with_skips' if manifest['issues'] else 'ready'
    except BaseException:
        manifest['status'] = 'incomplete'
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        ledger.close()
        write_json(meta / 'manifest.json', manifest)
    return manifest


def inventory(root: Path):
    read_manifest(root)
    items = []
    def visit(folder, depth):
        try:
            entries = sorted(folder.iterdir(), key=lambda p: p.name.casefold())
        except OSError:
            return
        for path in entries:
            if path.name in (STATE, OUTPUT, 'desktop.ini'):
                continue
            rel = path.relative_to(root).as_posix()
            try:
                if linked(path):
                    continue
                project = path.is_dir() and any((path / marker).exists() for marker in ('.git', 'package.json', 'pyproject.toml', 'Cargo.toml', 'go.mod', 'requirements.txt'))
                # ponytail: descend two category levels; a project remains one intact unit.
                if path.is_dir() and not project and depth < 2:
                    children = list(path.iterdir())
                    if children and any(p.is_dir() for p in children):
                        visit(path, depth + 1)
                        continue
                items.append({'relative': rel, 'name': path.name, 'kind': '프로젝트' if project else ('폴더' if path.is_dir() else '파일'), 'group': '프로젝트' if project else ('문서' if path.suffix.lower() in ('.pdf', '.docx', '.doc', '.txt', '.xlsx', '.pptx') else '검토 필요')})
            except OSError:
                items.append({'relative': rel, 'name': path.name, 'kind': '읽기 불가', 'group': '검토 필요'})
    visit(root, 0)
    return items


def git_report(path: Path):
    dot = path / '.git'
    if not dot.exists():
        return {'present': False, 'movable': True}
    report = {'present': True, 'movable': False}
    if not dot.is_dir() or linked(dot):
        return {**report, 'error': 'worktree/서브모듈/연결 Git 디렉터리입니다. 독립된 저장소인지 확인해야 합니다.'}
    for rel in ('commondir', 'objects/info/alternates', 'worktrees', 'modules'):
        if (dot / rel).exists():
            return {**report, 'error': '외부 객체·worktree·서브모듈 연결이 있어 첫 버전에서는 이동을 보류합니다.'}
    # Do not honor GIT_DIR, config includes, external worktree settings, or fsmonitor commands.
    try:
        config = (dot / 'config').read_text(encoding='utf-8', errors='replace')
        if re.search(r'(?im)^\s*worktree\s*=|^\s*bare\s*=\s*(?:true|yes|1)\s*$|^\s*\[include', config):
            return {**report, 'error': '외부 작업 경로나 include 설정이 있어 Git 실행을 보류합니다.'}
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_OPTIONAL_LOCKS='0', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_TERMINAL_PROMPT='0', GIT_NO_LAZY_FETCH='1')
        def git(*args, allow_fail=False):
            p = subprocess.run(['git', '--no-optional-locks', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=' + os.devnull,
                                '-c', 'core.quotePath=false', '-C', str(path), *args], capture_output=True, timeout=45,
                               env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if p.returncode and not allow_fail:
                raise ValueError('Git 상태 확인 실패')
            return p.stdout.decode('utf-8', errors='replace').strip('\n') if not p.returncode else ''
        if Path(git('rev-parse', '--show-toplevel')).resolve() != path.resolve():
            raise ValueError('다른 작업 경로가 연결되어 있습니다.')
        cached = {s for s in git('ls-files', '-z').split('\0') if s}
        head = {s for s in git('ls-tree', '-r', '--name-only', '-z', 'HEAD', allow_fail=True).split('\0') if s}
        records = git('status', '--porcelain=v1', '-z', '--untracked-files=all', '--ignored=matching').split('\0')
        staged, unstaged, untracked, ignored, changed = [], [], [], [], set()
        i = 0
        while i < len(records):
            row = records[i]; i += 1
            if not row:
                continue
            code, name = row[:2], row[3:]
            if code == '??': untracked.append(name)
            elif code == '!!': ignored.append(name)
            else:
                changed.add(name)
                if code[0] != ' ': staged.append(name)
                if code[1] != ' ': unstaged.append(name)
                if 'R' in code or 'C' in code:
                    if i < len(records): changed.add(records[i])
                    i += 1
        ignored_files = [s for s in git('ls-files', '--others', '--ignored', '--exclude-standard', '-z').split('\0') if s]
        upstream = git('rev-parse', '--abbrev-ref', '@{upstream}', allow_fail=True)
        divergence = git('rev-list', '--left-right', '--count', 'HEAD...@{upstream}', allow_fail=True) if upstream else ''
        stage_rows = git('ls-files', '--stage').splitlines()
        flag_rows = [s for s in git('ls-files', '-v', '-z').split('\0') if s]
        assumed = sum(s[0].islower() for s in flag_rows)
        sparse = sum(s[0].upper() == 'S' for s in flag_rows)
        submodule = any(s.startswith('160000 ') for s in stage_rows)
        report.update(movable=not submodule, tracked_count=len(cached), head_count=len(head),
                      unchanged_committed_count=len((cached & head) - changed),
                      staged=staged, unstaged=unstaged, untracked=untracked, ignored=ignored_files,
                      head=git('rev-parse', '--short', 'HEAD', allow_fail=True) or '커밋 없음',
                      branch=git('branch', '--show-current') or 'detached HEAD',
                      assume_unchanged_count=assumed, skip_worktree_count=sparse,
                      remote_comparison=divergence or '비교 불가 (upstream/추적 참조 없음)',
                      remote_note='fetch하지 않은 로컬 추적 참조 기준입니다. 실제 원격 백업 여부는 미확인입니다.',
                      note='변경 없음은 Git 상태 기준이며 HEAD 내용과 바이트를 전수 대조한 결과가 아닙니다. assume-unchanged/skip-worktree는 변경 표시를 숨길 수 있습니다. 미커밋 변경·미추적·무시 파일은 HEAD만으로 복구할 수 없습니다. LFS 외부 객체, 빈 폴더, 앱 설정과 모든 OS 메타데이터의 백업을 보장하지 않습니다.')
        if submodule:
            report['error'] = '서브모듈이 있어 이동을 보류합니다.'
        return report
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {**report, 'error': 'Git 확인 실패 또는 시간 초과입니다. 이동을 보류합니다.'}


def inspect_item(root: Path, relative: str):
    manifest = read_manifest(root)
    path = safe_path(root, relative)
    issues = [x for x in manifest.get('issues', []) if x['path'] == relative or x['path'].startswith(relative + '/') or relative.startswith(x['path'] + '/') or x['path'] == '.']
    git = git_report(path) if path.is_dir() else {'present': False, 'movable': True}
    samples, excerpts, warnings = [], [], []
    count = size = 0
    if issues:
        warnings.append(f'누락/검증 실패 {len(issues)}건: 이 항목 이동 보류')
    work = [path]
    inspected = 0
    while work and inspected < 12000:
        p = work.pop(); inspected += 1
        try:
            if linked(p):
                warnings.append('링크·정션 포함: 이동 보류'); continue
            if p.is_dir():
                if p.name in SKIP_CONTENT and p != path:
                    continue
                if p != path and (p / '.git').exists():
                    warnings.append('중첩 Git 저장소 포함: 이동 보류')
                work.extend(sorted(p.iterdir(), key=lambda q: q.name, reverse=True))
                continue
            if not p.is_file(): continue
            count += 1; size += p.stat().st_size
            local = p.relative_to(path).as_posix() if path.is_dir() else p.name
            sensitive_name = re.search(r'(?i)(^\.env|secret|credential|token|private|password|\.pem$|\.key$)', p.name)
            if not sensitive_name and len(samples) < 100:
                samples.append(local)
            # Content is opt-in in the UI; only small explanatory text is collected here.
            if not sensitive_name and p.name.lower() in ('readme.md', 'readme.txt', 'package.json', 'pyproject.toml', 'requirements.txt', 'cargo.toml', 'go.mod') and len(excerpts) < 4:
                if p.stat().st_size <= 100_000:
                    text = p.read_text(encoding='utf-8', errors='replace')[:2400]
                    text = re.sub(r'(?im)^.*(?:api[_ -]?key|secret|password|token\s*[:=]|-----BEGIN).*$', '[민감정보 후보 줄 제외]', text)
                    excerpts.append({'id': 'E' + str(len(excerpts) + 1), 'file': local, 'text': text})
        except OSError:
            warnings.append('읽을 수 없는 항목 있음: 이동 보류')
    if work: warnings.append('조사 항목 한도 초과: 일부 정보만 표시')
    return {'relative': relative, 'name': path.name, 'sample_files': samples, 'evidence': excerpts,
            'scanned_files': count, 'scanned_bytes': size, 'git': git, 'warnings': sorted(set(warnings)), 'copy_issues': issues}


def fingerprint(path: Path):
    """Hash the entire unit before a move; refuse links and unreadable files."""
    h = hashlib.sha256()
    if path.is_file():
        return file_hash(path)
    for parent, dirs, files in os.walk(path, followlinks=False, onerror=lambda e: (_ for _ in ()).throw(e)):
        dirs.sort(); files.sort()
        for name in dirs + files:
            p = Path(parent) / name
            if linked(p): raise ValueError('링크·정션이 있는 폴더는 이동하지 않습니다.')
            h.update(p.relative_to(path).as_posix().encode('utf-8'))
            if p.is_file(): h.update(file_hash(p).encode('ascii'))
    return h.hexdigest()


def move_item(root: Path, relative: str, group: str, git_ack=False, expected_fingerprint=None):
    if group not in GROUPS:
        raise ValueError('허용된 분류를 선택하세요.')
    read_manifest(root)
    if relative.split('/')[0] in (STATE, OUTPUT):
        raise ValueError('앱 관리 폴더는 이동할 수 없습니다.')
    src = safe_path(root, relative)
    destination = f'{OUTPUT}/{group}/{src.name}'
    dst = safe_path(root, destination, False)
    if dst.exists(): raise ValueError('같은 이름의 대상이 이미 있습니다. 덮어쓰지 않습니다.')
    report = inspect_item(root, relative)
    import relocation
    if relocation.project_root(dst.parent) or relocation.project_ancestor(root, dst.parent):
        raise ValueError('개발 프로젝트 내부를 정리 목적지로 사용할 수 없습니다.')
    readiness = relocation.assess(root, relative, report)
    if readiness['status'] != 'ready':
        raise ValueError(relocation.describe(readiness))
    if report['warnings'] or not report['git']['movable']:
        raise ValueError('누락·연결 구조·조사 한도 등 확인할 사항이 있어 이동을 보류합니다.')
    # Prevent moving only a piece of a containing Git repository.
    if any((p / '.git').exists() for p in src.parents if p == root or p.is_relative_to(root)):
        raise ValueError('상위 Git 저장소의 일부입니다. 저장소 전체 단위로 다뤄야 합니다.')
    if report['git']['present'] and not git_ack:
        raise ValueError('Git 상태를 확인한 뒤 폴더 이동을 직접 선택하세요.')
    before = fingerprint(src)
    if expected_fingerprint is not None and before != expected_fingerprint:
        raise ValueError('확인 화면 이후 내용이 바뀌었습니다. 다시 검토하세요.')
    record = {'id': uuid.uuid4().hex, 'source': relative, 'destination': destination,
              'fingerprint': before, 'status': 'pending', 'time': time.time()}
    journal = safe_path(root, STATE + '/moves/' + record['id'] + '.json', False)
    write_json(journal, record)
    dst.parent.mkdir(parents=True, exist_ok=True)
    # Recheck links and destination immediately before Windows' no-overwrite rename.
    safe_path(root, relative); safe_path(root, destination, False)
    if dst.exists(): raise ValueError('실행 직전에 목적지가 생겼습니다.')
    if relocation.project_root(dst.parent) or relocation.project_ancestor(root, dst.parent):
        raise ValueError('목적지의 개발 프로젝트 경계가 바뀌었습니다.')
    try:
        os.rename(src, dst)
        record['status'] = 'moved'
        if fingerprint(dst) != before:
            record['status'] = 'changed_after_move'
        write_json(journal, record)
    except BaseException:
        # pending stays on disk if the actual outcome cannot be recorded.
        raise
    return record


def history(root: Path):
    read_manifest(root)
    folder = safe_path(root, STATE + '/moves', False)
    if not folder.exists(): return []
    records = []
    for path in sorted(folder.glob('*.json'), reverse=True):
        if linked(path): raise ValueError('작업 기록에 링크가 있습니다.')
        records.append(json.loads(path.read_text(encoding='utf-8')))
    return sorted(records, key=lambda x: x['time'], reverse=True)


def undo_item(root: Path, record_id: str):
    read_manifest(root)
    if not re.fullmatch('[a-f0-9]{32}', record_id): raise ValueError('잘못된 기록 ID')
    journal = safe_path(root, STATE + '/moves/' + record_id + '.json')
    record = json.loads(journal.read_text(encoding='utf-8'))
    if record['status'] not in ('moved', 'pending'):
        raise ValueError('현재 상태에서는 자동 되돌리기를 할 수 없습니다.')
    if not record['destination'].startswith(OUTPUT + '/') or record['source'].split('/')[0] in (STATE, OUTPUT):
        raise ValueError('작업 기록의 경로가 잘못되었습니다.')
    src = safe_path(root, record['source'], False)
    dst = safe_path(root, record['destination'])
    if src.exists(): raise ValueError('원래 위치에 파일이 있습니다. 덮어쓰지 않습니다.')
    if fingerprint(dst) != record['fingerprint']:
        raise ValueError('이동 후 내용이 바뀌었습니다. 자동 되돌리기를 보류합니다.')
    src.parent.mkdir(parents=True, exist_ok=True)
    safe_path(root, record['source'], False)
    os.rename(dst, src)
    record['status'] = 'undone'
    write_json(journal, record)
    return record


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--copy', nargs=2, metavar=('SOURCE', 'DESTINATION'))
    parser.add_argument('--exclude', action='append', default=[])
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    if args.copy:
        result = copy_sandbox(*map(Path, args.copy), progress=lambda s: print(s, flush=True), exclude=args.exclude, resume=args.resume)
        print(json.dumps({k: result[k] for k in ('status', 'copied_files', 'copied_bytes')}, ensure_ascii=False))
        print('issues:', len(result['issues']))
