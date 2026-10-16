"""Disposable installed-wheel browser smoke test for the frozen 0.2.0 candidate."""

import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
CANDIDATE = ROOT / "candidate"
sys.path.insert(0, str(CANDIDATE / "integration_tests" / "scale"))
from postgres_scale import build_project, load_history  # noqa: E402


def app_process(project, port, browse):
    command = [str(Path(sys.executable).with_name("dbt-dqm")), "app",
               "--project-dir", str(project.path), "--profiles-dir", str(project.profiles),
               "--browse", browse, "--port", str(port)]
    log = open(CANDIDATE / f"stable-browser-{browse}.log", "w")
    process = subprocess.Popen(command, cwd=CANDIDATE, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{browse} app exited {process.returncode}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=2) as response:
                if response.status == 200:
                    return process, log
        except OSError:
            time.sleep(0.5)
    raise RuntimeError(f"{browse} app did not become healthy")


def wait_for_rows(page):
    expect(page.get_by_text(re.compile(r"showing \d+ of \d+ (?:matching )?issue record", re.I))).to_be_visible(timeout=180_000)


def run_browser(project, browse, port):
    process, log = app_process(project, port, browse)
    screenshot = CANDIDATE / f"stable-browser-{browse}.png"
    try:
        with sync_playwright() as playwright:
            chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
            if not chrome:
                raise RuntimeError("Google Chrome is missing from the CI runner")
            browser = playwright.chromium.launch(executable_path=chrome, headless=True,
                                                 args=["--no-sandbox"])
            page = browser.new_page(accept_downloads=True)
            page.set_default_timeout(30_000)
            try:
                page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
                expect(page.get_by_role("button", name="Sync", exact=True)).to_be_visible(timeout=90_000)
                page.get_by_role("button", name="Sync", exact=True).click()
                expect(page.get_by_role("button", name="Sync", exact=True)).to_be_disabled(timeout=30_000)
                expect(page.get_by_role("button", name="Sync", exact=True)).to_be_enabled(timeout=180_000)
                wait_for_rows(page)
                page.get_by_role("tab", name="Health").click()
                expect(page.get_by_text("Test health")).to_be_visible(timeout=30_000)
                expect(page.get_by_text("Maintenance", exact=True)).to_be_visible()
                page.get_by_role("tab", name="Issues").click()

                if browse == "snapshot":
                    page.get_by_text("Edit annotations").first.click()
                    note = f"stable-browser-qa-{uuid.uuid4().hex[:8]}"
                    editor = page.get_by_role("textbox", name="Notes").first
                    editor.fill(note)
                    editor.press("Tab")
                    expect(page.get_by_text("Pending changes: 1")).to_be_visible(timeout=30_000)
                    page.get_by_role("button", name="Apply and sync").click()
                    deadline = time.monotonic() + 120
                    while time.monotonic() < deadline:
                        count = project.sql("select count(*) from @schema.dqm_all_issues where notes = %s", (note,))[0][0]
                        if count == 1:
                            break
                        time.sleep(1)
                    else:
                        raise AssertionError("Applied note did not reach the warehouse")

                page.get_by_text("Export saved issues").click()
                page.get_by_role("button", name="Prepare CSV").click()
                expect(page.get_by_role("button", name="Download CSV")).to_be_visible(timeout=120_000)
                with page.expect_download() as download_info:
                    page.get_by_role("button", name="Download CSV").click()
                downloaded = download_info.value.path().read_bytes()
                records = list(csv.DictReader(io.StringIO(downloaded.decode("utf-8-sig"))))
                assert len(records) >= 35 and "record_values_json" in records[0]
                assert not page.locator(".stException").count()
                page.screenshot(path=str(screenshot))
                return {"mode": browse, "export_rows": len(records), "screenshot": screenshot.name}
            except Exception:
                page.screenshot(path=str(screenshot))
                raise
            finally:
                browser.close()
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
        log.close()


def main():
    dsn = os.environ["DBT_DQM_TEST_DSN"]
    with tempfile.TemporaryDirectory(prefix="stable-browser-") as directory:
        project = build_project(Path(directory), dsn, "dqm_browser_" + uuid.uuid4().hex[:12], 1)
        try:
            project.dbt("seed")
            project.dbt("run", "--select", "demo_records")
            command = [str(Path(sys.executable).with_name("dbt-dqm")), "run", "--full-refresh", "--yes",
                       "--project-dir", str(project.path), "--profiles-dir", str(project.profiles)]
            result = subprocess.run(command, cwd=CANDIDATE, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(f"dbt-dqm run failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
            load_history(project, SimpleNamespace(tests=1, occurrences=40, active_fraction=1.0,
                                                  executions=1, observations=40, payload_columns=2))
            output = [run_browser(project, "snapshot", 8501), run_browser(project, "warehouse", 8502)]
            (CANDIDATE / "stable-browser.json").write_text(json.dumps(output, indent=2) + "\n")
            print(json.dumps(output))
        finally:
            project.sql("drop schema if exists @schema cascade")
            project.sql(f'drop schema if exists "{project.schema}_dbt_test__audit" cascade')


if __name__ == "__main__":
    main()
