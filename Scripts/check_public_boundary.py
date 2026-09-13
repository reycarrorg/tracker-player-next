#!/usr/bin/env python3
"""Fail when prohibited private data or distributable media enters the repository."""
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1]
TEXT_SUFFIXES={'.md','.py','.swift','.json','.yml','.yaml','.txt','.sh'}
MEDIA_SUFFIXES={'.mp3','.m4a','.aac','.flac','.wav','.aiff','.ogg','.opus','.mp4','.mov','.mkv','.jpg','.jpeg','.png','.gif','.webp'}
FORBIDDEN_NAMES={'catalog.json','Library.sqlite','state.json','auth.json','cookies.json'}
FORBIDDEN_TEXT={
    'private-sheet-id':re.compile('1OIj31knjgSnBcYGjR_'+'GKfuaLxVZQQdVb83JVfs9ylkg'),
    'absolute-user-path':re.compile('/'+'Users/'+r'[^/\s]+/'),
    'google-drive-id':re.compile(r'drive\.google\.com/(?:drive/(?:u/\d+/)?folders|file/d)/[A-Za-z0-9_-]+'),
    'live-sheet-id':re.compile(r'docs\.google\.com/spreadsheets/d/(?!public-fixture-sheet(?:/|\b))[A-Za-z0-9_-]{20,}'),
    'credential-assignment':re.compile(r'(?i)\b(?:password|access[_-]?token|refresh[_-]?token|cookie|authorization)\s*[:=]\s*["\'][^"\']{8,}["\']'),
}
REQUIRED_WARNING='untrusted data'

errors=[]
for path in ROOT.rglob('*'):
    if any(part in {'.git','Build','.venv','__pycache__'} for part in path.parts) or path.is_symlink() or not path.is_file():
        continue
    relative=path.relative_to(ROOT)
    if path.name in FORBIDDEN_NAMES:
        errors.append(f'{relative}: forbidden private-state filename')
    if path.suffix.lower() in MEDIA_SUFFIXES:
        errors.append(f'{relative}: binary media/artwork is not public-source content')
    if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {'VERSION','.gitignore'}:
        errors.append(f'{relative}: unreviewed file type')
        continue
    try:text=path.read_text('utf-8')
    except UnicodeDecodeError:
        errors.append(f'{relative}: non-UTF-8 content')
        continue
    for label,pattern in FORBIDDEN_TEXT.items():
        if pattern.search(text):errors.append(f'{relative}: {label}')
for required in ('README.md','SECURITY.md','CONTRIBUTING.md'):
    if REQUIRED_WARNING not in (ROOT/required).read_text('utf-8').lower():
        errors.append(f'{required}: missing universal untrusted-data warning')
if errors:
    print('\n'.join(errors),file=sys.stderr);raise SystemExit(1)
print('Public boundary check passed.')
