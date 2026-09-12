"""Drain the single web process without taking its maintenance console offline."""

from contextlib import contextmanager
import os
from pathlib import Path
import threading
import time

from starlette.responses import JSONResponse


class MaintenanceGate:
    def __init__(self):
        self.condition = threading.Condition()
        self.paused = False
        self.active = 0

    def pause(self, value=True):
        with self.condition:
            self.paused = value
            self.condition.notify_all()

    def enter(self):
        with self.condition:
            if self.paused:
                return False
            self.active += 1
            return True

    def leave(self):
        with self.condition:
            self.active -= 1
            self.condition.notify_all()

    def drain(self, timeout=300):
        deadline = time.monotonic() + timeout
        with self.condition:
            while self.active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("Active requests or background work have not finished. Retry preparation or leave maintenance and try later.")
                self.condition.wait(remaining)

    @contextmanager
    def background(self):
        entered = self.enter()
        try:
            yield entered
        finally:
            if entered:
                self.leave()

    def start_thread(self, target):
        # Reserve before starting: a not-yet-scheduled thread must also be drained.
        if not self.enter():
            return False
        def run():
            try:
                target()
            finally:
                self.leave()
        try:
            threading.Thread(target=run, daemon=True).start()
        except BaseException:
            self.leave()
            raise
        return True


maintenance = MaintenanceGate()


def console_path(path):
    # Only these routes stay reachable. In particular no corpus GET is exempt:
    # downloads and detail handlers can populate caches or schedule index writes.
    return (path == "/api/circulars/mirror/identity" or path.startswith("/api/circulars/mirror/identity/")
            or path in {"/healthz", "/login", "/api/auth/login", "/api/auth/logout", "/api/auth/me", "/admin"}
            # The identity-migration panel lives on the Ingest section, which is where an
            # operator has to be able to land while the migration holds everything else.
            or path == "/admin/ingest" or path.startswith("/admin/ingest/")
            or path.startswith(("/spa/assets/", "/static/", "/assets/")))


class MaintenanceMiddleware:
    def __init__(self, app, gate=maintenance):
        self.app, self.gate = app, gate

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or console_path(scope["path"]):
            return await self.app(scope, receive, send)
        if not self.gate.enter():
            return await JSONResponse(
                {"error": "SBPEye is temporarily in maintenance while circular identities are migrated. Please try again shortly.",
                 "code": "identity_maintenance"}, status_code=503,
                headers={"Retry-After": "15"},
            )(scope, receive, send)
        try:
            # Pure ASGI, deliberately outside BaseHTTPMiddleware: includes streaming
            # bodies, dependency cleanup, and response BackgroundTasks in the drain.
            await self.app(scope, receive, send)
        finally:
            self.gate.leave()


@contextmanager
def web_process_lease(root: Path):
    """Enforce the deployment's one-web-process contract across workers/restarts."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / "web-process.lock").open("a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("SBPEye requires one web process per data volume for maintenance.") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("SBPEye requires one web process per data volume for maintenance.") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
