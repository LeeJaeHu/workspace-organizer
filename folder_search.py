"""Read-only recursive name search, streaming bounded results without following links."""
import os
import time
import stat
import organizer_core as core


def search(root, query, cancel, emit, limit=10000):
    stack=[root]; hits=[]; count=visited=skipped=0; last=0
    while stack and not cancel.is_set():
        parent=stack.pop()
        try:
            with os.scandir(core.copy_io_path(parent)) as entries:
                for entry in entries:
                    if cancel.is_set(): break
                    if entry.name.casefold() in (core.STATE.casefold(), '.git'): continue
                    path=parent/entry.name
                    try:
                        metadata=entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata,'st_file_attributes',0)&1024):
                            skipped+=1;continue
                        folder=entry.is_dir(follow_symlinks=False)
                        visited+=1
                        if query.casefold() in entry.name.casefold():
                            count+=1
                            if len(hits)<limit: hits.append((path.relative_to(root).as_posix(),folder))
                        if folder: stack.append(path)
                    except OSError: skipped+=1
                    if time.monotonic()-last>.25:
                        emit(hits[:],count,visited,skipped,False);last=time.monotonic()
        except OSError: skipped+=1
    emit(hits[:],count,visited,skipped,not cancel.is_set())
