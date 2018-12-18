import logging
from typing import Any
from typing import Dict
from typing import List

import aiohttp
from gcloud.aio.auth import Token
from gcloud.aio.datastore.constants import FORMATTERS
from gcloud.aio.datastore.constants import Mode
from gcloud.aio.datastore.constants import Operation
from gcloud.aio.datastore.constants import TypeName
from gcloud.aio.datastore.constants import TYPES
from gcloud.aio.datastore.key import Key
from gcloud.aio.datastore.key import PathElement
try:
    import ujson as json
except ModuleNotFoundError:
    import json  # type: ignore


API_ROOT = 'https://datastore.googleapis.com/v1/projects'
SCOPES = [
    'https://www.googleapis.com/auth/cloud-platform',
    'https://www.googleapis.com/auth/datastore',
]

log = logging.getLogger(__name__)


class Datastore:
    def __init__(self, project: str, service_file: str, namespace: str = '',
                 session: aiohttp.ClientSession = None,
                 token: Token = None) -> None:
        self.project = project
        self.namespace = namespace

        self.session = session
        self.token = token or Token(project, service_file, session=session,
                                    scopes=SCOPES)

    @staticmethod
    def _infer_type(value: Any) -> TypeName:
        kind = type(value)

        try:
            return TYPES[kind]
        except KeyError:
            raise Exception(f'unsupported value type {kind}')

    @staticmethod
    def _format_value(type_name: TypeName, value: Any) -> Any:
        return FORMATTERS.get(type_name, lambda v: v)(value)

    @staticmethod
    def _make_commit_body(
            transaction: str, mode: Mode = Mode.TRANSACTIONAL,
            mutations: List[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not mutations:
            raise Exception('at least one mutation record is required')

        return {
            'mode': mode.value,
            'mutations': mutations,
            'transaction': transaction,
        }

    @classmethod
    def _make_value(cls, value: Any) -> Dict[str, Any]:
        type_name = cls._infer_type(value)
        return {
            'excludeFromIndexes': False,
            type_name.value: cls._format_value(type_name, value),
        }

    async def headers(self) -> Dict[str, str]:
        token = await self.token.get()
        return {
            'Authorization': f'Bearer {token}',
        }

    @classmethod
    def make_mutation(cls, operation: Operation, key: Key,
                      properties: Dict[str, Any] = None) -> Dict[str, Any]:
        # pylint: disable=too-many-arguments
        if operation == Operation.DELETE:
            return {operation.value: key.to_repr()}

        return {
            operation.value: {
                'key': key.to_repr(),
                'properties': {k: cls._make_value(v)
                               for k, v in properties.items()},
            }
        }

    # https://cloud.google.com/datastore/docs/reference/data/rest/v1/projects/beginTransaction
    # TODO: support readwrite vs readonly transaction types
    async def beginTransaction(self, session: aiohttp.ClientSession = None,
                               timeout: int = 10) -> str:
        url = f'{API_ROOT}/{self.project}:beginTransaction'
        headers = await self.headers()
        headers.update({
            'Content-Length': '0',
            'Content-Type': 'application/json',
        })

        if not self.session:
            self.session = aiohttp.ClientSession(conn_timeout=10,
                                                 read_timeout=10)
        session = session or self.session
        resp = await session.post(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        content = await resp.json()

        transaction: str = content['transaction']
        return transaction

    async def commit(self, transaction: str, mutations: List[Dict[str, Any]],
                     mode: Mode = Mode.TRANSACTIONAL,
                     session: aiohttp.ClientSession = None,
                     timeout: int = 10) -> None:
        # pylint: disable=too-many-arguments
        url = f'{API_ROOT}/{self.project}:commit'

        body = self._make_commit_body(transaction, mode, mutations)
        payload = json.dumps(body).encode('utf-8')

        headers = await self.headers()
        headers.update({
            'Content-Length': str(len(payload)),
            'Content-Type': 'application/json',
        })

        if not self.session:
            self.session = aiohttp.ClientSession(conn_timeout=10,
                                                 read_timeout=10)
        session = session or self.session
        response = await session.post(url, data=payload, headers=headers,
                                      timeout=timeout)
        response.raise_for_status()

    async def delete(self, kind: str, name: str,
                     session: aiohttp.ClientSession = None) -> None:
        return await self.operate(Operation.DELETE, kind, name,
                                  session=session)

    async def insert(self, kind: str, name: str, properties: Dict[str, Any],
                     session: aiohttp.ClientSession = None) -> None:
        return await self.operate(Operation.INSERT, kind, name, properties,
                                  session=session)

    async def update(self, kind: str, name: str, properties: Dict[str, Any],
                     session: aiohttp.ClientSession = None) -> None:
        return await self.operate(Operation.UPDATE, kind, name, properties,
                                  session=session)

    async def upsert(self, kind: str, name: str, properties: Dict[str, Any],
                     session: aiohttp.ClientSession = None) -> None:
        return await self.operate(Operation.UPSERT, kind, name, properties,
                                  session=session)

    async def operate(self, operation: Operation, kind: str, name: str,
                      properties: Dict[str, Any] = None,
                      session: aiohttp.ClientSession = None) -> None:
        # pylint: disable=too-many-arguments
        transaction = await self.beginTransaction(session=session)
        key = Key(self.project, path=[PathElement(kind, name)],
                  namespace=self.namespace)
        mutation = self.make_mutation(operation, key, properties=properties)
        return await self.commit(transaction, mutations=[mutation],
                                 session=session)
