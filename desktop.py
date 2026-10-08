"""Three-pane native UI. Chat asks; the local executor enforces boundaries."""
import copy
import json
from pathlib import Path
import queue
import threading
import folder_search
import tkinter as tk
from tkinter import ttk, filedialog
import ai_providers as ai
import chat_planner as chat
import folder_agent
import organizer_core as core
import plan_engine as plans
import relocation
import credential_store


class Organizer(tk.Tk):
    def __init__(self, initial=None, credential_path=None):
        super().__init__()
        self.title('폴더착착')
        self.geometry(f'{min(1220,self.winfo_screenwidth()-50)}x{min(820,self.winfo_screenheight()-90)}')
        self.minsize(900, 600)
        self.configure(bg='white'); self.option_add('*Font', ('맑은 고딕', 10))
        style = ttk.Style(self); style.theme_use('clam')
        style.configure('.', background='white', font=('맑은 고딕', 10))
        style.configure('Treeview', background='white', fieldbackground='white', rowheight=30)
        style.configure('TButton', padding=(9, 5))
        style.configure('Approve.TButton', background='#7048c6', foreground='white')
        self.draft = None; self.root_path = None; self.selected = []; self.busy = False
        self.jobs = queue.Queue(); self.conversation = []; self.confirmations = []; self.naming = None
        self.ai_progress=queue.Queue();self.ai_cancel=threading.Event()
        self.drag = None; self.opened = {'root'}; self.provider = tk.StringVar(value='OpenAI')
        self.drag_ghost = None; self.drag_target = None
        self.bind('<Escape>', self.cancel_drag, add='+')
        self.keys = {p: tk.StringVar() for p in ai.MODELS}; self.location = tk.StringVar(value='')
        self.credential_path=credential_path if credential_path is not None else credential_store.default_path()
        self.key_save_job=None;self.dirty_keys=set();key_error=None
        try:
            for provider,key in credential_store.load(self.credential_path).items():self.keys[provider].set(key)
        except ValueError as exc:key_error=str(exc)
        for provider,value in self.keys.items():
            value.trace_add('write',lambda *_,p=provider:self.schedule_key_save(p))
        self.model_ids={p:tk.StringVar(value=m['id']) for p,m in ai.MODELS.items()}
        self.address = tk.StringVar(value='')
        self.status = tk.StringVar(); self.chat_visible = True
        self.search_cancel=threading.Event();self.search_queue=queue.Queue();self.search_generation=0
        self.search_timer=None;self.search_saved=None;self.search_hits=[];self.search_limit=50
        self.search_paths=set();self.search_ancestors=set();self.rendering=False
        head = ttk.Frame(self, padding=(18, 12)); head.pack(fill='x')
        toolbar = head
        ttk.Button(head, text='폴더 열기', command=self.choose_root).pack(side='left', padx=(0, 8))
        self.address_entry = ttk.Entry(head, textvariable=self.address, width=18)
        self.address_entry.pack(side='left', fill='x', expand=True)
        self.address_entry.bind('<Return>', self.navigate_address)
        self.address_entry.bind('<Escape>', lambda e: self.address.set(self.location.get()))
        ttk.Button(head, text='폴더 새로고침', command=self.refresh).pack(side='left', padx=(8, 20))
        ttk.Button(head, text='AI 연결', command=self.settings).pack(side='left', padx=(0, 8))
        ttk.Button(head, text='작업 기록', command=self.history).pack(side='left')
        ttk.Button(head, text='안내', command=self.show_guide).pack(side='left',padx=(8,0))
        self.pane_ratios = (.32, .66)
        self.restore_job = None
        self.panes = ttk.Panedwindow(self, orient='horizontal'); self.panes.pack(fill='both', expand=True, padx=12, pady=(0, 8))
        self.trees = []
        for index, title in enumerate(('현재 폴더', '변경 후')):
            frame = ttk.Frame(self.panes, padding=8); self.panes.add(frame, weight=3)
            bar = ttk.Frame(frame); bar.pack(fill='x', pady=(0, 10))
            ttk.Label(bar, text=title, font=('맑은 고딕', 11, 'bold')).pack(side='left')
            if index:
                self.count = ttk.Label(bar, foreground='#7048c6'); self.count.pack(side='left', padx=8)
                self.redo_button = ttk.Button(bar, text='↷', width=3, state='disabled', command=lambda: self.undo_draft(True))
                self.redo_button.pack(side='right')
                self.undo_button = ttk.Button(bar, text='↶', width=3, state='disabled', command=lambda: self.undo_draft())
                self.undo_button.pack(side='right')
            else:
                self.search = tk.StringVar(); entry = ttk.Entry(frame, textvariable=self.search)
                ttk.Label(frame, text='폴더·파일 이름 검색', foreground='#677287').pack(anchor='w')
                entry.pack(fill='x', pady=(0, 8)); self.search.trace_add('write', lambda *_: self.search_changed())
                self.search_bar=ttk.Frame(frame)
                self.search_info=ttk.Label(self.search_bar,wraplength=260,foreground='#677287');self.search_info.pack(fill='x')
                self.more_button=ttk.Button(self.search_bar,text='50개 더 보기',command=self.more_results)
                self.more_button.pack(side='left')
                self.stop_button=ttk.Button(self.search_bar,text='검색 중지',command=self.stop_search)
                self.stop_button.pack(side='left')
                self.search_bar.pack(fill='x',pady=(0,6));self.search_bar.pack_forget()
            tree = ttk.Treeview(frame, show='tree', selectmode='extended', columns=('note','by'))
            tree.column('note', width=44, stretch=False, anchor='center')
            tree.column('#0', width=250); tree.column('by', width=45, stretch=False, anchor='e')
            y = ttk.Scrollbar(frame, command=tree.yview); y.pack(side='right', fill='y')
            tree.configure(yscrollcommand=y.set); tree.pack(fill='both', expand=True)
            tree.tag_configure('AI', background='#eee7ff', foreground='#543098')
            tree.tag_configure('직접', foreground='#19745b')
            tree.tag_configure('하위 변경', background='#eee7ff', foreground='#543098')
            tree.tag_configure('검색', background='#fff1bd',foreground='#483300')
            tree.tag_configure('drop_target', background='#dbeafe',foreground='#174580')
            tree.bind('<<TreeviewOpen>>', lambda e, i=index: self.expand(i))
            tree.bind('<<TreeviewClose>>', lambda e, t=tree: self.opened.discard(t.focus()))
            tree.bind('<<TreeviewSelect>>', lambda e, i=index: self.select(i))
            tree.bind('<ButtonPress-1>', lambda e, i=index: self.start_drag(e, i), add='+')
            tree.bind('<ButtonRelease-1>', self.drop, add='+')
            tree.bind('<B1-Motion>', self.drag_motion, add='+')
            tree.bind('<Button-3>', self.context_menu)
            tree.bind('<Shift-F10>', self.context_menu)
            self.trees.append(tree)
        self.chat_frame = ttk.Frame(self.panes, padding=10); self.panes.add(self.chat_frame, weight=3)
        top = ttk.Frame(self.chat_frame); top.pack(fill='x')
        ttk.Label(top, text='AI와 수정하기', font=('맑은 고딕', 11, 'bold')).pack(side='left')
        ttk.Button(top, text='›', width=3, command=self.toggle_chat).pack(side='right')
        self.show_chat = ttk.Button(toolbar, text='‹ AI 채팅', command=self.toggle_chat)
        composer = ttk.Frame(self.chat_frame); composer.pack(side='bottom', fill='x', pady=(8, 0))
        self.target = ttk.Label(composer, foreground='#677287', wraplength=260); self.target.pack(anchor='w')
        self.input = tk.Text(composer, height=3, wrap='word', relief='solid', borderwidth=1, font=('맑은 고딕', 10))
        self.input.pack(fill='x'); self.input.bind('<Return>', self.enter)
        ttk.Button(composer, text='전송', width=7, command=self.send).pack(anchor='e', pady=4)
        self.canvas = tk.Canvas(self.chat_frame, bg='white', highlightthickness=0)
        self.chat_follow=True;self.scroll_job=None
        scrollbar = ttk.Scrollbar(self.chat_frame, command=self.scroll_chat); scrollbar.pack(side='right', fill='y')
        self.canvas.configure(yscrollcommand=scrollbar.set); self.canvas.pack(fill='both', expand=True)
        self.messages = ttk.Frame(self.canvas); self.window = self.canvas.create_window((0, 0), window=self.messages, anchor='nw')
        self.messages.bind('<Configure>', self.chat_content_resized)
        self.canvas.bind('<Configure>', self.resize_chat)
        self.bind_all('<MouseWheel>', self.wheel, add='+')
        ttk.Label(self, textvariable=self.status, foreground='#677287').pack(anchor='w', padx=20, pady=(0, 6))
        self.bind('<Control-z>', lambda e: self.undo_draft() if e.widget not in (self.input,) else None)
        self.bind('<Control-y>', lambda e: self.undo_draft(True) if e.widget not in (self.input,) else None)
        self.protocol('WM_DELETE_WINDOW', self.close); self.poll_job = self.after(100, self.poll)
        self.balance_job = self.after(350, self.balance_panes)
        self.message('폴더 열기에서 정리할 폴더를 선택하세요. 선택한 폴더에서 드래그나 AI 채팅으로 정리안을 만듭니다. 적용을 확인하기 전에는 자료를 이동하지 않습니다.',actions=[('폴더 열기',self.choose_root,False)])
        if initial: self.after(100, lambda: self.load(Path(initial)))
        if key_error:self.after_idle(lambda error=key_error:self.message(error))

    def schedule_key_save(self,provider):
        self.dirty_keys.add(provider)
        if self.key_save_job is not None:self.after_cancel(self.key_save_job)
        self.key_save_job=self.after(500,self.save_keys)

    def save_keys(self):
        if self.key_save_job is not None:
            self.after_cancel(self.key_save_job);self.key_save_job=None
        if not self.dirty_keys:return True
        try:
            credential_store.save(self.credential_path,{p:self.keys[p].get() for p in self.dirty_keys})
            self.dirty_keys.clear();return True
        except (ValueError,OSError):
            self.message('API 키 저장에 실패했습니다. 입력한 키는 이번 실행에서만 사용할 수 있습니다. 저장 위치와 Windows 계정 권한을 확인하세요.')
            return False

    def balance_panes(self):
        if self.chat_visible:
            self.panes.update_idletasks()
            width=self.panes.winfo_width()
            if width > 10:
                self.panes.sashpos(0,int(width*self.pane_ratios[0]))
                self.panes.sashpos(1,int(width*self.pane_ratios[1]))
        self.restore_job = None

    def resize_chat(self, event):
        self.canvas.itemconfigure(self.window, width=event.width)
        for frame in self.messages.winfo_children():
            for widget in frame.winfo_children():
                if isinstance(widget, ttk.Label): widget.configure(wraplength=max(150, event.width-22))

    def wheel(self, e):
        widget = e.widget
        while widget:
            if widget == self.chat_frame:
                self.scroll_chat('scroll',-int(e.delta/120)*3,'units'); return 'break'
            widget = getattr(widget, 'master', None)

    def scroll_chat(self,*args):
        self.canvas.yview(*args)
        self.chat_follow=self.canvas.yview()[1]>=.995

    def chat_content_resized(self, event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox('all'))
        if self.chat_follow:self.canvas.yview_moveto(1)

    def finish_chat_scroll(self):
        # Labels and buttons must finish sizing before the new scroll extent is used.
        self.canvas.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox('all'))
        if self.chat_follow:self.canvas.yview_moveto(1)
        self.scroll_job=None

    def message(self, text, user=False, actions=()):
        if not self.chat_visible: self.toggle_chat()
        if self.scroll_job is None:self.chat_follow=self.canvas.yview()[1]>=.995
        frame = ttk.Frame(self.messages, padding=(6, 8)); frame.pack(fill='x')
        ttk.Label(frame, text='나' if user else '폴더착착', foreground='#677287').pack(anchor='w')
        ttk.Label(frame, text=text, wraplength=max(210, self.canvas.winfo_width()-25), justify='left').pack(fill='x', anchor='w')
        for label, callback, approval in actions:
            button = ttk.Button(frame, text=label, command=callback, style='Approve.TButton' if approval else 'TButton')
            button.pack(anchor='w', pady=4)
            if approval: self.confirmations.append(button)
        if getattr(self,'scroll_job',None):self.after_cancel(self.scroll_job)
        self.scroll_job=self.after_idle(self.finish_chat_scroll)
        return frame

    def invalidate(self):
        for b in self.confirmations:
            if b.winfo_exists(): b.configure(state='disabled')
        self.confirmations.clear()

    def job(self, text, fn, done):
        if self.busy: return
        self.busy = True; self.status.set(text)
        def run():
            try: self.jobs.put((done, fn(), None))
            except Exception as exc:
                error = str(exc) if isinstance(exc, (ValueError, OSError)) else '작업 처리 중 오류가 발생했습니다. 다시 확인해 주세요.'
                self.jobs.put((None, None, error))
        threading.Thread(target=run, daemon=True).start()

    def poll(self):
        try:
            while True:self.status.set(self.ai_progress.get_nowait())
        except queue.Empty:pass
        try:
            while True:
                done, value, error = self.jobs.get_nowait(); self.busy = False; self.status.set('')
                if error: self.message(error)
                else:
                    try: done(value)
                    except (ValueError, OSError) as exc: self.message(str(exc))
        except queue.Empty: pass
        if not self.busy:
            latest=None
            try:
                while True:
                    event=self.search_queue.get_nowait()
                    if event[0]==self.search_generation: latest=event
            except queue.Empty: pass
            if latest:
                _,hits,count,visited,skipped,done=latest
                self.search_hits=hits
                self.search_info.configure(text=f'{count:,}개 결과 · {min(len(hits),self.search_limit)}개 표시'+
                    (f' · 검색 중 ({visited:,}개 확인)' if not done else ' · 완료')+
                    (f' · 접근/링크 제외 {skipped}개' if skipped else '')+
                    (' · 앞 10,000개만 보관' if count>len(hits) else ''))
                self.stop_button.configure(state='disabled' if done else 'normal')
                self.materialize_results()
        self.poll_job = self.after(100, self.poll)

    def stop_search(self):
        self.search_cancel.set();self.search_generation+=1
        self.stop_button.configure(state='disabled')
        self.search_info.configure(text=f'검색 중지 · 현재 {len(self.search_hits):,}개 결과')

    def search_changed(self):
        self.search_cancel.set();self.search_generation+=1
        if self.search_timer is not None:self.after_cancel(self.search_timer);self.search_timer=None
        if not self.draft:return
        query=self.search.get().strip()
        if not query:
            self.search_bar.pack_forget();self.search_paths=set();self.search_ancestors=set()
            if self.search_saved:
                self.opened,self.selected,views=self.search_saved;self.search_saved=None
                self.render()
                for tree,pos in zip(self.trees,views):tree.yview_moveto(pos)
            else:self.render()
            return
        if self.search_saved is None:
            self.search_saved=(self.opened.copy(),self.selected[:],[t.yview()[0] for t in self.trees])
        self.search_hits=[];self.search_limit=50;self.search_paths={'root'};self.search_ancestors={'root'}
        self.search_bar.pack(fill='x',pady=(0,6),before=self.trees[0])
        self.search_info.configure(text='검색 준비 중…');self.more_button.configure(state='disabled')
        self.render()
        self.search_timer=self.after(300,self.begin_search)

    def begin_search(self):
        self.search_timer=None
        if self.busy:
            self.search_timer=self.after(100,self.begin_search);return
        token=self.search_generation;root=self.root_path;query=self.search.get().strip()
        if not query or not root:return
        self.search_cancel=threading.Event();cancel=self.search_cancel
        self.stop_button.configure(state='normal')
        def emit(hits,count,visited,skipped,done):
            self.search_queue.put((token,hits,count,visited,skipped,done))
        threading.Thread(target=folder_search.search,args=(root,query,cancel,emit),daemon=True).start()

    def materialize_results(self):
        if not self.draft or not self.search.get().strip():return
        paths={'root'};ancestors={'root'}
        for relative,folder in self.search_hits[:self.search_limit]:
            parts=relative.split('/');parent='root'
            for index,name in enumerate(parts):
                node_id='/'.join(parts[:index+1]);paths.add(node_id)
                if index<len(parts)-1:ancestors.add(node_id)
                self.draft.nodes.setdefault(node_id,{'id':node_id,'source':node_id,'name':name,'parent':parent,
                    'folder':folder if index==len(parts)-1 else True,'linked':False})
                parent=node_id
        self.search_paths=paths;self.search_ancestors=ancestors
        self.more_button.configure(state='normal' if len(self.search_hits)>self.search_limit else 'disabled')
        self.render()

    def more_results(self):
        self.search_limit+=50;self.materialize_results()
        self.search_info.configure(text=f'{len(self.search_hits):,}개 검색 결과 · {min(len(self.search_hits),self.search_limit)}개 표시')

    def choose_root(self):
        if self.busy: return
        path = filedialog.askdirectory(initialdir=str(self.root_path or Path.home()),title='정리할 폴더 선택')
        if path: self.load(Path(path))

    def load(self, path):
        if self.draft and self.draft.operations():
            self.message('적용하지 않은 정리안이 있습니다. 버리고 폴더를 열까요?', actions=[('정리안 버리고 열기', lambda: self.load_now(path), True)])
        else: self.load_now(path)

    def load_now(self, path):
        if self.busy: return
        path=Path(path)
        self.search.set('');self.search_cancel.set();self.search_generation+=1
        self.invalidate()
        def done(d):
            self.draft=d; self.root_path=path; self.location.set(str(path)); self.address.set(str(path)); self.opened={'root'}; self.selected=[]
            self.conversation.clear(); self.naming=None; self.render()
            self.explain_external_git(path)
        def open_folder():
            core.register_folder(path)
            return plans.Draft(path)
        self.job('폴더를 확인하고 있어요…', open_folder, done)

    def explain_external_git(self, path):
        try: boundaries=core.external_git_boundaries(path)
        except (ValueError,OSError) as exc:
            self.message(str(exc));return
        if not boundaries:return
        def change(allow):
            if self.busy or self.root_path != path:return
            try:
                if allow:core.approve_external_git(path,boundaries)
                else:core.revoke_external_git(path)
            except (ValueError,OSError) as exc:self.message(str(exc));return
            self.invalidate();self.draft.version+=1
            self.message('상위 Git 예외를 승인했습니다. 선택한 폴더 안에서 정리안을 만들 수 있습니다. 실제 이동은 적용 확인 후 실행합니다.' if allow else '상위 Git 보호를 다시 적용했습니다. 기존 정리안도 적용 전에 다시 검사합니다.')
            self.explain_external_git(path)
        approved=core.external_git_consent(path) is not None
        self.message(('이 폴더의 상위 Git 예외가 승인되어 있습니다.' if approved else '선택한 폴더 밖에서 상위 Git 저장소를 발견했습니다.')+
            '\nGit 위치: '+', '.join(row['boundary'] for row in boundaries)+
            '\n정리 범위: '+str(path)+
            '\n상위 저장소를 사용하지 않는다면 예외를 승인하고 이 폴더 안을 정리할 수 있습니다. 추적 파일 이동은 Git에 삭제·추가 또는 이름 변경으로 표시될 수 있습니다. Git 기록은 삭제하지 않으며 커밋·스테이징하지 않습니다. 내부의 별도 프로젝트와 .git 자체는 계속 보호합니다. 승인은 앱 종료 시 해제됩니다.',
            actions=[('상위 Git 보호 다시 켜기' if approved else '상위 Git 예외 승인 · 이 폴더 정리',lambda:change(not approved),False)])

    def refresh(self):
        if self.root_path: self.load(self.root_path)

    def navigate_address(self, event=None):
        if self.busy: return 'break'
        text=self.address.get().strip().strip('"')
        path=Path(text)
        if not text or not path.is_absolute():
            self.message('폴더의 전체 경로를 입력한 뒤 Enter를 눌러 주세요.')
            self.address.set(self.location.get()); return 'break'
        self.load(path)
        return 'break'

    def render(self):
        if not self.draft or self.busy: return
        query = self.search.get().strip().casefold()
        operations=self.draft.operations()
        changed_parents=set()
        for op in operations:
            node_id=op['id']
            # Show both the folder losing an item and the folder receiving it.
            for planned in (False,True):
                parent=(self.draft.state(node_id) if planned else self.draft.nodes[node_id])['parent']
                while parent is not None:
                    changed_parents.add(parent)
                    parent=(self.draft.state(parent) if planned else self.draft.nodes[parent])['parent']
        self.rendering=True
        for index, tree in enumerate(self.trees):
            if index==0 and query:
                opened=self.search_ancestors
            else: opened=self.opened
            for node in tree.get_children(''): tree.delete(node)
            children_by_parent={}
            for i in self.draft.nodes:
                parent=(self.draft.state(i) if index else self.draft.nodes[i])['parent']
                children_by_parent.setdefault(parent,[]).append(i)
            def insert(parent):
                children = children_by_parent.get(parent,[])
                children.sort(key=lambda i: (not self.draft.nodes[i]['folder'], self.draft.state(i)['name'].casefold()))
                for node_id in children:
                    n = self.draft.state(node_id) if index else self.draft.nodes[node_id]
                    if not index and n['source'] is None: continue
                    if not index and query and node_id not in self.search_paths:continue
                    tag = n.get('by', '') if index else ''
                    inherited=index and node_id in changed_parents
                    if inherited and not tag:tag='하위 변경'
                    if not index and query and query in n['name'].casefold():tag='검색'
                    try: has_note = bool(self.draft.note(node_id))
                    except (ValueError, OSError): has_note = False
                    tree.insert(parent or '', 'end', iid=node_id, text=('▱ ' if n['folder'] else '· ') + n['name'],
                                values=('메모' if has_note else '',tag if tag!='검색' else ''), tags=tuple(dict.fromkeys(([tag] if tag else [])+(['하위 변경'] if inherited else []))), open=node_id in opened)
                    insert(node_id)
                    if n['folder'] and node_id not in self.draft.loaded and not tree.get_children(node_id) and not (index==0 and query):
                        tree.insert(node_id, 'end', iid='?'+node_id, text='…')
            insert(None)
            for node_id in self.selected:
                if tree.exists(node_id): tree.selection_add(node_id)
        self.count.configure(text=f'{len(operations)}개 변경')
        self.undo_button.configure(state='normal' if self.draft.past else 'disabled')
        self.redo_button.configure(state='normal' if self.draft.future else 'disabled')
        self.rendering=False

    def expand(self, index):
        if not self.draft or self.busy: return
        node_id=self.trees[index].focus()
        if node_id not in self.draft.nodes: return
        self.opened.add(node_id)
        self.job('폴더를 여는 중…', lambda: self.draft.expand(node_id), lambda _: self.render())

    def select(self, index):
        if not self.draft or self.rendering: return
        ids=[i for i in self.trees[index].selection() if i in self.draft.nodes]
        if ids:
            self.selected=ids
            self.target.configure(text='선택: '+', '.join(self.draft.state(i)['name'] for i in ids[:3]))
            if index==0 and self.search.get().strip():
                right=self.trees[1]
                for node_id in ids:
                    parent=self.draft.state(node_id)['parent']
                    while parent:
                        self.opened.add(parent)
                        if right.exists(parent):right.item(parent,open=True)
                        parent=self.draft.state(parent)['parent']
                existing=[i for i in ids if right.exists(i)]
                if existing:right.selection_set(existing);right.see(existing[0])

    def start_drag(self, e, index):
        self.cancel_drag()
        if self.busy: return
        tree=self.trees[index]
        node_id=tree.identify_row(e.y)
        if self.draft and node_id in self.draft.nodes and self.trees[index].identify_column(e.x)=='#1':
            self.drag=None
            if self.draft.note(node_id):
                self.edit_note(node_id,self.trees[index]); return 'break'
        if not self.draft or node_id not in self.draft.nodes or node_id=='root': return
        if tree.identify_column(e.x)!='#0' or 'indicator' in tree.identify_element(e.x,e.y): return
        ids=list(self.trees[index].selection())
        self.drag=([i for i in ids if i!='root'] if node_id in ids else [node_id], e.x_root, e.y_root)

    def cancel_drag(self, e=None):
        self.drag=None
        if self.drag_ghost is not None:
            self.drag_ghost.destroy(); self.drag_ghost=None
        self.highlight_drop(None)

    def highlight_drop(self, node_id):
        if self.drag_target==node_id:return
        tree=self.trees[1]
        if self.drag_target and tree.exists(self.drag_target):
            tree.item(self.drag_target,tags=tuple(t for t in tree.item(self.drag_target,'tags') if t!='drop_target'))
        self.drag_target=node_id
        if node_id:tree.item(node_id,tags=('drop_target',)+tuple(tree.item(node_id,'tags')))

    def drop_destination(self, e):
        tree=self.trees[1]
        if self.winfo_containing(e.x_root,e.y_root)!=tree:return None
        node_id=tree.identify_row(e.y_root-tree.winfo_rooty())
        if node_id not in self.draft.nodes or not self.draft.state(node_id)['folder']:return None
        parent=node_id
        while parent:
            if parent in self.drag[0]:return None
            parent=self.draft.state(parent)['parent']
        return node_id

    def drag_motion(self, e):
        if not self.drag:return
        if self.busy or not self.draft:self.cancel_drag();return
        ids,x,y=self.drag
        if abs(e.x_root-x)+abs(e.y_root-y)<8:return
        if self.drag_ghost is None:
            node=self.draft.state(ids[0])
            text=('▱ ' if node['folder'] else '· ')+node['name']
            if len(ids)>1:text+=f' 외 {len(ids)-1}개'
            ghost=tk.Toplevel(self);ghost.withdraw();ghost.overrideredirect(True)
            ghost.attributes('-topmost',True);ghost.attributes('-alpha',.72)
            tk.Label(ghost,text=text,bg='#e8f0fc',fg='#243750',padx=10,pady=6,
                     relief='solid',borderwidth=1,wraplength=280).pack()
            self.drag_ghost=ghost
        self.drag_ghost.geometry(f'+{e.x_root+18}+{e.y_root+20}')
        self.drag_ghost.deiconify()
        self.highlight_drop(self.drop_destination(e))

    def drop(self, e):
        if not self.drag or self.busy or not self.draft:
            self.cancel_drag();return
        ids, x, y=self.drag
        dest=self.drop_destination(e)
        self.cancel_drag()
        if abs(e.x_root-x)+abs(e.y_root-y)<8: return
        if dest is None:return
        def edit():
            trial=copy.deepcopy(self.draft)
            for node_id in ids: trial.edit(node_id, parent=dest)
            self.draft.checkpoint(); self.draft.nodes=trial.nodes; self.draft.edits=trial.edits; self.draft.loaded=trial.loaded
        self.change(edit)

    def change(self, fn):
        if self.busy or not self.draft: return
        try: fn(); self.invalidate(); self.render()
        except (ValueError, OSError) as exc: self.message(str(exc))

    def context_menu(self,e):
        if self.busy or not self.draft: return
        tree=e.widget; node_id=tree.identify_row(e.y) if getattr(e,'num',None)==3 else tree.focus()
        if node_id not in self.draft.nodes: return
        self.selected=[node_id]; tree.selection_set(node_id)
        menu=tk.Menu(self,tearoff=False)
        menu.add_command(label='메모',command=lambda:self.edit_note(node_id,tree))
        if tree == self.trees[1]:
            menu.add_command(label='이름 바꾸기',command=lambda:self.ask_name('rename',node_id))
            menu.add_command(label='새 폴더',command=lambda:self.ask_name('mkdir',node_id))
        menu.tk_popup(e.x_root,e.y_root)

    def edit_note(self,node_id,tree=None):
        if self.busy or not self.draft: return
        if not self.draft.nodes[node_id]['source']:
            self.message('실제로 존재하는 파일이나 폴더를 선택하세요. 새 폴더는 생성 후 메모를 작성할 수 있습니다.'); return
        draft=self.draft
        try:
            value=draft.note(node_id)
            identity=core.note_identity(draft.root,draft.nodes[node_id]['source'])
        except (ValueError,OSError) as exc: self.message(str(exc)); return
        w=tk.Toplevel(self);w.title('메모 · '+draft.state(node_id)['name'])
        tree=tree or self.trees[0]
        box=tree.bbox(node_id)
        width,height=380,275
        x=tree.winfo_rootx()+(min(box[0]+box[2],220) if box else 20)
        y=tree.winfo_rooty()+(box[1]+box[3] if box else 40)
        x=max(0,min(x,self.winfo_screenwidth()-width-12))
        y=max(0,min(y,self.winfo_screenheight()-height-45))
        w.geometry(f'{width}x{height}+{x}+{y}')
        if self.tk.call('tk','windowingsystem') == 'win32': w.attributes('-toolwindow',True)
        w.resizable(False,False)
        w.configure(highlightthickness=1,highlightbackground='#b5a4d7')
        w.transient(self);w.grab_set()
        ttk.Label(w,text=draft.state(node_id)['name']+' · 메모',wraplength=345,padding=(12,8)).pack(fill='x')
        ttk.Label(w,text='다음 AI 요청에 참고 정보로 함께 전달됩니다.',wraplength=345,padding=(12,0)).pack(fill='x')
        editor=tk.Text(w,wrap='word',undo=True,height=6,padx=10,pady=8)
        editor.insert('1.0',value)
        error=ttk.Label(w,foreground='#a03535',wraplength=345)
        def save():
            if self.busy or self.draft is not draft:
                error.configure(text='작업 상태가 바뀌었습니다. 창을 닫고 다시 선택하세요.');return
            try:
                if core.note_identity(draft.root,draft.nodes[node_id]['source']) != identity:
                    raise ValueError('선택한 항목이 교체되었습니다. 다시 선택해 주세요.')
                draft.set_note(node_id,editor.get('1.0','end-1c'))
                self.invalidate();self.render();w.destroy()
                self.status.set('메모를 저장했습니다. 다음 AI 요청부터 참고합니다.')
            except (ValueError,OSError) as exc:error.configure(text=str(exc))
        row=ttk.Frame(w,padding=12);row.pack(side='bottom',fill='x')
        ttk.Button(row,text='저장',command=save).pack(side='right')
        ttk.Button(row,text='취소',command=w.destroy).pack(side='right',padx=5)
        ttk.Label(w,text='2,000자까지 · 비우고 저장하면 메모 제거',wraplength=345).pack(side='bottom',anchor='w',padx=12)
        error.pack(side='bottom',anchor='w',padx=12)
        editor.pack(fill='both',expand=True,padx=12,pady=8)
        editor.focus_set()
        return w

    def ask_name(self,kind,node_id):
        self.naming=(kind,node_id)
        self.message('새 이름을 입력해 주세요.' if kind=='rename' else '새 폴더 이름을 입력해 주세요. 취소하려면 “취소”라고 입력하세요.')
        self.input.focus_set()

    def undo_draft(self,redo=False):
        if self.busy or not self.draft: return
        if self.draft.undo(redo): self.invalidate(); self.render()

    def toggle_chat(self):
        if self.restore_job is not None:
            self.after_cancel(self.restore_job); self.restore_job = None
        if self.chat_visible:
            self.panes.update_idletasks()
            width = self.panes.winfo_width()
            if width > 10:
                self.pane_ratios = (self.panes.sashpos(0)/width, self.panes.sashpos(1)/width)
            self.panes.forget(self.chat_frame); self.show_chat.pack(side='right')
        else:
            self.panes.add(self.chat_frame,weight=3); self.show_chat.pack_forget()
        self.chat_visible=not self.chat_visible
        if self.chat_visible:
            # Re-add finishes geometry negotiation before restoring the saved dividers.
            self.restore_job = self.after(60, self.balance_panes)

    def enter(self,e):
        if e.state & 1: return
        self.send(); return 'break'

    def send(self):
        if self.busy or not self.draft: return
        text=self.input.get('1.0','end').strip()
        if not text: return
        self.input.delete('1.0','end'); self.message(text,True)
        if self.naming:
            kind,node_id=self.naming; self.naming=None
            if text=='취소': return
            if kind=='rename': self.change(lambda:self.draft.edit(node_id,name=text))
            else:
                parent=node_id if self.draft.state(node_id)['folder'] else self.draft.state(node_id)['parent']
                self.change(lambda:self.draft.mkdir(parent,text))
            return
        if text in ('적용','적용해줘','적용해 줘','확정','이대로 적용'):
            self.review(); return
        provider=self.provider.get(); key=self.keys[provider].get().strip()
        model_id=self.model_ids[provider].get()
        if not key:
            self.message('AI 연결에서 사용할 공급자의 키를 입력해 주세요.',actions=[('AI 연결',self.settings,False)]); return
        version=self.draft.version
        frozen=self.draft
        self.invalidate()
        self.ai_cancel=threading.Event()
        cancel=self.ai_cancel
        self.message(f'{model_id}\n폴더 구조와 필요한 내용을 조사하고 있습니다.',actions=[('조사 중지',cancel.set,False)])
        snapshot=copy.deepcopy(self.draft);selected=self.selected[:];conversation=self.conversation[:]
        def done(response):
            usage=response['usage']
            with core.safe_path(self.root_path,core.STATE+'/usage.jsonl',False).open('a',encoding='utf-8') as f:f.write(json.dumps(usage)+'\n')
            trace='\n'.join(f"{r['action']} · {r['id']} · {'완료' if r['ok'] else '제한'}" for r in response['trace'])
            if trace:self.message(f"조사 {len(response['trace'])}건을 확인했습니다.",actions=[('조사 내역',lambda:self.text_window('조사 내역',trace),False)])
            if response.get('diagnostic_failed'):self.message('진단 기록을 저장하지 못했습니다. 작업 폴더의 접근 권한을 확인해 주세요.')
            if not response['result']:
                self.message(response['error']+'\n요청 번호: '+response.get('request_id',''),
                    actions=[('AI 연결',self.settings,False),('진단 기록 보기',self.show_diagnostics,False)]);return
            if self.draft is not frozen: self.message('작업 폴더가 바뀌어 응답을 반영하지 않았습니다.');return
            result=response['result'];folder_agent.apply_result(self.draft,response,version)
            self.invalidate();self.render()
            self.conversation.extend([{'role':'user','text':text},{'role':'assistant','text':result['message']}])
            self.message(result['message'],actions=[('이 정리안 적용…',self.review,False)] if self.draft.operations() else [])
            cost=usage['estimated_usd'];self.status.set(f"{usage['calls']}회 요청 · 추정 ${cost:.6f}" if cost is not None else '비용 미산정 · 공급자 사용량 확인 필요')
        self.job('AI가 조사할 항목을 고르는 중…',lambda:folder_agent.run(provider,key,snapshot,selected,text,conversation,
            cancel=cancel,progress=self.ai_progress.put,diagnostic=folder_agent.diagnostic_writer(snapshot.root),model_id=model_id),done)

    def review(self):
        if self.busy or not self.draft: return
        version=self.draft.version; draft=self.draft; ops=self.draft.operations()
        def done(reviewed):
            if draft is not self.draft or version!=draft.version:return
            prepared=reviewed['prepared']; held=reviewed['held']
            if held:
                guide='\n\n'.join((h['operation'].get('source') or h['operation']['destination'])+'\n'+h['guide'] for h in held)
                self.message(f"{len(held)}개 항목은 준비/확인이 필요해 보류했습니다. 보류한 정리안은 남겨 둡니다.",
                             actions=[('문제와 준비 방법 보기',lambda:self.text_window('이동 준비 안내',guide),False)])
            if not prepared:return
            warnings=[op for op in prepared if op.get('readiness',{}).get('findings')]
            warning_text='\n\n'.join(op['source']+'\n'+relocation.describe(op['readiness']) for op in warnings)
            lines=[]; summaries=[]
            for op in prepared:
                lines.append(('새 폴더' if op['kind']=='mkdir' else op['source'])+' → '+op['destination'])
                g=op.get('git',{})
                summaries.append(lines[-1])
                if g.get('present'):
                    summaries.append(f"Git: 커밋 기록 {g.get('head_count',0)}개 · 스테이징 {len(g.get('staged',[]))}개 · 미스테이징 {len(g.get('unstaged',[]))}개 · 미추적 {len(g.get('untracked',[]))}개 · 무시 {len(g.get('ignored',[]))}개. 모두 함께 이동합니다.")
                    lines.append('Git 확인: '+json.dumps(g,ensure_ascii=False))
            def apply():
                if self.busy:return
                if self.draft is not draft or self.draft.version!=version:
                    self.message('정리안이 바뀌었습니다. 다시 확인해 주세요.');return
                self.invalidate()
                def finished(result):
                    self.message(f"{len(result['completed'])}개 작업 완료."+((' 중단: '+result['error']+' 작업 기록에서 실제 상태를 확인하세요.') if result['error'] else ' 파일 내용 보존을 확인했습니다. 프로젝트 정상 실행은 미확인입니다. 작업 기록에서 되돌릴 수 있어요.'))
                    if result['error']:
                        # The disk outcome may be uncertain; keep unapplied plans inspectable.
                        self.message('미적용 정리안은 남아 있습니다. 오류 기록을 확인하고 새로고침한 뒤 다시 검토하세요.')
                    else:
                        self.draft.rebase(result['completed']);self.render()
                        remaining=len(self.draft.operations())
                        if remaining:self.message(f'{remaining}개 변경은 아직 미적용입니다. 변경 후 화면에는 남은 정리안이 포함되어 있습니다.')
                self.job('확인한 변경을 적용하고 있어요…',lambda:plans.execute(self.root_path,prepared,path_warnings_confirmed=bool(warnings)),finished)
            if warnings:
                self.message(f'{len(warnings)}개 항목에서 경로 참조가 발견됐습니다. 과거 기록일 수도 있지만 이동 후 실행이나 파일 참조가 깨질 수 있습니다. 경로를 자동 수정하지 않습니다. 내용을 확인한 뒤 진행 여부를 선택하세요.\n'+warning_text)
            self.message(f'{len(prepared)}개 변경을 실제 폴더에 적용할까요?\n대상: {self.root_path}\n'+'\n'.join(summaries[:8])+ ('\n추가 내역은 전체 목록에서 확인하세요.' if len(summaries)>8 else '')+'\n'+reviewed['scope'],actions=[('전체 변경·Git 내역 보기',lambda:self.text_window('적용할 변경','\n\n'.join(lines)+'\n\n'+warning_text),False),('경로 경고 확인 · 적용' if warnings else '확인했어요 · 적용',apply,True),('계속 수정',self.invalidate,False)])
        self.job('내용·Git·이동 준비 상태를 확인하고 있어요…',lambda:plans.review(self.root_path,ops),done)

    def text_window(self,title,text):
        w=tk.Toplevel(self);w.title(title);w.geometry('760x540')
        t=tk.Text(w,wrap='word',padx=14,pady=14);t.pack(fill='both',expand=True);t.insert('1.0',text);t.configure(state='disabled')

    def settings(self):
        w=tk.Toplevel(self);w.title('AI 연결');w.geometry(f'690x{min(460,self.winfo_screenheight()-80)}')
        ttk.Label(w,text='OpenAI API 키를 입력하세요. 이 Windows 계정으로 암호화해 자동 저장합니다.',padding=15).pack(anchor='w')
        for provider,model in ai.MODELS.items():
            group=ttk.LabelFrame(w,text=provider,padding=8);group.pack(fill='x',padx=15,pady=4)
            options=ttk.Frame(group);options.pack(fill='x')
            ttk.Label(options,text='모델').pack(side='left')
            picker=ttk.Combobox(options,textvariable=self.model_ids[provider],
                values=[model['id'],*[i for i in ai.MODEL_OPTIONS[provider] if i!=model['id']]],state='readonly',width=23)
            picker.pack(side='left',padx=8)
            price=ttk.Label(options);price.pack(side='left')
            def show_price(event=None,p=provider,label=price):
                rates=ai.model_config(p,self.model_ids[p].get())
                label.configure(text=f"100만 토큰: 입력 ${rates['input']:g} / 출력 ${rates['output']:g}")
            picker.bind('<<ComboboxSelected>>',show_price);show_price()
            row=ttk.Frame(group);row.pack(fill='x',pady=(6,0))
            ttk.Label(row,text='API 키').pack(side='left')
            ttk.Entry(row,textvariable=self.keys[provider],show='●',width=44).pack(side='left',padx=10,fill='x',expand=True)
            ttk.Button(row,text='지우기',command=lambda p=provider:self.keys[p].set('')).pack(side='right')
        ttk.Label(w,text='전송하면 AI가 바로 조사하고 정리안을 만듭니다. 키는 자동 저장되며 지우기를 누르면 저장된 키도 삭제됩니다.',padding=15,wraplength=620).pack(anchor='w')
        ttk.Button(w,text='AI 이용·전송·비용 안내',command=self.show_guide).pack(anchor='w',padx=15)
        ttk.Button(w,text='진단 기록 보기',command=self.show_diagnostics).pack(anchor='w',padx=15,pady=5)

    def show_diagnostics(self):
        if not self.root_path:return
        path=core.safe_path(self.root_path,core.STATE+'/diagnostics.jsonl',False)
        from collections import deque
        if path.exists():
            with path.open(encoding='utf-8') as f:recent=''.join(deque(f,maxlen=150))
        else:recent='아직 기록이 없습니다. 새 버전에서 AI 요청을 보내면 자동으로 기록됩니다.'
        self.text_window('진단 기록',str(path)+'\n\n최근 150개 사건입니다. 문제 발생 시 이 기록 파일을 전달해 주세요.\n키·채팅·파일 이름·본문은 저장하지 않습니다.\n\n'+recent)

    def show_guide(self):
        self.text_window('폴더착착 이용 안내', '''전송하면 바로 진행합니다
AI 연결에 키를 입력하고 메시지를 전송하면 선택한 모델이 조사와 정리안 작성을 시작합니다. 추가 전송 확인은 없습니다. 진행 중에는 채팅의 조사 중지 버튼을 사용할 수 있습니다.

무엇이 AI에 전송되나요?
현재 작업 폴더의 이름·정리안·메모·최근 대화와 AI가 필요한 만큼 조회한 목록 및 텍스트/코드/CSV/JSON/DOCX 발췌가 선택한 공급자에게 전송됩니다. 관리 영역·생성물·링크·인증정보 후보는 제외하지만 모든 민감정보를 판별하지는 못합니다. 민감한 자료가 없는 작업 범위를 선택하세요. PDF·이미지·음성 본문은 아직 지원하지 않습니다.

얼마나 조사하고 비용이 드나요?
기본 조사에는 고정 호출 횟수·누적 항목 수 제한이 없습니다. 자료가 많으면 이전 조회 자료를 나눠 보내고 필요할 때 재조회합니다. 파일 2MB와 한 번에 본문 6,000자, 지원 형식 제한은 유지합니다. 앱 자체의 메시지당 비용 한도는 없습니다. 실제 청구는 공급자 콘솔에서 확인하세요. 중지해도 이미 전송한 요청은 과금될 수 있습니다.

언제 실제 파일이 바뀌나요?
AI와 드래그는 변경 후 화면의 정리안만 편집합니다. 적용 요청 → 변경 검토 → ‘확인했어요 · 적용’ 이후에만 프로그램이 파일을 이동합니다. 삭제·임의 명령 실행·덮어쓰기 기능은 제공하지 않습니다. Git/개발 환경 등의 이동 제약은 최종 검사에서 확인하며, 준비가 필요한 항목은 보류합니다.

문제가 생겼을 때
요청별 진행 단계와 오류 발생 지점을 .organizer-state/diagnostics.jsonl에 자동 기록합니다. AI 연결 → 진단 기록 보기에서 확인할 수 있습니다. 화면 배치나 제안 내용 문제는 추가 설명이 필요할 수 있습니다.

키와 기록
API 키는 Windows DPAPI로 암호화해 로컬 사용자 설정 폴더에 저장하며 재실행 시 불러옵니다. AI 연결에서 지우면 저장된 키도 삭제됩니다. 사용량과 이동 기록, 메모는 작업 폴더의 앱 관리 영역에 보관합니다. 조사 본문과 API 키는 일반 사용 기록에 저장하지 않습니다.''')

    def history(self):
        if not self.root_path or self.busy:return
        try: rows=core.history(self.root_path)
        except (ValueError,OSError) as exc:self.message(str(exc));return
        w=tk.Toplevel(self);w.title('작업 기록');w.geometry('820x460')
        tree=ttk.Treeview(w,columns=('state','path'),show='headings');tree.heading('state',text='상태');tree.heading('path',text='변경 위치');tree.column('state',width=130);tree.column('path',width=620);tree.pack(fill='both',expand=True)
        for row in rows:tree.insert('', 'end',iid=row['id'],values=(row['status'],row['destination']))
        def ask():
            if not tree.selection():return
            record_id=tree.selection()[0];w.destroy()
            def perform():
                if self.busy:return
                self.invalidate()
                self.job('기록을 확인하고 되돌리는 중…',lambda:plans.undo_record(self.root_path,record_id),lambda _:self.after_undo())
            self.message('선택한 실제 이동을 되돌릴까요? 현재 내용과 원래 위치를 다시 검사합니다.',actions=[('확인하고 되돌리기',perform,True)])
        ttk.Button(w,text='선택한 이동 되돌리기',command=ask).pack(pady=10)

    def after_undo(self):
        self.message('이동을 되돌렸습니다. 생성한 빈 폴더는 남겨 두었습니다.');self.draft=None;self.load_now(self.root_path)

    def close(self):
        if self.busy:self.message('진행 중인 작업이 끝난 뒤 닫아 주세요.');return
        if not self.save_keys():return
        self.destroy()

    def destroy(self):
        self.save_keys()
        self.ai_cancel.set()
        self.cancel_drag()
        self.search_cancel.set()
        for timer in (getattr(self, 'poll_job', None), getattr(self, 'restore_job', None),getattr(self,'search_timer',None),getattr(self,'balance_job',None),getattr(self,'scroll_job',None)):
            if timer is not None:
                try: self.after_cancel(timer)
                except tk.TclError: pass
        super().destroy()
