"""
在线更新模块

功能：
- 从远程仓库检查新版本（GitHub Releases 或自定义服务器）
- 下载更新包并校验
- 支持增量更新和完整更新
- 更新前自动备份
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import urllib.request
import urllib.error

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 默认更新源配置
# ---------------------------------------------------------------------------

DEFAULT_UPDATE_MANIFEST_URL = os.getenv(
    "IOT_UPDATE_MANIFEST_URL",
    "https://api.github.com/repos/mikokeyu1986-arch/iot_box_comercia/contents/manifest.json?ref=master",
)

# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


class VersionInfo:
    """版本信息"""
    __slots__ = ("version", "release_date", "changelog", "download_url", "checksum", "min_version", "file_size")

    def __init__(self, data: dict[str, Any]) -> None:
        self.version: str = str(data.get("version", ""))
        self.release_date: str = str(data.get("release_date", ""))
        self.changelog: str = str(data.get("changelog", ""))
        self.download_url: str = str(data.get("download_url", ""))
        self.checksum: str = str(data.get("checksum", "")).lower()
        self.min_version: str = str(data.get("min_version", ""))
        self.file_size: int = int(data.get("file_size", 0))

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "release_date": self.release_date,
            "changelog": self.changelog,
            "download_url": self.download_url,
            "checksum": self.checksum,
            "min_version": self.min_version,
            "file_size": self.file_size,
        }


class UpdateResult:
    """更新操作结果"""
    __slots__ = ("success", "message", "current_version", "latest_version", "can_update", "details")

    def __init__(self, success: bool, message: str = "", **kwargs: Any) -> None:
        self.success = success
        self.message = message
        self.current_version: str = kwargs.get("current_version", "")
        self.latest_version: str = kwargs.get("latest_version", "")
        self.can_update: bool = kwargs.get("can_update", False)
        self.details: Any = kwargs.get("details")


# ---------------------------------------------------------------------------
# 版本号比较
# ---------------------------------------------------------------------------


def _parse_version(version_str: str) -> tuple[int, ...]:
    """将版本号字符串解析为可比较的元组，如 \"2026.04.10\" -> (2026, 4, 10)"""
    parts = version_str.strip().replace("-", ".").replace("_", ".").split(".")
    result: list[int] = []
    for p in parts:
        try:
            result.append(int(p))
        except ValueError:
            result.append(0)
    return tuple(result)


def is_newer(latest: str, current: str) -> bool:
    """判断 latest 版本是否比 current 更新"""
    return _parse_version(latest) > _parse_version(current)


# ---------------------------------------------------------------------------
# 更新源解析器
# ---------------------------------------------------------------------------


class UpdateSource:
    """更新源接口。默认实现从自定义 JSON manifest 获取版本信息。"""

    def __init__(self, manifest_url: str | None = None) -> None:
        self.manifest_url = (manifest_url or DEFAULT_UPDATE_MANIFEST_URL).strip()

    def fetch_manifest(self) -> dict[str, Any]:
        """获取远端的更新清单 JSON"""
        if not self.manifest_url:
            raise ValueError("未配置更新源 URL（IOT_UPDATE_MANIFEST_URL）")

        req = urllib.request.Request(
            self.manifest_url,
            headers={
                "User-Agent": "IoTBox-Updater/1.0",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, dict) and data.get("encoding") == "base64" and data.get("content"):
                content = base64.b64decode(data["content"].replace("\n", "")).decode("utf-8")
                data = json.loads(content)
            if not isinstance(data, dict):
                raise ValueError("更新清单格式无效")
            return data
        except urllib.error.HTTPError as e:
            raise ConnectionError(f"获取更新清单失败: HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise ConnectionError(f"无法连接更新服务器: {e.reason}") from e
        except json.JSONDecodeError as e:
            raise ValueError(f"更新清单 JSON 解析失败: {e}") from e

    def parse_latest_release(self, manifest: dict[str, Any]) -> VersionInfo | None:
        """从 manifest 中解析最新版本信息"""
        releases = manifest.get("releases")
        if isinstance(releases, list) and releases:
            latest = releases[0]
            if isinstance(latest, dict):
                return VersionInfo(latest)

        # 兼容扁平结构
        version = manifest.get("version") or manifest.get("latest_version")
        if version:
            return VersionInfo(manifest)

        return None


# ---------------------------------------------------------------------------
# GitHub Releases 更新源
# ---------------------------------------------------------------------------


class GitHubUpdateSource(UpdateSource):
    """从 GitHub Releases 获取更新信息"""

    def __init__(self, owner: str, repo: str, use_prerelease: bool = False) -> None:
        super().__init__()
        self.owner = owner
        self.repo = repo
        self.use_prerelease = use_prerelease
        self.manifest_url = (
            f"https://api.github.com/repos/{owner}/{repo}/releases"
        )

    def fetch_manifest(self) -> dict[str, Any]:
        req = urllib.request.Request(
            self.manifest_url,
            headers={
                "User-Agent": "IoTBox-Updater/1.0",
                "Accept": "application/vnd.github.v3+json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                releases = json.loads(resp.read().decode("utf-8"))
            if not isinstance(releases, list):
                raise ValueError("GitHub 返回格式无效")
            return {"releases": releases}
        except urllib.error.HTTPError as e:
            raise ConnectionError(f"GitHub API 请求失败: HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise ConnectionError(f"无法连接 GitHub: {e.reason}") from e

    def parse_latest_release(self, manifest: dict[str, Any]) -> VersionInfo | None:
        releases: list[dict[str, Any]] = manifest.get("releases", [])
        for release in releases:
            if not isinstance(release, dict):
                continue
            if release.get("draft"):
                continue
            if release.get("prerelease") and not self.use_prerelease:
                continue
            tag = str(release.get("tag_name", "")).lstrip("vV")
            if not tag:
                continue

            # 优先使用上传的 assets（.zip 文件）
            assets = release.get("assets") or []
            download_url = ""
            file_size = 0
            for asset in assets:
                if not isinstance(asset, dict):
                    continue
                name = str(asset.get("name", "")).lower()
                if name.endswith(".zip"):
                    download_url = str(asset.get("browser_download_url", ""))
                    file_size = int(asset.get("size", 0))
                    break
            if not download_url and assets:
                first = assets[0]
                if isinstance(first, dict):
                    download_url = str(first.get("browser_download_url", ""))
                    file_size = int(first.get("size", 0))

            # 如果没有上传 assets，回退到 GitHub 自动生成的源码 zip
            if not download_url:
                download_url = str(release.get("zipball_url", ""))

            return VersionInfo({
                "version": tag,
                "release_date": str(release.get("published_at", "")),
                "changelog": str(release.get("body", "")),
                "download_url": download_url,
                "checksum": "",
                "file_size": file_size,
            })
        return None


# ---------------------------------------------------------------------------
# 更新管理器
# ---------------------------------------------------------------------------


class UpdateManager:
    """更新管理器：版本检查 -> 下载 -> 安装 -> 重启"""

    def __init__(
        self,
        current_version: str,
        base_dir: Path,
        source: UpdateSource | None = None,
    ) -> None:
        self.current_version = current_version
        self.base_dir = Path(base_dir).resolve()
        self.source = source or UpdateSource()
        self._backup_dir = self.base_dir / "backups"

    # ------------------------------------------------------------------
    # 版本检查
    # ------------------------------------------------------------------

    def check_for_updates(self) -> UpdateResult:
        """检查是否有可用更新"""
        try:
            manifest = self.source.fetch_manifest()
            latest_release = self.source.parse_latest_release(manifest)
        except Exception as exc:
            _logger.exception("检查更新失败")
            return UpdateResult(
                success=False,
                message=f"检查更新失败: {exc}",
                current_version=self.current_version,
            )

        if latest_release is None:
            return UpdateResult(
                success=True,
                message="未找到任何发布版本",
                current_version=self.current_version,
            )

        if not is_newer(latest_release.version, self.current_version):
            return UpdateResult(
                success=True,
                message=f"已是最新版本 ({self.current_version})",
                current_version=self.current_version,
                latest_version=latest_release.version,
                can_update=False,
                details=latest_release.to_dict(),
            )

        return UpdateResult(
            success=True,
            message=f"发现新版本 {latest_release.version}",
            current_version=self.current_version,
            latest_version=latest_release.version,
            can_update=True,
            details=latest_release.to_dict(),
        )

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------

    def download_update(self, version_info: VersionInfo, progress_callback=None) -> Path:
        """下载更新包，返回临时文件路径

        progress_callback(percent: int) 可用于进度报告
        """
        if not version_info.download_url:
            raise ValueError("该版本没有可下载的文件")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", version_info.checksum):
            raise ValueError("更新清单缺少有效的 SHA-256 校验和，已拒绝不安全更新")

        # 确定临时文件路径
        ext = ".zip"
        url_path = version_info.download_url.split("?")[0]
        if url_path.lower().endswith(".zip"):
            ext = ".zip"
        elif url_path.lower().endswith(".7z"):
            ext = ".7z"
        elif url_path.lower().endswith(".exe"):
            ext = ".exe"
        else:
            ext = ".zip"

        tmp_dir = Path(tempfile.gettempdir()) / "iot_box_updates"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_file = tmp_dir / f"iot_box_{version_info.version}{ext}"

        _logger.info("开始下载更新包: %s -> %s", version_info.download_url, tmp_file)

        req = urllib.request.Request(
            version_info.download_url,
            headers={"User-Agent": "IoTBox-Updater/1.0"},
        )

        total_size = version_info.file_size
        downloaded = 0

        with urllib.request.urlopen(req, timeout=300) as resp:
            total = int(resp.headers.get("Content-Length", total_size))
            with open(tmp_file, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total > 0:
                        pct = min(100, int(downloaded * 100 / total))
                        progress_callback(pct)

        _logger.info("校验下载文件...")
        actual_hash = _sha256_file(tmp_file)
        if actual_hash.lower() != version_info.checksum.lower():
            tmp_file.unlink(missing_ok=True)
            raise ValueError(f"校验和不匹配: 期望 {version_info.checksum}, 实际 {actual_hash}")

        _logger.info("下载完成: %s (%d bytes)", tmp_file, downloaded)
        return tmp_file

    # ------------------------------------------------------------------
    # 安装
    # ------------------------------------------------------------------

    def install_update(self, package_path: Path, version: str) -> UpdateResult:
        """安装更新包

        流程：
        1. 备份当前代码
        2. 解压更新包到临时目录
        3. 覆盖文件
        4. 清理临时文件
        """
        extract_dir: Path | None = None
        if package_path.suffix.lower() == ".exe":
            backup_path = self._create_backup()
            subprocess.Popen(
                [str(package_path), "/SILENT", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"],
                cwd=str(package_path.parent),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return UpdateResult(
                success=True,
                message=f"安装包已启动: {version}，现场配置将被保留。",
                current_version=self.current_version,
                latest_version=version,
                details={"backup_path": str(backup_path), "installer": str(package_path)},
            )
        try:
            # 1. 备份
            backup_path = self._create_backup()
            _logger.info("已备份到: %s", backup_path)

            # 2. 解压到临时目录（自动处理 GitHub 嵌套目录）
            extract_dir = Path(tempfile.mkdtemp(prefix="iot_box_extract_"))
            effective_dir = self._extract(package_path, extract_dir)

            # 3. 覆盖文件（排除运行时配置文件）
            self._apply_files(effective_dir)

            # 4. 清理
            package_path.unlink(missing_ok=True)

            # 写入版本标记
            version_file = self.base_dir / ".version"
            version_file.write_text(version, encoding="utf-8")

            _logger.info("更新安装完成，新版本: %s", version)
            return UpdateResult(
                success=True,
                message=f"更新安装完成: {version}。请重启服务以应用更新。",
                current_version=self.current_version,
                latest_version=version,
                details={"backup_path": str(backup_path)},
            )
        except Exception as exc:
            _logger.exception("安装更新失败")
            return UpdateResult(
                success=False,
                message=f"安装更新失败: {exc}",
                current_version=self.current_version,
                latest_version=version,
            )
        finally:
            if extract_dir is not None:
                shutil.rmtree(extract_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _create_backup(self) -> Path:
        """创建当前代码的备份"""
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = __import__("time").strftime("%Y%m%d_%H%M%S")
        backup_name = f"backup_{timestamp}.zip"
        backup_path = self._backup_dir / backup_name

        with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in self._collect_source_files():
                arcname = file_path.relative_to(self.base_dir)
                zf.write(file_path, arcname)

        # 保留最近 5 个备份
        backups = sorted(
            [p for p in self._backup_dir.glob("backup_*.zip")],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in backups[5:]:
            old.unlink(missing_ok=True)

        return backup_path

    def _collect_source_files(self) -> list[Path]:
        """收集需要备份的源文件（排除日志、临时文件等）"""
        exclude_names = {
            "__pycache__", ".git", "logs", "spool", "certs",
            "backups", ".pytest_cache", ".mypy_cache", ".ruff_cache",
            "node_modules", "*.egg-info", ".codebuddy", ".claude",
        }
        exclude_suffixes = {".pyc", ".pyo", ".log", ".tmp"}
        result: list[Path] = []
        for root, directory_names, file_names in os.walk(self.base_dir):
            directory_names[:] = [
                name for name in directory_names
                if name not in exclude_names and not name.startswith(".")
            ]
            root_path = Path(root)
            for name in file_names:
                item = root_path / name
                if name in exclude_names or item.suffix in exclude_suffixes:
                    continue
                result.append(item)
        return result

    def _extract(self, package_path: Path, dest: Path) -> Path:
        """解压更新包，返回有效的源目录（处理 GitHub 自动生成的嵌套目录）"""
        dest.mkdir(parents=True, exist_ok=True)
        if package_path.suffix.lower() == ".zip":
            with zipfile.ZipFile(package_path, "r") as zf:
                destination = dest.resolve()
                for member in zf.infolist():
                    member_path = (dest / member.filename).resolve()
                    try:
                        member_path.relative_to(destination)
                    except ValueError as exc:
                        raise ValueError(f"更新包包含越界路径: {member.filename}") from exc
                    file_type = (member.external_attr >> 16) & 0o170000
                    if file_type == stat.S_IFLNK:
                        raise ValueError(f"更新包不允许符号链接: {member.filename}")
                zf.extractall(dest)
        else:
            raise ValueError(f"不支持的文件格式: {package_path.suffix}")

        # 如果解压后只有一个顶层目录，自动进入该目录
        items = list(dest.iterdir())
        if len(items) == 1 and items[0].is_dir():
            return items[0]
        return dest

    def _apply_files(self, source_dir: Path) -> None:
        """将解压后的文件覆盖到 base_dir"""
        # 不覆盖的文件（运行时配置等）
        do_not_overwrite = {
            "runtime_config.json",
        }

        for src in source_dir.rglob("*"):
            if src.is_dir():
                continue
            rel_path = src.relative_to(source_dir)

            # 跳过配置文件
            if rel_path.name in do_not_overwrite:
                _logger.info("跳过配置文件: %s", rel_path)
                continue

            dest = self.base_dir / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)

    def get_backups(self) -> list[dict[str, Any]]:
        """获取备份列表"""
        if not self._backup_dir.exists():
            return []
        result = []
        for p in sorted(
            self._backup_dir.glob("backup_*.zip"),
            key=lambda x: x.stat().st_mtime, reverse=True,
        ):
            stat = p.stat()
            result.append({
                "name": p.name,
                "path": str(p),
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "created": __import__("time").strftime(
                    "%Y-%m-%d %H:%M:%S",
                    __import__("time").localtime(stat.st_mtime),
                ),
            })
        return result

    def rollback(self, backup_name: str) -> UpdateResult:
        """回滚到指定的备份"""
        if not re.fullmatch(r"backup_[0-9]{8}_[0-9]{6}\.zip", backup_name):
            return UpdateResult(success=False, message="无效的备份文件名")
        backup_path = self._backup_dir / backup_name
        if not backup_path.exists():
            return UpdateResult(success=False, message=f"备份不存在: {backup_name}")

        extract_dir: Path | None = None
        try:
            # 解压备份到临时目录
            extract_dir = Path(tempfile.mkdtemp(prefix="iot_box_rollback_"))
            effective_dir = self._extract(backup_path, extract_dir)
            self._apply_files(effective_dir)
            return UpdateResult(success=True, message=f"已回滚到备份: {backup_name}")
        except Exception as exc:
            return UpdateResult(success=False, message=f"回滚失败: {exc}")
        finally:
            if extract_dir is not None:
                shutil.rmtree(extract_dir, ignore_errors=True)


def _sha256_file(path: Path) -> str:
    """计算文件的 SHA256"""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()
