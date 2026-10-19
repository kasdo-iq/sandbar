import json
import os
from pathlib import Path

import requests


def manus_compatibility_enabled() -> bool:
    """Manus compat guard (MANUS_COMPAT env, default true)."""
    return os.getenv('MANUS_COMPAT', 'true').lower() != 'false'


class ApiClient:
    """
    A client class for making requests to an internal API aggregation platform.

    This class provides a simple interface to call APIs through an internal proxy service
    that aggregates multiple external APIs.

    Env:
        MANUS_COMPAT=true|false   set false to disable entirely (server never
                                  crashes on missing Manus host/token file).
    """

    def __init__(self):
        """
        Initialize the API client with the base host URL for the proxy service.

        Raises:
            RuntimeError: when MANUS_COMPAT=false, or when the sandbox API
                token file is missing (with recovery instructions).
        """
        if not manus_compatibility_enabled():
            raise RuntimeError(
                "ApiClient is disabled: MANUS_COMPAT=false. "
                "Set MANUS_COMPAT=true to use the Manus proxy client."
            )
        host = os.getenv('RUNTIME_API_HOST', 'https://api.manus.im')
        self.host = f'{host}/apiproxy.v1.ApiProxyService/CallApi'
        token_path = Path().home().joinpath('.secrets/sandbox_api_token')
        if not token_path.is_file():
            raise RuntimeError(
                f"Missing sandbox API token file: {token_path}. "
                "Create it (POST /init-sandbox with secrets "
                "{'sandbox_api_token': '<value>'}) or docker cp one in."
            )
        with token_path.open('r') as f:
            self.token = f.read().strip()

    def _convert_bool_to_str(self, data: dict | None) -> dict | None:
        """Convert boolean values in dictionary to strings.

        Args:
            data (dict | None): Input dictionary that may contain boolean values

        Returns:
            dict | None: Dictionary with boolean values converted to strings, or None if input is None
        """
        if data is None:
            return None

        result = {}
        for key, value in data.items():
            if isinstance(value, bool):
                result[key] = str(value).lower()
            elif isinstance(value, dict):
                result[key] = self._convert_bool_to_str(value)
            else:
                result[key] = value
        return result

    def call_api(
        self,
        api_id_or_name: str,
        body: dict | None = None,
        query: dict | None = None,
    ):
        """
        Make an API call through the proxy service.

        Args:
            api_id_or_name (str): The ID or name of the API to call
            body (dict, optional): The request body to send. Defaults to None.
            query (dict, optional): The query parameters to send. Defaults to None.

        Returns:
            dict: The JSON response from the API call, or an error dict if the call fails
        """
        try:
            resp = requests.post(
                f'{self.host}',
                json={
                    'apiId': api_id_or_name,
                    'body': self._convert_bool_to_str(body),
                    'query': self._convert_bool_to_str(query),
                },
                headers={'x-sandbox-token': self.token},
            )
            data = resp.json()
            if 'jsonData' in data:
                return json.loads(data['jsonData'])
            else:
                return data
        except Exception as e:
            return {'error': str(e)}
