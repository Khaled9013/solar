"""The portable `uv run solar` entry point."""
import argparse
from datetime import datetime, timedelta, timezone
import errno
import ipaddress
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys


def bootstrap():
    from django.core.management import call_command
    from solar.config import config, save_config
    from solar.crypto import seal
    from solar.core.models import User
    from solar.core.providers import default_providers

    call_command("migrate", interactive=False, verbosity=0)
    if not User.objects.exists():
        password = secrets.token_urlsafe(24)
        user = User.objects.create_superuser("admin", password)
        default_providers(user)
        config()["bootstrap"].update(user_id=str(user.id), username_encrypted=seal("admin", "bootstrap"),
                                    password_encrypted=seal(password, "bootstrap"), password_hash=user.password)
        save_config(config())
        print("Created a temporary administrator. Run 'uv run solar credentials' to display the local sign-in details.")


def certificate():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from solar.config import config, local_hosts, private_permissions

    cfg = config()
    if cfg["server"]["certificate"] or cfg["server"]["private_key"]:
        cert, key = Path(cfg["server"]["certificate"]), Path(cfg["server"]["private_key"])
        if not cert.is_file() or not key.is_file():
            raise ValueError("Configure both an existing certificate and private key")
        return cert, key
    cert_path, key_path = cfg["data_dir"] / "tls" / "server.crt", cfg["data_dir"] / "tls" / "server.key"
    if cert_path.exists() and key_path.exists():
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        if cert.not_valid_after_utc > datetime.now(timezone.utc) + timedelta(days=15):
            return cert_path, key_path
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Solar private workspace")])
    sans = []
    for host in local_hosts() + cfg["server"]["allowed_hosts"]:
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            if "*" not in host and not host.startswith("."):
                sans.append(x509.DNSName(host))
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=365)).add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    private_permissions(key_path)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def bind_sockets(hosts, port):
    sockets = []
    try:
        for host in hosts:
            family = socket.AF_INET6 if ":" in host else socket.AF_INET
            try:
                sock = socket.socket(family, socket.SOCK_STREAM)
            except OSError as exc:
                if host == "::" and exc.errno in (errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT):
                    continue
                raise
            if os.name == "nt":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            try:
                sock.bind((host, port))
                sock.listen(128)
                sock.setblocking(False)
                sockets.append(sock)
            except OSError as exc:
                sock.close()
                if host == "::" and exc.errno in (errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT):
                    continue
                raise
        return sockets
    except Exception:
        for sock in sockets:
            sock.close()
        raise


def main():
    parser = argparse.ArgumentParser(description="Solar — private agent workspace")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "init", "credentials", "reset-password", "check", "viewer-assets"])
    parser.add_argument("--config", help="Path to local solar.conf")
    parser.add_argument("--home", help="Installation/data root (default: current working directory)")
    parser.add_argument("--username", help="Username for local password recovery")
    args = parser.parse_args()
    if args.config:
        os.environ["SOLAR_CONFIG"] = str(Path(args.config).resolve())
    if args.home:
        os.environ["SOLAR_HOME"] = str(Path(args.home).resolve())
    from filelock import FileLock, Timeout
    from solar.config import CONFIG_PATH, initialize_storage, save_config
    try:
        cfg = initialize_storage()
    except subprocess.CalledProcessError:
        print("Solar could not restrict access to its private storage. Run it as the owning OS account in a location where you can set file permissions.", file=sys.stderr)
        raise SystemExit(1) from None
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "solar.settings")
    import django
    if django.VERSION < (5, 2, 18) or django.VERSION[:2] != (5, 2):
        print("Solar requires Django 5.2.18 or newer in the 5.2 LTS series. Run 'uv sync' before starting it.", file=sys.stderr)
        raise SystemExit(1)
    django.setup()
    from django.core.management import call_command
    from solar.crypto import digest, seal, unseal
    from solar.core.models import User
    try:
        with FileLock(str(cfg["data_dir"] / "locks" / "bootstrap.lock"), timeout=15):
            bootstrap()
        if args.command == "init":
            print(f"Initialized Solar. Local configuration: {CONFIG_PATH}")
            return
        if args.command == "credentials":
            bootstrap_cfg = cfg["bootstrap"]
            if not bootstrap_cfg.get("password_encrypted"):
                print("The initial password was replaced. Use your account password or 'uv run solar reset-password --username NAME'.")
                return
            print("Username:", unseal(bootstrap_cfg["username_encrypted"], "bootstrap"))
            print("Temporary password:", unseal(bootstrap_cfg["password_encrypted"], "bootstrap"))
            print("A password change is required at first sign-in.")
            return
        if args.command == "reset-password":
            username = args.username or input("Username: ").strip()
            user = User.objects.filter(login_id=digest(username.casefold(), "login"), is_active=True).first()
            if not user:
                raise ValueError("Active user not found")
            password = secrets.token_urlsafe(24)
            user.set_password(password)
            user.must_change_password = True
            user.save(update_fields=["password", "must_change_password"])
            if cfg["bootstrap"].get("user_id") == str(user.id):
                cfg["bootstrap"].update(password_encrypted=seal(password, "bootstrap"), password_hash=user.password)
                save_config(cfg)
            print("Temporary password:", password)
            return
        if args.command == "check":
            call_command("check", deploy=True)
            from solar.core.processes import executable
            for name in ("codex", "claude"):
                try:
                    executable(name)
                    print(f"{name}: available")
                except ValueError:
                    print(f"{name}: not installed (API providers remain available)")
            return
        lock = FileLock(str(cfg["data_dir"] / "locks" / "server.lock"), timeout=0)
        with lock:
            from solar.viewer_assets import ensure_cad_runtime
            try:
                ensure_cad_runtime()
            except (OSError, ValueError) as exc:
                print(f"STEP/IGES viewer runtime is unavailable: {exc}. Retry with 'uv run solar viewer-assets'.", file=sys.stderr)
                if args.command == "viewer-assets":
                    raise SystemExit(1) from None
            if args.command == "viewer-assets":
                print("CAD viewer runtime is ready for offline use.")
                return
            tls = {}
            if cfg["server"]["tls"]:
                cert, key = certificate()
                tls = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
            try:
                sockets = bind_sockets(cfg["server"]["hosts"], cfg["server"]["port"])
            except OSError as exc:
                raise ValueError(f"Cannot bind port {cfg['server']['port']}. It may already be in use; change server.port in solar.conf.") from exc
            import uvicorn
            from solar.core.runner import Worker
            worker = Worker()
            server = uvicorn.Server(uvicorn.Config(
                "solar.asgi:application", host=cfg["server"]["hosts"][0], port=cfg["server"]["port"],
                access_log=False, proxy_headers=False, server_header=False, ws="wsproto", ws_max_size=16384,
                timeout_keep_alive=5, limit_concurrency=80, log_level="warning", **tls,
            ))
            scheme = "https" if tls else "http"
            print(f"Solar is listening on {', '.join(cfg['server']['hosts'])}:{cfg['server']['port']}")
            print(f"Open {scheme}://localhost:{cfg['server']['port']}")
            if tls:
                from cryptography import x509
                from cryptography.hazmat.primitives import hashes
                fingerprint = x509.load_pem_x509_certificate(cert.read_bytes()).fingerprint(hashes.SHA256()).hex(":")
                print(f"Certificate SHA-256: {fingerprint}")
                print("Trust this certificate on your devices, or configure a certificate issued by your CA.")
            print("Configuration:", CONFIG_PATH)
            worker.start()
            try:
                server.run(sockets=sockets)
            finally:
                worker.stop()
                for sock in sockets:
                    sock.close()
    except Timeout:
        print("Solar is already running with this data directory.", file=sys.stderr)
        raise SystemExit(1) from None
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
