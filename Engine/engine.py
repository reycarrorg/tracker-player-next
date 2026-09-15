"""Tracker Player: transactional catalog, registry and bounded background jobs.

No listening sockets. Native client communicates using JSON lines over pipes.
All source payloads are data, never executable instructions.
"""
import concurrent.futures, contextlib, datetime, fcntl, hashlib, json, os, re
import shutil, signal, sqlite3, stat, sys, threading, time, unicodedata, uuid
from pathlib import Path
from urllib.parse import urlsplit
import transport
import download_providers
from artwork import ArtworkCatalog
from delivery import DeliveryMixin, classification
from delivery_metadata import destination, public_record, base_name

MIB = 1048576

class Problem(ValueError):
    def __init__(self, code, message): super().__init__(message); self.code = code

def stamp(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
def encode(x): return json.dumps(x, ensure_ascii=False, separators=(',', ':'))
def normalized(x): return ' '.join(unicodedata.normalize('NFKC', str(x)).casefold().split())
def digest(path):
    h = hashlib.sha256()
    with regular_reader(path) as f:
        for b in iter(lambda: f.read(MIB), b''): h.update(b)
    return h.hexdigest()
def regular_reader(path):
    fd=os.open(path,os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd);raise Problem('invalid_file','An existing regular file is required.')
    return os.fdopen(fd,'rb')
def copy_verified(source,target,checksum,size):
    with regular_reader(source) as src, open(target,'xb') as dst:
        if os.fstat(src.fileno()).st_size!=size:raise Problem('changed','Source size changed before copying.')
        shutil.copyfileobj(src,dst,MIB);dst.flush();os.fsync(dst.fileno())
    if target.stat().st_size!=size or digest(target)!=checksum:
        raise Problem('copy_checksum','Copied bytes differ from the recorded source. Original preserved.')
def safe_name(x):
    x = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', '_', str(x)).strip(' .')
    return x.encode()[:100].decode('utf-8', 'ignore') or 'Untitled'
def contained(root, relative):
    root=Path(root).absolute();relative=Path(relative)
    if relative.is_absolute() or not relative.parts or '..' in relative.parts:
        raise Problem('unsafe_path','Expected a relative app-owned file path.')
    p=root/relative
    if any(x.is_symlink() for x in (p,*p.parents)):
        raise Problem('unsafe_path','Symbolic links are not allowed in app-owned paths.')
    return p
def atomic_json(path, payload):
    part = path.with_name(path.name + '.' + uuid.uuid4().hex + '.part')
    with open(part, 'x') as f: f.write(encode(payload)); f.flush(); os.fsync(f.fileno())
    os.replace(part, path)

class SnapshotCatalog:
    """Validate an immutable Google Sheets snapshot without embedding its private ID."""
    def __init__(self, path):
        self.data = json.loads(Path(path).read_text())
        c = self.data
        source=urlsplit(c.get('sourceUrl',''))
        if source.scheme!='https' or source.hostname!='docs.google.com' or not re.fullmatch(r'/spreadsheets/d/[A-Za-z0-9_-]{6,128}/edit',source.path): raise Problem('wrong_source', 'This adapter requires a captured Google Sheets tracker URL.')
        rows = c.get('rows', [])
        if not rows or len({r['id'] for r in rows}) != len(rows): raise Problem('invalid_identity', 'Source IDs must be present and unique.')
        for r in rows:
            for k in ('id','workbook','era','title','name','sourceHash','sourceUrl','fields','links'):
                if k not in r: raise Problem('invalid_row', 'Missing source field: ' + k)
            if not isinstance(r['id'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,96}',r['id']):
                raise Problem('invalid_identity','Source identity contains unsafe file-name characters.')
        expected=hashlib.sha256(json.dumps([(r['id'],r['sourceHash']) for r in rows],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        if c.get('snapshotHash') != expected: raise Problem('revision_mismatch', 'Snapshot checksum does not match its source rows.')
        artwork_dir = Path(path).with_name('Artwork')
        self.bundled_art = ArtworkCatalog(artwork_dir, {(r['workbook'],r['era']) for r in rows} if artwork_dir.exists() else None)
        self.art_candidates={}
        for r in rows:
            if r['workbook']=='Art' and r['fields'].get('Project Type')=='Front Cover' and r['fields'].get('Use')=='Used':
                self.art_candidates.setdefault((normalized(r['era']),normalized(r['title'])),[]).append(r)
    def matching_art(self,era,title):
        candidates=self.art_candidates.get((normalized(era),normalized(title)),[])
        if len(candidates)!=1:return None
        r=candidates[0]
        if r.get('ambiguous') or len(r['links'])!=1:return None
        return {'rowId':r['id'],'url':r['links'][0],'sourceUrl':r['sourceUrl'],'association':'inferred_name_match','exportEligible':False}
    def artwork(self, r):
        # Legacy dictionaries picked the first duplicate and hid competing covers.
        if (normalized(r['era']),normalized(r['title'])) in self.art_candidates:
            return self.matching_art(r['era'],r['title'])
        return self.matching_art(r['era'],r['era'])
    @staticmethod
    def explicit_artist(r):
        # Do not export the 0.2 adapter's inferred solo artist.
        values={str(r['fields'].get(label,'')).strip() for label in ('Artist','Artists','Primary Artist')}
        values.discard('')
        return values.pop() if len(values)==1 else ''

class Engine(DeliveryMixin):
    def __init__(self, root, snapshot, seed=None):
        self.root = Path(root).absolute()
        if any(x.is_symlink() for x in (self.root,*self.root.parents)):raise Problem('unsafe_path','Library root must not contain symbolic links.')
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ('engine.lock','Library.sqlite','Library.sqlite-wal','Library.sqlite-shm','Downloads','Cache','Artwork','Exports'):contained(self.root,name)
        self.file_lock = open(self.root/'engine.lock','a+')
        try: fcntl.flock(self.file_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file_lock.close();raise Problem('already_open', 'Another Tracker Player Next window owns this library.')
        self.lock = threading.RLock(); self.cache_lock = threading.RLock()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix='transfer')
        self.cancelled=set(); self.closing=False; self.pins={}; self.art_failed=set();self.reservations={};self.size_requests={}
        try:self.initialize(snapshot,seed)
        except BaseException:
            self.pool.shutdown(wait=False,cancel_futures=True)
            if hasattr(self,'db'):self.db.close()
            self.file_lock.close();raise
    def initialize(self,snapshot,seed):
        for name in ('Downloads','Cache','Artwork','Exports'): (self.root/name).mkdir(exist_ok=True)
        self.db=sqlite3.connect(self.root/'Library.sqlite',check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        version=self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0,1): raise Problem('newer_database','This library was created by a newer version. It was left unchanged.')
        self.integrity=self.db.execute('PRAGMA quick_check').fetchone()[0]
        if self.integrity!='ok':raise Problem('integrity','Database integrity check failed; library preserved.')
        self.db.executescript('PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA synchronous=FULL; PRAGMA busy_timeout=5000;')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS revisions(id TEXT PRIMARY KEY, captured TEXT, source TEXT, imported TEXT, row_count INTEGER, metadata TEXT);
        CREATE TABLE IF NOT EXISTS rows(id TEXT PRIMARY KEY, revision TEXT REFERENCES revisions(id), workbook TEXT, era TEXT, title TEXT, name TEXT, version TEXT, kind TEXT, ordinal INTEGER, source_row INTEGER, ambiguous INTEGER, eligible INTEGER, search TEXT, payload TEXT);
        CREATE INDEX IF NOT EXISTS rows_scope ON rows(workbook,era,ordinal);
        CREATE INDEX IF NOT EXISTS rows_state ON rows(kind,ambiguous,eligible);
        CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(id UNINDEXED, text, tokenize='unicode61 remove_diacritics 2');
        CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY REFERENCES rows(id), path TEXT, checksum TEXT, bytes INTEGER, kind TEXT, mode TEXT, state TEXT, downloaded TEXT, verified TEXT, record TEXT);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, row_id TEXT REFERENCES rows(id), batch TEXT, state TEXT, bytes INTEGER DEFAULT 0, total INTEGER, received INTEGER DEFAULT 0, created TEXT, updated TEXT, error TEXT, code TEXT);
        CREATE INDEX IF NOT EXISTS job_state ON jobs(state);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, at TEXT, type TEXT, row_id TEXT, payload TEXT);
        CREATE TABLE IF NOT EXISTS exports(id TEXT PRIMARY KEY, row_id TEXT REFERENCES rows(id), path TEXT, checksum TEXT, created TEXT, payload TEXT);
        CREATE TABLE IF NOT EXISTS preferences(key TEXT PRIMARY KEY, payload TEXT);
        PRAGMA user_version=1;
        ''')
        self.adapter=SnapshotCatalog(snapshot); self.catalog=self.adapter.data
        self.ingest()
        if seed and not self.pref('legacyImported',False): self.import_legacy(Path(seed))
        with self.transaction():
            self.db.execute("UPDATE jobs SET state='interrupted',code='interrupted',error='App closed before completion. Retry restarts this file; completed files are preserved.' WHERE state IN ('queued','running','cancelling','awaiting_approval')")
            # Reservations exist only during a live read, never across a restart.
            for rec in self.db.execute("SELECT key,payload FROM preferences WHERE key LIKE 'batch:%'").fetchall():
                budget=json.loads(rec['payload']);budget.pop('reserved',None);self.setpref(rec['key'],budget)
        self.recover_parts()
        self.integrity=self.db.execute('PRAGMA quick_check').fetchone()[0]
        if self.integrity!='ok': raise Problem('integrity','Database integrity check failed. Restore from the preserved backup.')

    def recover_parts(self):
        # The exclusive library lock proves no previous worker is still writing.
        # Inspect only immediate private scratch directories, never media trees.
        for folder in ('Cache','Artwork'):
            for p in (self.root/folder).iterdir():
                if re.fullmatch(r'[A-Za-z0-9_-]{1,96}(\.[a-f0-9]{32})?\.part',p.name) and not p.is_symlink() and p.is_file():p.unlink()
        for job in self.db.execute("SELECT id FROM jobs WHERE state IN ('interrupted','failed','cancelled','completed')"):
            if not re.fullmatch(r'[a-f0-9]{32}',job[0]):continue
            p=contained(self.root/'Downloads','.'+job[0]+'.part')
            if p.is_file():p.unlink()

    @contextlib.contextmanager
    def transaction(self):
        # sqlite Connection context managers commit nested callers prematurely.
        with self.lock:
            name='s'+uuid.uuid4().hex;self.db.execute('SAVEPOINT '+name)
            try:
                yield
                self.db.execute('RELEASE '+name)
            except BaseException:
                self.db.execute('ROLLBACK TO '+name);self.db.execute('RELEASE '+name);raise

    def ingest(self):
        c=self.catalog; rev=c['snapshotHash']
        with self.transaction():
            prior=self.db.execute('SELECT id FROM revisions').fetchone()
            if prior:
                if prior[0]!=rev: raise Problem('source_changed','Different source revision requires an explicit future source import. Current library was preserved.')
                return
            meta={k:v for k,v in c.items() if k!='rows'}
            self.db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?)',(rev,c['captured'],c['sourceUrl'],stamp(),len(c['rows']),encode(meta)))
            records=[]; searches=[]
            for i,r in enumerate(c['rows']):
                text=normalized(r['name']+' '+r['era']+' '+' '.join(str(v) for v in r['fields'].values()))
                records.append((r['id'],rev,r['workbook'],r['era'],r['title'],r['name'],r.get('version',''),r['kind'],i,r['row'],int(r['ambiguous']),int(r['eligible']),text,encode(r)))
                searches.append((r['id'],text))
            self.db.executemany('INSERT INTO rows VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',records)
            self.db.executemany('INSERT INTO search_index VALUES(?,?)',searches)
            self.event('source_import',None,{'revision':rev,'rows':len(records)})

    def pref(self,key,default=None):
        with self.lock:
            row=self.db.execute('SELECT payload FROM preferences WHERE key=?',(key,)).fetchone()
            return json.loads(row[0]) if row else default
    def setpref(self,key,value):
        with self.transaction(): self.db.execute('INSERT OR REPLACE INTO preferences VALUES(?,?)',(key,encode(value)))
    def event(self,type,rid,payload): self.db.execute('INSERT INTO events(at,type,row_id,payload) VALUES(?,?,?,?)',(stamp(),type,rid,encode(payload)))
    def import_legacy(self,seed):
        state=contained(seed,'state.json')
        if not state.exists():return
        s=json.loads(state.read_text());records=[];created=[];parts=[]
        try:
            # Validate and copy first. A failed attempt exposes no registry/session
            # changes; existing destinations and the seed are never overwritten.
            for rid,record in s.get('files',{}).items():
                if not self.db.execute('SELECT 1 FROM rows WHERE id=?',(rid,)).fetchone():continue
                relative=record['relativePath'];source=contained(seed/'Downloads',relative);target=contained(self.root/'Downloads',relative)
                valid=source.is_file() and source.stat().st_size==record['bytes'] and digest(source)==record['checksum']
                if valid and not target.exists():
                    self.disk_guard(record['bytes']);target.parent.mkdir(parents=True,exist_ok=True)
                    part=target.with_name('.'+target.name+'.'+uuid.uuid4().hex+'.part');parts.append(part)
                    copy_verified(source,part,record['checksum'],record['bytes'])
                    os.link(part,target);created.append(target);part.unlink()
                status='detached' if record.get('forgotten') else ('available' if target.is_file() and target.stat().st_size==record['bytes'] and digest(target)==record['checksum'] else 'missing')
                records.append((rid,str(target),record['checksum'],record['bytes'],record['kind'],'permanent',status,record.get('downloadedAt'),stamp() if status=='available' else None,encode(record)))
            old=s.get('playback',{})
            with self.transaction():
                self.db.executemany('INSERT OR IGNORE INTO files VALUES(?,?,?,?,?,?,?,?,?,?)',records)
                self.event('legacy_history',None,s)
                self.event('copy_migration',None,{'files':len(records),'source':'0.2 preserved snapshot','originalsModified':False})
                self.setpref('session',{'workbook':old.get('workbook','Unreleased'),'selected':old.get('selected'),'playing':old.get('playing'),'time':old.get('time',0),'volume':old.get('volume',.8)})
                self.setpref('legacyImported',True)
        except BaseException:
            for target in created:
                if target.exists():target.unlink()
            raise
        finally:
            for part in parts:
                if part.exists():part.unlink()

    def summary(self,r):
        payload=json.loads(r['payload'])
        return {k:r[k] for k in ('id','workbook','era','title','name','version','kind','source_row','ambiguous','eligible')} | {'availability':r['availability'] or 'remote','bytes':r['file_bytes'] or 0,'sourceCount':len(payload.get('links',[])),'fields':{k:v for k,v in payload.get('fields',{}).items() if k in ('Available Length','Quality','Track Length','Length')}}
    def where(self,p):
        clauses=[];args=[]
        for key in ('workbook','era','kind'):
            if p.get(key): clauses.append('r.'+key+'=?');args.append(p[key])
        q=normalized(p.get('query',''))[:500]
        if q:
            terms=re.findall(r'\w+',q)
            if terms:
                expression=' AND '.join('"'+x.replace('"','""')+'"*' for x in terms)
                clauses.append('r.id IN (SELECT id FROM search_index WHERE text MATCH ?)');args.append(expression)
        f=p.get('filter','all')
        if f=='local': clauses.append("f.state='available'")
        if f=='ambiguous': clauses.append('r.ambiguous=1')
        if f=='missing': clauses.append("f.state IN ('missing','changed')")
        if f=='playable' or p.get('playableOnly') is True: clauses.append("r.eligible=1 AND r.kind IN ('audio','video')")
        if p.get('saveOnly') is True:clauses.append("r.eligible=1 AND r.ambiguous=0 AND (f.state IS NULL OR f.state!='available')")
        return (' WHERE '+' AND '.join(clauses) if clauses else ''),args
    def query(self,p):
        where,args=self.where(p);limit=max(1,min(int(p.get('limit',500)),1000));offset=max(0,int(p.get('offset',0)))
        with self.lock:
            total=self.db.execute('SELECT count(*) FROM rows r LEFT JOIN files f ON r.id=f.id'+where,args).fetchone()[0]
            data=self.db.execute("SELECT r.*,f.state availability,f.bytes file_bytes FROM rows r LEFT JOIN files f ON r.id=f.id"+where+' ORDER BY r.ordinal LIMIT ? OFFSET ?',args+[limit,offset]).fetchall()
        return {'rows':[self.summary(r) for r in data],'total':total,'offset':offset}
    def ids(self,p):
        where,args=self.where(p)
        with self.lock: return [r[0] for r in self.db.execute('SELECT r.id FROM rows r LEFT JOIN files f ON r.id=f.id'+where+' ORDER BY r.ordinal',args)]
    def shuffle_candidates(self,p):
        requested=set(p.get('ids',[]));result=[];short=unknown=0
        for row in self.catalog['rows']:
            if row['id'] not in requested or not row.get('eligible') or row.get('kind') not in ('audio','video'):continue
            fields=row.get('fields',{});raw=str(fields.get('Track Length') or fields.get('Length') or '').strip()
            match=re.fullmatch(r'(\d+):([0-5]\d)(?::([0-5]\d))?',raw)
            seconds=None
            if match:
                a,b,c=match.groups();seconds=int(a)*60+int(b) if c is None else int(a)*3600+int(b)*60+int(c)
            if seconds is None:
                unknown+=1
                if not p.get('includeUnknown',True):continue
            elif seconds<30:
                short+=1
                if p.get('skipShort',False):continue
            result.append(row['id'])
        return {'ids':result,'short':short,'unknown':unknown}
    def row(self,rid):
        with self.lock:
            r=self.db.execute('SELECT payload FROM rows WHERE id=?',(rid,)).fetchone()
            if not r: raise Problem('unknown_row','The source row is absent from this revision.')
            return json.loads(r[0])
    def detail(self,p):
        r=self.row(p['id'])
        with self.lock:
            f=self.db.execute('SELECT * FROM files WHERE id=?',(r['id'],)).fetchone()
            attempts=[dict(x) for x in self.db.execute('SELECT state,error,code,updated FROM jobs WHERE row_id=? ORDER BY created DESC LIMIT 5',(r['id'],))]
            exports=[dict(x) for x in self.db.execute('SELECT * FROM exports WHERE row_id=? ORDER BY created DESC',(r['id'],))]
        era_art = self.adapter.bundled_art.for_group(r['workbook'],r['era']) or self.delivery_art(dict(r,id='__group__'))
        return {'row':r,'file':dict(f) if f else None,'artwork':self.delivery_art(r),'eraArtwork':era_art,'artistForExport':self.adapter.explicit_artist(r),'attempts':attempts,'exports':exports}
    def boot(self,p):
        with self.lock:
            workbooks=[{'name':r['workbook'],'count':r['n']} for r in self.db.execute('SELECT workbook,count(*) n FROM rows GROUP BY workbook ORDER BY min(ordinal)')]
        return {'workbooks':workbooks,'count':len(self.catalog['rows']),'revision':self.catalog['snapshotHash'],'captured':self.catalog['captured'],'session':self.pref('session',{}),'root':str(self.root),'integrity':self.integrity,'limits':self.limits()}
    def eras(self,p):
        with self.lock:
            groups=self.db.execute("SELECT r.era,count(*) n,sum(CASE WHEN f.state='available' THEN 1 ELSE 0 END) downloaded FROM rows r LEFT JOIN files f ON r.id=f.id WHERE r.workbook=? GROUP BY r.era ORDER BY min(r.ordinal)",(p['workbook'],)).fetchall()
        result=[]
        for group in groups:
            era=group['era'];meta=self.catalog.get('eras',{}).get(p['workbook']+'|'+era,{})
            sample=next(r for r in self.catalog['rows'] if r['workbook']==p['workbook'] and r['era']==era)
            display_art=self.adapter.bundled_art.for_group(p['workbook'],era) or self.delivery_art(dict(sample,id='__group__'))
            result.append({'name':era,'count':group['n'],'downloaded':group['downloaded'] or 0,'metadata':meta,'artwork':display_art})
        return result
    def limits(self): return self.pref('limits',{'count':25,'fileMB':128,'batchMB':512,'cacheMB':256,'previewMB':64})
    def settings(self,p):
        cfg=self.limits()
        for k,hi in [('count',250),('fileMB',1024),('batchMB',4096),('cacheMB',1024),('previewMB',128)]:
            if k in p:
                v=int(p[k])
                if not 1<=v<=hi: raise Problem('limit','Value outside allowed bounds: '+k)
                cfg[k]=v
        if cfg['cacheMB']<cfg['previewMB']: raise Problem('limit','Cache budget must be at least the preview budget.')
        self.setpref('limits',cfg);return cfg
    def local(self,rid,verify=True):
        with self.transaction():
            rec=self.db.execute('SELECT * FROM files WHERE id=?',(rid,)).fetchone()
            if not rec or rec['state']=='detached':return None
            p=Path(rec['path']);status='available'
            try:
                if rec['mode']=='permanent':p=contained(self.root/'Downloads',p.relative_to(self.root/'Downloads'))
                if p.is_symlink():status='changed'
                elif not p.is_file():status='missing'
                elif p.stat().st_size!=rec['bytes'] or (verify and digest(p)!=rec['checksum']):status='changed'
            except (OSError,ValueError):status='changed'
            self.db.execute('UPDATE files SET state=?,verified=? WHERE id=?',(status,stamp() if verify and status=='available' else rec['verified'],rid))
        return dict(rec)|{'state':status} if status=='available' else None

    def prepare(self,p):
        r=self.row(p['id']);source=p.get('source');force=p.get('remote',False)
        if source is not None and source not in r['links']: raise Problem('unknown_source','Choose a source attached to this row.')
        local=self.local(r['id'])
        if local and not source and not force:
            return {'path':local['path'],'kind':local['kind'],'mode':local['mode'],'notice':'SHA-256 verified','id':r['id']}
        links=[source] if source else download_providers.ordered_sources(r['links'])
        if not links: raise Problem('no_source','This row has no media link. Its tracker fields remain available.')
        approved=0
        if p.get('sizeToken'):
            with self.lock:request=self.size_requests.pop(p['sizeToken'],None)
            if not request or request['rowId']!=r['id'] or request['source'] not in links or request['expires']<time.monotonic():
                raise Problem('stale_approval','This file approval expired or belongs to another source. Try Play again.')
            links=[request['source']];approved=request['requestedBytes'];self.disk_guard(approved)
        errors=[]
        with self.cache_lock:
            if self.closing:raise Problem('closing','Player is shutting down.')
            for url in links:
                key=hashlib.sha256((r['id']+'|'+url).encode()).hexdigest()[:32]
                meta=contained(self.root/'Cache',key+'.json')
                if meta.exists():
                    try:
                        saved=json.loads(meta.read_text());target=contained(self.root/'Cache',saved['filename'])
                        if target.stem==key and target.is_file() and target.stat().st_size==saved['bytes'] and digest(target)==saved['checksum']:
                            self.pin(target,saved['kind']);return saved|{'path':str(target),'mode':'temporary','notice':'Verified temporary cache; original media endpoint','id':r['id']}
                    except (ValueError,KeyError,OSError):pass
                cfg=self.limits();file_limit=approved or cfg['previewMB']*MIB
                cache_cap=cfg['cacheMB']*MIB+approved
                self.trim_cache(min(cache_cap,file_limit+4096),cap=cache_cap)
                part=contained(self.root/'Cache',key+'.'+uuid.uuid4().hex+'.part')
                try:
                    info=download_providers.download(url,part,file_limit,consume=self.disk_guard,cancel=lambda:self.closing,original_media=True)
                    if self.closing:raise Problem('closing','Player is shutting down.')
                    self.trim_cache(4096,cap=cache_cap)
                    target=contained(self.root/'Cache',key+'.'+info['extension'])
                    if str(target) in self.pins:raise Problem('cache_pinned','Stop playback before replacing this cached recording.')
                    os.replace(part,target)
                    saved=info|{'filename':target.name,'sourceAttempts':errors};atomic_json(meta,saved);self.pin(target,info['kind'])
                    with self.transaction():self.event('preview',r['id'],saved)
                    return saved|{'path':str(target),'mode':'temporary','notice':'Bounded temporary copy; easiest attached source tried first','id':r['id']}
                except transport.FileLimit as e:
                    return {'approval':self.size_request(r,url,e)}
                except Exception as e:
                    failure={'source':url,'classification':classification(e),'message':str(e)};errors.append(failure)
                    with self.transaction():self.event('source_failure',r['id'],public_record(failure))
                    if source is not None or classification(e)=='cancelled':raise
                finally:
                    if part.exists():part.unlink()
        last=errors[-1] if errors else {'classification':'access_unavailable','message':'No attached source succeeded.'}
        raise transport.AccessError(last['classification'],last['message'])
    def disk_guard(self,n):
        if shutil.disk_usage(self.root).free < 256*MIB+n: raise Problem('disk_reserve','256 MB free-space reserve reached.')
    def size_request(self,row,url,error):
        token=uuid.uuid4().hex
        request={'token':token,'rowId':row['id'],'title':row['title'],'source':url,'limitBytes':error.limit,'totalBytes':error.total,'requestedBytes':error.requested,'expires':time.monotonic()+600}
        with self.lock:
            self.size_requests={k:v for k,v in self.size_requests.items() if v['expires']>time.monotonic()}
            self.size_requests[token]=request
        return request
    def discard_size(self,p):
        with self.lock:self.size_requests.pop(p.get('token'),None)
        return {'ok':True}
    def size_decision(self,p):
        with self.transaction():
            job=self.db.execute("SELECT * FROM jobs WHERE id=? AND state='awaiting_approval'",(p['id'],)).fetchone()
            if not job:raise Problem('stale_approval','This transfer is no longer waiting for approval.')
            request=self.pref('fileApproval:'+job['id'],{})
            if p.get('allow') is not True:
                self.job_update(job['id'],state='cancelled',code='declined',error='Large download declined. Default limits unchanged.')
                return {'ok':True}
            amount=request.get('requestedBytes',0)
            if amount<=0:raise Problem('stale_approval','No valid file size is available. Retry this transfer.')
            self.disk_guard(amount)
            cfg=self.pref('batch:'+job['batch']);cfg.setdefault('fileLimits',{})[job['id']]=amount
            self.setpref('batch:'+job['batch'],cfg)
            self.job_update(job['id'],state='queued',bytes=0,error='',code='')
            self.pool.submit(self.run_job,job['id'])
        return {'ok':True}
    def trim_cache(self,reserve,cap=None):
        entries=[p for folder in ('Cache','Artwork') for p in (self.root/folder).iterdir()]
        if any(p.is_symlink() or not p.is_file() for p in entries):raise Problem('unsafe_cache','Unexpected cache entry; no files removed.')
        evictable=[p for p in entries if not p.name.startswith('assigned-')]
        total=sum(p.stat().st_size for p in evictable)
        cap=self.limits()['cacheMB']*MIB if cap is None else cap
        for p in sorted(evictable,key=lambda x:x.stat().st_mtime):
            if total+reserve<=cap:break
            if str(p) in self.pins or p.suffix=='.part':continue
            if p.suffix=='.json' and any(Path(pin).stem==p.stem for pin in self.pins):continue
            size=p.stat().st_size;p.unlink();total-=size
        if total+reserve>cap:raise Problem('cache_full','Cache is pinned by playback. Stop playback or increase the cache budget.')
    def pin(self,path,kind):
        if kind in ('audio','video'):self.pins[str(path)]=self.pins.get(str(path),0)+1
    def unpin(self,p):
        with self.cache_lock:
            path=p.get('path','');count=self.pins.get(path,0)
            if count<=1:self.pins.pop(path,None)
            else:self.pins[path]=count-1
        return {'ok':True}
    def art(self,p):
        rid=p['id']
        if rid.startswith('assigned-'):
            matches=[v for v in self.pref('artAssignments',{}).values() if 'assigned-'+v['sha256']==rid]
            if not matches:raise Problem('no_art','No assigned image for this identity.')
            target=contained(self.root/'Artwork',matches[0]['file'])
            if not target.is_file() or digest(target)!=matches[0]['sha256']:raise Problem('no_art','Assigned image is missing or changed.')
            return {'path':str(target),'exportEligible':True}
        bundled = self.adapter.bundled_art.path(rid)
        if bundled: return {'path':bundled,'bundled':True,'exportEligible':False}
        if p.get('localOnly'): raise Problem('no_local_art','No bundled artwork for this identity.')
        r=self.row(rid)
        if r['workbook']!='Art' or r['ambiguous'] or len(r['links'])!=1:raise Problem('no_art','Artwork requires one unambiguous source.')
        target=contained(self.root/'Artwork',rid)
        meta=contained(self.root/'Artwork',rid+'.json')
        with self.cache_lock:
            if self.closing:raise Problem('closing','Player is shutting down.')
            if target.is_file() and meta.is_file():
                try:
                    saved=json.loads(meta.read_text())
                    if saved['bytes']<=8*MIB and target.stat().st_size==saved['bytes'] and digest(target)==saved['checksum']:return {'path':str(target)}
                except (ValueError,KeyError,OSError):pass
            if rid in self.art_failed:raise Problem('art_unavailable','Artwork was unavailable this session. Retry after reopening.')
            self.trim_cache(8*MIB+4096)
            part=target.with_name(rid+'.'+uuid.uuid4().hex+'.part')
            try:
                info=download_providers.download(r['links'][0],part,8*MIB,art=True,consume=self.disk_guard,cancel=lambda:self.closing)
                if self.closing:raise Problem('closing','Player is shutting down.')
                os.replace(part,target);atomic_json(meta,info);return {'path':str(target)}
            except Exception:self.art_failed.add(rid);raise
            finally:
                if part.exists():part.unlink()

    def enqueue(self,p):
        ids=list(dict.fromkeys(p.get('ids',[])));cfg=self.limits()
        if not ids or (len(ids)>cfg['count'] and not p.get('allEra')):raise Problem('batch_cap',f'Select 1–{cfg["count"]} rows per batch. Use Download All for a complete era.')
        rows=[self.row(i) for i in ids]
        self.disk_guard(cfg['fileMB']*MIB)
        batch=uuid.uuid4().hex;jobs=[]
        with self.transaction():
            if self.closing:raise Problem('closing','Player is shutting down.')
            if self.db.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running','cancelling','awaiting_approval')").fetchone():raise Problem('batch_active','Finish or cancel the active batch first.')
            workbook=p.get('workbook') or (rows[0]['workbook'] if rows and len({r['workbook'] for r in rows})==1 else '')
            era=p.get('era') or (rows[0]['era'] if rows and len({(r['workbook'],r['era']) for r in rows})==1 else '')
            label=(workbook+' / '+era) if workbook and era else ('Selected rows in '+workbook if workbook else 'Selected source rows')
            self.setpref('batch:'+batch,{'received':0,'limit':(2**60 if p.get('allEra') else cfg['batchMB']*MIB),'fileLimit':cfg['fileMB']*MIB,'allEra':bool(p.get('allEra')),'total':len(rows),'workbook':workbook,'era':era,'label':label})
            for r in rows:
                jid=uuid.uuid4().hex;jobs.append(jid)
                self.db.execute('INSERT INTO jobs(id,row_id,batch,state,created,updated) VALUES(?,?,?,?,?,?)',(jid,r['id'],batch,'queued',stamp(),stamp()))
            for jid in jobs:self.pool.submit(self.run_job,jid)
        return {'jobs':jobs,'batch':batch}
    def job_update(self,jid,**kw):
        kw['updated']=stamp()
        with self.transaction():self.db.execute('UPDATE jobs SET '+','.join(k+'=?' for k in kw)+' WHERE id=?',list(kw.values())+[jid])
    def run_job(self,jid):
        with self.transaction():
            found=self.db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
            if not found or found['state'] not in ('queued','cancelling'):return
            job=dict(found)
            if jid in self.cancelled or self.closing or job['state']=='cancelling':self.job_update(jid,state='cancelled',code='cancelled',error='Cancelled before transfer.');return
            self.job_update(jid,state='running',error='',code='')
        row=self.row(job['row_id']);part=self.root/'Downloads'/('.'+jid+'.part');asset_lock=None
        try:
            existing=self.local(row['id'])
            if existing and not json.loads(existing['record']).get('metadataFailure'):
                self.job_update(jid,state='skipped',error='Existing file checksum verified.');return
            if not row['links']:raise Problem('no_source','No source link supplied.')
            if row['ambiguous'] or not row.get('eligible'):
                raise transport.AccessError('access_unavailable','Source identity requires user review or is not classified as an available file.')
            cfg=self.pref('batch:'+job['batch']);last=[0.0]
            file_limit=cfg.get('fileLimits',{}).get(jid,cfg['fileLimit'])
            approved=jid in cfg.get('fileLimits',{})
            def consume(n):
                with self.transaction():
                    budget=self.pref('batch:'+job['batch']);budget['received']+=n
                    if approved:budget['approvedReceived']=budget.get('approvedReceived',0)+n
                    self.setpref('batch:'+job['batch'],budget)
                    self.db.execute('UPDATE jobs SET received=received+? WHERE id=?',(n,jid))
                    self.reservations[jid]=max(0,self.reservations.get(jid,0)-n)
                # Received bytes stay recorded even if disk or budget checks fail.
                if not approved and budget['received']-budget.get('approvedReceived',0)>budget['limit']:raise Problem('batch_bytes','Received-byte batch budget reached, including failed attempts.')
                self.disk_guard(n)
            def reserve(n):
                with self.lock:
                    if jid in self.cancelled or self.closing:raise Problem('cancelled','Transfer cancelled.')
                    budget=self.pref('batch:'+job['batch']);reserved=sum(v for k,v in self.reservations.items() if k not in budget.get('fileLimits',{}))
                    count=n if approved else max(0,min(n,budget['limit']-budget['received']+budget.get('approvedReceived',0)-reserved));self.reservations[jid]=count;return count
            def release(n):
                with self.lock:
                    self.reservations.pop(jid,None)
            def progress(n,total):
                if time.monotonic()-last[0]>.15:self.job_update(jid,bytes=n,total=total);last[0]=time.monotonic()
            if existing:
                copy_verified(Path(existing['path']),part,existing['checksum'],existing['bytes'])
                info=json.loads(existing['record']);info.pop('metadataFailure',None)
                info.update(bytes=existing['bytes'],checksum=existing['checksum'])
            else:
                attempts=[];last_error=None
                for source in download_providers.ordered_sources(row['links']):
                    try:
                        info,asset_lock=self.request_asset(row,part,file_limit,source=source,consume=consume,cancel=lambda:jid in self.cancelled or self.closing,progress=progress,reserve=reserve,release=release)
                        info['sourceAttempts']=attempts;break
                    except transport.FileLimit:raise
                    except Exception as error:
                        last_error=error;attempts.append({'source':source,'classification':classification(error),'message':str(error)})
                        if part.exists():part.unlink()
                        if classification(error)=='cancelled':raise
                else:
                    self.recovery_failure(row,job,last_error or transport.AccessError('access_unavailable','No attached source succeeded.'),source=attempts[-1]['source'] if attempts else '',attempts=attempts)
                    return
            if part.stat().st_size!=info['bytes'] or digest(part)!=info['checksum']:raise Problem('download_checksum','Transfer checksum mismatch.')
            info=self.tag_download(part,row,info)
            folder=Path('Suzy Tracker')/safe_name(row['workbook'])/safe_name(row['era'])
            directory=contained(self.root/'Downloads',folder);directory.mkdir(parents=True,exist_ok=True)
            target=destination(directory,row,info['extension'],info['checksum'])
            record=public_record(info|{'sourceSnapshot':row,'revision':self.catalog['snapshotHash']})
            installed=False
            with self.transaction():
                if jid in self.cancelled or self.closing:raise Problem('cancelled','Cancelled; no permanent file installed.')
                if part.stat().st_size!=info['bytes'] or digest(part)!=info['checksum']:raise Problem('download_checksum','Transfer checksum mismatch.')
                if target.exists():
                    if digest(target)!=info['checksum']:raise Problem('collision','A different file occupies the destination. Nothing overwritten.')
                else:os.link(part,target);installed=True
                try:
                    self.db.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?,?)',(row['id'],str(target),info['checksum'],info['bytes'],info['kind'],'permanent','available',stamp(),stamp(),encode(record)))
                    self.event('download',row['id'],record)
                    warning=info.get('metadataFailure') or info.get('artworkAssignment',{}).get('reason','')
                    self.job_update(jid,state='metadata_failed' if info.get('metadataFailure') else 'completed',code='metadata_failed' if info.get('metadataFailure') else 'downloadable',error=warning,bytes=info['bytes'],total=info['bytes'])
                except BaseException:
                    if installed:target.unlink()
                    raise
            try:self.manifest({})
            except Exception as e:
                with self.transaction():self.event('manifest_failure',row['id'],{'message':str(e)})
        except transport.FileLimit as e:
            if part.exists():part.unlink()
            with self.transaction():
                if jid in self.cancelled or self.closing:self.job_update(jid,state='cancelled',code='cancelled',error='Cancelled.')
                else:
                    request={'title':row['title'],'limitBytes':e.limit,'totalBytes':e.total,'requestedBytes':e.requested,'rowId':row['id']}
                    self.setpref('fileApproval:'+jid,request)
                    self.job_update(jid,state='awaiting_approval',code='file_limit',total=e.total,error=str(e))
        except Exception as e:
            cancelled=jid in self.cancelled or self.closing
            if cancelled:self.job_update(jid,state='cancelled',error='Cancelled.',code='cancelled')
            else:self.recovery_failure(row,job,e)
        finally:
            if asset_lock:asset_lock.release()
            with self.lock:self.reservations.pop(jid,None)
            if part.exists():part.unlink()
    def cancel(self,p):
        with self.lock:
            ids=[r[0] for r in self.db.execute("SELECT id FROM jobs WHERE state IN ('queued','running','awaiting_approval','awaiting_access')")]
            for jid in ids:
                self.cancelled.add(jid)
                state=self.db.execute('SELECT state FROM jobs WHERE id=?',(jid,)).fetchone()[0]
                if state=='awaiting_access':self.placeholder(self.row(self.db.execute('SELECT row_id FROM jobs WHERE id=?',(jid,)).fetchone()[0]),jid,'access_unavailable','User cancelled source recovery.')
                else:self.job_update(jid,state='cancelled' if state in ('queued','awaiting_approval') else 'cancelling',code='cancelled',error='Stopping after the current network operation.')
        return {'count':len(ids)}
    def legacy_retry(self,p):
        with self.lock:
            row=self.db.execute("SELECT row_id FROM jobs WHERE id=? AND state IN ('failed','cancelled','interrupted')",(p['id'],)).fetchone()
        if not row:raise Problem('not_retryable','Only stopped or failed transfers can be retried.')
        return self.enqueue({'ids':[row[0]]})
    def activity(self,p):
        with self.lock:
            jobs=[dict(x) for x in self.db.execute('SELECT j.*,r.title,r.workbook,r.era FROM jobs j JOIN rows r ON r.id=j.row_id ORDER BY j.created DESC LIMIT 150')]
            for job in jobs:
                if job['state']=='awaiting_approval':job['approval']=self.pref('fileApproval:'+job['id'],{})
            exports=[dict(x) for x in self.db.execute('SELECT e.*,r.title FROM exports e JOIN rows r ON r.id=e.row_id ORDER BY created DESC LIMIT 100')]
            batches=[]
            for b in self.db.execute('SELECT batch,max(created) recent,count(*) total FROM jobs GROUP BY batch ORDER BY recent DESC LIMIT 10').fetchall():
                counts=dict(self.db.execute('SELECT state,count(*) FROM jobs WHERE batch=? GROUP BY state',(b['batch'],)).fetchall())
                cfg=self.pref('batch:'+b['batch'],{})
                batches.append(dict(id=b['batch'],batch=b['batch'],total=b['total'],counts=counts,workbook=cfg.get('workbook',''),era=cfg.get('era',''),label=cfg.get('label','Selected source rows')))
        return {'jobs':jobs,'exports':exports,'limits':self.limits(),'batches':batches}
    def stats(self,p):
        where,args=self.where(p)
        with self.lock:
            s=dict(self.db.execute("SELECT count(*) rows,sum(r.ambiguous) ambiguous,sum(r.eligible) eligible,sum(CASE WHEN f.id IS NOT NULL THEN 1 ELSE 0 END) downloaded,sum(CASE WHEN f.state='available' THEN 1 ELSE 0 END) available,sum(CASE WHEN f.state IN ('missing','changed') THEN 1 ELSE 0 END) missing,max(f.downloaded) last_download,max(f.verified) last_verified FROM rows r LEFT JOIN files f ON r.id=f.id"+where,args).fetchone())
        s={k:(v if v is not None else ('' if k.startswith('last') else 0)) for k,v in s.items()}
        s['complete']=s['rows']>0 and s['available']==s['rows'];s['root']=str(self.root)
        with self.cache_lock:
            s['cacheBytes']=sum(x.stat().st_size for folder in ('Cache','Artwork') for x in (self.root/folder).iterdir() if x.is_file() and not x.is_symlink() and not x.name.startswith('assigned-'))
            s['assignedArtworkBytes']=sum(x.stat().st_size for x in (self.root/'Artwork').iterdir() if x.is_file() and not x.is_symlink() and x.name.startswith('assigned-'))
        s['integrity']=self.integrity;s['snapshot']=self.catalog['captured'];return s
    def verify(self,p):
        with self.lock:ids=[x[0] for x in self.db.execute('SELECT id FROM files')]
        n=sum(bool(self.local(i)) for i in ids)
        return {'verified':n,'missingOrChanged':len(ids)-n}
    def detach(self,p):
        with self.transaction():
            self.db.execute("UPDATE files SET state='detached' WHERE id=?",(p['id'],));self.event('detach',p['id'],{})
        return {'ok':True}
    def relink(self,p):
        rid=p['id'];path=Path(p['path'])
        if not path.is_absolute() or path.is_symlink() or not path.is_file():raise Problem('invalid_file','Choose an existing regular file.')
        with self.transaction():
            rec=self.db.execute('SELECT checksum,bytes FROM files WHERE id=?',(rid,)).fetchone()
            if not rec:raise Problem('no_known_hash','No prior download checksum exists for this row. An identity match cannot be guessed.')
            if path.stat().st_size!=rec['bytes'] or digest(path)!=rec['checksum']:raise Problem('hash_mismatch','The selected file differs from this exact row’s recorded download.')
            self.db.execute("UPDATE files SET path=?,mode='external',state='available',verified=? WHERE id=?",(str(path),stamp(),rid));self.event('relink',rid,{'path':str(path)})
        return {'ok':True,'path':str(path)}
    def export(self,p):
        rid=p['id'];row=self.row(rid)
        if row['ambiguous']:raise Problem('ambiguous','Export requires an unambiguous source identity.')
        rec=self.local(rid)
        if not rec:raise Problem('local_required','Save or relink this exact recording before preparing an import package.')
        if rec['kind']!='audio':raise Problem('audio_required','Music import packages require audio. Other assets retain their original file.')
        source=Path(rec['path']);eid=uuid.uuid4().hex
        folder=contained(self.root/'Exports',safe_name(row['title'])+'--'+rid+'--'+eid);stage=folder.with_name('.'+folder.name+'.part');stage.mkdir()
        try:
            target=stage/(base_name(row)+source.suffix);copy_verified(source,target,rec['checksum'],rec['bytes'])
            export_row=dict(row);export_row['artist']=self.adapter.explicit_artist(row)
            art=self.delivery_art(row);art_bytes=None
            # A unique name match is useful for display but does not establish
            # that this image belongs in this exact recording's exported tags.
            if art and art.get('exportEligible'):
                af=Path(art['path'])
                with self.cache_lock:
                    if af.is_file() and af.stat().st_size<=8*MIB:
                        with regular_reader(af) as f:art_bytes=f.read(8*MIB+1)
            tags=write_tags(target,export_row,art_bytes)
            proof={'schema':1,'identity':rid,'revision':self.catalog['snapshotHash'],'source':row,'originalChecksum':rec['checksum'],'file':target.name,'checksum':digest(target),'tags':tags,'artworkSource':art if art_bytes else None,'created':stamp()}
            atomic_json(stage/'Tracker Manifest.json',proof)
            with self.transaction():
                if self.closing:raise Problem('closing','Player is shutting down.')
                os.rename(stage,folder)
                self.db.execute('INSERT INTO exports VALUES(?,?,?,?,?,?)',(eid,rid,str(folder),proof['checksum'],stamp(),encode(proof)));self.event('export',rid,proof)
            return {'path':str(folder),'tags':tags}
        except Exception:
            # App-owned incomplete export only; never the original media.
            if stage.exists():shutil.rmtree(stage)
            raise
    def manifest(self,p):
        with self.lock:
            def safe_rows(table):
                rows=[]
                for stored in self.db.execute('SELECT * FROM '+table):
                    item=dict(stored)
                    for key in ('record','proof','metadata','payload'):
                        raw=item.get(key)
                        if isinstance(raw,str):
                            try:item[key]=encode(public_record(json.loads(raw)))
                            except (TypeError,ValueError):item[key]=public_record(raw)
                    rows.append(public_record(item))
                return rows
            payload=public_record({'schema':1,'source':self.catalog['sourceUrl'],'revision':self.catalog['snapshotHash'],'files':safe_rows('files'),'exports':safe_rows('exports'),'events':safe_rows('events')})
            path=contained(self.root/'Exports','Library Manifest.json');atomic_json(path,payload)
        return {'path':str(path)}
    def session(self,p):self.setpref('session',p);return {'ok':True}
    def handle(self,command,p):
        allowed={'download_all','retry_batch','source_recovery','mark_unresolved','attach_download','copy_info','save_copy','assign_art','artwork_report','shuffle_candidates','boot','query','eras','detail','ids','prepare','unpin','art','enqueue','cancel','retry','activity','stats','verify','detach','relink','export','manifest','session','settings','size_decision','discard_size'}
        if command not in allowed:raise Problem('unknown_command','Unsupported client command.')
        if not isinstance(p,dict):raise Problem('invalid_request','Command parameters must be an object.')
        if self.closing:raise Problem('closing','Player is shutting down.')
        return getattr(self,command)(p)
    def close(self):
        with self.lock:self.closing=True
        self.cancel({});self.pool.shutdown(wait=True,cancel_futures=True)
        with self.transaction():self.db.execute("UPDATE jobs SET state='interrupted',code='interrupted' WHERE state IN ('queued','running','cancelling')")
        with self.lock:self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)');self.db.close()
        self.file_lock.close()

def legacy_write_tags(path,row,artwork=None):
    """Only source-supported music metadata; tracker order stays in manifest."""
    import mutagen
    from mutagen.id3 import TIT2,TALB,TPE1,COMM,APIC,TDRC
    from mutagen.mp4 import MP4,MP4Cover
    from mutagen.flac import FLAC,Picture
    from mutagen._vorbis import VCommentDict
    audio=mutagen.File(path)
    if audio is None:raise Problem('unsupported_tags','The audio container does not support validated tag export.')
    if audio.tags is None:audio.add_tags()
    audio.tags.clear();tags=audio.tags
    title=row['title'];album=row['era'];artist=SnapshotCatalog.explicit_artist(row)
    comment=encode({'tracker':'Suzy Tracker','worksheet':row['workbook'],'identity':row['id'],'version':row['version'],'source':row['sourceUrl'],'sourceHash':row['sourceHash'],'trackerOrder':row['order']})
    release=str(row['fields'].get('Release Date','')).strip();date=''
    for fmt in ('%b %d, %Y','%Y-%m-%d'):
        try:date=datetime.datetime.strptime(release,fmt).date().isoformat();break
        except ValueError:pass
    artwork=artwork if artwork and artwork.startswith((b'\x89PNG\r\n\x1a\n',b'\xff\xd8\xff')) else None
    embedded=False
    if isinstance(audio,MP4):
        tags['\xa9nam']=[title];tags['\xa9alb']=[album];tags['\xa9cmt']=[comment]
        if artist:tags['\xa9ART']=[artist]
        if date:tags['\xa9day']=[date]
        if artwork:tags['covr']=[MP4Cover(artwork,imageformat=MP4Cover.FORMAT_PNG if artwork.startswith(b'\x89PNG') else MP4Cover.FORMAT_JPEG)];embedded=True
        title_key='\xa9nam';album_key='\xa9alb'
        artist_key='\xa9ART';date_key='\xa9day';track_key='trkn';art_key='covr'
    elif isinstance(audio,FLAC) or isinstance(tags,VCommentDict):
        tags['title']=title;tags['album']=album;tags['comment']=comment
        if artist:tags['artist']=artist
        if date:tags['date']=date
        if isinstance(audio,FLAC):
            audio.clear_pictures()
            if artwork:
                picture=Picture();picture.type=3;picture.mime='image/png' if artwork.startswith(b'\x89PNG') else 'image/jpeg';picture.data=artwork;audio.add_picture(picture);embedded=True
        title_key='title';album_key='album'
        artist_key='artist';date_key='date';track_key='tracknumber';art_key=None
    elif hasattr(tags,'add'):
        tags.add(TIT2(encoding=3,text=title));tags.add(TALB(encoding=3,text=album));tags.add(COMM(encoding=3,lang='eng',desc='Source identity',text=comment))
        if artist:tags.add(TPE1(encoding=3,text=artist))
        if date:tags.add(TDRC(encoding=3,text=date))
        if artwork:tags.add(APIC(encoding=3,mime='image/png' if artwork.startswith(b'\x89PNG') else 'image/jpeg',type=3,desc='Source cover',data=artwork));embedded=True
        title_key='TIT2';album_key='TALB'
        artist_key='TPE1';date_key='TDRC';track_key='TRCK';art_key='APIC:Source cover'
    else:raise Problem('unsupported_tags','Unsupported tag family; original preserved.')
    audio.save();check=mutagen.File(path)
    def values(v):return [str(x) for x in (v.text if hasattr(v,'text') else v if isinstance(v,list) else [v])]
    if check is None or check.tags is None:raise Problem('tag_readback','Export tags could not be reopened.')
    for key,expected in ((title_key,title),(album_key,album),(artist_key,artist),(date_key,date)):
        actual=values(check.tags[key]) if key in check.tags else []
        if (expected and expected not in actual) or (not expected and actual):raise Problem('tag_readback','Tag readback failed: '+key)
    if track_key in check.tags:raise Problem('tag_readback','Unexpected inferred track number.')
    if embedded:
        pictures=[x.data for x in check.pictures] if isinstance(check,FLAC) else [bytes(x) for x in check.tags.get(art_key,[])] if isinstance(check,MP4) else [check.tags[art_key].data] if art_key in check.tags else []
        if artwork not in pictures:raise Problem('tag_readback','Artwork readback failed.')
    return {'title':title,'album':album,'artist':artist,'releaseDate':date,'trackNumber':'','artwork':embedded,'note':'Artist only when an unambiguous explicit source field exists. Tracker order retained in provenance, not album track number. Name-matched cover art is display-only.'}

from delivery_metadata import write_tags

def main():
    root=Path(os.environ['TRACKER_NEXT_ROOT']);base=Path(__file__).parent
    if not (base/'Artwork/manifest.json').is_file(): raise Problem('missing_artwork','The app’s bundled era artwork is missing. Reinstall the complete app.')
    engine=Engine(root,base/'catalog.json',base/'seed')
    write_lock=threading.Lock();requests=concurrent.futures.ThreadPoolExecutor(max_workers=6)
    pending=threading.BoundedSemaphore(32)
    def stop(signum,frame):
        engine.closing=True
        raise SystemExit(0)
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    def send(reply):
        with write_lock:print(encode(reply),flush=True)
    def run(msg):
        try:
            try:reply={'request':msg['request'],'result':engine.handle(msg['command'],msg.get('params',{}))}
            except Exception as e:reply={'request':msg.get('request',''),'error':{'code':getattr(e,'code','source_error'),'message':str(e)}}
            send(reply)
        finally:pending.release()
    try:
        while True:
            line=sys.stdin.buffer.readline(2*MIB+1)
            if not line:break
            if len(line)>2*MIB:
                while line and not line.endswith(b'\n'):line=sys.stdin.buffer.readline(2*MIB+1)
                continue
            try:msg=json.loads(line)
            except ValueError:continue
            if not isinstance(msg,dict):continue
            if msg.get('command')=='shutdown':break
            if not pending.acquire(blocking=False):
                send({'request':msg.get('request',''),'error':{'code':'busy','message':'Too many pending requests. Retry after current work completes.'}})
                continue
            requests.submit(run,msg)
    finally:
        engine.closing=True;requests.shutdown(wait=True);engine.close()

if __name__=='__main__':main()
