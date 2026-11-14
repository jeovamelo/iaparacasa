"""Diagnóstico sem atuação. Execute no host Windows para observar mDNS da LAN."""
import argparse
import concurrent.futures
import ipaddress
import json
import socket
import time
from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--subnet', default='192.168.100.0/24')
    parser.add_argument('--seconds', type=float, default=12)
    args = parser.parse_args()
    network = ipaddress.ip_network(args.subnet)
    if not network.is_private or network.num_addresses > 256:
        parser.error('Informe uma sub-rede privada /24 ou menor.')
    services = {}

    def on_service(zeroconf, service_type, name, state_change):
        if state_change not in (ServiceStateChange.Added, ServiceStateChange.Updated):
            return
        info = zeroconf.get_service_info(service_type, name, timeout=1500)
        if info:
            props = info.properties
            # Não exponha TXT de estado, SSID, chaves ou tokens.
            services[name] = {
                'service': name, 'addresses': info.parsed_addresses(), 'port': info.port,
                'hardware_id': props.get(b'id', b'').decode(errors='replace'),
                'encrypted': props.get(b'encrypt', b'').decode(errors='replace'),
                'type': props.get(b'type', b'').decode(errors='replace'),
            }

    def probe(ip):
        ports = []
        for port in (8081, 1883):
            try:
                with socket.create_connection((str(ip), port), timeout=.3):
                    ports.append(port)
            except OSError:
                pass
        return {'ip': str(ip), 'open_ports': ports} if ports else None

    with Zeroconf() as zc:
        browser = ServiceBrowser(zc, '_ewelink._tcp.local.', handlers=[on_service])
        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
            hosts = [item for item in pool.map(probe, network.hosts()) if item]
        time.sleep(max(0, min(args.seconds, 60)))
        browser.cancel()
    print(json.dumps({'mdns': list(services.values()), 'tcp': hosts}, indent=2))


if __name__ == '__main__':
    main()
