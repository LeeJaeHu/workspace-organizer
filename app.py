"""폴더착착 desktop entry point."""
import argparse
import os
from pathlib import Path
import tempfile
import organizer_core as core
from desktop import Organizer


def make_demo(folder):
    source = folder / 'source'; source.mkdir()
    project = source / 'tmp' / 'chat-practice'; project.mkdir(parents=True)
    (project / 'README.md').write_text('# 채팅 실습\n채팅 메시지 표시와 로그인 화면을 연습하는 프로젝트입니다.', encoding='utf-8')
    (project / 'package.json').write_text('{"name":"chat-practice","scripts":{"dev":"vite"}}', encoding='utf-8')
    (source / 'lecture-notes.txt').write_text('파이썬 수업 노트', encoding='utf-8')
    dest = folder / 'sandbox'; core.copy_sandbox(source, dest)
    return dest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='C:/test')
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--smoke-test', action='store_true')
    args = parser.parse_args()
    if os.name == 'nt':
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    temporary = tempfile.TemporaryDirectory(prefix='organizer-ui-') if args.demo or args.smoke_test else None
    initial = make_demo(Path(temporary.name)) if temporary else Path(args.root)
    app = Organizer(initial,credential_path=Path(temporary.name)/'credentials.bin' if args.smoke_test else None)
    if args.smoke_test:
        def check():
            assert app.draft and len(app.draft.nodes) >= 3, 'demo tree failed'
            assert len(app.keys) == 3
            app.destroy()
        app.after(2500, check)
    app.mainloop()
    if temporary: temporary.cleanup()
