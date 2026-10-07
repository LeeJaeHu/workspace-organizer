"""Bounded, read-only relocation checks. Findings are not an execution warranty."""
import os
from pathlib import Path
import re

MARKERS = {'.git', 'pyproject.toml', 'requirements.txt', 'package.json', 'Cargo.toml',
           'go.mod', 'CMakeLists.txt', 'pom.xml', 'build.gradle', 'pyvenv.cfg'}
GENERATED = {'.venv', 'venv', 'node_modules', '.next', 'dist', 'build', '__pycache__'}
CONFIG_SUFFIXES = {'.json', '.toml', '.yaml', '.yml', '.ini', '.cfg', '.code-workspace',
                   '.py', '.js', '.ts', '.sh', '.ps1', '.bat', '.cmd', '.csproj', '.sln', '.props'}
SCOPE = ('검사 범위: 선택 항목 내부의 Git·환경 폴더·일부 설정/소스 경로 단서. '
         '외부에서 들어오는 참조, 전역 Git/IDE/예약 작업 설정과 정상 실행은 미확인입니다.')


def project_root(path):
    return path.is_dir() and any(os.path.lexists(path / name) for name in MARKERS)


def effective_markers(root, path):
    import organizer_core as core
    markers = sorted(name for name in MARKERS if os.path.lexists(path / name))
    if '.git' in markers and core.approved_external_git(root, path):
        markers.remove('.git')
    return markers


def project_ancestor(root, path):
    for parent in path.parents:
        if effective_markers(root, parent):
            return parent
    return None


def boundary_info(root, path, include_self=False):
    """Explain a detected boundary without disclosing absolute paths to the model."""
    root, path = Path(root), Path(path)
    for parent in ([path, *path.parents] if include_self else path.parents):
        markers = effective_markers(root, parent)
        if not markers: continue
        inside = parent.is_relative_to(root)
        relative = parent.relative_to(root).as_posix() if inside else '(선택 폴더 상위)/' + parent.name
        node_id = ('root' if parent == root else relative) if inside else None
        return {'boundary': relative, 'id': node_id, 'markers': markers,
                'reason': 'Git 내부 개별 변경 제한' if '.git' in markers else '개발 프로젝트 내부 변경 제한',
                'next_step': ('채팅의 상위 Git 예외 승인 버튼에서 영향을 확인하세요. 사용자 승인 후 선택 폴더 안에서 정리할 수 있습니다. 다른 프로젝트 표시 파일에 따른 제한은 남을 수 있습니다.'
                              if not inside and markers == ['.git'] else
                              '프로젝트 내부는 유지하고 이 프로젝트 폴더 전체를 외부 분류 폴더로 이동하는 안을 검토하세요.'
                              if inside and parent != root else
                              '현재 선택 범위 전체가 프로젝트 경계 안입니다. 외부 목적지를 만들려면 프로젝트의 상위 폴더를 열어 프로젝트 전체를 선택해야 합니다. 경계 표시 파일이 잘못 놓인 것인지 사용자가 확인할 수도 있습니다. 앱이 Git/설정 파일을 삭제하거나 이동해 제한을 우회하지 않습니다.')}
    return None


def assess(root, relative, report=None):
    # Import here so both execution entry points share the same check without a cycle.
    import organizer_core as core
    root = Path(root)
    findings = []
    def add(code, path, reason, guide, unknown=False):
        if not any(f['code'] == code and f['path'] == path for f in findings):
            findings.append(dict(code=code, path=path, reason=reason, guide=guide, unknown=unknown))
    try:
        path = core.safe_path(root, relative)
        if project_ancestor(root, path):
            add('project_piece', relative, '개발 프로젝트 내부 항목입니다.',
                '프로젝트 전체를 선택하세요. 내부 구조 변경은 프로젝트에서 별도로 검증하세요.')
        report = report or core.inspect_item(root, relative)
        for warning in report['warnings']:
            add('inspection', relative, warning, '누락·접근 실패·조사 한도를 해결한 뒤 다시 검사하세요.', True)
        if not report['git']['movable']:
            add('git', relative, report['git'].get('error') or 'Git 구조를 확인할 수 없습니다.',
                'worktree/서브모듈/외부 Git 연결을 확인하고 Git 전용 이전 절차를 준비하세요. 자동 수정하지 않습니다.', True)
        work = [path]; count = total = 0
        while work:
            p = work.pop(); count += 1
            if count > 12000:
                add('limit', relative, '검사 항목 수 한도에 도달했습니다.', '더 작은 단위로 선택해 다시 검사하세요.', True)
                break
            local = p.relative_to(root).as_posix()
            if core.linked(p):
                add('link', local, '링크·정션이 있습니다.', '연결 대상을 확인하고 별도 이전 계획을 세우세요.', True)
                continue
            if p.is_dir():
                if p.name == '.git':
                    continue
                if p.name in GENERATED:
                    if p.name in {'.venv', 'venv', 'node_modules'}:
                        add('environment', local, '경로에 의존할 수 있는 실행 환경/설치 폴더가 있습니다.',
                            '의존성·런타임 버전을 확보하고 새 위치에서 환경을 재생성·실행 검증하세요. 현재 앱은 자동 재설치하지 않습니다.')
                    continue
                entries = list(p.iterdir())
                if count + len(work) + len(entries) > 12000:
                    add('limit', local, '검사 항목 수 한도에 도달했습니다.', '더 작은 단위로 선택해 다시 검사하세요.', True)
                    break
                work.extend(sorted(entries, key=lambda q: q.name, reverse=True)); continue
            if p.name == 'pyvenv.cfg':
                add('environment', local, 'Python 가상환경 설정이 있습니다.', '새 위치에서 가상환경을 재생성하고 실행을 확인하세요.')
            if p.suffix.lower() not in CONFIG_SUFFIXES and p.name not in {'.env', '.gitmodules', 'Makefile', 'CMakeLists.txt'}:
                continue
            size = p.stat().st_size
            if size > 128000 or total + size > 4000000:
                add('text_limit', local, '설정/소스 검사 크기 한도를 초과했습니다.', '해당 파일의 경로 참조를 별도로 검토하세요.', True)
                continue
            total += size
            try:
                content = p.read_text(encoding='utf-8-sig')
            except UnicodeError:
                add('encoding', local, '텍스트 인코딩을 확인할 수 없습니다.', '해당 파일을 직접 열어 경로 참조를 확인하세요.', True)
                continue
            # Only report the file name and finding type, never config values or secrets.
            if re.search(r'(?i)[a-z]:[\\/]|\\\\[^\s\\]+\\|(?:[\s\x22\x27=]|^)/(?:home|Users|opt|var|mnt|etc)/', content):
                add('absolute_path', local, '절대 경로로 보이는 설정/소스 단서가 있습니다.',
                    '이전 위치·공유 자료·외부 실행 경로인지 확인하고 필요한 경로를 수정한 뒤 재검사하세요. 주석/예시일 수도 있습니다.')
            if re.search(r'\.\.[\\/]|[\x22\x27](?:file:|link:)', content):
                add('relative_path', local, '상위 폴더 또는 로컬 패키지 참조 단서가 있습니다.',
                    '연결된 폴더와 상대 위치를 보존하는 계획을 검토하세요. 이 버전은 해당 참조의 유효성을 자동 증명하지 않습니다.')
    except (ValueError, OSError) as exc:
        add('unverified', relative, '경로/내용 검사에 실패했습니다.',
            '대상 존재·권한·링크 여부를 확인한 뒤 다시 검사하세요.', True)
    status = 'unknown' if any(f['unknown'] for f in findings) else ('preparation' if findings else 'ready')
    return {'status': status, 'findings': findings, 'scope': SCOPE, 'execution_verified': False}


def describe(result):
    labels = {'ready': '검사 통과(확인 범위)', 'preparation': '준비 필요', 'unknown': '확인 불가'}
    lines = [labels[result['status']]]
    for f in result['findings']:
        lines.append(f"{f['path']}: {f['reason']}\n준비 방법: {f['guide']}")
    lines.append(result['scope'])
    return '\n'.join(lines)
