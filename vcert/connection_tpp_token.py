#
# Copyright 2020 Venafi, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


from __future__ import (absolute_import, division, generators, unicode_literals, print_function, nested_scopes,
                        with_statement)

import logging as log
import re
import time

import requests

from .common import MIME_JSON, TokenInfo, Authentication, KeyType, Policy, ZoneConfig, CertField
from .connection_tpp_abstract import AbstractTPPConnection, URLS
from .errors import (ClientBadData, ServerUnexptedBehavior, AuthenticationError)
from .http import HTTPStatus

HEADER_AUTHORIZATION = "Authorization"  # type: str

KEY_ACCESS_TOKEN = "access_token"  # type: str  # nosec
KEY_REFRESH_TOKEN = "refresh_token"  # type: str  # nosec
KEY_EXPIRATION_DATE = "expiration_date"  # type: str


class TPPTokenConnection(AbstractTPPConnection):
    def __init__(self, url, user=None, password=None, access_token=None, refresh_token=None, http_request_kwargs=None):
        """
        :param str url:
        :param str user:
        :param str password:
        :param str access_token:
        :param str refresh_token:
        :param dict[str,Any] http_request_kwargs:
        """
        self._base_url = url  # type: str
        self._auth = Authentication(user=user, password=password, access_token=access_token,
                                    refresh_token=refresh_token)  # type: Authentication
        if http_request_kwargs is None:
            http_request_kwargs = {"timeout": 180}
        elif "timeout" not in http_request_kwargs:
            http_request_kwargs["timeout"] = 180
        self._http_request_kwargs = http_request_kwargs or {}

    def __setattr__(self, key, value):
        if key == "_base_url":
            value = self._normalize_and_verify_base_url(value)
        self.__dict__[key] = value

    def __str__(self):
        return "[TPP] %s" % self._base_url

    def _get(self, url=None, params=None, check_token=True, include_headers=True):
        if check_token:
            self._check_token()

        headers = {}
        if include_headers:
            token = self._get_auth_header_value(self._auth.access_token)
            headers = {HEADER_AUTHORIZATION: token, 'content-type': MIME_JSON, 'cache-control': 'no-cache'}

        r = requests.get(self._base_url + url, headers=headers, params=params, **self._http_request_kwargs)
        return self.process_server_response(r)

    def _post(self, url=None, data=None, check_token=True, include_headers=True):
        if check_token:
            self._check_token()

        headers = {}
        if include_headers:
            token = self._get_auth_header_value(self._auth.access_token)
            headers = {HEADER_AUTHORIZATION: token, 'content-type': MIME_JSON, "cache-control": "no-cache"}

        if isinstance(data, dict):
            log.debug("POST Request\n\tURL: %s\n\tHeaders:%s\n\tBody:%s\n" % (self._base_url+url, headers, data))
            r = requests.post(self._base_url + url, headers=headers, json=data,  **self._http_request_kwargs)
        else:
            log.error("Unexpected client data type: %s for %s" % (type(data), url))
            raise ClientBadData
        return self.process_server_response(r)

    def _check_token(self):
        if not self._auth.access_token:
            self.get_access_token()
            log.debug("Token is %s, expire date is %s" % (self._auth.access_token, self._auth.token_expires))

        # Token expired, get new token
        elif self._auth.token_expires and self._auth.token_expires < time.time():
            if self._auth.refresh_token:
                self.refresh_access_token()
                log.debug("Token is %s, expire date is %s" % (self._auth.access_token, self._auth.token_expires))
            else:
                raise AuthenticationError("Access Token expired. No refresh token provided.")

    @staticmethod
    def _normalize_and_verify_base_url(u):
        if u.startswith("http://"):
            u = "https://" + u[7:]
        elif not u.startswith("https://"):
            u = "https://" + u
        if not u.endswith("/"):
            u += "/"
        if not re.match(r"^https://[a-zA-Z\d]+[-a-zA-Z\d.]+[a-zA-Z\d][:\d]*/$", u):
            raise ClientBadData
        return u

    def auth(self):
        raise NotImplementedError

    def get_access_token(self, authentication=None):
        """
        Obtains an access token to be used for subsequent api operations.
        """
        if authentication and isinstance(authentication, Authentication):
            self._auth = authentication

        if self._auth.refresh_token:
            return self.refresh_access_token()

        if self._auth.user is None or self._auth.password is None:
            raise ClientBadData("Missing credentials. Cannot request new access token")

        request_data = {
            "username": self._auth.user,
            "password": self._auth.password,
            "client_id": self._auth.client_id,
            "scope": self._auth.scope,
            "state": "",
        }
        status, resp_data = self._post(URLS.AUTHORIZE_TOKEN, request_data, False, False)
        if status != HTTPStatus.OK:
            raise ServerUnexptedBehavior("Server returns %d status on retrieving access token." % status)

        token_info = self._parse_access_token_data_to_object(resp_data)
        self._update_auth(token_info)
        return token_info

    def refresh_access_token(self):
        request_data = {
            "refresh_token": self._auth.refresh_token,
            "client_id": self._auth.client_id,
        }
        status, resp_data = self._post(URLS.REFRESH_TOKEN, request_data, False, False)
        if status != HTTPStatus.OK:
            raise ServerUnexptedBehavior("Server returns %d status on refreshing access token" % status)

        token_info = self._parse_access_token_data_to_object(resp_data)
        self._update_auth(token_info)
        return token_info

    def revoke_access_token(self):
        status, resp_data = self._get(url=URLS.REVOKE_TOKEN, params=None, check_token=False)
        if status != HTTPStatus.OK:
            raise ServerUnexptedBehavior("Server returns %d status on revoking access token" % status)
        return status, resp_data

    def _update_auth(self, token_info):
        if isinstance(token_info, TokenInfo):
            self._auth.access_token = token_info.access_token
            self._auth.refresh_token = token_info.refresh_token
            self._auth.token_expire_date = token_info.expires

    @staticmethod
    def _get_auth_header_value(token):
        return 'Bearer ' + token

    @staticmethod
    def _parse_access_token_data_to_object(data):
        token_info = TokenInfo(
            access_token=data["access_token"],
            expires=data["expires"],
            refresh_token=data["refresh_token"],
        )
        return token_info

    def _is_valid_auth(self):
        if self._auth and self._auth.access_token:
            return True
        return False
