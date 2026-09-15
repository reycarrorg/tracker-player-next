"""Replaceable download-provider boundary.

The built-in provider handles public HTTPS media without credentials. Provider
browser sessions are owned by WebKit in the native app and never enter Python.
"""
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import urlsplit

import transport


@dataclass(frozen=True)
class DownloadProvider:
    name: str
    accepts: Callable[[str], bool]
    fetch: Callable[..., dict]

    def can_handle(self, url):
        return bool(self.accepts(url))

    def download(self, url, path, limit, **kwargs):
        return self.fetch(url, path, limit, **kwargs)


class ProviderRegistry:
    def __init__(self, providers: Iterable[DownloadProvider]):
        self.providers = tuple(providers)

    def provider_for(self, url):
        for provider in self.providers:
            if provider.can_handle(url):
                return provider
        raise transport.AccessError('access_unavailable', 'No installed provider accepts this source.')

    def download(self, url, path, limit, **kwargs):
        provider = self.provider_for(url)
        result = provider.download(url, path, limit, **kwargs)
        result['provider'] = provider.name
        return result

    def ordered_sources(self, links):
        return transport.ordered_sources(links)


def _public_fetch(url, path, limit, **kwargs):
    return transport.download(url, path, limit, **kwargs)


BUILTIN = ProviderRegistry((
    DownloadProvider('public_https', lambda url: isinstance(url, str) and urlsplit(url).scheme == 'https', _public_fetch),
))


def download(url, path, limit, **kwargs):
    return BUILTIN.download(url, path, limit, **kwargs)


def ordered_sources(links):
    return BUILTIN.ordered_sources(links)
