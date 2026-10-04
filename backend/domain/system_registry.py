"""System-wide image destination; each build retains its own validated selection."""
import re
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from backend.core import secret_box
from backend.domain.app_images import validate
from backend.domain.tenant_ai import SERVICE_ACCOUNT, POOL_PATTERN

KEY = 'app-registry'


class RegistrySelection(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    kind: Literal['private', 'artifact'] = 'private'
    host: str = Field(default='', max_length=250)
    wif_project_number: str = Field(default='', max_length=20)
    wif_pool_id: str = Field(default='', max_length=64)
    wif_provider_id: str = Field(default='', max_length=64)
    writer_service_account: str = Field(default='', max_length=200)
    reader_service_account: str = Field(default='', max_length=200)
    scanning_enabled: bool = False
    username: str = Field(default='', max_length=200)
    password: Annotated[str, StringConstraints(strip_whitespace=False)] = Field(default='', max_length=4096, repr=False)
    http: bool = False

    @model_validator(mode='after')
    def complete(self):
        if self.kind == 'private':
            if self.host:
                validate('private', self.host)
                if not self.username:
                    raise ValueError('内部Registryのユーザー名を入力してください。')
            self.scanning_enabled = False
            self.wif_project_number = self.wif_pool_id = self.wif_provider_id = ''
            self.writer_service_account = self.reader_service_account = ''
            return self
        self.username = self.password = ''
        self.http = False
        validate(self.kind, self.host)
        for pattern, value in [(r'^\d{6,20}$', self.wif_project_number),
                               (POOL_PATTERN, self.wif_pool_id), (POOL_PATTERN, self.wif_provider_id)]:
            if not re.fullmatch(pattern, value):
                raise ValueError('Artifact RegistryのWIF項目を正しく入力してください。')
        for account in (self.writer_service_account, self.reader_service_account):
            if account and not re.fullmatch(SERVICE_ACCOUNT, account):
                raise ValueError('サービスアカウントは xxx@project.iam.gserviceaccount.com の形で入力してください。')
        if self.writer_service_account and self.writer_service_account == self.reader_service_account:
            raise ValueError('Push用とPull用には別のサービスアカウントを指定してください。')
        return self

    def provider(self):
        return (f'projects/{self.wif_project_number}/locations/global/workloadIdentityPools/'
                f'{self.wif_pool_id}/providers/{self.wif_provider_id}')

    def audience(self):
        return 'https://iam.googleapis.com/' + self.provider()

    def credential_config(self, reader=False):
        account = self.reader_service_account if reader else self.writer_service_account
        config = {'audience': '//iam.googleapis.com/' + self.provider(),
                'subject_token_type': 'urn:ietf:params:oauth:token-type:jwt',
                'token_url': 'https://sts.googleapis.com/v1/token'}
        if account:
            config['service_account_impersonation_url'] = (
                f'https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{account}:generateAccessToken')
        return config


def visible(stored):
    if not stored:
        return None
    return {**{k: v for k, v in stored.items() if k not in {'password', 'password_encrypted'}},
            'password': '', 'password_configured': bool(stored.get('password_encrypted'))}


def apply(stored, payload, key):
    value = payload.model_dump(exclude={'password'})
    previous = stored or {}
    encrypted = None
    if payload.kind == 'private' and payload.host:
        if payload.password:
            encrypted = secret_box.seal(key, payload.password)
        elif (previous.get('kind') == 'private' and previous.get('host') == payload.host
              and previous.get('username') == payload.username):
            encrypted = previous.get('password_encrypted')
        if not encrypted:
            raise ValueError('接続先・ユーザー名を変更する場合はパスワードを入力してください。')
    return {**value, 'password_encrypted': encrypted}


def selection(stored, key=''):
    if not stored:
        return None
    value = {k: v for k, v in stored.items() if k != 'password_encrypted'}
    if stored.get('password_encrypted'):
        value['password'] = secret_box.open_(key, stored['password_encrypted'])
    return RegistrySelection.model_validate(value)
