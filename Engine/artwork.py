"""Immutable display artwork shipped with the captured catalog.

Era selections are explicit and attributed. They never grant export-tag authority.
No networking, cache mutation, or normalization-based identity guesses occur here.
"""
import hashlib
import json
import re
from pathlib import Path


class ArtworkCatalog:
    def __init__(self, directory, groups=None):
        self.directory = Path(directory)
        self.assets = {}
        self.groups = {}
        manifest = self.directory / 'manifest.json'
        if not manifest.exists():
            if groups is not None:
                raise ValueError('Bundled era artwork is missing. Reinstall the complete app.')
            return
        data = json.loads(manifest.read_text())
        if data.get('schema') != 2:
            raise ValueError('Unsupported artwork manifest.')
        self.assets = data['assets']
        self.groups = data['groups']
        for key, asset in self.assets.items():
            if not re.fullmatch(r'bundle-[a-f0-9]{24}', key):
                raise ValueError('Invalid bundled artwork identity.')
            name = asset['file']
            if not re.fullmatch(r'[a-f0-9]{64}\.(jpg|png|webp|gif)', name):
                raise ValueError('Invalid bundled artwork path.')
            path = self.directory / name
            if path.is_symlink() or not path.is_file():
                raise ValueError('Bundled artwork file is missing.')
            payload = path.read_bytes()
            if len(payload) != asset['bytes'] or hashlib.sha256(payload).hexdigest() != asset['sha256']:
                raise ValueError('Bundled artwork checksum mismatch.')
        assigned=[]
        for group, selection in self.groups.items():
            try:identity=json.loads(group)
            except (TypeError,ValueError):raise ValueError('Invalid worksheet-era artwork identity.')
            if not isinstance(identity,list) or len(identity)!=2 or not all(isinstance(x,str) and x for x in identity):
                raise ValueError('Invalid worksheet-era artwork identity.')
            if selection['assetId'] not in self.assets or not selection['sourceUrl'] or not selection['caption']:
                raise ValueError('Incomplete era artwork attribution.')
            if selection.get('exportEligible') is not False:
                raise ValueError('Display artwork cannot authorize audio tags.')
            assigned.append(selection['assetId'])
        if len(assigned)!=len(set(assigned)) or len({self.assets[x]['sha256'] for x in assigned})!=len(assigned):
            raise ValueError('Every worksheet-era group must use a unique artwork image.')
        if groups is not None:
            actual={tuple(json.loads(key)) for key in self.groups}
            if set(groups)!=actual:raise ValueError('The catalog and bundled worksheet-era artwork do not have identical coverage.')

    @staticmethod
    def key(workbook,era):return json.dumps([str(workbook),str(era)],ensure_ascii=False,separators=(',',':'))

    def for_group(self, workbook, era):
        selection = self.groups.get(self.key(workbook,era))
        if selection is None:
            return None
        return dict(selection, rowId=selection['assetId'], bundled=True, exportEligible=False)

    def path(self, identity):
        asset = self.assets.get(identity)
        return str(self.directory / asset['file']) if asset else None
