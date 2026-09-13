"""Immutable display artwork shipped with the captured catalog.

Era selections are explicit and attributed. They never grant export-tag authority.
No networking, cache mutation, or normalization-based identity guesses occur here.
"""
import hashlib
import json
import re
from pathlib import Path


class ArtworkCatalog:
    def __init__(self, directory, eras=None):
        self.directory = Path(directory)
        self.assets = {}
        self.eras = {}
        manifest = self.directory / 'manifest.json'
        if not manifest.exists():
            if eras is not None:
                raise ValueError('Bundled era artwork is missing. Reinstall the complete app.')
            return
        data = json.loads(manifest.read_text())
        if data.get('schema') != 1:
            raise ValueError('Unsupported artwork manifest.')
        self.assets = data['assets']
        self.eras = data['eras']
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
        for era, selection in self.eras.items():
            if not era or selection['assetId'] not in self.assets or not selection['sourceUrl'] or not selection['caption']:
                raise ValueError('Incomplete era artwork attribution.')
            if selection.get('exportEligible') is not False:
                raise ValueError('Display artwork cannot authorize audio tags.')
        if eras is not None and set(eras) != set(self.eras):
            raise ValueError('The catalog and bundled era artwork do not have identical coverage.')

    def for_era(self, era):
        selection = self.eras.get(era)
        if selection is None:
            return None
        return dict(selection, rowId=selection['assetId'], bundled=True, exportEligible=False)

    def path(self, identity):
        asset = self.assets.get(identity)
        return str(self.directory / asset['file']) if asset else None
