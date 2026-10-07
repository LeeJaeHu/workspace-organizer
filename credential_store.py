"""Current-user Windows DPAPI storage. Never store plaintext credentials."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import tempfile


def default_path():
    return Path(os.environ['LOCALAPPDATA'])/'FolderChakchak'/'credentials.bin'


def crypt(data, decrypt=False):
    if os.name!='nt':raise ValueError('키 저장은 Windows에서 지원합니다.')
    class Blob(ctypes.Structure):
        _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]
    buffer=ctypes.create_string_buffer(data)
    source=Blob(len(data),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)))
    output=Blob()
    library=ctypes.WinDLL('crypt32',use_last_error=True)
    fn=library.CryptUnprotectData if decrypt else library.CryptProtectData
    fn.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,
                 ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
    fn.restype=wintypes.BOOL
    free=ctypes.WinDLL('kernel32').LocalFree
    free.argtypes=[ctypes.c_void_p];free.restype=ctypes.c_void_p
    try:
        if not fn(ctypes.byref(source),None,None,None,None,1,ctypes.byref(output)):
            raise ValueError('Windows 계정 암호화 처리에 실패했습니다.')
        return ctypes.string_at(output.data,output.size)
    finally:
        ctypes.memset(buffer,0,len(data))
        if output.data:
            ctypes.memset(output.data,0,output.size);free(output.data)


def load(path):
    path=Path(path)
    if not path.exists():return {}
    try:
        value=json.loads(crypt(path.read_bytes(),decrypt=True))
        if not isinstance(value,dict) or any(p not in ('OpenAI','Gemini','Grok') or not isinstance(k,str) for p,k in value.items()):
            raise ValueError()
        return value
    except (OSError,ValueError):
        raise ValueError('저장된 API 키를 불러오지 못했습니다. 현재 Windows 계정과 저장 파일을 확인하세요.') from None


def save(path, changes):
    path=Path(path)
    values=load(path)
    for provider,key in changes.items():
        if provider not in ('OpenAI','Gemini','Grok'):raise ValueError('지원하지 않는 공급자입니다.')
        if key.strip():values[provider]=key.strip()
        else:values.pop(provider,None)
    if not values:
        path.unlink(missing_ok=True);return
    encrypted=crypt(json.dumps(values).encode('utf-8'))
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as stream:
            temporary=Path(stream.name);stream.write(encrypted)
        os.replace(temporary,path)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)
