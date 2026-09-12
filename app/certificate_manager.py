"""Certificate generation using only Python standard library.

We use ``ssl`` + ``subprocess`` (openssl if available) to generate self-signed
TLS certificates and PKCS12 bundles.  If the openssl binary is not found we
fall back to the ``cryptography`` package (installed via pip).  If neither is
available uvicorn will raise a clear error at startup.
"""

from __future__ import annotations

import ipaddress
import hashlib
import os
import secrets
import shutil
import ssl
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


class CertificateManager:
    _CA_COMMON_NAME = "Custom IoT Box CA"
    _CA_VALIDITY_DAYS = 3650
    # Server certificates are re-issued whenever the box address changes, so
    # they only need to outlive the next re-issue.
    _LEAF_VALIDITY_DAYS = 3650

    def __init__(
        self,
        certs_dir: Path,
        *,
        iot_ip: str,
        p12_password: str = "",
    ) -> None:
        self.certs_dir = certs_dir
        self.iot_ip = iot_ip
        self.p12_password = p12_password
        self.password_path = self.certs_dir / ".p12_password"

        self.key_path = self.certs_dir / "iotbox.key"
        self.crt_path = self.certs_dir / "iotbox.crt"
        self.p12_path = self.certs_dir / "iotbox.p12"
        self.ca_key_path = self.certs_dir / "iotbox-ca.key"
        self.ca_crt_path = self.certs_dir / "iotbox-ca.crt"
        self.windows_trust_marker_path = self.certs_dir / ".windows_trust_sha256"

    def ensure(self) -> None:
        self.certs_dir.mkdir(parents=True, exist_ok=True)
        password_created = self._ensure_p12_password()
        # The server certificate is re-issued whenever the box address changes,
        # so clients pin the CA instead -- which means the CA has to exist even
        # when the current certificate is still perfectly valid.  Creating it
        # re-issues the certificate once, from then on the pin survives every
        # re-issue.
        ca_created = self._ensure_ca()
        if not password_created and not ca_created and self._is_existing_bundle_usable():
            return
        # Generate the complete replacement before touching the active files.
        # If OpenSSL/cryptography fails, the currently loaded HTTPS assets stay
        # intact and the next startup can retry safely.
        self._generate_with_best_available()

    def status(self) -> dict[str, Any]:
        return {
            "crt_ready": self.crt_path.exists(),
            "p12_ready": self.p12_path.exists(),
            "ca_ready": self.ca_crt_path.exists(),
            "password_configured": bool(self.p12_password),
            "password_file": str(self.password_path),
        }

    def install_for_current_windows_user(self) -> bool:
        """Trust the box's CA for the current Windows user.

        The user-level Root store requires no administrator prompt.  A digest
        marker prevents adding duplicate entries at each HTTPS startup.
        """
        if os.name != "nt":
            return False
        self.ensure()
        # Trust the CA rather than the server certificate: the certificate is
        # re-issued on every address change, and a Root entry for it would have
        # to be replaced each time, whereas the CA entry keeps the chain valid
        # for every certificate it signs.
        trust_path = self.ca_crt_path if self.ca_crt_path.exists() else self.crt_path
        digest = hashlib.sha256(trust_path.read_bytes()).hexdigest()
        try:
            if self.windows_trust_marker_path.read_text(encoding="ascii").strip() == digest:
                return True
        except OSError:
            pass

        completed = subprocess.run(
            ["certutil.exe", "-user", "-addstore", "Root", str(trust_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(f"Unable to trust the IoT Box HTTPS certificate: {detail}")
        self.windows_trust_marker_path.write_text(digest, encoding="ascii")
        return True

    def _ensure_p12_password(self) -> bool:
        if self.p12_password:
            return False
        if self.password_path.exists():
            value = self.password_path.read_text(encoding="utf-8").strip()
            if value:
                self.p12_password = value
                return False
        self.p12_password = secrets.token_urlsafe(18)
        self.password_path.write_text(self.p12_password, encoding="utf-8")
        try:
            self.password_path.chmod(0o600)
        except OSError:
            pass
        return True

    # ------------------------------------------------------------------
    # Generation strategy
    # ------------------------------------------------------------------

    def _replace_into_place(self, source: Path, target: Path) -> None:
        """Atomically replace *target* with *source*, tolerating cross-drive
        staging directories (e.g. %%TEMP%% on C: while certs live on D:).

        Windows os.replace() raises WinError 17 when source and target are on
        different volumes, so the file is first staged next to the target
        (same volume) and then swapped in atomically.  If anything fails the
        previously active certificate files stay intact.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(f".{target.name}.new-{secrets.token_hex(4)}")
        try:
            shutil.copy2(source, staged)
            os.replace(staged, target)
        finally:
            try:
                staged.unlink()
            except OSError:
                pass

    def _generate_with_best_available(self) -> None:
        """Try openssl CLI first, then fall back to cryptography package."""
        openssl_bin = self._find_openssl_bin()
        if openssl_bin:
            self._generate_via_openssl(openssl_bin)
            return
        # Fallback: use the cryptography library
        self._generate_via_cryptography()

    # ------------------------------------------------------------------
    # Signing CA
    # ------------------------------------------------------------------

    def _ensure_ca(self) -> bool:
        """Create the box's signing CA if it is missing; return whether it was.

        This certificate is what clients pin, so it is generated once and never
        rotated -- replacing it invalidates every client that trusts it.  It is
        only regenerated when absent or unreadable.
        """
        if self._ca_is_usable():
            return False
        openssl_bin = self._find_openssl_bin()
        if openssl_bin:
            self._generate_ca_via_openssl(openssl_bin)
        else:
            self._generate_ca_via_cryptography()
        return True

    def _ca_is_usable(self) -> bool:
        if not (self.ca_crt_path.exists() and self.ca_key_path.exists()):
            return False
        try:
            cert = ssl._ssl._test_decode_cert(os.fspath(self.ca_crt_path))
            not_after = str(cert.get("notAfter") or "").strip()
            if not not_after:
                return False
            # Same margin as the server certificate: renew before the last
            # month rather than fail during a service.
            return ssl.cert_time_to_seconds(not_after) > time.time() + 30 * 86400
        except (OSError, TypeError, ValueError, OverflowError):
            return False

    def _generate_ca_via_openssl(self, openssl_bin: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "ca.cnf"
            config_path.write_text(
                "\n".join(
                    [
                        "[req]",
                        "default_bits = 2048",
                        "prompt = no",
                        "default_md = sha256",
                        "distinguished_name = dn",
                        "x509_extensions = v3_ca",
                        "",
                        "[dn]",
                        f"CN = {self._CA_COMMON_NAME}",
                        "",
                        "[v3_ca]",
                        "basicConstraints = critical, CA:TRUE",
                        "keyUsage = critical, keyCertSign, cRLSign",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            key_tmp = tmp_path / "iotbox-ca.key"
            crt_tmp = tmp_path / "iotbox-ca.crt"
            subprocess.run(
                [
                    openssl_bin, "req", "-x509", "-nodes", "-newkey", "rsa:2048",
                    "-keyout", str(key_tmp), "-out", str(crt_tmp),
                    "-days", str(self._CA_VALIDITY_DAYS),
                    "-subj", f"/CN={self._CA_COMMON_NAME}",
                    "-extensions", "v3_ca",
                    "-config", str(config_path),
                ],
                check=True, capture_output=True, text=True,
            )
            self._replace_into_place(key_tmp, self.ca_key_path)
            self._replace_into_place(crt_tmp, self.ca_crt_path)

    def _generate_ca_via_cryptography(self) -> None:
        _x509, _hashes, _serialization, _rsa, _NameOID = self._load_cryptography()
        from datetime import datetime, timedelta, timezone

        ca_key = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ca_name = _x509.Name(
            [_x509.NameAttribute(_NameOID.COMMON_NAME, self._CA_COMMON_NAME)]
        )
        now = datetime.now(timezone.utc)
        ca_cert = (
            _x509.CertificateBuilder()
            .subject_name(ca_name)
            .issuer_name(ca_name)
            .public_key(ca_key.public_key())
            .serial_number(_x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=self._CA_VALIDITY_DAYS))
            .add_extension(_x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(
                _x509.KeyUsage(
                    digital_signature=False,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(ca_key, _hashes.SHA256())
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            key_tmp = tmp_path / self.ca_key_path.name
            crt_tmp = tmp_path / self.ca_crt_path.name
            key_tmp.write_bytes(
                ca_key.private_bytes(
                    encoding=_serialization.Encoding.PEM,
                    format=_serialization.PrivateFormat.TraditionalOpenSSL,
                    encryption_algorithm=_serialization.NoEncryption(),
                )
            )
            crt_tmp.write_bytes(ca_cert.public_bytes(_serialization.Encoding.PEM))
            self._replace_into_place(key_tmp, self.ca_key_path)
            self._replace_into_place(crt_tmp, self.ca_crt_path)

    @staticmethod
    def _load_cryptography() -> tuple[Any, Any, Any, Any, Any]:
        """Import the cryptography pieces shared by both generators."""
        try:
            from cryptography import x509 as _x509
            from cryptography.hazmat.primitives import (
                hashes as _hashes,
                serialization as _serialization,
            )
            from cryptography.hazmat.primitives.asymmetric import rsa as _rsa
            from cryptography.x509.oid import NameOID as _NameOID
        except ImportError:
            raise RuntimeError(
                "No certificate generation method available. "
                "Install cryptography: pip install cryptography"
            )
        return _x509, _hashes, _serialization, _rsa, _NameOID

    def _find_openssl_bin(self) -> str | None:
        candidates = [
            os.getenv("IOT_OPENSSL_BIN", ""),
            r"C:\Program Files\Git\usr\bin\openssl.exe",
            r"C:\Program Files\Git\mingw64\bin\openssl.exe",
            r"C:\Program Files\OpenSSL-Win64\bin\openssl.exe",
            r"C:\Program Files (x86)\OpenSSL-Win32\bin\openssl.exe",
            r"C:\msys64\usr\bin\openssl.exe",
        ]
        for c in candidates:
            if c and Path(c).exists():
                return c
        # Search PATH
        which = shutil.which("openssl")
        if which:
            return which
        return None

    # ------------------------------------------------------------------
    # OpenSSL CLI path
    # ------------------------------------------------------------------

    def _generate_via_openssl(self, openssl_bin: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "openssl.cnf"
            self._write_openssl_config(config_path)

            key_tmp = tmp_path / "iotbox.key"
            csr_tmp = tmp_path / "iotbox.csr"
            crt_tmp = tmp_path / "iotbox.crt"
            p12_tmp = tmp_path / "iotbox.p12"

            # Leaf key + CSR.  The extensions are applied when signing.
            subprocess.run(
                [
                    openssl_bin, "req", "-nodes", "-newkey", "rsa:2048",
                    "-keyout", str(key_tmp), "-out", str(csr_tmp),
                    "-subj", "/CN=Custom IoT Box",
                    "-config", str(config_path),
                ],
                check=True, capture_output=True, text=True,
            )
            # Sign it with the box's own CA so clients can pin the CA instead
            # of this certificate, which is re-issued on every address change.
            # A random serial avoids writing a .srl file next to the CA.
            subprocess.run(
                [
                    openssl_bin, "x509", "-req",
                    "-in", str(csr_tmp),
                    "-CA", str(self.ca_crt_path),
                    "-CAkey", str(self.ca_key_path),
                    "-set_serial", str(secrets.randbits(63)),
                    "-out", str(crt_tmp),
                    "-days", str(self._LEAF_VALIDITY_DAYS),
                    "-extensions", "v3_req",
                    "-extfile", str(config_path),
                ],
                check=True, capture_output=True, text=True,
            )

            # PKCS12 carries the CA as well, so importing it also trusts the
            # chain the leaf was issued from.
            subprocess.run(
                [
                    openssl_bin, "pkcs12", "-export",
                    "-out", str(p12_tmp),
                    "-inkey", str(key_tmp), "-in", str(crt_tmp),
                    "-certfile", str(self.ca_crt_path),
                    "-passout", f"pass:{self.p12_password}",
                    "-name", "Custom IoT Box",
                ],
                check=True, capture_output=True, text=True,
            )

            self._replace_into_place(key_tmp, self.key_path)
            self._replace_into_place(crt_tmp, self.crt_path)
            self._replace_into_place(p12_tmp, self.p12_path)

    def _write_openssl_config(self, config_path: Path) -> None:
        san_entries = self._subject_alt_names()
        lines = [
            "[req]",
            "default_bits = 2048",
            "prompt = no",
            "default_md = sha256",
            "distinguished_name = dn",
            "x509_extensions = v3_req",
            "",
            "[dn]",
            "CN = Custom IoT Box",
            "",
            "[v3_req]",
            "subjectAltName = @alt_names",
            "extendedKeyUsage = serverAuth",
            "",
            "[alt_names]",
        ]
        lines.extend(san_entries)
        config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _subject_alt_names(self) -> list[str]:
        host = self.iot_ip.split(":", 1)[0].strip()
        names: list[str] = [
            "DNS.1 = localhost",
            "IP.1 = 127.0.0.1",
        ]
        if not host:
            return names
        next_dns = 2
        next_ip = 2
        try:
            ipaddress.ip_address(host)
            names.append(f"IP.{next_ip} = {host}")
        except ValueError:
            names.append(f"DNS.{next_dns} = {host}")
        return names

    # ------------------------------------------------------------------
    # cryptography library path (fallback)
    # ------------------------------------------------------------------

    def _generate_via_cryptography(self) -> None:
        """Lazy-import cryptography only when needed."""
        try:
            from cryptography import x509 as _x509
            from cryptography.hazmat.primitives import hashes as _hashes, serialization as _serialization
            from cryptography.hazmat.primitives.asymmetric import rsa as _rsa
            from cryptography.hazmat.primitives.serialization import pkcs12 as _pkcs12
            from cryptography.x509.oid import NameOID as _NameOID
        except ImportError:
            raise RuntimeError(
                "No certificate generation method available. "
                "Install cryptography: pip install cryptography"
            )

        from datetime import datetime, timedelta, timezone

        # Issued from the box's own CA so clients can pin the CA and survive
        # this certificate being re-issued on the next address change.
        ca_key = _serialization.load_pem_private_key(
            self.ca_key_path.read_bytes(), password=None
        )
        ca_cert = _x509.load_pem_x509_certificate(self.ca_crt_path.read_bytes())

        key = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = _x509.Name([
            _x509.NameAttribute(_NameOID.COMMON_NAME, "Custom IoT Box"),
        ])

        san_list: list[_x509.GeneralName] = [
            _x509.DNSName("localhost"),
            _x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
        ]
        host = self.iot_ip.split(":", 1)[0].strip()
        if host:
            try:
                san_list.append(_x509.IPAddress(ipaddress.ip_address(host)))
            except ValueError:
                san_list.append(_x509.DNSName(host))

        now = datetime.now(timezone.utc)
        cert = (
            _x509.CertificateBuilder()
            .subject_name(subject).issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(_x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=self._LEAF_VALIDITY_DAYS))
            .add_extension(_x509.SubjectAlternativeName(san_list), critical=False)
            .add_extension(
                _x509.ExtendedKeyUsage([_x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
                critical=False,
            )
            .sign(ca_key, _hashes.SHA256())
        )

        key_data = key.private_bytes(
            encoding=_serialization.Encoding.PEM,
            format=_serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=_serialization.NoEncryption(),
        )
        crt_data = cert.public_bytes(_serialization.Encoding.PEM)
        p12_data = _pkcs12.serialize_key_and_certificates(
            name=b"Custom IoT Box", key=key, cert=cert, cas=[ca_cert],
            encryption_algorithm=_serialization.BestAvailableEncryption(
                self.p12_password.encode("utf-8")
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            key_tmp = tmp_path / self.key_path.name
            crt_tmp = tmp_path / self.crt_path.name
            p12_tmp = tmp_path / self.p12_path.name
            key_tmp.write_bytes(key_data)
            crt_tmp.write_bytes(crt_data)
            p12_tmp.write_bytes(p12_data)
            self._replace_into_place(key_tmp, self.key_path)
            self._replace_into_place(crt_tmp, self.crt_path)
            self._replace_into_place(p12_tmp, self.p12_path)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _clear_existing_bundle(self) -> None:
        for path in (self.key_path, self.crt_path, self.p12_path):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass

    def _is_existing_bundle_usable(self) -> bool:
        if not (self.crt_path.exists() and self.key_path.exists() and self.p12_path.exists()):
            return False
        try:
            cert = ssl._ssl._test_decode_cert(os.fspath(self.crt_path))
        except Exception:
            return False
        not_after = str(cert.get("notAfter") or "").strip()
        if not not_after:
            return False
        try:
            # Renew before the final week instead of failing unexpectedly in
            # the middle of a restaurant service.
            if ssl.cert_time_to_seconds(not_after) <= time.time() + 7 * 86400:
                return False
        except (TypeError, ValueError, OverflowError):
            return False
        subject_alt_names = cert.get("subjectAltName", ())
        wanted_host = self.iot_ip.split(":", 1)[0].strip()
        if not wanted_host:
            return True
        for san_type, san_value in subject_alt_names:
            if san_type == "IP Address" and san_value == wanted_host:
                return True
            if san_type == "DNS" and san_value.lower() == wanted_host.lower():
                return True
        return False


def ensure_runtime_tls_assets(
    certs_dir: Path,
    *,
    iot_ip: str,
    p12_password: str = "",
) -> CertificateManager:
    manager = CertificateManager(
        certs_dir,
        iot_ip=iot_ip,
        p12_password=p12_password,
    )
    manager.ensure()
    return manager
