"""Local paper service management. No live endpoint, secrets in reports, or risk resets."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from .intraday import NY, session_time, timeout_request
from .dashboard import read_json, local_status


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    os.replace(temp, path)


def inventory(root):
    # Only this project's Python workers are returned; never include other process arguments.
    script = r'''$r=@(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Select-Object ProcessId,CommandLine); ConvertTo-Json -InputObject $r -Compress'''
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                            capture_output=True, text=True, timeout=20,
                            creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    rows = json.loads(result.stdout or '[]')
    prefix = str(root.resolve()).lower()+'\\'
    found = {}
    for row in rows:
        command = (row.get('CommandLine') or '').lower()
        if prefix not in command:
            continue
        for name in ('web_dashboard.py', 'alpaca_auto.py', 'paper_intraday.py', 'scan_intraday.py',
                     'moomoo_auto.py', 'moomoo_market.py', 'moomoo_observer.py', 'moomoo_options_watch.py'):
            if any(token.strip('"').endswith(name) for token in command.split()):
                found.setdefault(name, []).append(row['ProcessId'])
    return found


def launch(root, script, args=(), broker=False):
    python = root/('.venv-brokers' if broker else '.venv')/'Scripts'/'python.exe'
    if not python.is_file() or not (root/script).is_file():
        raise FileNotFoundError('Configured local runtime or worker absent')
    (root/'logs').mkdir(exist_ok=True)
    with (root/'logs'/f'{Path(script).stem}_service.log').open('ab') as out:
        with (root/'logs'/f'{Path(script).stem}_service_error.log').open('ab') as err:
            return subprocess.Popen([str(python), '-u', script, *args], cwd=root,
                                    stdout=out, stderr=err, creationflags=subprocess.CREATE_NO_WINDOW)


class InstanceLock:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        import msvcrt
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open('a+b')
        self.handle.seek(0, 2)
        if not self.handle.tell():
            self.handle.write(b'0'); self.handle.flush()
        self.handle.seek(0)
        try:
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.handle.close()
            raise RuntimeError('Service already running') from None
        return self

    def __exit__(self, *args):
        self.handle.close()


def fresh(record, now=None, seconds=90):
    try:
        age = ((now or datetime.now(timezone.utc))-datetime.fromisoformat(record['updated'])).total_seconds()
        return -5 <= age <= seconds
    except (KeyError, ValueError, TypeError):
        return False


def public_status(root):
    service = read_json(root/'state'/'automation_status.json', {})
    alpaca = read_json(root/'state'/'alpaca_auto_status.json', {})
    moomoo = read_json(root/'state'/'brokers'/'moomoo_auto_status.json', {})
    config = read_json(root/'state'/'automation_config.json', {})
    fields = ('status', 'updated', 'session', 'next_open', 'error_type', 'retry_after')
    def broker(row, enabled, stop):
        return {**{k: row[k] for k in fields if k in row}, 'fresh': fresh(row),
                'enabled': enabled, 'stopped': (root/stop).exists()}
    return dict(updated=service.get('updated'), fresh=fresh(service),
                status=service.get('status', 'NOT_RUNNING'), startup=config.get('startup', 'MANUAL'),
                workers=[{k: r[k] for k in ('name', 'state', 'restarts') if k in r}
                         for r in service.get('workers', [])],
                alpaca=broker(alpaca, bool(config.get('alpaca_enabled')), 'STOP_PAPER'),
                moomoo=broker(moomoo, bool(config.get('moomoo_enabled')), 'STOP_MOOMOO'))


def session_plan(now, opened, closed, flat, stopped, completed, same_session):
    if not same_session and not flat:
        return 'RECONCILIATION_HOLD'
    if now >= closed:
        return 'SESSION_COMPLETE' if flat else 'OVERNIGHT_POSITION_HOLD'
    if stopped and flat:
        return 'MANUAL_STOP'
    if completed and flat:
        return 'SESSION_COMPLETE'
    if now < opened-timedelta(minutes=15):
        return 'WAIT_REGULAR_SESSION'
    if now >= closed-timedelta(minutes=10) and flat:
        return 'SESSION_COMPLETE'
    return 'START'


def renew_authorization(root, client, config, auth, day):
    """Copy the already authorized policy only after reconciling the old session."""
    from .paper_execution import require_paper, TERMINAL, position_limits, slot_limit
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus
    from .scanner import UNIVERSE
    require_paper(client)
    account = client.get_account()
    if (not config.get('alpaca_enabled') or not config.get('recurring_paper_authorized')
            or config.get('alpaca_account_id') != str(account.id)
            or auth.get('account_id') != str(account.id) or not auth.get('paper_only')
            or not auth.get('authorized') or auth.get('strategy') != 'multi_1m'
            or tuple(auth.get('universe', [])) != UNIVERSE or auth.get('session', '') > day):
        raise ValueError('Recurring paper authorization mismatch')
    position_limits(auth); slot_limit(auth)
    if auth['session'] == day:
        return auth  # Preserve same-day journal, halt, baseline and entry limits.
    if client.get_all_positions() or client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)):
        raise ValueError('Previous session not flat and reconciled')
    old_path = root/'state'/f"paper_intents_{auth['session']}.json"
    old = read_json(old_path, None) if old_path.exists() else {}
    if not isinstance(old, dict):
        raise ValueError('Previous journal unreadable')
    if old and (old.get('account_id') != str(account.id) or old.get('session') != auth['session']):
        raise ValueError('Previous journal mismatch')
    for cid, row in old.get('orders', {}).items():
        if row.get('status') not in TERMINAL:
            confirmed = client.get_order_by_client_id(cid).status
            status = str(getattr(confirmed, 'value', confirmed))
            if status not in TERMINAL:
                raise ValueError('Previous order remains unresolved')
    # Keep prior auth and ledger for review; do not create/reset a new risk state here.
    write(root/'state'/'paper_authorization_history'/f"{auth['session']}.json", auth)
    updated = dict(auth, session=day)
    write(root/'state'/'paper_authorization.json', updated)
    return updated


class AlpacaService:
    def __init__(self, root, client, start=launch, processes=inventory):
        self.root, self.client, self.start, self.processes = root, client, start, processes
        self.child = None
        self.last_start = 0

    def step(self):
        from .paper_execution import require_paper
        from alpaca.trading.requests import GetCalendarRequest, GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        require_paper(self.client)
        config = read_json(self.root/'state'/'automation_config.json', {})
        if not config.get('alpaca_enabled'):
            return dict(status='DISABLED')
        clock = self.client.get_clock()
        now = clock.timestamp.astimezone(NY)
        day = str(now.date())
        common = dict(session=day, next_open=clock.next_open.isoformat())
        account = self.client.get_account()
        if config.get('alpaca_account_id') != str(account.id) or not config.get('recurring_paper_authorized'):
            raise ValueError('Recurring account not authorized')
        auth = read_json(self.root/'state'/'paper_authorization.json', {})
        positions = self.client.get_all_positions()
        orders = self.client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
        flat = not positions and not orders
        # Never write a new authorization while a manual/managed executor still owns its lock.
        if self.child is not None:
            self.child.poll()
        if self.processes(self.root).get('paper_intraday.py'):
            local = local_status(self.root)
            return common | dict(status='RUNNING' if local['healthy'] else 'EXECUTOR_STARTING_OR_STALE')
        calendar = self.client.get_calendar(GetCalendarRequest(start=now.date(), end=now.date()))
        if len(calendar) != 1:
            return common | dict(status='WAIT_TRADING_DAY' if flat else 'OVERNIGHT_POSITION_HOLD')
        opened, closed = session_time(calendar[0].open), session_time(calendar[0].close)
        last = local_status(self.root)['last']
        completed = (last.get('session') == day and last.get('done') is True
                     and last.get('status') in {'FLAT', 'SESSION_FINISHED', 'STOPPED_FLAT'})
        plan = session_plan(now, opened, closed, flat, (self.root/'STOP_PAPER').exists(),
                            completed, auth.get('session') == day)
        if plan != 'START':
            return common | dict(status=plan)
        if time.monotonic()-self.last_start < 120:
            return common | dict(status='RETRY_BACKOFF')
        renew_authorization(self.root, self.client, config, auth, day)
        self.child = self.start(self.root, 'paper_intraday.py', ('--execute-paper', '--session', day))
        self.last_start = time.monotonic()
        write(self.root/'state'/'paper_process.json', dict(pid=self.child.pid, program='paper_intraday.py', session=day))
        return common | dict(status='EXECUTOR_STARTING_OR_STALE')


def run_alpaca(root):
    from dotenv import load_dotenv
    from alpaca.trading.client import TradingClient
    load_dotenv(root/'.env', override=False)
    with InstanceLock(root/'state'/'alpaca_auto.lock'):
        client = TradingClient(os.environ['ALPACA_KEY'], os.environ['ALPACA_SECRET'], paper=True)
        client._session.request = timeout_request(client._session.request)
        client._retry = 0
        service = AlpacaService(root, client)
        while True:
            try:
                report = service.step()
            except Exception as exc:
                report = dict(status='CONNECTION_OR_RECONCILIATION_HOLD', error_type=type(exc).__name__)
            write(root/'state'/'alpaca_auto_status.json', report | dict(updated=datetime.now(timezone.utc).isoformat()))
            time.sleep(20)


class Supervisor:
    def __init__(self, root, start=launch, processes=inventory):
        self.root, self.start, self.processes = root, start, processes
        self.children, self.attempts, self.next_retry = {}, {}, {}

    def step(self):
        config = read_json(self.root/'state'/'automation_config.json', {})
        found = self.processes(self.root)  # An inventory error prevents spawning, rather than duplicates.
        wanted = [('web_dashboard.py', (), False)]
        if config.get('alpaca_enabled'):
            wanted.append(('alpaca_auto.py', ('--execute-paper',), False))
        if config.get('moomoo_enabled'):
            wanted += [('moomoo_market.py', (), True), ('moomoo_observer.py', ('--watch',), True),
                       ('moomoo_options_watch.py', (), True), ('moomoo_auto.py', ('--execute-paper',), True)]
        rows = []
        for name, args, broker in wanted:
            child = self.children.get(name)
            running = bool(found.get(name)) or child is not None and child.poll() is None
            state = 'RUNNING'
            if not running:
                if time.monotonic() < self.next_retry.get(name, 0):
                    state = 'RETRY_BACKOFF'
                else:
                    count = self.attempts.get(name, 0)+1
                    self.attempts[name] = count
                    self.next_retry[name] = time.monotonic()+min(900, 30*2**min(count-1, 5))
                    try:
                        self.children[name] = self.start(self.root, name, args, broker)
                        state = 'STARTING'
                    except Exception:
                        state = 'START_FAILED'
            rows.append(dict(name=name, state=state, restarts=self.attempts.get(name, 0)))
        return dict(status='RUNNING', workers=rows, updated=datetime.now(timezone.utc).isoformat())


def run_supervisor(root):
    with InstanceLock(root/'state'/'automation_supervisor.lock'):
        supervisor = Supervisor(root)
        while True:
            try:
                report = supervisor.step()
            except Exception as exc:
                report = dict(status='INVENTORY_HOLD', error_type=type(exc).__name__,
                              updated=datetime.now(timezone.utc).isoformat())
            write(root/'state'/'automation_status.json', report)
            time.sleep(20)
