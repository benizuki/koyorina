"""Resolve an API or registry endpoint into one NetworkPolicy CIDR."""
import argparse
import ipaddress
import socket
from urllib.parse import urlsplit


def resolve(server, registry=False):
    label = 'Registry' if registry else 'API'
    setting = 'forge_publication_registry_cidr' if registry else 'forge_publication_kubernetes_api_cidr'
    host = urlsplit(server).hostname
    if not host:
        raise ValueError(f'{label}の接続先が不正です。')
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        addresses = {ipaddress.ip_address(item[4][0]) for item in
                     socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
    if len(addresses) != 1:
        raise ValueError(f'{label}の接続先IPが複数あります。{setting}を明示してください。')
    address = addresses.pop()
    if address.is_loopback or address.is_unspecified:
        raise ValueError(f'{label}の接続先はPodから到達できるIPである必要があります。')
    return f'{address}/{address.max_prefixlen}'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('server')
    parser.add_argument('--registry', action='store_true')
    args = parser.parse_args()
    try:
        print(resolve(args.server, registry=args.registry))
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == '__main__':
    main()
