"""Source-based names and atomic, format-aware Music-facing tags. No transcoding."""
import base64
import datetime
import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode


class MetadataError(ValueError):
    code = 'metadata_failed'


def public_record(value):
    """Never put session material or signed query values in portable records."""
    if isinstance(value, dict):
        return {str(k): public_record(v) for k, v in value.items()
                if not re.search(r'password|cookie|credential|authorization|token|secret|session', str(k), re.I)}
    if isinstance(value, list): return [public_record(v) for v in value]
    if not isinstance(value, str): return value
    def clean_url(match):
        try:
            p = urlsplit(match.group())
            host = p.hostname or ''
            if p.port: host += ':' + str(p.port)
            query = urlencode([(k, '[redacted]') for k, _ in parse_qsl(p.query, keep_blank_values=True)])
            return urlunsplit((p.scheme, host, p.path, query, ''))
        except ValueError: return '[invalid URL omitted]'
    value = re.sub(r'https?://[^\s<>"\']+', clean_url, value)
    return re.sub(r'(?i)\b(password|cookie|authorization|bearer|access[_ -]?token|secret)\s*[:= ]\s*[^\s,;]+', r'\1=[redacted]', value)


def version_token(row):
    raw = str(row.get('version', ''))
    found = re.search(r'(?i)\bV\s*(\d+)\b', raw)
    if not found: found = re.search(r'(?i)\[V\s*(\d+)\]', str(row.get('name', row.get('title', ''))))
    return '[V' + str(int(found[1])) + ']' if found else ''


def portion(row):
    f = row.get('fields', {})
    value = str(row.get('portion') or f.get('Available Length') or f.get('Portion') or '').casefold()
    if 'snippet' in value: return '[Snippet]'
    if 'tagged' in value: return '[Tagged]'
    return '[Full]'


def song_name(row):
    name = str(row.get('name') or row.get('title') or 'Untitled')
    name = re.sub(r'(?i)\[V\s*\d+\]|\[(?:Snippet|Tagged|Full)\]', '', name)
    return ' '.join(name.split()) or 'Untitled'


def base_name(row):
    text = ' '.join(x for x in (song_name(row), version_token(row), portion(row)) if x)
    # Preserve Unicode, punctuation and featured-artist text except prohibited characters.
    text = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', '_', text).strip(' .')
    if len(text.encode('utf-8')) > 220:
        raise MetadataError('Filename exceeds the filesystem limit. Shorten the source name explicitly; no silent truncation was made.')
    return text


def destination(folder, row, extension, checksum):
    """Same name first; deterministic nonsemantic suffix only on a collision."""
    if not re.fullmatch(r'[a-z0-9]{1,8}', extension): raise MetadataError('Unsafe media extension.')
    stem = base_name(row)
    candidates = [folder / (stem + '.' + extension),
                  folder / (stem + ' --' + hashlib.sha256(str(row['id']).encode()).hexdigest()[:10] + '.' + extension),
                  folder / (stem + ' --' + checksum[:12] + '.' + extension)]
    for path in candidates:
        if path.is_symlink(): raise MetadataError('A symbolic link occupies the destination.')
        if not path.exists(): return path
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == checksum: return path
    raise MetadataError('Conflicting files occupy all deterministic destinations. Existing files preserved.')


def metadata(row):
    fields = row.get('fields', {})
    def one(*keys):
        values = {str(fields[k]).strip() for k in keys if fields.get(k) and str(fields[k]).strip() not in ('—', 'N/A', '?')}
        return next(iter(values)) if len(values) == 1 else ''
    date = one('Release Date', 'Year', 'Release Year')
    year = ''
    if re.fullmatch(r'\d{4}', date): year = date
    else:
        for fmt in ('%Y-%m-%d', '%b %d, %Y', '%B %d, %Y'):
            try: year = str(datetime.datetime.strptime(date, fmt).year); break
            except ValueError: pass
    bpm = one('BPM', 'Tempo')
    if not re.fullmatch(r'\d+(?:\.\d+)?', bpm) or not 0 < float(bpm or 0) <= 999: bpm = ''
    notes = []
    for key in ('Notes', 'Comments', 'Comment', 'Description'):
        value = str(fields.get(key, '')).strip()
        if value and value not in notes: notes.append(value)
    return dict(title=song_name(row), album=str(row['era']) + ' [' + str(row['workbook']) + ']',
                artist=one('Artist', 'Artists', 'Primary Artist'), year=year, bpm=bpm,
                comment=public_record('\n\n'.join(notes)), version=version_token(row), portion=portion(row),
                composer=one('Composer', 'Composers'), genre=one('Genre'))


def write_tags(path, row, artwork=None):
    """Write to a sibling temporary copy, verify every requested field, then replace."""
    import mutagen
    from mutagen.id3 import ID3, TIT2, TALB, TPE1, TDRC, TBPM, COMM, TXXX, APIC, TCOM, TCON
    from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
    from mutagen.flac import FLAC, Picture
    from mutagen._vorbis import VCommentDict
    path = Path(path)
    if path.is_symlink() or not path.is_file(): raise MetadataError('Tagging requires a regular app-owned file.')
    values = metadata(row)
    if artwork and not artwork.startswith((b'\xff\xd8\xff', b'\x89PNG\r\n\x1a\n')):
        raise MetadataError('Music cover art must be a validated JPEG or PNG.')
    stage = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tags')
    try:
        shutil.copyfile(path, stage)
        audio = mutagen.File(stage)
        if audio is None: raise MetadataError('Unrecognized audio container; original payload preserved.')
        before_duration = audio.info.length
        if audio.tags is None:
            try: audio.add_tags()
            except Exception: raise MetadataError('This audio container cannot hold Music-readable tags without repackaging. Original payload preserved.')
        tags = audio.tags
        def discard(key):
            if key in tags: del tags[key]
        picture = None
        if artwork:
            picture = Picture(); picture.type = 3; picture.mime = 'image/png' if artwork.startswith(b'\x89PNG') else 'image/jpeg'; picture.data = artwork
        if isinstance(audio, MP4):
            mapping = dict(title='\xa9nam', album='\xa9alb', artist='\xa9ART', year='\xa9day', comment='\xa9cmt', composer='\xa9wrt', genre='\xa9gen',
                           version='----:com.apple.iTunes:VERSION', portion='----:com.apple.iTunes:PORTION')
            for key, tag in mapping.items():
                discard(tag)
                if values[key]: tags[tag] = [MP4FreeForm(values[key].encode())] if key in ('version', 'portion') else [values[key]]
            discard('tmpo'); discard('----:com.apple.iTunes:BPM')
            if values['bpm']:
                if float(values['bpm']).is_integer(): tags['tmpo'] = [int(float(values['bpm']))]
                else: tags['----:com.apple.iTunes:BPM'] = [MP4FreeForm(values['bpm'].encode())]
            discard('covr')
            if artwork: tags['covr'] = [MP4Cover(artwork, imageformat=MP4Cover.FORMAT_PNG if picture.mime=='image/png' else MP4Cover.FORMAT_JPEG)]
            family = 'MP4'
        elif isinstance(audio, FLAC) or isinstance(tags, VCommentDict):
            mapping = dict(title='title', album='album', artist='artist', year='date', bpm='bpm', comment='comment', version='version', portion='portion', composer='composer', genre='genre')
            for key, tag in mapping.items():
                discard(tag)
                if values[key]: tags[tag] = [values[key]]
            discard('description')
            if values['comment']: tags['description'] = [values['comment']]
            discard('metadata_block_picture')
            if isinstance(audio, FLAC):
                audio.clear_pictures()
                if picture: audio.add_picture(picture)
            elif picture: tags['metadata_block_picture'] = [base64.b64encode(picture.write()).decode()]
            family = 'Vorbis'
        elif isinstance(tags, ID3):
            mapping = dict(title=('TIT2', TIT2), album=('TALB', TALB), artist=('TPE1', TPE1), year=('TDRC', TDRC), bpm=('TBPM', TBPM), composer=('TCOM', TCOM), genre=('TCON', TCON))
            for key, (tag, frame) in mapping.items():
                tags.delall(tag)
                if values[key]: tags.add(frame(encoding=3, text=[values[key]]))
            tags.delall('COMM')
            if values['comment']: tags.add(COMM(encoding=3, lang='eng', desc='', text=[values['comment']]))
            for key in ('version', 'portion'):
                tags.delall('TXXX:' + key.upper())
                if values[key]: tags.add(TXXX(encoding=3, desc=key.upper(), text=[values[key]]))
            tags.delall('APIC')
            if picture: tags.add(APIC(encoding=3, mime=picture.mime, type=3, desc='Cover', data=artwork))
            family = 'ID3'
        else: raise MetadataError('Unsupported tag family; original payload preserved.')
        audio.save()
        check = mutagen.File(stage)
        if check is None or check.tags is None or abs(check.info.length-before_duration) > 0.02:
            raise MetadataError('Audio duration or tag readback failed.')
        got = {}
        def strings(v):
            return [x.decode() if isinstance(x, bytes) else str(x) for x in (v.text if hasattr(v, 'text') else v or [])]
        if family == 'ID3':
            for key, (tag, _) in mapping.items(): got[key] = strings(check.tags.get(tag))
            got['comment'] = [str(x) for frame in check.tags.getall('COMM') if frame.desc=='' for x in frame.text]
            for key in ('version','portion'): got[key] = strings(check.tags.get('TXXX:'+key.upper()))
            pictures = [p.data for p in check.tags.getall('APIC')]
        else:
            for key, tag in mapping.items(): got[key] = strings(check.tags.get(tag))
            if family == 'MP4':
                got['bpm'] = strings(check.tags.get('tmpo') or check.tags.get('----:com.apple.iTunes:BPM'))
                pictures = [bytes(x) for x in check.tags.get('covr', [])]
            else: pictures = [p.data for p in check.pictures] if isinstance(check, FLAC) else [Picture(base64.b64decode(x)).data for x in check.tags.get('metadata_block_picture', [])]
        for key, expected in values.items():
            actual = got.get(key, [])
            if key=='bpm' and expected and actual and float(actual[0])==float(expected): continue
            if actual != ([expected] if expected else []): raise MetadataError('Tag readback mismatch: ' + key)
        if pictures != ([artwork] if artwork else []): raise MetadataError('Artwork readback mismatch.')
        with open(stage, 'rb') as f: os.fsync(f.fileno())
        os.replace(stage, path)
        return dict(values, artwork=bool(artwork), family=family, releaseDate=values['year'], trackNumber='',
                    appleMusicComments='Standard Comments: COMM (eng, empty description), ©cmt, or Vorbis COMMENT/DESCRIPTION',
                    warnings=['Fractional BPM stored in MP4 freeform BPM; Apple Music integer BPM display requires manual verification.'] if family=='MP4' and values['bpm'] and not float(values['bpm']).is_integer() else [])
    finally:
        if stage.exists(): stage.unlink()
