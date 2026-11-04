"""Build the native model picker against the pinned AstrBot dashboard source."""

import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

DASHBOARD_VERSION = "4.29.3"
SOURCE_COMMIT = "f61a7f42696da6df25dc7a249ec9b8d54ed9da18"
PNPM_VERSION = "10.15.1"
ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime"
SOURCE_ROOT = RUNTIME / "source" / f"AstrBot-{SOURCE_COMMIT}"
DASHBOARD = SOURCE_ROOT / "dashboard"


def main() -> None:
    """Download the release source, apply the Vue patch, and build static assets."""
    RUNTIME.mkdir(parents=True, exist_ok=True)
    archive_path = RUNTIME / "astrbot-source.tar.gz"
    if not archive_path.exists():
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        source_url = (
            f"https://codeload.github.com/AstrBotDevs/AstrBot/tar.gz/{SOURCE_COMMIT}"
        )
        with (
            opener.open(source_url, timeout=40) as response,
            archive_path.open("wb") as output,
        ):
            shutil.copyfileobj(response, output)

    # Restore release files before patching; keep downloaded dependencies.
    with tarfile.open(archive_path) as archive:
        members = [
            member
            for member in archive
            if "/dashboard/" in member.name
            or member.name.endswith(
                (
                    "/AGENTS.md",
                    "/LICENSE",
                    "/astrbot/core/utils/t2i/template/shiki_runtime.iife.js",
                )
            )
        ]
        archive.extractall(RUNTIME / "source", members=members, filter="data")

    subprocess.run(
        ["patch", "-p1", "-i", str(ROOT / "native-model-picker.patch")],
        cwd=DASHBOARD,
        check=True,
    )
    shutil.copy2(
        ROOT / "PluginModelPicker.vue",
        DASHBOARD / "src/components/shared/PluginModelPicker.vue",
    )
    pnpm = [
        "npm",
        "exec",
        "--yes",
        "--registry=https://registry.npmjs.org",
        f"--package=pnpm@{PNPM_VERSION}",
        "--",
        "pnpm",
    ]
    subprocess.run(
        [
            *pnpm,
            "install",
            "--frozen-lockfile",
            "--registry=https://registry.npmjs.org",
        ],
        cwd=DASHBOARD,
        check=True,
    )
    subprocess.run([*pnpm, "run", "build"], cwd=DASHBOARD, check=True)
    (DASHBOARD / "dist/assets/version").write_text(DASHBOARD_VERSION + "\n")
    shutil.copytree(DASHBOARD / "dist", RUNTIME / "dist", dirs_exist_ok=True)
    print(f"Dashboard built from AstrBot {SOURCE_COMMIT}: {RUNTIME / 'dist'}")


if __name__ == "__main__":
    main()
