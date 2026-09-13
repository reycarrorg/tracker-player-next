"""Offline duration and row presentation contracts; no live library or downloads."""
import json,tempfile,unittest
from pathlib import Path
import engine
from test_engine import row,snapshot

class SongRows(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();base=Path(self.tmp.name).resolve()
        self.rows=[row('short',fields={'Track Length':'0:29','Available Length':'Snippet','Quality':'CD Quality'}),row('boundary',fields={'Track Length':'0:30'}),row('long',fields={'Length':'3:45'}),row('hour',fields={'Track Length':'1:02:03'}),row('unknown',fields={}),row('range',fields={'Track Length':'0:15–0:45'}),row('invalid',fields={'Track Length':'0:99'}),row('blocked',fields={'Track Length':'3:00'},eligible=False)]
        self.e=engine.Engine(base/'library',snapshot(base/'catalog.json',self.rows))
    def tearDown(self):self.e.close();self.tmp.cleanup()
    def candidates(self,**options):
        return self.e.handle('shuffle_candidates',dict(ids=[r['id'] for r in self.rows],**options))['ids']
    def test_threshold_inclusive(self):
        self.assertEqual(self.candidates(skipShort=True,includeUnknown=False),['boundary','long','hour'])
    def test_unknown_is_not_zero(self):
        self.assertEqual(self.candidates(skipShort=True,includeUnknown=True),['boundary','long','hour','unknown','range','invalid'])
    def test_allow_short_with_unknown_excluded(self):
        self.assertEqual(self.candidates(skipShort=False,includeUnknown=False),['short','boundary','long','hour'])
    def test_only_captured_ids_and_playable(self):
        self.assertEqual(self.e.shuffle_candidates({'ids':['outside','blocked','short','short']})['ids'],['short'])
    def test_rows_keep_original_tags(self):
        rows=self.e.query({'limit':500})['rows'];short=next(r for r in rows if r['id']=='short')
        self.assertEqual(short['fields'],self.rows[0]['fields']);self.assertEqual(short['version'],'[v1]')
        self.assertEqual(short['sourceCount'],1)
        self.assertEqual(self.e.detail({'id':'short'})['row']['fields'],short['fields'])
    def test_zero_link_summary_has_no_remote_source(self):
        self.e.catalog['rows'][0]['links']=[]
        with self.e.transaction():
            self.e.db.execute('UPDATE rows SET payload=? WHERE id=?',(json.dumps(self.e.catalog['rows'][0]),'short'))
        short=self.e.query({'limit':500})['rows'][0]
        self.assertEqual(short['sourceCount'],0)
    def test_full_scope_beyond_page(self):
        self.e.catalog['rows']=[row(str(i),fields={'Length':'0:10' if i%2 else '2:00'}) for i in range(1200)]
        result=self.e.shuffle_candidates({'ids':[str(i) for i in range(1200)],'skipShort':True})
        self.assertEqual(len(result['ids']),600);self.assertIn('1198',result['ids']);self.assertNotIn('1199',result['ids'])
    def test_preferences_persist(self):
        value={'shuffle':True,'skipShort':True,'includeUnknown':False,'avoidRepeats':True,'shuffleCycle':['long']}
        self.e.handle('session',value);self.assertEqual(self.e.boot({})['session'],value)
if __name__=='__main__':unittest.main()
