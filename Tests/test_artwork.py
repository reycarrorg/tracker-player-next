"""Focused tests for exact worksheet-era artwork mappings."""
import hashlib,json,tempfile,unittest
from pathlib import Path
from artwork import ArtworkCatalog

class ArtworkTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.payload=b'fixture bytes: image decoding is verified separately with ImageIO'
        self.digest=hashlib.sha256(self.payload).hexdigest();self.key='bundle-'+self.digest[:24]
        self.file=self.root/(self.digest+'.png');self.file.write_bytes(self.payload)
        self.group=ArtworkCatalog.key('Released','Donda [V1]')
        self.manifest={'schema':2,'assets':{self.key:{'file':self.file.name,'sha256':self.digest,'bytes':len(self.payload)}},
                       'groups':{self.group:{'assetId':self.key,'caption':'Documented project concept','sourceUrl':'https://example.com/art','exportEligible':False}}}
        self.write()
    def tearDown(self):self.temp.cleanup()
    def write(self):(self.root/'manifest.json').write_text(json.dumps(self.manifest))
    def test_exact_worksheet_and_era_identity(self):
        art=ArtworkCatalog(self.root,{('Released','Donda [V1]')})
        self.assertEqual(art.for_group('Released','Donda [V1]')['rowId'],self.key)
        self.assertIsNone(art.for_group('Unreleased','Donda [V1]'))
        self.assertIsNone(art.for_group('Released','Donda'))
    def test_unknown_identity_has_no_file_and_no_guess(self):self.assertIsNone(ArtworkCatalog(self.root).path('../../outside'))
    def test_tampered_image_rejected(self):
        self.file.write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError,'checksum'):ArtworkCatalog(self.root)
    def test_missing_asset_rejected(self):
        self.file.unlink()
        with self.assertRaisesRegex(ValueError,'missing'):ArtworkCatalog(self.root)
    def test_symlink_asset_rejected(self):
        target=self.root/'outside.png';self.file.rename(target);self.file.symlink_to(target)
        with self.assertRaisesRegex(ValueError,'missing'):ArtworkCatalog(self.root)
    def test_path_escape_rejected(self):
        self.manifest['assets'][self.key]['file']='../outside.png';self.write()
        with self.assertRaisesRegex(ValueError,'path'):ArtworkCatalog(self.root)
    def test_coverage_gap_and_extra_group_rejected(self):
        for expected in ({('Released','Donda [V1]'),('Unreleased','Graduation')},set()):
            with self.assertRaisesRegex(ValueError,'coverage'):ArtworkCatalog(self.root,expected)
    def test_duplicate_image_rejected_across_groups(self):
        self.manifest['groups'][ArtworkCatalog.key('Unreleased','Donda [V1]')]=dict(self.manifest['groups'][self.group])
        self.write()
        with self.assertRaisesRegex(ValueError,'unique'):ArtworkCatalog(self.root)
    def test_display_art_never_authorizes_export(self):
        self.assertFalse(ArtworkCatalog(self.root).for_group('Released','Donda [V1]')['exportEligible'])
        self.manifest['groups'][self.group]['exportEligible']=True;self.write()
        with self.assertRaisesRegex(ValueError,'audio tags'):ArtworkCatalog(self.root)
    def test_provenance_required(self):
        self.manifest['groups'][self.group]['sourceUrl']='';self.write()
        with self.assertRaisesRegex(ValueError,'attribution'):ArtworkCatalog(self.root)
    def test_required_manifest_cannot_be_missing(self):
        (self.root/'manifest.json').unlink()
        with self.assertRaisesRegex(ValueError,'missing'):ArtworkCatalog(self.root,{('Released','Donda [V1]')})
if __name__=='__main__':unittest.main()
