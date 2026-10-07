"""Editable in-memory plans; explicit review, no shell or deletion operations."""
import copy
import os
from pathlib import Path
import re
import time
import uuid
import organizer_core as core
import relocation

HOLD = '제거확인'


def key(path):
    return path.replace('\\', '/').casefold()


def within(child, parent):
    return key(child) == key(parent) or key(child).startswith(key(parent) + '/')


def checked(root, relative, exists=True):
    p = core.safe_path(root, relative, exists)
    if any(x.casefold() in (core.STATE.casefold(), '.git') for x in relative.replace('\\', '/').split('/')):
        raise ValueError('관리 폴더는 편집할 수 없습니다.')
    return p


def git_ancestor(root, path, include_self=False):
    parts = [path, *path.parents] if include_self else path.parents
    for p in parts:
        if os.path.lexists(p / '.git') and not core.approved_external_git(root, p):
            return p
    return None


class Draft:
    def __init__(self, root):
        self.root = Path(root)
        core.read_manifest(self.root)
        self.notes = core.read_notes(self.root)
        self.nodes = {'root': {'id': 'root', 'source': '', 'name': self.root.name, 'parent': None, 'folder': True}}
        self.loaded = set()
        self.edits = {}
        self.past = []
        self.future = []
        self.version = 0
        self.batch = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
        self.expand('root')

    def expand(self, node_id):
        if node_id in self.loaded: return
        n = self.nodes[node_id]
        if n['source'] is None:
            self.loaded.add(node_id); return
        folder = self.root if node_id == 'root' else core.safe_path(self.root, n['source'])
        if not folder.is_dir(): return
        entries = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
        # Avoid silently omitting siblings: refuse a huge level rather than invent an incomplete tree.
        if len(entries) > 10000: raise ValueError('한 폴더에 항목이 10,000개를 넘습니다. 더 작은 작업 범위를 선택하세요.')
        for p in entries:
            if p.name.casefold() in (core.STATE.casefold(), '.git'): continue
            rel = p.relative_to(self.root).as_posix()
            self.nodes.setdefault(rel, {'id': rel, 'source': rel, 'name': p.name, 'parent': node_id,
                                        'folder': p.is_dir(), 'linked': core.linked(p)})
        self.loaded.add(node_id)

    def state(self, node_id):
        return {**self.nodes[node_id], **self.edits.get(node_id, {})}

    def note(self, node_id):
        source = self.nodes[node_id]['source']
        if not source: return ''
        return self.notes.get(core.note_identity(self.root, source), '')

    def set_note(self, node_id, text):
        source = self.nodes[node_id]['source']
        if not source: raise ValueError('실제로 존재하는 파일이나 폴더를 선택하세요. 새 폴더는 생성 후 메모를 작성할 수 있습니다.')
        self.notes = core.save_note(self.root, source, text)
        self.version += 1

    def rebase(self, completed):
        """Keep held drafts, but never undo a completed physical move as a draft edit."""
        for record in completed:
            node_id = record['node_id']
            if record['kind'] == 'move':
                source, dest = record['source'], record['destination']
                for n in self.nodes.values():
                    if n['source'] and within(n['source'], source):
                        n['source'] = dest + n['source'][len(source):]
            else:
                self.nodes[node_id]['source'] = record['destination']
            edit = self.edits.pop(node_id, {})
            self.nodes[node_id].update({k: v for k, v in edit.items() if k != 'by'})
        self.past.clear(); self.future.clear(); self.version += 1

    def path(self, node_id, planned=True, seen=None):
        if node_id == 'root': return ''
        seen = set() if seen is None else seen
        if node_id in seen: raise ValueError('폴더 순환 구조는 만들 수 없습니다.')
        seen.add(node_id)
        n = self.state(node_id) if planned else self.nodes[node_id]
        prefix = self.path(n['parent'], planned, seen)
        return (prefix + '/' if prefix else '') + n['name']

    def checkpoint(self):
        self.past.append((copy.deepcopy(self.edits), copy.deepcopy(self.nodes)))
        self.future.clear()
        self.version += 1

    def undo(self, redo=False):
        src, dst = (self.future, self.past) if redo else (self.past, self.future)
        if not src: return False
        dst.append((copy.deepcopy(self.edits), copy.deepcopy(self.nodes)))
        self.edits, self.nodes = src.pop()
        self.loaded.intersection_update(self.nodes)
        # Re-expanding is safe and restores nodes discovered since the checkpoint.
        self.loaded.clear()
        self.version += 1
        return True

    def validate_edit(self, node_id, parent, name):
        if node_id == 'root': raise ValueError('작업 폴더 자체는 옮길 수 없습니다.')
        if parent not in self.nodes or not self.state(parent)['folder']: raise ValueError('이동할 폴더를 선택하세요.')
        if '/' in name or '\\' in name: raise ValueError('이름에는 경로 구분자를 사용할 수 없습니다.')
        checked(self.root, name, False)
        n = self.nodes[node_id]
        if n.get('linked'): raise ValueError('링크·정션은 변경할 수 없습니다.')
        if n['source']:
            if within(n['source'], HOLD): raise ValueError('제거확인 안의 항목은 작업 기록에서 되돌리세요.')
            if git_ancestor(self.root, checked(self.root, n['source'])):
                raise ValueError('Git 프로젝트 내부 파일은 개별 변경할 수 없습니다. 프로젝트 전체를 선택하세요.')
            if relocation.project_ancestor(self.root, checked(self.root, n['source'])):
                raise ValueError('개발 프로젝트 내부 항목은 전체 프로젝트와 함께 옮기세요. 내부 구조 변경은 별도 검증이 필요합니다.')
        p = parent
        while p is not None:
            if p == node_id: raise ValueError('자기 자신이나 하위 폴더로 옮길 수 없습니다.')
            p = self.state(p)['parent']
        target = self.root / self.path(parent)
        if git_ancestor(self.root, target, True): raise ValueError('Git 프로젝트 내부를 목적지로 사용할 수 없습니다.')
        if relocation.project_root(target) or relocation.project_ancestor(self.root, target):
            raise ValueError('개발 프로젝트 내부를 정리 목적지로 사용할 수 없습니다.')
        self.expand(parent)
        for other in self.nodes:
            if other != node_id and self.state(other)['parent'] == parent and key(self.state(other)['name']) == key(name):
                raise ValueError('같은 이름이 있습니다. 덮어쓰지 않습니다.')

    def edit(self, node_id, parent=None, name=None, by='직접'):
        n = self.state(node_id)
        parent = n['parent'] if parent is None else parent
        name = n['name'] if name is None else name.strip()
        self.validate_edit(node_id, parent, name)
        self.checkpoint()
        self.edits[node_id] = {'parent': parent, 'name': name, 'by': by}

    def mkdir(self, parent, name, by='직접'):
        node_id = '@' + uuid.uuid4().hex
        self.nodes[node_id] = {'id': node_id, 'source': None, 'name': name, 'parent': parent, 'folder': True}
        try: self.validate_edit(node_id, parent, name)
        except Exception:
            self.nodes.pop(node_id); raise
        self.nodes.pop(node_id)
        self.checkpoint()
        self.nodes[node_id] = {'id': node_id, 'source': None, 'name': name, 'parent': parent, 'folder': True}
        self.edits[node_id] = {'parent': parent, 'name': name, 'by': by}
        return node_id

    def keep(self, node_id, by='직접'):
        n = self.nodes[node_id]
        if not n['source']: raise ValueError('기존 하위 항목만 유지할 수 있습니다. 새 폴더는 수정 취소로 되돌리세요.')
        # Keeping an existing location cancels a draft edit; it does not move a file.
        self.checkpoint()
        self.edits[node_id] = {'parent': n['parent'], 'name': n['name'], 'by': by}

    def quarantine(self, node_id, by='직접'):
        n = self.nodes[node_id]
        if not n['source']: raise ValueError('기존 항목을 선택하세요.')
        original = copy.deepcopy((self.nodes, self.edits, self.past, self.future, self.version))
        try:
            parent = 'root'
            for name in [HOLD, self.batch, *Path(n['source']).parts[:-1]]:
                self.expand(parent)
                found = next((i for i in self.nodes if self.state(i)['parent'] == parent and key(self.state(i)['name']) == key(name)), None)
                parent = found or self.mkdir(parent, name, by)
            self.edit(node_id, parent, n['name'], by)
        except Exception:
            self.nodes, self.edits, self.past, self.future, self.version = original
            raise

    def operations(self):
        ops = []
        for node_id, n in self.nodes.items():
            if node_id == 'root': continue
            dest = self.path(node_id)
            # Child paths follow parent moves; do not produce duplicate physical operations.
            if n['source'] is None:
                ops.append({'kind': 'mkdir', 'source': None, 'destination': dest, 'id': node_id})
            elif node_id in self.edits and (self.state(node_id)['parent'] != n['parent'] or self.state(node_id)['name'] != n['name']):
                ops.append({'kind': 'move', 'source': n['source'], 'destination': dest, 'id': node_id})
        return sorted(ops, key=lambda o: (o['kind'] != 'mkdir', o['destination'].count('/')))


def prepare(root, operations, allow_path_warnings=False):
    root = Path(root); core.read_manifest(root)
    if not operations: raise ValueError('적용할 변경이 없습니다.')
    if len(operations) > 100: raise ValueError('한 번에 100개 작업까지 검토할 수 있습니다.')
    prepared = copy.deepcopy(operations)
    destinations = set(); sources = [o['source'] for o in prepared if o['kind'] == 'move']
    newdirs = {key(o['destination']) for o in prepared if o['kind'] == 'mkdir'}
    for i, a in enumerate(sources):
        for b in sources[i+1:]:
            if within(a, b) or within(b, a): raise ValueError('상위 폴더와 내부 항목을 동시에 변경할 수 없습니다. 한 단계씩 적용하세요.')
    for op in prepared:
        op['external_git_consent'] = copy.deepcopy(core.external_git_consent(root))
        if op['kind'] not in ('mkdir', 'move'): raise ValueError('허용되지 않은 작업입니다.')
        dest = op['destination']; dst = checked(root, dest, False)
        if key(dest) in destinations or os.path.lexists(dst): raise ValueError('목적지 이름이 충돌합니다. 덮어쓰지 않습니다.')
        destinations.add(key(dest))
        if any(within(dest, s) for s in sources): raise ValueError('이동할 폴더 내부를 목적지로 사용할 수 없습니다. 한 단계씩 적용하세요.')
        if git_ancestor(root, dst.parent, True): raise ValueError('Git 내부를 목적지로 사용할 수 없습니다.')
        if relocation.project_root(dst.parent) or relocation.project_ancestor(root, dst.parent):
            raise ValueError('개발 프로젝트 내부를 정리 목적지로 사용할 수 없습니다.')
        for parent in dst.parents:
            if parent == root: break
            rel = parent.relative_to(root).as_posix()
            if not parent.exists() and key(rel) not in newdirs: raise ValueError('목적지 상위 폴더가 없습니다.')
            if parent.exists() and not parent.is_dir(): raise ValueError('목적지 상위 항목이 파일입니다.')
        if op['kind'] == 'mkdir': continue
        source = op['source']; src = checked(root, source)
        if within(source, HOLD): raise ValueError('제거확인 항목은 작업 기록으로 되돌리세요.')
        if git_ancestor(root, src): raise ValueError('Git 내부 파일의 개별 이동은 허용하지 않습니다.')
        report = core.inspect_item(root, source)
        readiness = relocation.assess(root, source, report)
        path_only=bool(readiness['findings']) and all(
            f['code'] in ('absolute_path','relative_path') and not f['unknown'] for f in readiness['findings'])
        if readiness['status'] != 'ready' and not (allow_path_warnings and path_only):
            raise ValueError(source + ': ' + relocation.describe(readiness))
        op['fingerprint'] = core.fingerprint(src)
        op['git'] = report['git']
        op['readiness'] = readiness
    return prepared


def review(root, operations):
    """Assess independently, then validate the accepted plan as a whole."""
    if not operations: raise ValueError('적용할 변경이 없습니다.')
    if len(operations) > 100: raise ValueError('한 번에 100개 작업까지 검토할 수 있습니다.')
    directories = [op for op in operations if op['kind'] == 'mkdir']
    accepted, held = [], []
    for op in operations:
        dependencies = [d for d in directories if d is not op and within(op['destination'], d['destination'])]
        try:
            prepare(root, dependencies + [op],allow_path_warnings=True)
            accepted.append(op)
        except (ValueError, OSError) as exc:
            held.append({'operation': op, 'guide': str(exc)})
    # A folder created solely for a held move can wait with that move.
    held_paths = [h['operation']['destination'] for h in held]
    move_paths = [o['destination'] for o in accepted if o['kind'] == 'move']
    accepted = [o for o in accepted if o['kind'] != 'mkdir' or
                not any(within(p, o['destination']) for p in held_paths) or
                any(within(p, o['destination']) for p in move_paths)]
    if accepted:
        try: accepted = prepare(root, accepted,allow_path_warnings=True)
        except (ValueError, OSError) as exc:
            held.extend({'operation': op, 'guide': str(exc)} for op in accepted)
            accepted = []
    return {'prepared': accepted, 'held': held, 'scope': relocation.SCOPE}


def execute(root, prepared, path_warnings_confirmed=False):
    root = Path(root)
    fresh = prepare(root, prepared,allow_path_warnings=path_warnings_confirmed)
    for old, new in zip(prepared, fresh):
        if (old.get('fingerprint') != new.get('fingerprint') or old.get('git') != new.get('git')
                or old.get('readiness') != new.get('readiness')
                or old.get('external_git_consent') != new.get('external_git_consent')):
            raise ValueError('확인 이후 내용 또는 Git 상태가 바뀌었습니다. 다시 검토하세요.')
    completed = []
    for op in fresh:
        try:
            if op.get('external_git_consent') != core.external_git_consent(root):
                raise ValueError('상위 Git 승인 상태가 바뀌었습니다. 다시 검토하세요.')
            dst = checked(root, op['destination'], False)
            if os.path.lexists(dst): raise ValueError('실행 직전에 목적지가 생겼습니다.')
            if git_ancestor(root, dst.parent, True): raise ValueError('목적지 Git 상태가 달라졌습니다.')
            if relocation.project_root(dst.parent) or relocation.project_ancestor(root, dst.parent):
                raise ValueError('목적지에 개발 프로젝트가 생겼습니다. 다시 검토하세요.')
            record = {**op, 'node_id': op.get('id'), 'id': uuid.uuid4().hex, 'version': 2, 'status': 'pending', 'time': time.time()}
            record['path_warnings_confirmed']=bool(path_warnings_confirmed and op.get('readiness',{}).get('findings'))
            journal = core.safe_path(root, core.STATE + '/moves/' + record['id'] + '.json', False)
            core.write_json(journal, record)
            if op['kind'] == 'mkdir':
                dst.mkdir(); record['status'] = 'created'
            else:
                src = checked(root, op['source'])
                if git_ancestor(root, src) or core.fingerprint(src) != op['fingerprint']:
                    raise ValueError('실행 직전에 항목이 바뀌었습니다.')
                os.rename(src, dst)
                record['status'] = 'moved' if core.fingerprint(dst) == op['fingerprint'] else 'changed_after_move'
            core.write_json(journal, record); completed.append(record)
            if record['status'] == 'changed_after_move': raise ValueError('이동 후 내용 확인이 필요합니다.')
        except (OSError, ValueError) as exc:
            return {'completed': completed, 'error': str(exc), 'remaining': len(fresh)-len(completed)}
    return {'completed': completed, 'error': None, 'remaining': 0}


def undo_record(root, record_id):
    root = Path(root); core.read_manifest(root)
    if not re.fullmatch('[a-f0-9]{32}', record_id): raise ValueError('기록 ID 오류')
    import json
    journal = core.safe_path(root, core.STATE + '/moves/' + record_id + '.json')
    record = json.loads(journal.read_text(encoding='utf-8'))
    if record.get('version') != 2: return core.undo_item(root, record_id)
    if record.get('kind') != 'move' or record['status'] not in ('moved', 'pending'):
        raise ValueError('이 기록은 자동 되돌릴 수 없습니다. 생성한 빈 폴더는 남겨 둡니다.')
    src = checked(root, record['source'], False); dst = checked(root, record['destination'])
    if src.exists() or not src.parent.is_dir(): raise ValueError('원래 위치가 없거나 충돌합니다.')
    if git_ancestor(root, src) or git_ancestor(root, dst): raise ValueError('Git 경계가 바뀌었습니다.')
    if relocation.project_ancestor(root, src) or relocation.project_ancestor(root, dst):
        raise ValueError('개발 프로젝트 경계가 바뀌었습니다. 복구 위치를 확인하세요.')
    if core.fingerprint(dst) != record['fingerprint']: raise ValueError('내용이 바뀌어 자동 복구를 보류합니다.')
    os.rename(dst, src); record['status'] = 'undone'; core.write_json(journal, record)
    return record
