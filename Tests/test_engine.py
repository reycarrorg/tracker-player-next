"""Offline tiny fixtures. Run only under the owner's explicit test resource grant.

No source snapshot, user library, network, media player, subprocess or UI access.
"""
import concurrent.futures
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

import engine

SYNTHETIC_SOURCE_ID='public-fixture-sheet'


def row(rid='a', **changes):
    value=dict(id=rid,workbook='Unreleased',era='Era',title='Song '+rid,name='Song '+rid,
               sourceHash='hash-'+rid,sourceUrl='https://docs.google.com/spreadsheets/d/'+SYNTHETIC_SOURCE_ID+'/edit#range=A2',
               fields={},links=['https://example.com/'+rid],version='[v1]',kind='audio',row=2,
               ambiguous=False,eligible=True,artist='INFERRED ARTIST',order=47)
    value.update(changes)
    return value


def snapshot(path, rows):
    value=dict(sourceUrl='https://docs.google.com/spreadsheets/d/'+SYNTHETIC_SOURCE_ID+'/edit',
               captured='fixture',rows=rows,eras={},artwork={},rowArtwork={})
    value['snapshotHash']=hashlib.sha256(json.dumps([(r['id'],r['sourceHash']) for r in rows],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    path.write_text(json.dumps(value))
    return path


def wav_bytes():
    dest=io.BytesIO()
    with wave.open(dest,'wb') as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(8000);f.writeframes(b'\0\0'*800)
    return dest.getvalue()


class DeferredPool:
    def __init__(self):self.calls=[]
    def submit(self,fn,*args):self.calls.append((fn,args))
    def shutdown(self,**kwargs):pass


class EngineFixtures(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='tracker-next-fixture-')
        self.base=Path(self.tmp.name).resolve()
        self.snap=snapshot(self.base/'catalog.json',[row('a'),row('b'),row('c',kind='other',eligible=False)])
        self.e=engine.Engine(self.base/'isolated',self.snap)
        self.e.pool.shutdown(wait=True);self.e.pool=DeferredPool()
        self.e.disk_guard=lambda n:None

    def tearDown(self):
        if self.e:self.e.close()
        self.tmp.cleanup()

    def job(self,jid):
        with self.e.lock:return dict(self.e.db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())

    def install(self,rid='a',payload=None):
        payload=payload if payload is not None else wav_bytes()
        path=self.e.root/'Downloads'/(rid+'.wav');path.write_bytes(payload)
        checksum=engine.digest(path)
        with self.e.transaction():
            self.e.db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?)',
                              (rid,str(path),checksum,len(payload),'audio','permanent','available',engine.stamp(),engine.stamp(),'{}'))
        return path

    def fake_download(self,url,path,limit,consume=lambda n:None,reserve=None,release=None,**kwargs):
        payload=wav_bytes()
        amount=reserve(len(payload)) if reserve else len(payload)
        if amount<len(payload):raise engine.Problem('batch_bytes','Synthetic budget exhausted')
        try:consume(len(payload))
        finally:
            if release:release(amount)
        Path(path).write_bytes(payload)
        return dict(bytes=len(payload),kind='audio',extension='wav',checksum=engine.digest(path),originalUrl=url,resolvedUrl=url,mime='audio/wav')

    def seed(self,files):
        seed=self.base/'seed';(seed/'Downloads').mkdir(parents=True)
        records={}
        for rid,payload in files.items():
            path=seed/'Downloads'/(rid+'.wav');path.write_bytes(payload)
            records[rid]=dict(relativePath=path.name,checksum=engine.digest(path),bytes=len(payload),kind='audio')
        (seed/'state.json').write_text(json.dumps(dict(files=records,playback={'selected':'a','time':12})))
        return seed

    def test_source_ids_search_and_playable_scope(self):
        self.install('a');self.install('c')
        self.assertEqual(self.e.query({'query':'Song','limit':1})['total'],3)
        self.assertEqual(self.e.ids({'filter':'local','playableOnly':True}),['a'])
        self.assertEqual(self.e.ids({'filter':'local'}),['a','c'])
        self.assertEqual(self.e.ids({'saveOnly':True}),['b'])
        self.assertEqual(self.e.ids({'filter':'local','saveOnly':True}),[])
        self.assertEqual(self.e.query({'workbook':'Absent'})['total'],0)

    def test_nested_preference_and_events_rollback_together(self):
        with self.assertRaises(RuntimeError):
            with self.e.transaction():
                self.e.setpref('first',{'value':1})
                self.e.event('should_rollback',None,{})
                self.e.setpref('second',2)
                raise RuntimeError('late outer failure')
        self.assertIsNone(self.e.pref('first'));self.assertIsNone(self.e.pref('second'))
        self.assertEqual(self.e.db.execute("SELECT count(*) FROM events WHERE type='should_rollback'").fetchone()[0],0)

    def test_copy_migration_atomic_idempotent_and_source_preserved(self):
        seed=self.seed({'a':wav_bytes(),'b':wav_bytes()})
        before=(seed/'Downloads/a.wav').read_bytes()
        original=self.e.event
        def fail(kind,*args):
            if kind=='copy_migration':raise RuntimeError('injected transaction failure')
            return original(kind,*args)
        with patch.object(self.e,'event',side_effect=fail):
            with self.assertRaises(RuntimeError):self.e.import_legacy(seed)
        self.assertEqual(self.e.db.execute('SELECT count(*) FROM files').fetchone()[0],0)
        self.assertFalse(self.e.pref('legacyImported',False));self.assertIsNone(self.e.pref('session'))
        self.assertFalse((self.e.root/'Downloads/a.wav').exists())
        self.assertEqual((seed/'Downloads/a.wav').read_bytes(),before)
        self.e.import_legacy(seed)
        self.assertEqual(self.e.verify({})['verified'],2)
        self.assertTrue(self.e.pref('legacyImported'));self.assertEqual(self.e.pref('session')['time'],12)
        self.e.close();self.e=engine.Engine(self.base/'isolated',self.snap,seed)
        self.assertEqual(self.e.db.execute("SELECT count(*) FROM events WHERE type='copy_migration'").fetchone()[0],1)

    def test_migration_collision_never_overwrites(self):
        seed=self.seed({'a':wav_bytes()});target=self.e.root/'Downloads/a.wav';target.write_bytes(b'existing foreign bytes')
        self.e.import_legacy(seed)
        self.assertEqual(target.read_bytes(),b'existing foreign bytes')
        self.assertEqual(self.e.detail({'id':'a'})['file']['state'],'missing')

    def test_unsafe_ids_paths_symlinks_and_fifo_refused(self):
        with self.assertRaises(engine.Problem):engine.SnapshotCatalog(snapshot(self.base/'unsafe.json',[row('../../escape')]))
        outside=self.base/'outside';outside.write_bytes(b'preserve')
        (self.e.root/'Cache/link').symlink_to(outside)
        for relative in ('../outside',str(outside),'link'):
            with self.assertRaises(engine.Problem):engine.contained(self.e.root/'Cache',relative)
        import os
        fifo=self.base/'pipe';os.mkfifo(fifo)
        with self.assertRaises(engine.Problem):engine.digest(fifo)
        self.assertEqual(outside.read_bytes(),b'preserve')

    def test_download_registry_and_job_commit_together(self):
        jid=self.e.enqueue({'ids':['a']})['jobs'][0]
        with patch('engine.transport.download',side_effect=self.fake_download):self.e.run_job(jid)
        self.assertEqual(self.job(jid)['state'],'completed')
        self.assertEqual(self.job(jid)['received'],len(wav_bytes()))
        self.assertIsNotNone(self.e.local('a'))
        self.assertTrue((self.e.root/'Exports/Library Manifest.json').exists())

    def test_late_download_database_failure_removes_only_new_install(self):
        jid=self.e.enqueue({'ids':['a']})['jobs'][0]
        original=self.e.event
        def fail(kind,*args):
            if kind=='download':raise RuntimeError('injected late write failure')
            return original(kind,*args)
        with patch('engine.transport.download',side_effect=self.fake_download),patch.object(self.e,'event',side_effect=fail):self.e.run_job(jid)
        self.assertEqual(self.job(jid)['state'],'failed');self.assertIsNone(self.e.local('a'))
        self.assertFalse(any(p.is_file() for p in (self.e.root/'Downloads').rglob('*')))

    def test_cancel_at_finalization_installs_nothing_and_retry_is_explicit(self):
        jid=self.e.enqueue({'ids':['a']})['jobs'][0]
        def cancel_last(*args,**kwargs):
            info=self.fake_download(*args,**kwargs);self.e.cancel({});return info
        with patch('engine.transport.download',side_effect=cancel_last):self.e.run_job(jid)
        self.assertEqual(self.job(jid)['state'],'cancelled');self.assertIsNone(self.e.local('a'))
        retry=self.e.retry({'id':jid});self.assertTrue(retry['ok']);self.assertEqual(self.job(jid)['state'],'queued')

    def test_queued_cancel_does_not_start_transport(self):
        jid=self.e.enqueue({'ids':['a']})['jobs'][0];self.e.cancel({})
        with patch('engine.transport.download') as download:self.e.run_job(jid)
        download.assert_not_called();self.assertEqual(self.job(jid)['state'],'cancelled')

    def test_failed_received_bytes_are_counted(self):
        batch=self.e.enqueue({'ids':['a']});jid=batch['jobs'][0]
        self.e.disk_guard=lambda n:(_ for _ in ()).throw(engine.Problem('disk_reserve','synthetic full disk'))
        with patch('engine.transport.download',side_effect=self.fake_download):self.e.run_job(jid)
        self.assertEqual(self.job(jid)['received'],len(wav_bytes()))
        self.assertEqual(self.e.pref('batch:'+batch['batch'])['received'],len(wav_bytes()))
        self.assertEqual(self.e.reservations,{})
        self.assertEqual(self.job(jid)['state'],'failed')

    def test_two_workers_never_reserve_more_than_batch(self):
        batch=self.e.enqueue({'ids':['a','b']});cfg=self.e.pref('batch:'+batch['batch']);cfg['limit']=7;self.e.setpref('batch:'+batch['batch'],cfg)
        rendezvous=threading.Barrier(2);amounts=[];guard=threading.Lock()
        def bounded(url,path,limit,reserve,consume,release,**kwargs):
            amount=reserve(5)
            with guard:amounts.append(amount)
            rendezvous.wait(timeout=3)
            try:consume(amount)
            finally:release(amount)
            raise engine.Problem('fixture_stop','No permanent media in reservation fixture')
        with patch('engine.transport.download',side_effect=bounded),concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(self.e.run_job,jid) for jid in batch['jobs']]
            for future in futures:future.result(timeout=5)
        self.assertEqual(sorted(amounts),[2,5]);self.assertEqual(self.e.pref('batch:'+batch['batch'])['received'],7)
        self.assertEqual(sum(self.job(jid)['received'] for jid in batch['jobs']),7)
        self.assertEqual(self.e.reservations,{})

    def test_restart_interrupts_jobs_and_clears_old_reservations(self):
        batch=self.e.enqueue({'ids':['a']});self.e.job_update(batch['jobs'][0],state='running')
        cfg=self.e.pref('batch:'+batch['batch']);cfg.update(reserved=22,received=3);self.e.setpref('batch:'+batch['batch'],cfg)
        cachepart=self.e.root/'Cache'/('a'*32+'.part');cachepart.write_bytes(b'incomplete')
        downloadpart=self.e.root/'Downloads'/('.'+batch['jobs'][0]+'.part');downloadpart.write_bytes(b'incomplete')
        unrelated=self.e.root/'Downloads/keep.part';unrelated.write_bytes(b'preserve')
        # Simulate the committed pre-crash database without invoking worker shutdown.
        self.e.db.close();self.e.file_lock.close();self.e=engine.Engine(self.base/'isolated',self.snap)
        self.assertEqual(self.job(batch['jobs'][0])['state'],'interrupted')
        self.assertNotIn('reserved',self.e.pref('batch:'+batch['batch']))
        self.assertEqual(self.e.pref('batch:'+batch['batch'])['received'],3)
        self.assertFalse(cachepart.exists());self.assertFalse(downloadpart.exists());self.assertEqual(unrelated.read_bytes(),b'preserve')

    def test_manifest_failure_does_not_relabel_completed_download(self):
        jid=self.e.enqueue({'ids':['a']})['jobs'][0]
        with patch('engine.transport.download',side_effect=self.fake_download),patch.object(self.e,'manifest',side_effect=OSError('synthetic manifest failure')):self.e.run_job(jid)
        self.assertEqual(self.job(jid)['state'],'completed');self.assertIsNotNone(self.e.local('a'))

    def test_preview_pins_count_and_nonmedia_does_not_pin(self):
        with patch('engine.transport.download',side_effect=self.fake_download) as download:
            first=self.e.prepare({'id':'a'});second=self.e.prepare({'id':'a'})
        self.assertEqual(download.call_count,1);self.assertEqual(first['path'],second['path'])
        self.assertEqual(self.e.pins[first['path']],2)
        self.e.unpin(first);self.assertEqual(self.e.pins[first['path']],1)
        self.e.unpin(second);self.assertNotIn(first['path'],self.e.pins)
        self.e.pin('nonmedia','image');self.assertNotIn('nonmedia',self.e.pins)

    def test_detach_changed_and_hash_relink(self):
        path=self.install();payload=path.read_bytes();self.e.detach({'id':'a'});self.assertIsNone(self.e.local('a'))
        candidate=self.base/'relocated.wav';candidate.write_bytes(payload)
        self.e.relink({'id':'a','path':str(candidate)});self.assertEqual(self.e.local('a')['mode'],'external')
        candidate.write_bytes(b'changed');self.assertIsNone(self.e.local('a'))
        self.assertEqual(self.e.detail({'id':'a'})['file']['state'],'changed')

    def test_ambiguous_art_and_conflicting_artist_are_not_guessed(self):
        cover=row('art1',workbook='Art',title='Era',fields={'Project Type':'Front Cover','Use':'Used'})
        adapter=engine.SnapshotCatalog(snapshot(self.base/'art.json',[row(),cover]))
        self.assertFalse(adapter.artwork(row())['exportEligible'])
        duplicate=copy.deepcopy(cover);duplicate['id']='art2'
        adapter=engine.SnapshotCatalog(snapshot(self.base/'art.json',[row(),cover,duplicate]))
        self.assertIsNone(adapter.artwork(row()))
        self.assertEqual(engine.SnapshotCatalog.explicit_artist(row(fields={'Artist':'A','Artists':'B'})),'')

    def test_export_wav_tags_readback_no_inferred_artist_date_or_track(self):
        import mutagen
        original=self.install();before=original.read_bytes()
        result=self.e.export({'id':'a'});folder=Path(result['path'])
        proof=json.loads((folder/'Tracker Manifest.json').read_text());target=folder/proof['file']
        tags=mutagen.File(target).tags
        self.assertEqual(result['tags']['artist'],'');self.assertEqual(result['tags']['releaseDate'],'')
        self.assertNotIn('TPE1',tags);self.assertNotIn('TDRC',tags);self.assertNotIn('TRCK',tags)
        self.assertEqual(proof['identity'],'a');self.assertEqual(proof['source']['order'],47)
        self.assertEqual(engine.digest(target),proof['checksum']);self.assertEqual(original.read_bytes(),before)

    def test_explicit_tags_and_corrupt_readback(self):
        import mutagen
        path=self.base/'tags.wav';path.write_bytes(wav_bytes())
        metadata=row(fields={'Artist':'Actual Artist','Release Date':'2024-02-29'})
        result=engine.write_tags(path,metadata)
        self.assertEqual(result['artist'],'Actual Artist');self.assertEqual(result['year'],'2024')
        actual=mutagen.File(path);actual.tags.delall('TPE1')
        with patch('mutagen.File',side_effect=[mutagen.File(path),actual]):
            from delivery_metadata import MetadataError
            with self.assertRaises(MetadataError):engine.write_tags(path,metadata)


if __name__=='__main__':unittest.main()
