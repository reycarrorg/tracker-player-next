"""Public HTTP media transport; DNS is checked and pinned for each redirect."""
import hashlib, http.client, ipaddress, json, os, re, socket, ssl, time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit, urljoin
import mutagen
import mutagen.mp4, mutagen.flac, mutagen._vorbis

class MediaError(ValueError):
    code='access_unavailable'

class AccessError(MediaError):
    def __init__(self,code,message):self.code=code;super().__init__(message)

def classify_http(status,challenge=False):
    if status==401 or challenge:return 'authentication_required'
    if status in (404,410):return 'broken_source'
    if status in (408,425,429) or status>=500:return 'network_failure'
    return 'access_unavailable'

def classify_page(payload):
    text=payload.decode('utf-8',errors='replace').lower()
    if any(x in text for x in ('type="password"',"type='password'",'sign in','log in','login','authenticate','authorization required','captcha')):
        return 'authentication_required'
    return 'access_unavailable'

def gigabytes(n): return f'{n/1_000_000_000:.3f} GB'

class FileLimit(MediaError):
    code='file_limit'
    def __init__(self,limit,total=None):
        self.limit=limit;self.total=total;self.requested=total if total else limit*2
        super().__init__(f'File size {gigabytes(total)} exceeds the {gigabytes(limit)} limit.' if total else f'The source did not provide its size and reached the {gigabytes(limit)} limit.')

def public_target(url):
    p=urlsplit(url)
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or p.port not in (None,80,443): raise MediaError('Only public HTTP(S) source URLs on standard ports are supported.')
    if any(ord(c)<33 for c in url) or '\\' in url: raise MediaError('Malformed source URL.')
    addresses=socket.getaddrinfo(p.hostname,p.port or (443 if p.scheme=='https' else 80),type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses): raise MediaError('Private, local, reserved or mixed DNS destinations are blocked.')
    return p,addresses[0][4][0]

class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self,host,ip,port): super().__init__(host,port,timeout=15,context=ssl.create_default_context()); self.ip=ip
    def connect(self): self.sock=self._context.wrap_socket(socket.create_connection((self.ip,self.port),self.timeout),server_hostname=self.host)
class PinnedHTTP(http.client.HTTPConnection):
    def __init__(self,host,ip,port): super().__init__(host,port,timeout=15); self.ip=ip
    def connect(self): self.sock=socket.create_connection((self.ip,self.port),self.timeout)

def response(url):
    for _ in range(6):
        p,ip=public_target(url); cls=PinnedHTTPS if p.scheme=='https' else PinnedHTTP
        conn=cls(p.hostname,ip,p.port or (443 if p.scheme=='https' else 80))
        try:
            conn.request('GET',(p.path or '/')+('?' +p.query if p.query else ''),headers={'User-Agent':'TrackerPlayer/1.0','Accept-Encoding':'identity','Connection':'close'})
            r=conn.getresponse()
            if r.status in (301,302,303,307,308):
                location=r.getheader('Location'); conn.close()
                if not location: raise MediaError('Redirect without destination.')
                url=urljoin(url,location); continue
            if r.status!=200: raise AccessError(classify_http(r.status,bool(r.getheader('WWW-Authenticate'))),f'Source returned HTTP {r.status}. Open the provider to review access; no browser session was imported.')
            return conn,r,url
        except Exception: conn.close(); raise
    raise MediaError('Too many redirects.')

class PageMedia(HTMLParser):
    def __init__(self): super().__init__(); self.media=[]; self.images=[]
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag in ('audio','video','source') and a.get('src'): self.media.append(a['src'])
        if tag=='a' and a.get('href') and ('download' in a or a.get('id','').lower() in ('download','downloadbutton','download-button')): self.media.append(a['href'])
        if tag=='meta' and a.get('property')=='og:image' and a.get('content'): self.images.append(a['content'])

def sniff(b,header=''):
    t=b.lstrip().lower()
    if t.startswith((b'<!doctype html',b'<html',b'<head',b'<script')) or 'text/html' in header: raise MediaError('HTML page, login, or error response; not playable media.')
    if len(b)>1 and b[0]==255 and b[1]&0xf6==0xf0: return 'audio','aac','audio/aac'
    if b.startswith(b'ID3') or len(b)>1 and b[0]==255 and b[1]&0xe0==0xe0: return 'audio','mp3','audio/mpeg'
    if b.startswith(b'fLaC'): return 'audio','flac','audio/flac'
    if b.startswith(b'RIFF') and b[8:12]==b'WAVE': return 'audio','wav','audio/wav'
    if b.startswith(b'RIFF') and b[8:12]==b'WEBP': return 'image','webp','image/webp'
    if b.startswith(b'OggS'): return 'audio','ogg','audio/ogg'
    if b.startswith(b'FORM') and b[8:12] in (b'AIFF',b'AIFC'): return 'audio','aiff','audio/aiff'
    if len(b)>12 and b[4:8]==b'ftyp': return ('audio','m4a','audio/mp4') if b[8:12] in (b'M4A ',b'M4B ') else ('video','mp4','video/mp4')
    if b.startswith(b'\x1aE\xdf\xa3'): return 'video','webm','video/webm'
    if b.startswith(b'\x89PNG\r\n\x1a\n'): return 'image','png','image/png'
    if b.startswith(b'\xff\xd8\xff'): return 'image','jpg','image/jpeg'
    if b.startswith((b'GIF87a',b'GIF89a')): return 'image','gif','image/gif'
    if b.startswith(b'%PDF-'): return 'pdf','pdf','application/pdf'
    if b.startswith(b'PK\x03\x04'): return 'archive','zip','application/zip'
    if b.startswith((b'Rar!\x1a\x07',b'7z\xbc\xaf\x27\x1c',b'\x1f\x8b')): return 'archive','bin','application/octet-stream'
    if header.startswith('text/plain') and b'\x00' not in b: return 'text','txt','text/plain'
    raise MediaError('Unsupported or unrecognized content; use Open source. No extension-based assumptions.')

def download(url,path,limit,consume=lambda n:None,cancel=lambda:False,progress=lambda n,total:None,art=False,original_media=True,reserve=None,release=None):
    original=url; deadline=time.monotonic()+max(180,min(7200,limit/262144+60) if limit>128*1048576 else 180); received=0
    def read_chunk(response,wanted):
        nonlocal received
        allowance=min(wanted,limit-received)
        if allowance<=0: raise FileLimit(limit)
        if reserve: allowance=reserve(allowance)
        if allowance<=0: raise MediaError('Batch actual-byte cap reached.')
        try:
            data=response.read(allowance); received+=len(data); consume(len(data)); return data
        finally:
            if release: release(allowance)
    p=urlsplit(url)
    if p.hostname in ('pillows.su','www.pillows.su') and re.fullmatch(r'/f/[a-f0-9]{32}',p.path):
        # Same public media endpoint used by the host player, verified 2026-09-05.
        url='https://api.pillows.su/api/'+('download/' if original_media else 'get/')+p.path.rsplit('/',1)[1]
    for depth in range(3):
        conn,r,final=response(url)
        try:
            total=int(r.getheader('Content-Length') or 0); header=r.getheader('Content-Type','').split(';')[0].lower()
            if total>limit and header!='text/html': raise FileLimit(limit,total)
            first=read_chunk(r,8192)
            if header=='text/html' or first.lstrip().lower().startswith((b'<!doctype html',b'<html')):
                host=urlsplit(final).hostname
                if host not in ('pillows.su','www.pillows.su','ibb.co','www.ibb.co'): raise AccessError(classify_page(first),'Provider page requires browser interaction or does not expose supported public media. Open the provider; browser sign-in is not shared with this downloader.')
                extra=read_chunk(r,256000); page=PageMedia(); page.feed((first+extra).decode('utf-8',errors='replace'))
                candidates=page.images if art else page.media
                if not candidates: raise MediaError('No supported public media URL in the source page. Use Open source.')
                url=urljoin(final,candidates[0]); continue
            kind,ext,mime=sniff(first,header)
            if art and kind!='image': raise MediaError('Cover source did not return an image.')
            size=len(first)
            if size>limit: raise MediaError('Per-file byte cap reached.')
            with open(path,'xb') as f:
                f.write(first)
                while True:
                    if cancel(): raise MediaError('Cancelled')
                    if time.monotonic()>deadline: raise MediaError('Transfer time limit reached; retry explicitly.')
                    if total and size>=total: break
                    chunk=read_chunk(r,65536)
                    if not chunk: break
                    size+=len(chunk)
                    if size>limit: raise MediaError('Per-file byte cap reached.')
                    f.write(chunk); progress(size,total or None)
                f.flush(); os.fsync(f.fileno())
            if total and size!=total: raise MediaError('Incomplete transfer; file was not completed.')
            if kind=='audio':
                try:
                    audio=mutagen.File(path)
                    if audio is None or not audio.info.length>0: raise MediaError('Audio decoding metadata validation failed.')
                    if isinstance(audio,mutagen.mp4.MP4): ext='m4a'; mime='audio/mp4'
                except Exception as e: raise MediaError('Audio payload could not be validated: '+str(e))
            return dict(originalUrl=original,resolvedUrl=final,bytes=size,kind=kind,extension=ext,mime=mime,checksum=hashlib.sha256(Path(path).read_bytes()).hexdigest(),declaredBytes=total or None)
        finally: conn.close()
    raise MediaError('Source page resolution limit reached.')

def tag_copy(path,row,artwork=None):
    """No transcoding; mutate only a separate copy, keep original and hashes."""
    from mutagen.id3 import TIT2,TPE1,TALB,TRCK,COMM,APIC
    from mutagen.mp4 import MP4,MP4Cover
    from mutagen.flac import FLAC,Picture
    audio=mutagen.File(path)
    if audio is None: return 'Unsupported tag container; original preserved.'
    title=row['title']; artist=row['artist']; album=row['era']; order=str(row['order'])
    comment=f"Suzy Tracker | Workbook: {row['workbook']} | Version: {row['version']} | Row identity: {row['id']} | Source: {row['sourceUrl']} | Snapshot: {row['sourceHash']}"
    if audio.tags is None: audio.add_tags()
    tags=audio.tags
    tags.clear()  # Only the import copy is changed; unknown or conflicting old tags stay in the preserved original.
    if isinstance(audio,MP4):
        tags['\xa9nam']=[title]; tags['\xa9alb']=[album]; tags['trkn']=[(int(order),0)]; tags['\xa9cmt']=[comment]
        if artist: tags['\xa9ART']=[artist]
        if artwork: tags['covr']=[MP4Cover(artwork,imageformat=MP4Cover.FORMAT_PNG if artwork.startswith(b'\x89PNG') else MP4Cover.FORMAT_JPEG)]
    elif isinstance(audio,FLAC) or isinstance(tags,mutagen._vorbis.VCommentDict):
        tags['title']=title; tags['album']=album; tags['tracknumber']=order; tags['comment']=comment
        if artist: tags['artist']=artist
        if artwork and isinstance(audio,FLAC):
            pic=Picture(); pic.type=3; pic.mime='image/png' if artwork.startswith(b'\x89PNG') else 'image/jpeg'; pic.data=artwork; audio.add_picture(pic)
    elif hasattr(tags,'add'):
        tags.delall('TIT2'); tags.add(TIT2(encoding=3,text=title)); tags.delall('TALB'); tags.add(TALB(encoding=3,text=album)); tags.delall('TRCK'); tags.add(TRCK(encoding=3,text=order)); tags.add(COMM(encoding=3,lang='eng',desc='Tracker provenance',text=comment))
        if artist: tags.delall('TPE1'); tags.add(TPE1(encoding=3,text=artist))
        if artwork: tags.add(APIC(encoding=3,mime='image/png' if artwork.startswith(b'\x89PNG') else 'image/jpeg',type=3,desc='Tracker era artwork',data=artwork))
    else: return 'Unsupported tag container; original preserved.'
    release=row.get('fields',{}).get('Release Date','')
    if re.fullmatch(r'[A-Za-z]{3} [0-9]{1,2}, [0-9]{4}',release):
        try:
            date=time.strftime('%Y-%m-%d',time.strptime(release,'%b %d, %Y'))
            if isinstance(audio,MP4): tags['\xa9day']=[date]
            elif isinstance(audio,FLAC) or isinstance(tags,mutagen._vorbis.VCommentDict): tags['date']=date
            elif hasattr(tags,'add'):
                from mutagen.id3 import TDRC
                tags.add(TDRC(encoding=3,text=date))
        except ValueError: pass
    audio.save()
    check=mutagen.File(path)
    if not check or title not in str(check.tags): raise MediaError('Tag readback failed.')
    return 'Tags written and read back; original preserved. Dates not inferred. Order is tracker order.'
