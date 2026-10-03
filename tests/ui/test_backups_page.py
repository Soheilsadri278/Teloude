"""Live progress + "failed backup can be resumed" at the Qt layer.

These tests drive a real backup through :class:`AppController` on the asyncio thread and watch the
widgets, so they cover the whole path Telegram acknowledgement -> pipeline -> events -> Qt -> bar.
"""

from __future__ import annotations

import os
import time

import pytest

pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.application.notifications import human_size  # noqa: E402
from app.application.pipeline import PipelineConfig  # noqa: E402
from app.domain.errors import PermanentTransferError, TransientNetworkError  # noqa: E402
from app.domain.models import RunOutcome, UploadLimits  # noqa: E402
from app.domain.retry import RetryPolicy  # noqa: E402
from app.infrastructure.config import AppPaths  # noqa: E402
from app.presentation.controller import AppController  # noqa: E402
from app.presentation.icon import app_icon  # noqa: E402
from app.presentation.main_window import MainWindow  # noqa: E402
from app.presentation.theme import LIGHT, apply_theme  # noqa: E402
from app.testing.fake_gateway import FakeGateway  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def wait_for(qapp, cond, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def fast_config() -> PipelineConfig:
    """The production config retries with real 1s..30s backoff (right for a laptop that lost wifi).

    These tests inject failures a lot, so they use the same pipeline with an instant sleep instead -
    the code under test is identical, only the clock is different.
    """
    async def instant(_d: float) -> None:
        return None

    return PipelineConfig(
        concurrency=4,
        retry=RetryPolicy(base_delay=0.01, max_delay=0.05, jitter=0.0, max_attempts=3),
        progress_interval=0.02,
        checkpoint_interval=0.02,
        sleep=instant,
    )


@pytest.fixture
def ctl(qapp, tmp_path):
    paths = AppPaths(tmp_path / "data").ensure()
    c = AppController(paths, gateway=FakeGateway())
    c.settings.set_api_credentials(12345, "hash-for-tests-only")
    c.start()
    c.backup._pipeline_config = fast_config  # injected after construction: BackupService captured it
    # 1 KiB parts (instead of the 512 KiB default) so a tiny test file really is many parts,
    # and a 10 ms round trip so the 20 ms progress reporter has something to report.
    c.gateway.limits = UploadLimits(part_size=1024, max_parts=4000)
    c.gateway.part_latency = 0.01
    yield c
    if c.active:  # never tear the database out from under a running transfer
        c.active.control.cancel()
        wait_for(qapp, lambda: c.active is None, timeout=5)
    c.shutdown()


@pytest.fixture
def win(qapp, ctl):
    apply_theme(qapp, "light")
    w = MainWindow(ctl, app_icon(), LIGHT)
    w.pages.setCurrentIndex(2)  # Backups
    w.show()  # layouts are only computed for a shown window
    settle(qapp, ctl, w)
    return w


def settle(qapp, ctl, w, timeout: float = 10.0) -> None:
    """Let the async bootstrap finish, then force the signed-in shell (the page under test)."""
    seen: list[str] = []
    ctl.login_state.connect(lambda state, detail: seen.append(state))
    end = time.time() + timeout
    while time.time() < end and not seen:
        qapp.processEvents()
        time.sleep(0.005)
    w._login_state("ready", "")
    qapp.processEvents()
    qapp.processEvents()


def make_source(tmp_path, size=40 * 1024):
    src = tmp_path / "Docs"
    src.mkdir()
    (src / "a.bin").write_bytes(os.urandom(size))
    return src


def new_storage(qapp, ctl, name="Docs"):
    created: list[int] = []
    ctl.create_storage(name, created.append)
    assert wait_for(qapp, lambda: created)
    return created[0]


# ------------------------------------------------------------------ live progress
def test_overall_progress_bar_moves_continuously_not_only_at_the_end(qapp, ctl, win, tmp_path):
    """The reported bug: the bar sat at 0 and only turned fully blue when the backup finished."""
    src = make_source(tmp_path)
    sid = new_storage(qapp, ctl)
    page = win.page_widgets["Backups"]

    samples: list[int] = []
    texts: list[str] = []
    finished: list = []
    ctl.op_finished.connect(finished.append)
    ctl.start_backup(sid, str(src))

    end = time.time() + 20
    while time.time() < end and not finished:
        qapp.processEvents()
        samples.append(page.overall.value())
        texts.append(page.op_bytes.text())
        time.sleep(0.005)
    assert wait_for(qapp, lambda: finished)

    partial = [v for v in samples if 0 < v < 1000]
    assert partial, f"the overall bar only ever showed {sorted(set(samples))} - it never moved"
    assert partial == sorted(partial), "the bar went backwards: it is not tracking acknowledged bytes"
    assert page.overall.value() == 1000
    assert any("/" in t for t in texts if t), "no human_size(done)/human_size(total) text was shown"
    assert finished[0].outcome == RunOutcome.COMPLETED


def test_per_file_progress_never_goes_backwards_when_the_state_changes(qapp, ctl, win, tmp_path):
    """A RECONNECTING/RETRYING state event must not wipe the acknowledged progress."""
    src = make_source(tmp_path, 60 * 1024)
    blips = {"n": 0}

    def blip(op, n):  # one transient failure in the middle -> RECONNECTING / RETRYING states
        if op == "upload_part" and blips["n"] < 1 and n > 5:
            blips["n"] += 1
            return TransientNetworkError("blip")
        return None

    ctl.gateway.fail_hook = blip
    sid = new_storage(qapp, ctl)
    page = win.page_widgets["Backups"]

    states: list[str] = []
    values: list[int] = []

    # Connected AFTER the page, so this slot runs after the page already painted the value.
    def record(fid, rel, st, done, total, speed, eta):
        row = page.rows.get(fid)
        if row is None:
            return
        states.append(st)
        values.append(int(page.table.cellWidget(row, 2).value()))

    ctl.file_event.connect(record)
    finished: list = []
    ctl.op_finished.connect(finished.append)
    ctl.start_backup(sid, str(src))
    assert wait_for(qapp, lambda: finished, timeout=30)

    assert "reconnecting" in states or "retrying" in states, "the transient failure never happened"
    # Drop the leading zeros (before the first acknowledged part) and require a non-decreasing run.
    started = False
    prev = 0
    for st, v in zip(states, values, strict=False):
        if v == 0 and not started:
            continue
        started = True
        assert v >= prev, f"progress bar went backwards on state {st!r}: {prev} -> {v}"
        prev = v
    assert values[-1] == 1000


def test_progress_after_resume_starts_from_the_checkpoint_in_the_ui(qapp, ctl, win, tmp_path):
    src = make_source(tmp_path, 60 * 1024)
    down = {"on": True}
    ctl.gateway.fail_hook = lambda op, n: TransientNetworkError("down") if op == "upload_part" and down["on"] else None
    sid = new_storage(qapp, ctl)
    page = win.page_widgets["Backups"]

    finished: list = []
    ctl.op_finished.connect(finished.append)
    ctl.start_backup(sid, str(src))
    assert wait_for(qapp, lambda: finished)
    assert finished[0].outcome == RunOutcome.FAILED
    assert page.resume_btn.isEnabled(), "a failed backup must offer Resume"

    down["on"] = False
    seen: list[float] = []
    ctl.op_progress.connect(lambda d, t, s, e: seen.append(d))
    finished.clear()
    page.resume_btn.click()  # the Resume action, not a re-run of the wizard
    assert wait_for(qapp, lambda: finished)
    assert finished[0].outcome == RunOutcome.COMPLETED
    assert seen and seen[0] < max(seen) and max(seen) > 0, "resumed progress did not move"
    assert page.overall.value() == 1000


# ------------------------------------------------------------------ resume after failure
def test_failed_backup_shows_reason_progress_and_a_working_resume(qapp, ctl, win, tmp_path):
    src = make_source(tmp_path, 80 * 1024)
    down = {"on": True}
    ctl.gateway.fail_hook = lambda op, n: TransientNetworkError("down") if op == "upload_part" and down["on"] else None
    sid = new_storage(qapp, ctl)
    page = win.page_widgets["Backups"]

    finished: list = []
    started: list = []
    ctl.op_finished.connect(finished.append)
    ctl.op_started.connect(lambda kind, label: started.append(kind))
    ctl.start_backup(sid, str(src))
    assert wait_for(qapp, lambda: finished)
    assert finished[0].outcome == RunOutcome.FAILED
    started.clear()

    text = page.op_state.text()
    assert "failed" in text.lower()
    assert "Reason:" in text, f"no reason shown to the user: {text!r}"
    assert "/" in text and "MB" in text or "KB" in text, f"no done/total shown: {text!r}"
    assert page.details_btn.isEnabled()
    assert not page.pause_btn.isEnabled() and page.cancel_btn.isEnabled() is False

    down["on"] = False
    finished.clear()
    page.resume_btn.click()
    page.resume_btn.click()  # a second click must not start a second upload
    assert wait_for(qapp, lambda: finished, timeout=30)
    assert len(started) == 1, f"Resume started {len(started)} operations"
    assert finished[0].outcome == RunOutcome.COMPLETED
    assert len(ctl.gateway.all_messages()) == 1


def test_permanent_failure_does_not_offer_resume(qapp, ctl, win, tmp_path):
    src = make_source(tmp_path, 3000)
    sid = new_storage(qapp, ctl)
    page = win.page_widgets["Backups"]

    async def rejects(*a, **k):  # type: ignore[no-untyped-def]
        raise PermanentTransferError("unsupported file")

    ctl.gateway.finalize_upload = rejects  # type: ignore[method-assign]
    finished: list = []
    ctl.op_finished.connect(finished.append)
    ctl.start_backup(sid, str(src))
    assert wait_for(qapp, lambda: finished)

    assert finished[0].outcome == RunOutcome.FAILED and finished[0].permanent == 1
    assert not page.resume_btn.isEnabled(), "a permanently failed file must not offer Resume"
    assert page.details_btn.isEnabled()


def test_resume_service_reports_human_readable_totals(qapp, ctl, tmp_path):
    src = make_source(tmp_path, 40 * 1024)
    down = {"on": True}
    ctl.gateway.fail_hook = (
        lambda op, n: TransientNetworkError("down") if op == "upload_part" and down["on"] and n >= 10 else None
    )
    sid = new_storage(qapp, ctl)
    finished: list = []
    ctl.op_finished.connect(finished.append)
    ctl.start_backup(sid, str(src))
    assert wait_for(qapp, lambda: finished)

    summary = ctl.resumable_summary(sid)
    assert summary.resumable and summary.count == 1
    assert summary.total_bytes == 40 * 1024
    assert 0 < summary.done_bytes < summary.total_bytes
    assert human_size(summary.done_bytes) and summary.local_root == str(src)
