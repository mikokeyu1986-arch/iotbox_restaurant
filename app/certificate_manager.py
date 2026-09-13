"""Certificate generation using only Python standard library.

We use ``ssl`` + ``subprocess`` (openssl if available) to generate self-signed
TLS certificates and PKCS12 bundles.  If the openssl binary is not found we
fall back to the ``cryptography`` package (installed via pip).  If neither is
available uvicorn will raise a clear error at startup.
"""

from __future__ import annotations

import ipaddress
import hashlib
import logging
import os
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


_logger = logging.getLogger(__name__)


def current_trust_platform() -> str:
    """Which platform trust store this box can write: "windows", "macos" or "".

    Both systems keep the box's CA in the running user's own trust store, so
    neither needs an administrator: Windows has ``certutil``, macOS has
    ``security add-trusted-cert``.
    """
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return ""


class CertificateManager:
    _CA_COMMON_NAME = "Custom IoT Box CA"
    _CA_VALIDITY_DAYS = 3650
    # Server certificates are re-issued whenever the box address changes, so
    # they only need to outlive the next re-issue.
    _LEAF_VALIDITY_DAYS = 3650
    # Long enough for an operator to answer the macOS authorisation dialog.
    _MACOS_TRUST_TIMEOUT_SECONDS = 120
    # How long a reported trust state is reused before the store is read again.
    _TRUST_CACHE_SECONDS = 30

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
        self.macos_trust_marker_path = self.certs_dir / ".macos_trust_sha256"
        self._trust_cache: tuple[float, bool | None] | None = None

    def ensure(self) -> None:
        self.certs_dir.mkdir(parents=True, exist_ok=True)
        password_created = self._ensure_p12_password()
        # The server certificate is re-issued whenever the box address changes,
        # so clients pin the CA instead -- which means the CA has to exist even
        # when the current certificate is still perfectly valid.  Creating it
        # re-issues the certificate once, from then on the pin survives every
        # re-issue.
        ca_created = self._ensure_ca()
        problem = None
        if not password_created and not ca_created:
            problem = self._bundle_problem()
            if problem is None:
                return
        # Generate the complete replacement before touching the active files.
        # If OpenSSL/cryptography fails, the currently loaded HTTPS assets stay
        # intact and the next startup can retry safely.
        self._generate_with_best_available()
        # Every re-issue invalidates a client's per-certificate exception, which
        # is exactly how a working POS starts failing right after a restart.
        _logger.info(
            "Re-issued the IoT Box HTTPS server certificate reason=%s address=%s; "
            "clients must trust the CA at %s rather than a previously saved certificate",
            problem or "installation identity changed",
            self.iot_ip,
            self.ca_crt_path,
        )

    def trusted_state_cached(self) -> bool | None:
        """``is_trusted_for_current_user`` for surfaces that are polled often.

        Reading the platform trust store costs a subprocess, and the control
        page asks for the status every few seconds.
        """
        now = time.time()
        if self._trust_cache is not None and now - self._trust_cache[0] < self._TRUST_CACHE_SECONDS:
            return self._trust_cache[1]
        try:
            trusted = self.is_trusted_for_current_user()
        except Exception:
            _logger.exception("Unable to read the certificate trust state")
            trusted = None
        self._trust_cache = (now, trusted)
        return trusted

    def status(self) -> dict[str, Any]:
        trusted = self.trusted_state_cached()
        return {
            "crt_ready": self.crt_path.exists(),
            "p12_ready": self.p12_path.exists(),
            "ca_ready": self.ca_crt_path.exists(),
            "password_configured": bool(self.p12_password),
            "password_file": str(self.password_path),
            # Whether *this* machine trusts the CA the box signs with.  A client
            # that does not will block every request to the box, which the POS
            # can only report as a printer error.
            "ca_trusted": trusted,
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

    def install_for_current_macos_user(self) -> bool:
        """Trust the box's CA for the current macOS user.

        Without this, macOS browsers keep a per-certificate exception instead,
        and every re-issue of the server certificate -- which happens whenever
        the box address changes -- silently invalidates it: the POS then reports
        a printer error even though the box never sees a request.
        """
        if sys.platform != "darwin":
            return False
        self.ensure()
        trust_path = self.ca_crt_path if self.ca_crt_path.exists() else self.crt_path
        digest = hashlib.sha256(trust_path.read_bytes()).hexdigest()
        try:
            if self.macos_trust_marker_path.read_text(encoding="ascii").strip() == digest:
                return True
        except OSError:
            pass

        # macOS authorises trust changes interactively.  The runtime is a
        # LaunchAgent in the user's GUI session, so the dialog can be answered;
        # the timeout bounds the wait when nobody is there to answer it.
        try:
            completed = subprocess.run(
                [
                    "security", "add-trusted-cert", "-r", "trustRoot",
                    "-k", self._macos_user_keychain(), str(trust_path),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=self._MACOS_TRUST_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                "Trusting the IoT Box CA needs an administrator to approve the macOS prompt"
            ) from error
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(f"Unable to trust the IoT Box HTTPS certificate: {detail}")
        # macOS reports success even when it stored the certificate without a
        # trust setting, which leaves every client rejecting the box, so the
        # result is read back before it is called done.
        if not self.is_trusted_for_current_user():
            raise RuntimeError(
                "macOS kept no trust setting for the IoT Box CA. Run this in a Terminal "
                "(it needs an authorisation prompt the box cannot answer unattended): "
                f"sudo security add-trusted-cert -d -r trustRoot "
                f"-k /Library/Keychains/System.keychain {trust_path}"
            )
        self.macos_trust_marker_path.write_text(digest, encoding="ascii")
        return True

    def install_for_current_user(self) -> bool:
        """Trust the box's CA for whoever is running this box, if this OS needs it."""
        platform = current_trust_platform()
        if platform == "windows":
            return self.install_for_current_windows_user()
        if platform == "macos":
            return self.install_for_current_macos_user()
        return False

    def is_trusted_for_current_user(self) -> bool | None:
        """Whether this box's CA is trusted here; ``None`` when not applicable.

        The marker alone would report a trust that someone has since removed, so
        the answer is read back from the platform trust store.
        """
        platform = current_trust_platform()
        if platform == "windows":
            try:
                return self.windows_trust_marker_path.read_text(encoding="ascii").strip() == \
                    hashlib.sha256(self._trust_path().read_bytes()).hexdigest()
            except OSError:
                return False
        if platform != "macos":
            return None
        if not self.ca_crt_path.exists():
            return False
        for domain in ([], ["-d"]):
            completed = subprocess.run(
                ["security", "dump-trust-settings", *domain],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                continue
            for name, settings, fingerprints in self._macos_trust_entries(completed.stdout):
                if name != self._CA_COMMON_NAME or settings <= 0:
                    continue
                wanted = self._sha1_fingerprint(self.ca_crt_path)
                # A stored certificate without a trust setting is listed here
                # too, which is why the setting count and the fingerprint both
                # have to agree.
                if not fingerprints or wanted in fingerprints:
                    return True
        return False

    @staticmethod
    def _macos_trust_entries(dump: str) -> list[tuple[str, int, str]]:
        """Parse ``dump-trust-settings`` into ``(name, trust settings, hashes)``."""
        entries: list[tuple[str, int, str]] = []
        name = ""
        settings = 0
        hashes = ""
        for line in dump.splitlines():
            stripped = line.strip()
            if stripped.startswith("Cert ") and ":" in stripped:
                if name:
                    entries.append((name, settings, hashes))
                name = stripped.split(":", 1)[1].strip()
                settings = 0
                hashes = ""
            elif stripped.lower().startswith("number of trust settings"):
                try:
                    settings = int(stripped.split(":", 1)[1].strip())
                except (IndexError, ValueError):
                    settings = 0
            elif "hash" in stripped.lower():
                # "SHA-1 hash: BA90A8F3..." -- only the value is a fingerprint.
                hashes += CertificateManager._normalized_fingerprints(stripped.split(":", 1)[-1])
        if name:
            entries.append((name, settings, hashes))
        return entries

    def _trust_path(self) -> Path:
        return self.ca_crt_path if self.ca_crt_path.exists() else self.crt_path

    @staticmethod
    def _sha1_fingerprint(cert_path: Path) -> str:
        """SHA-1 of the DER form, which is how ``dump-trust-settings`` lists certs."""
        try:
            der = ssl.PEM_cert_to_DER_cert(cert_path.read_text(encoding="ascii"))
        except (OSError, ValueError):
            return ""
        return hashlib.sha1(der).hexdigest().upper()

    @staticmethod
    def _normalized_fingerprints(dump: str) -> str:
        return re.sub(r"[^0-9A-F]", "", dump.upper())

    def _macos_user_keychain(self) -> str:
        completed = subprocess.run(
            ["security", "default-keychain", "-d", "user"],
            capture_output=True,
            text=True,
            check=False,
        )
        path = completed.stdout.strip().strip('"')
        if completed.returncode == 0 and path:
            return path
        return str(Path.home() / "Library" / "Keychains" / "login.keychain-db")

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

    def clear_server_certificate(self) -> list[str]:
        """Remove the server certificate so the next start re-issues it.

        Used by unbind, which hands the box to a new deployment.  The leaf is
        bound to the box's LAN address and is re-issued automatically, so
        dropping it is safe.

        The signing CA is deliberately **not** touched: clients pin it, so
        replacing it would invalidate every client that trusts this box (see
        ``_ensure_ca``).  Because the CA survives, ``.windows_trust_sha256``
        still matches and is left alone as well.

        A failure to unlink is logged and swallowed -- Windows refuses to remove
        a file the running process still holds, and an unbind must not fail over
        a stale certificate.

        Returns the names actually removed.
        """
        removed: list[str] = []
        for path in (self.key_path, self.crt_path, self.p12_path):
            try:
                if path.exists():
                    path.unlink()
                    removed.append(path.name)
            except OSError as exc:
                _logger.warning("Could not remove %s during unbind: %s", path, exc)
        return removed

    def _bundle_problem(self) -> str | None:
        """Why the active server certificate has to be re-issued, if it has to.

        Returns ``None`` when the current bundle is still good.  The reason is
        logged on every re-issue: "clients suddenly cannot reach the box" is
        otherwise indistinguishable from a network fault.
        """
        missing = [
            path.name for path in (self.crt_path, self.key_path, self.p12_path)
            if not path.exists()
        ]
        if missing:
            return f"missing files {'/'.join(missing)}"
        try:
            cert = ssl._ssl._test_decode_cert(os.fspath(self.crt_path))
        except Exception:
            return "unreadable server certificate"
        not_after = str(cert.get("notAfter") or "").strip()
        if not not_after:
            return "server certificate without an expiry date"
        try:
            # Renew before the final week instead of failing unexpectedly in
            # the middle of a restaurant service.
            if ssl.cert_time_to_seconds(not_after) <= time.time() + 7 * 86400:
                return f"server certificate expires {not_after}"
        except (TypeError, ValueError, OverflowError):
            return "unreadable server certificate expiry"
        subject_alt_names = cert.get("subjectAltName", ())
        wanted_host = self.iot_ip.split(":", 1)[0].strip()
        if not wanted_host:
            return None
        for san_type, san_value in subject_alt_names:
            if san_type == "IP Address" and san_value == wanted_host:
                return None
            if san_type == "DNS" and san_value.lower() == wanted_host.lower():
                return None
        covered = ", ".join(str(value) for _type, value in subject_alt_names) or "nothing"
        return f"the box moved to {wanted_host} but the certificate covers {covered}"


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
