"""官方网页处理器使用的 HTTP 传输。每次连接及重定向只允许已解析的公网地址。"""

import ipaddress
import socket
from urllib.parse import urlsplit

import requests
from requests import RequestException as RequestException
from requests.adapters import HTTPAdapter


class PublicWebAdapter(HTTPAdapter):
    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host, tls = super().build_connection_pool_key_attributes(request, verify, cert)
        url = urlsplit(request.url)
        if url.username is not None or url.password is not None:
            raise RequestException("网页 URL 不能包含凭据")
        try:
            addresses = socket.getaddrinfo(host["host"], url.port, type=socket.SOCK_STREAM)
        except socket.gaierror as error:
            raise requests.ConnectionError(str(error)) from error
        for _, _, _, _, address in addresses:
            ip = ipaddress.ip_address(address[0])
            if not ip.is_global or ip.is_multicast or (
                ip.version == 6 and (
                    ip.sixtofour or ip.teredo or ip.ipv4_mapped
                    or ip in ipaddress.IPv6Network("64:ff9b::/96")
                )
            ):
                raise RequestException("MARBLE 网页工具只允许访问公网地址")
        # 连接池连接已校验的 IP，TLS 和 HTTP 仍使用原域名。
        if url.scheme == "https":
            tls.update(server_hostname=url.hostname, assert_hostname=url.hostname)
        host["host"] = addresses[0][4][0]
        request.headers["Host"] = url.netloc
        return host, tls


def get(url, **kwargs):
    with requests.Session() as session:
        session.trust_env = False  # 不向任意网页转发宿主代理或 .netrc 凭据。
        for scheme in ("http://", "https://"):
            session.mount(scheme, PublicWebAdapter())
        return session.get(url, **kwargs)
