"""Secret-free portable profiles and user-local Windows Credential Manager keys."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass
import hashlib
import math
import os
import urllib.parse

from .providers import SCENE_SYSTEM, ContextProvider, OpenAICompatibleProvider

PROVIDERS = ('OpenAI', 'RelayModels', 'Custom OpenAI-compatible')
URLS = {'OpenAI': 'https://api.openai.com/v1', 'RelayModels': 'https://api.relaymodels.com/v1'}


class _Credential(ctypes.Structure):
    _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD),
                ('TargetName', wintypes.LPWSTR), ('Comment', wintypes.LPWSTR),
                ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
                ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)), ('Persist', wintypes.DWORD),
                ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
                ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR)]


class WindowsCredentials:
    """Native Win32 API; no plaintext fallback and no portable secret files."""
    def __init__(self):
        if os.name != 'nt':
            raise RuntimeError('Защищённое хранилище требует Windows')
        self.api = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
        self.api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                       ctypes.POINTER(ctypes.POINTER(_Credential))]
        self.api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
        self.api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self.api.CredFree.argtypes = [ctypes.c_void_p]

    def get(self, target):
        pointer = ctypes.POINTER(_Credential)()
        if not self.api.CredReadW(target, 1, 0, ctypes.byref(pointer)):
            if ctypes.get_last_error() == 1168:
                return None
            raise RuntimeError('Windows Credential Manager недоступен')
        try:
            return ctypes.string_at(pointer.contents.CredentialBlob,
                                    pointer.contents.CredentialBlobSize).decode('utf-16-le')
        finally:
            self.api.CredFree(pointer)

    def set(self, target, key):
        if not key or len(key) > 1200 or not key.isascii() or any(c.isspace() for c in key):
            raise ValueError('Некорректный API-ключ')
        raw = key.encode('utf-16-le')
        blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        credential = _Credential(Type=1, TargetName=target, CredentialBlobSize=len(raw),
                                 CredentialBlob=blob, Persist=2, UserName='WuWaRussian')
        try:
            if not self.api.CredWriteW(ctypes.byref(credential), 0):
                raise RuntimeError('Не удалось сохранить ключ в Windows Credential Manager')
        finally:
            ctypes.memset(blob, 0, len(raw))

    def delete(self, target):
        if not self.api.CredDeleteW(target, 1, 0) and ctypes.get_last_error() != 1168:
            raise RuntimeError('Не удалось удалить ключ из Windows Credential Manager')


@dataclass
class ProviderProfile:
    name: str
    base_url: str = ''
    model: str = 'gpt-6-luna'
    input_price: float | None = None
    output_price: float | None = None

    def __post_init__(self):
        if not self.base_url:
            self.base_url = URLS.get(self.name, '')

    def validate(self):
        if self.name not in PROVIDERS:
            raise ValueError('Неизвестный провайдер')
        parsed = urllib.parse.urlsplit(self.base_url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or any(c.isspace() for c in self.base_url)
                or len(self.base_url) > 2048):
            raise ValueError('Base URL: требуется HTTPS без учётных данных, query и fragment')
        if self.name in URLS and self.base_url.rstrip('/') != URLS[self.name]:
            raise ValueError('Для другого Base URL выбери Custom OpenAI-compatible')
        if (not self.model or len(self.model) > 200 or any(c.isspace() for c in self.model)):
            raise ValueError('Укажи идентификатор модели без пробелов')
        for price in (self.input_price, self.output_price):
            if price is not None and (type(price) not in (int, float) or not math.isfinite(price) or price < 0):
                raise ValueError('Тариф: неотрицательное число USD / миллион токенов или UNKNOWN')

    @property
    def credential_target(self):
        self.validate()
        endpoint = hashlib.sha256(self.base_url.rstrip('/').encode()).hexdigest()[:24]
        return f'WuWaRussian/API/{self.name}/{endpoint}'

    def key(self):
        key = WindowsCredentials().get(self.credential_target)
        # Legacy environment compatibility belongs only to the official OpenAI endpoint.
        if not key and self.name == 'OpenAI':
            key = os.environ.get('WUWARU_API_KEY')
        return key

    def provider(self, *, max_output_tokens=8192, scenes=False, key_getter=None):
        self.validate()
        if scenes:
            return ContextProvider(self.base_url, self.model, key_getter=key_getter or self.key, profile=self.name,
                                   max_output_tokens=max_output_tokens, timeout=120, system=SCENE_SYSTEM)
        return OpenAICompatibleProvider(self.base_url, self.model, key_getter=self.key,
            profile=self.name, max_output_tokens=max_output_tokens)


class ProviderSettings:
    def __init__(self, paths):
        self.paths = paths
        self.data = paths._settings().get('api_providers', {})
        self.selected = self.data.get('selected', 'OpenAI')
        if self.selected not in PROVIDERS:
            self.selected = 'OpenAI'

    def profile(self, name, model=None, base_url=None):
        data = self.data.get('profiles', {}).get(name, {})
        allowed = ('base_url', 'model', 'input_price', 'output_price')
        profile = ProviderProfile(name, **{key: data[key] for key in allowed if key in data})
        if base_url is not None:
            profile.base_url = base_url
        if model is not None:
            profile.model = model
            prices = data.get('prices', {}).get(self.price_key(profile), {})
            profile.input_price, profile.output_price = prices.get('input'), prices.get('output')
        if profile.base_url and model is None and base_url is None:
            profile.validate()
        return profile

    def save(self, profile):
        profile.validate()
        # Re-read to preserve changes by other windows, and serialize only known fields.
        self.data = self.paths._settings().get('api_providers', {})
        profiles = dict(self.data.get('profiles', {}))
        prices = dict(profiles.get(profile.name, {}).get('prices', {}))
        prices[self.price_key(profile)] = dict(input=profile.input_price, output=profile.output_price)
        profiles[profile.name] = dict(**asdict(profile), prices=prices)
        self.data = dict(selected=profile.name, profiles=profiles)
        self.paths._save_settings(api_providers=self.data)
        self.selected = profile.name

    @staticmethod
    def price_key(profile):
        return hashlib.sha256((profile.base_url.rstrip('/') + '\n' + profile.model).encode()).hexdigest()
