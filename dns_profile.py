"""
.mobileconfig generator for DNS profiles. Apple's docs:
https://developer.apple.com/documentation/devicemanagement/dnssettings

Supports both DNS-over-HTTPS (DoH) and DNS-over-TLS (DoT). Unsigned — iOS
will show an "Unverified" banner at install time, which is normal for
self-made profiles.
"""

import uuid
import plistlib
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class DNSPreset:
    name: str                 # Display name (e.g. "Cloudflare 1.1.1.1")
    identifier: str           # Reverse-DNS id (e.g. "net.iSideload.dns.cloudflare")
    protocol: str             # "HTTPS" or "TLS"
    server_url: str = ""      # DoH URL (for HTTPS)
    server_name: str = ""     # DoT hostname (for TLS)
    server_addresses: List[str] = field(default_factory=list)  # Optional IP list


# A sensible starter catalog. Add/remove freely.
PRESETS: List[DNSPreset] = [
    DNSPreset(
        name="Cloudflare 1.1.1.1 (DoH)",
        identifier="net.iSideload.dns.cloudflare",
        protocol="HTTPS",
        server_url="https://cloudflare-dns.com/dns-query",
        server_addresses=["1.1.1.1", "1.0.0.1", "2606:4700:4700::1111", "2606:4700:4700::1001"],
    ),
    DNSPreset(
        name="Cloudflare for Families — malware block (DoH)",
        identifier="net.iSideload.dns.cloudflare.family.malware",
        protocol="HTTPS",
        server_url="https://security.cloudflare-dns.com/dns-query",
        server_addresses=["1.1.1.2", "1.0.0.2"],
    ),
    DNSPreset(
        name="Cloudflare for Families — malware + adult (DoH)",
        identifier="net.iSideload.dns.cloudflare.family.adult",
        protocol="HTTPS",
        server_url="https://family.cloudflare-dns.com/dns-query",
        server_addresses=["1.1.1.3", "1.0.0.3"],
    ),
    DNSPreset(
        name="Google Public DNS (DoH)",
        identifier="net.iSideload.dns.google",
        protocol="HTTPS",
        server_url="https://dns.google/dns-query",
        server_addresses=["8.8.8.8", "8.8.4.4"],
    ),
    DNSPreset(
        name="Quad9 (DoH)",
        identifier="net.iSideload.dns.quad9",
        protocol="HTTPS",
        server_url="https://dns.quad9.net/dns-query",
        server_addresses=["9.9.9.9", "149.112.112.112"],
    ),
    DNSPreset(
        name="AdGuard DNS — default (DoH)",
        identifier="net.iSideload.dns.adguard.default",
        protocol="HTTPS",
        server_url="https://dns.adguard-dns.com/dns-query",
        server_addresses=["94.140.14.14", "94.140.15.15"],
    ),
    DNSPreset(
        name="AdGuard DNS — family (DoH)",
        identifier="net.iSideload.dns.adguard.family",
        protocol="HTTPS",
        server_url="https://family.adguard-dns.com/dns-query",
        server_addresses=["94.140.14.15", "94.140.15.16"],
    ),
    DNSPreset(
        name="NextDNS (DoH) — needs your config ID",
        identifier="net.iSideload.dns.nextdns",
        protocol="HTTPS",
        server_url="https://dns.nextdns.io/CONFIG_ID",
        server_addresses=["45.90.28.0", "45.90.30.0"],
    ),
    DNSPreset(
        name="Mullvad base (DoH)",
        identifier="net.iSideload.dns.mullvad.base",
        protocol="HTTPS",
        server_url="https://base.dns.mullvad.net/dns-query",
        server_addresses=["194.242.2.2", "193.19.108.2"],
    ),
    DNSPreset(
        name="Mullvad adblock (DoH)",
        identifier="net.iSideload.dns.mullvad.adblock",
        protocol="HTTPS",
        server_url="https://adblock.dns.mullvad.net/dns-query",
        server_addresses=["194.242.2.3", "193.19.108.3"],
    ),
    DNSPreset(
        name="Cloudflare 1.1.1.1 (DoT)",
        identifier="net.iSideload.dns.cloudflare.dot",
        protocol="TLS",
        server_name="one.one.one.one",
        server_addresses=["1.1.1.1", "1.0.0.1"],
    ),
    DNSPreset(
        name="Quad9 (DoT)",
        identifier="net.iSideload.dns.quad9.dot",
        protocol="TLS",
        server_name="dns.quad9.net",
        server_addresses=["9.9.9.9", "149.112.112.112"],
    ),
]


def build_mobileconfig(
    preset: DNSPreset,
    profile_display_name: Optional[str] = None,
    profile_identifier: Optional[str] = None,
    on_demand: bool = True,
) -> bytes:
    """
    Build an unsigned .mobileconfig as bytes (an XML plist).
    on_demand=True makes iOS apply the DNS everywhere except VPNs/cellular-
    routed-captive-portals (the recommended default).
    """
    display = profile_display_name or preset.name
    top_id = profile_identifier or preset.identifier + ".profile"

    dns_payload = {
        "PayloadType": "com.apple.dnsSettings.managed",
        "PayloadVersion": 1,
        "PayloadIdentifier": preset.identifier,
        "PayloadUUID": str(uuid.uuid4()).upper(),
        "PayloadDisplayName": display,
        "DNSSettings": {
            "DNSProtocol": preset.protocol,  # "HTTPS" or "TLS"
        },
    }

    if preset.protocol == "HTTPS":
        if not preset.server_url:
            raise ValueError("DoH preset requires server_url (the /dns-query endpoint).")
        dns_payload["DNSSettings"]["ServerURL"] = preset.server_url
    elif preset.protocol == "TLS":
        if not preset.server_name:
            raise ValueError("DoT preset requires server_name (the TLS hostname).")
        dns_payload["DNSSettings"]["ServerName"] = preset.server_name
    else:
        raise ValueError(f"Unknown DNSProtocol: {preset.protocol}")

    if preset.server_addresses:
        dns_payload["DNSSettings"]["ServerAddresses"] = preset.server_addresses

    if on_demand:
        # Apply unconditionally. Any-connection match with Action=Connect
        # means iOS always routes DNS through this profile.
        dns_payload["OnDemandRules"] = [
            {"Action": "Connect"},
        ]

    top = {
        "PayloadType": "Configuration",
        "PayloadVersion": 1,
        "PayloadIdentifier": top_id,
        "PayloadUUID": str(uuid.uuid4()).upper(),
        "PayloadDisplayName": display,
        "PayloadDescription": f"Encrypted DNS profile generated by iSideload for {display}.",
        "PayloadOrganization": "iSideload",
        "PayloadRemovalDisallowed": False,
        "ConsentText": {
            "default": (
                "This profile sets your iPhone's DNS resolver to "
                f"{preset.name}. Remove it any time under "
                "Settings → General → VPN & Device Management."
            )
        },
        "PayloadContent": [dns_payload],
    }

    return plistlib.dumps(top, fmt=plistlib.FMT_XML)
