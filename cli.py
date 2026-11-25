#!/usr/bin/env python3
"""SMS Server CLI Controller.

Command-line interface to inspect SIM relay heartbeat pings, manually
retrieve OTPs from the SMS server base URL, stream live arrivals, and monitor
service status.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from urllib import error, parse, request

DEFAULT_BASE_URL = os.environ.get("SMS_SERVER_URL") or "http://127.0.0.1:5000"
REAL_DIR = os.path.abspath(os.path.dirname(os.path.realpath(__file__)))
_CANDIDATE_DBS = [
    os.path.join(REAL_DIR, "device_auth.db"),
    os.path.join(REAL_DIR, "sms_server", "device_auth.db"),
    os.path.join(os.getcwd(), "sms_server", "device_auth.db"),
    os.path.join(os.getcwd(), "device_auth.db"),
]
DEFAULT_DB_PATH = next((p for p in _CANDIDATE_DBS if os.path.exists(p)), _CANDIDATE_DBS[0])


def normalize_bd_phone(raw: str) -> str:
    """Normalize Bangladeshi phone numbers to standard 11-digit 01XXXXXXXXX."""
    s = re.sub(r"[\s\-\(\)\.]", "", str(raw or "").strip())
    if s.startswith("+880"):
        s = s[4:]
    elif s.startswith("880"):
        s = s[3:]
    if len(s) == 10 and s.startswith("1"):
        s = "0" + s
    if re.fullmatch(r"01[3-9]\d{8}", s):
        return s
    return s


class SmsServerClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, db_path: str = DEFAULT_DB_PATH):
        self.base_url = base_url.rstrip("/")
        self.db_path = db_path

    def _http_get(self, path: str, timeout: float = 5.0):
        url = f"{self.base_url}{path}"
        req = request.Request(url, headers={"User-Agent": "SMS-CLI-Client/1.0", "Accept": "application/json"})
        with request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8")
            return json.loads(data)

    def _http_post(self, path: str, payload: dict, timeout: float = 5.0):
        url = f"{self.base_url}{path}"
        raw_body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url,
            data=raw_body,
            headers={"User-Agent": "SMS-CLI-Client/1.0", "Content-Type": "application/json", "Accept": "application/json"},
        )
        with request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8")
            return json.loads(data)

    def get_pings(self):
        """Fetch list of device pings via HTTP API, with local SQLite fallback."""
        try:
            res = self._http_get("/api/ping")
            return res.get("pings", []), "api"
        except Exception:
            # Fallback to local DB if available
            if os.path.exists(self.db_path):
                return self._get_pings_from_db(), "db"
            raise

    def _get_pings_from_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        try:
            cur.execute("SELECT * FROM device_ping ORDER BY updated_at DESC")
            rows = [dict(r) for r in cur.fetchall()]
            return rows
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

    def get_otp(self, phone: str, pub: str = None):
        """Fetch latest active OTP for a phone number via HTTP API or local DB fallback."""
        norm = normalize_bd_phone(phone)
        path = f"/api/messages/{parse.quote(norm)}"
        if pub:
            path += f"?pub={parse.quote(pub)}"
        try:
            res = self._http_get(path)
            return res, "api"
        except Exception:
            if os.path.exists(self.db_path):
                return self._get_otp_from_db(norm, pub=pub), "db"
            raise

    def _get_otp_from_db(self, phone: str, pub: str = None):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        try:
            if pub:
                cur.execute(
                    "SELECT * FROM otp_message WHERE phone = ? AND UPPER(message_pub) = ? ORDER BY id DESC LIMIT 1",
                    (phone, pub.strip().upper()),
                )
            else:
                cur.execute(
                    "SELECT * FROM otp_message WHERE phone = ? ORDER BY id DESC LIMIT 1",
                    (phone,),
                )
            r = cur.fetchone()
            if not r:
                return {}
            row = dict(r)
            # Extract 6-digit OTP
            msg = row.get("otp_message", "")
            match = re.search(r"\b(\d{6})\b", msg)
            otp = match.group(1) if match else msg
            return {
                "message": otp,
                "message_pub": row.get("message_pub", "IVAC"),
                "last_updated": row.get("updated_at"),
                "revision": row.get("revision", 0),
            }
        finally:
            conn.close()

    def get_messages(self):
        """Fetch recent message metadata."""
        try:
            res = self._http_get("/api/messages")
            return res.get("messages", []), "api"
        except Exception:
            if os.path.exists(self.db_path):
                conn = sqlite3.connect(self.db_path)
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                try:
                    cur.execute("SELECT * FROM otp_message ORDER BY updated_at DESC LIMIT 50")
                    rows = [dict(r) for r in cur.fetchall()]
                    return rows, "db"
                finally:
                    conn.close()
            raise

    def send_ping(self, phone: str, slot: int = 0, interval: int = 15, status: str = "online", message: str = ""):
        """Send a heartbeat ping to /api/ping."""
        norm = normalize_bd_phone(phone)
        if not message:
            slot_label = f"SIM {slot + 1}"
            message = f"PING | phone={norm} | {slot_label} | every={interval}m | status={status}"
        payload = {
            "phone": norm,
            "slot": slot,
            "interval_minutes": interval,
            "status": status,
            "message": message,
        }
        return self._http_post("/api/ping", payload)

    def get_health(self):
        return self._http_get("/api/health")


# ── Formatted Output Helpers ───────────────────────────────────────────


def print_pings(pings, source="api"):
    if not pings:
        print("\n[!] No device ping messages found.\n")
        return

    banner_src = "(via HTTP API)" if source == "api" else f"(via local DB: {DEFAULT_DB_PATH})"
    print(f"\n{'═' * 96}")
    print(f" SIM RELAY HEARTBEAT PINGS {banner_src}")
    print(f"{'═' * 96}")
    print(
        f"{'PHONE':<13} | {'SLOT':<7} | {'STATUS':<7} | {'INTERVAL':<8} | {'LAST PING (BDT)':<19} | {'RAW MESSAGE'}"
    )
    print(f"{'─' * 96}")
    for p in pings:
        slot_val = p.get("slot")
        slot_str = f"SIM {int(slot_val) + 1}" if slot_val is not None else "SIM ?"
        status_str = str(p.get("status") or "online")
        interval_str = f"{p.get('interval_minutes') or 15}m"
        updated = str(p.get("updated_at") or "")[:19]
        msg = str(p.get("message") or "")
        print(
            f"{str(p.get('phone') or ''):<13} | {slot_str:<7} | {status_str:<7} | {interval_str:<8} | {updated:<19} | {msg}"
        )
    print(f"{'═' * 96}")
    print(f"Total active relays: {len(pings)}\n")


def print_otp(phone: str, data: dict, source="api"):
    norm = normalize_bd_phone(phone)
    if not data or not data.get("message"):
        print(f"\n[!] No active OTP found for phone {norm} in retention window.\n")
        return

    banner_src = "(via HTTP API)" if source == "api" else f"(via local DB fallback)"
    print(f"\n{'─' * 52}")
    print(f" RETRIEVED OTP {banner_src}")
    print(f"{'─' * 52}")
    print(f"  Phone Number : {norm}")
    print(f"  OTP Code     : \033[1;32m{data.get('message')}\033[0m")
    print(f"  Publisher    : {data.get('message_pub', 'IVAC')}")
    print(f"  Last Updated : {data.get('last_updated', 'N/A')} BDT")
    print(f"  Revision     : {data.get('revision', 0)}")
    print(f"{'─' * 52}\n")


def print_messages(messages, source="api"):
    if not messages:
        print("\n[!] No recent messages found.\n")
        return

    print(f"\n{'═' * 65}")
    print(f" RECENT MESSAGES IN RETENTION WINDOW")
    print(f"{'═' * 65}")
    print(f"{'PHONE':<13} | {'PUB':<6} | {'REVISION':<8} | {'LAST UPDATED (BDT)'}")
    print(f"{'─' * 65}")
    for m in messages:
        print(
            f"{str(m.get('phone') or ''):<13} | "
            f"{str(m.get('message_pub') or 'IVAC'):<6} | "
            f"{str(m.get('revision') or 0):<8} | "
            f"{str(m.get('last_updated') or m.get('updated_at') or '')}"
        )
    print(f"{'═' * 65}")
    print(f"Total: {len(messages)}\n")


def print_health(data):
    print(f"\n{'─' * 45}")
    print(f" SERVER HEALTH STATUS")
    print(f"{'─' * 45}")
    print(f"  Service          : {data.get('service', 'sms')}")
    print(f"  Server PID       : {data.get('pid')}")
    print(f"  OTP Fanout Path  : \033[1;32m{data.get('otp_fanout')}\033[0m")
    print(f"  WebSocket Clients: {data.get('ws_clients', 0)}")
    print(f"  Redis Configured : {data.get('redis_configured')}")
    print(f"  Redis Subscriber : {data.get('redis_subscriber_alive')}")
    print(f"  DB Poller Alive  : {data.get('db_poller_alive')}")
    print(f"  Database Engine  : {data.get('database')}")
    print(f"{'─' * 45}\n")


# ── Watch Mode (Live Poller) ───────────────────────────────────────────


def watch_loop(client: SmsServerClient, poll_seconds: float = 2.0):
    print(f"\n[*] Starting live watch on {client.base_url} (Ctrl+C to stop)...")
    seen_pings = {}
    seen_messages = {}

    try:
        while True:
            # Check pings
            try:
                pings, _ = client.get_pings()
                for p in pings:
                    ph = p.get("phone")
                    updated = p.get("updated_at")
                    if ph and seen_pings.get(ph) != updated:
                        seen_pings[ph] = updated
                        slot = p.get("slot")
                        slot_str = f"SIM {int(slot) + 1}" if slot is not None else "SIM ?"
                        print(
                            f"[\033[1;36mPING\033[0m] {updated} | {ph} | {slot_str} | {p.get('status')} | {p.get('message')}"
                        )
            except Exception:
                pass

            # Check messages
            try:
                msgs, _ = client.get_messages()
                for m in msgs:
                    ph = m.get("phone")
                    rev = m.get("revision")
                    if ph and seen_messages.get(ph) != rev:
                        seen_messages[ph] = rev
                        # Fetch the OTP code
                        otp_info, _ = client.get_otp(ph)
                        otp_code = otp_info.get("message", "N/A")
                        print(
                            f"[\033[1;32mOTP ARRIVED\033[0m] {ph} | Code: \033[1;32m{otp_code}\033[0m | Pub: {m.get('message_pub')} | Rev: {rev}"
                        )
            except Exception:
                pass

            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        print("\n[*] Stopped live watch.\n")


# ── Interactive REPL Console ───────────────────────────────────────────


def run_interactive(client: SmsServerClient):
    print(
        f"""
======================================================================
                     SMS SERVER CLI CONTROLLER
                     Base URL: {client.base_url}
======================================================================
  [1] pings               - View all device ping messages text
  [2] otp <phone>         - Retrieve active OTP for phone number
  [3] messages            - List recent OTP messages
  [4] watch               - Live monitor new incoming OTPs and pings
  [5] ping <phone> [slot] - Send manual ping heartbeat
  [6] health              - Check server status & fanout health
  [7] url <base_url>      - Switch SMS server target URL
  [h] help                - Show this menu
  [q] exit                - Quit
======================================================================
"""
    )

    while True:
        try:
            line = input("sms-cli> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break

        if not line:
            continue

        parts = line.split()
        cmd = parts[0].lower()
        args = parts[1:]

        if cmd in ("q", "quit", "exit"):
            print("Goodbye.")
            break
        elif cmd in ("h", "help", "?"):
            print("Commands: pings (1), otp <phone> (2), messages (3), watch (4), ping <phone> [slot] (5), health (6), url <new_url> (7), clear, exit (q)")
        elif cmd in ("clear", "cls"):
            os.system("clear" if os.name != "nt" else "cls")
        elif cmd in ("1", "pings", "ping-list"):
            try:
                pings, src = client.get_pings()
                print_pings(pings, src)
            except Exception as e:
                print(f"[!] Error fetching pings: {e}")
        elif cmd in ("2", "otp"):
            phone = args[0] if args else input("Enter phone number: ").strip()
            pub = args[1] if len(args) > 1 else None
            if not phone:
                print("[!] Phone number required.")
                continue
            try:
                data, src = client.get_otp(phone, pub=pub)
                print_otp(phone, data, src)
            except Exception as e:
                print(f"[!] Error retrieving OTP: {e}")
        elif cmd in ("3", "messages", "msgs"):
            try:
                msgs, src = client.get_messages()
                print_messages(msgs, src)
            except Exception as e:
                print(f"[!] Error fetching messages: {e}")
        elif cmd in ("4", "watch"):
            interval = float(args[0]) if args else 2.0
            watch_loop(client, interval)
        elif cmd in ("5", "send-ping"):
            phone = args[0] if args else input("Enter phone number: ").strip()
            slot = int(args[1]) if len(args) > 1 else 0
            if not phone:
                print("[!] Phone number required.")
                continue
            try:
                res = client.send_ping(phone, slot=slot)
                print(f"\n[+] Ping recorded successfully: {res.get('ping', {})}\n")
            except Exception as e:
                print(f"[!] Error sending ping: {e}")
        elif cmd in ("6", "health"):
            try:
                data = client.get_health()
                print_health(data)
            except Exception as e:
                print(f"[!] Error fetching health: {e}")
        elif cmd in ("7", "url"):
            if args:
                client.base_url = args[0].rstrip("/")
                print(f"[+] Target base URL changed to: {client.base_url}")
            else:
                new_url = input(f"Enter new base URL [{client.base_url}]: ").strip()
                if new_url:
                    client.base_url = new_url.rstrip("/")
                    print(f"[+] Target base URL changed to: {client.base_url}")
        else:
            # If input is a raw phone number:
            if re.fullmatch(r"(\+?880|0)?1[3-9]\d{8}", cmd):
                try:
                    data, src = client.get_otp(cmd)
                    print_otp(cmd, data, src)
                except Exception as e:
                    print(f"[!] Error retrieving OTP: {e}")
            else:
                print(f"[!] Unknown command '{cmd}'. Type 'help' for options.")


# ── Main Entrypoint ────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="SMS Server CLI Controller: View pings, retrieve OTPs, and monitor SMS server."
    )
    parser.add_argument(
        "--url",
        "-u",
        default=DEFAULT_BASE_URL,
        help=f"Target SMS Server Base URL (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help=f"Path to local device_auth.db fallback (default: {DEFAULT_DB_PATH})",
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # pings
    subparsers.add_parser("pings", help="View SIM relay heartbeat ping messages")
    subparsers.add_parser("ping-list", help="Alias for pings")

    # otp
    otp_p = subparsers.add_parser("otp", help="Retrieve latest OTP for a phone number")
    otp_p.add_argument("phone", help="Bangladeshi mobile number (e.g. 017XXXXXXXX)")
    otp_p.add_argument("--pub", "-p", default=None, help="Filter by publisher (VFS or IVAC)")

    # messages
    subparsers.add_parser("messages", help="List recent OTP messages in retention window")

    # health
    subparsers.add_parser("health", help="Check SMS server health")

    # send-ping
    sp_p = subparsers.add_parser("send-ping", help="Send a manual heartbeat ping")
    sp_p.add_argument("phone", help="Phone number")
    sp_p.add_argument("--slot", "-s", type=int, default=0, help="SIM slot index (0 or 1)")
    sp_p.add_argument("--interval", "-i", type=int, default=15, help="Interval minutes")
    sp_p.add_argument("--status", default="online", help="Status string")
    sp_p.add_argument("--msg", "-m", default="", help="Custom message text")

    # watch
    w_p = subparsers.add_parser("watch", help="Live stream new incoming OTPs and pings")
    w_p.add_argument("--interval", "-i", type=float, default=2.0, help="Poll interval in seconds")

    args = parser.parse_args()
    client = SmsServerClient(base_url=args.url, db_path=args.db)

    if not args.command:
        # Default to interactive REPL
        run_interactive(client)
        return

    cmd = args.command.lower()
    if cmd in ("pings", "ping-list"):
        try:
            pings, src = client.get_pings()
            print_pings(pings, src)
        except Exception as e:
            print(f"[!] Error fetching pings: {e}", file=sys.stderr)
            sys.exit(1)
    elif cmd == "otp":
        try:
            data, src = client.get_otp(args.phone, pub=args.pub)
            print_otp(args.phone, data, src)
        except Exception as e:
            print(f"[!] Error retrieving OTP: {e}", file=sys.stderr)
            sys.exit(1)
    elif cmd == "messages":
        try:
            msgs, src = client.get_messages()
            print_messages(msgs, src)
        except Exception as e:
            print(f"[!] Error fetching messages: {e}", file=sys.stderr)
            sys.exit(1)
    elif cmd == "health":
        try:
            data = client.get_health()
            print_health(data)
        except Exception as e:
            print(f"[!] Error fetching health: {e}", file=sys.stderr)
            sys.exit(1)
    elif cmd == "send-ping":
        try:
            res = client.send_ping(
                phone=args.phone,
                slot=args.slot,
                interval=args.interval,
                status=args.status,
                message=args.msg,
            )
            print(f"[+] Ping recorded: {res.get('ping', {})}")
        except Exception as e:
            print(f"[!] Error sending ping: {e}", file=sys.stderr)
            sys.exit(1)
    elif cmd == "watch":
        watch_loop(client, args.interval)


if __name__ == "__main__":
    main()
