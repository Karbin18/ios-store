"""
ZSign Telegram Bot — Production Grade
Supports: IPA signing via zsign + GitHub Releases + Netlify static hosting
"""

import os
import json
import uuid
import shlex
import shutil
import asyncio
import logging
import plistlib
import urllib.parse
from pathlib import Path
from zipfile import ZipFile, BadZipFile

from telegram import Update, constants
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("zsign-bot")

# ──────────────────────────────────────────────
# Config (loaded once at startup)
# ──────────────────────────────────────────────
class Config:
    """Validates and holds runtime configuration."""

    REQUIRED = ["token", "p12", "password", "mobileprovision", "domain", "github_token", "github_repo"]

    def __init__(self, path: str = "config.json"):
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        missing = [k for k in self.REQUIRED if not raw.get(k)]
        if missing:
            raise ValueError(f"config.json is missing required keys: {missing}")

        self.token: str = raw["token"]
        self.p12: str = raw["p12"]
        self.password: str = raw["password"]
        self.mobileprovision: str = raw["mobileprovision"]
        self.domain: str = raw["domain"].rstrip("/")
        self.github_token: str = raw["github_token"]
        self.github_repo: str = raw["github_repo"]

        # Optional zsign flags
        self.zsign_extra: str = raw.get("zsign_extra", "-z 9")

        # File size limit (bytes). Default 500 MB.
        self.max_size: int = raw.get("max_size_mb", 500) * 1024 * 1024

        # Directory layout (for local storage)
        self.public_dir = Path(raw.get("public_dir", "public"))
        self.signed_dir = self.public_dir / "signed"
        self.plist_dir = self.public_dir / "plist"
        self.install_dir = self.public_dir / "install"

        # Create all directories
        for d in (self.signed_dir, self.plist_dir, self.install_dir):
            d.mkdir(parents=True, exist_ok=True)
            log.info(f"Directory created/verified: {d}")

        log.info("Config loaded. Domain: %s, GitHub Repo: %s", self.domain, self.github_repo)


# ──────────────────────────────────────────────
# Directory structure helper
# ──────────────────────────────────────────────
class Dirs:
    def __init__(self, cfg: Config):
        self.signed = cfg.signed_dir
        self.plist = cfg.plist_dir
        self.install = cfg.install_dir


# ──────────────────────────────────────────────
# Asset Builder (handles GitHub uploads, plist, HTML)
# ──────────────────────────────────────────────
class AssetBuilder:
    def __init__(self, cfg: dict, dirs: Dirs):
        self.cfg = cfg
        self.dirs = dirs

    def _plist_url(self, job_id: str) -> str:
        return f"{self.cfg['domain']}/plist/{job_id}.plist"

    def _install_url(self, ipa_url: str, job_id: str) -> str:
        encoded = urllib.parse.quote(self._plist_url(job_id), safe="")
        return f"itms-services://?action=download-manifest&url={encoded}"

    def upload_to_github(self, job_id: str, ipa_path: Path, app_name: str) -> str:
        """Upload IPA to GitHub Releases and return direct download URL"""
        from github import Github, GithubException, UnknownObjectException
        
        g = Github(self.cfg["github_token"])
        
        # Verify authentication first
        try:
            user = g.get_user()
            log.info(f"Authenticated as: {user.login}")
        except Exception as e:
            raise Exception(f"GitHub authentication failed: {e}")
        
        # Try to get the repository
        try:
            repo = g.get_repo(self.cfg["github_repo"])
            log.info(f"Found repository: {repo.full_name}")
        except UnknownObjectException as e:
            raise Exception(
                f"Repository '{self.cfg['github_repo']}' not found. "
                f"Make sure it exists and your token has access."
            )
        
        tag = f"signed-{job_id}"
        
        # Create tag if it doesn't exist
        try:
            repo.create_git_ref(
                ref=f"refs/tags/{tag}",
                sha=repo.get_branch(repo.default_branch).commit.sha
            )
            log.info(f"Created tag: {tag}")
        except GithubException as e:
            if e.status == 422:
                log.info(f"Tag {tag} already exists")
            else:
                log.warning(f"Could not create tag: {e}")
        
        # Create release (try both methods for compatibility)
        release = None
        try:
            # Method 1: create_release (newer versions)
            release = repo.create_release(
                tag=tag,
                name=f"{app_name} ({job_id})",
                draft=False,
                prerelease=True
            )
        except AttributeError:
            # Method 2: create_git_release (older versions)
            release = repo.create_git_release(
                tag=tag,
                name=f"{app_name} ({job_id})",
                message=f"Release for {app_name} - {job_id}",
                draft=False,
                prerelease=True
            )
        
        # Upload asset
        with open(ipa_path, "rb") as f:
            asset = release.upload_asset(
                path=str(ipa_path),
                content_type="application/octet-stream",
                name=f"{job_id}.ipa",
                label=f"{app_name}.ipa"
            )
        
        log.info(f"Uploaded to GitHub: {asset.browser_download_url}")
        return asset.browser_download_url

    def write_plist(self, job_id: str, meta: dict, ipa_url: str):
        manifest = {
            "items": [{
                "assets": [
                    {"kind": "software-package", "url": ipa_url},
                    {"kind": "display-image",    "url": f"{self.cfg['domain']}/icon.png"},
                    {"kind": "full-size-image",  "url": f"{self.cfg['domain']}/icon.png"},
                ],
                "metadata": {
                    "bundle-identifier": meta["bundle_id"],
                    "bundle-version":    meta["version"],
                    "kind":              "software",
                    "title":             meta["name"],
                },
            }]
        }
        out = self.dirs.plist / f"{job_id}.plist"
        with open(out, "wb") as f:
            plistlib.dump(manifest, f)
        log.info(f"Plist written: {out}")
        return out

    def write_html(self, job_id: str, meta: dict, ipa_url: str) -> Path:
        install_url = self._install_url(ipa_url, job_id)
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Install {meta['name']}</title>
  <style>
    body {{ font-family: -apple-system, sans-serif; background: #0a0a0f; color: #f0f0f5;
           display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }}
    .card {{ background: #13131a; border: 1px solid #2a2a38; border-radius: 24px;
             padding: 40px 36px; max-width: 400px; width: 90%; text-align: center; }}
    h1 {{ font-size: 1.5rem; margin: 0 0 8px; }}
    .meta {{ color: #8888a0; font-size: .85rem; margin: 0 0 28px; }}
    .btn {{ display: block; padding: 16px; background: #3b82f6; color: #fff;
            font-weight: 700; text-decoration: none; border-radius: 14px; font-size: 1rem; }}
    .note {{ margin-top: 16px; font-size: .78rem; color: #8888a0; line-height: 1.6; }}
    .warn {{ color: #f59e0b; margin-top: 12px; font-size: .8rem; display: none; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>{meta['name']}</h1>
    <p class="meta">v{meta['version']} · {meta['bundle_id']}</p>
    <a href="{install_url}" class="btn">📲 Install Now</a>
    <p class="note">دەبێت لە <strong>Safari</strong> بکەیتەوە</p>
    <p class="warn" id="w">⚠️ ئەم پەڕەیە لە Safari بکەرەوە بۆ نصب کردن</p>
  </div>
  <script>
    var ua = navigator.userAgent;
    if (!/Safari/.test(ua) || /Chrome|CriOS|FxiOS/.test(ua))
      document.getElementById('w').style.display='block';
  </script>
</body>
</html>"""
        out = self.dirs.install / f"{job_id}.html"
        out.write_text(html, encoding="utf-8")
        log.info(f"HTML written: {out}")
        return out

    def page_url(self, job_id: str) -> str:
        return f"{self.cfg['domain']}/install/{job_id}.html"


# ──────────────────────────────────────────────
# IPA metadata extraction
# ──────────────────────────────────────────────
def extract_ipa_metadata(ipa_path: Path) -> dict | None:
    """
    Reads Info.plist from the IPA zip and returns bundle metadata.
    Returns None on any failure so the caller can handle gracefully.
    """
    try:
        with ZipFile(ipa_path, "r") as zf:
            plists = [
                n for n in zf.namelist()
                if n.endswith(".app/Info.plist") and "__MACOSX" not in n
            ]
            if not plists:
                log.warning("No Info.plist found in %s", ipa_path)
                return None

            # Pick the shortest path (most likely the root app bundle)
            plists.sort(key=len)
            with zf.open(plists[0]) as f:
                data = plistlib.load(f)

            bundle_id = data.get("CFBundleIdentifier")
            if not bundle_id:
                log.warning("CFBundleIdentifier missing in %s", ipa_path)
                return None

            return {
                "bundle_id": bundle_id,
                "version":   data.get("CFBundleShortVersionString", "1.0"),
                "name":      data.get("CFBundleDisplayName")
                             or data.get("CFBundleName", "App"),
            }
    except BadZipFile:
        log.error("Not a valid zip/IPA: %s", ipa_path)
        return None
    except Exception as exc:
        log.exception("Unexpected error reading IPA metadata: %s", exc)
        return None


# ──────────────────────────────────────────────
# zsign runner
# ──────────────────────────────────────────────
async def run_zsign(cfg: Config, input_path: Path, output_path: Path) -> tuple[bool, str]:
    """
    Invokes zsign asynchronously.
    Returns (success: bool, stderr_output: str).
    """
    cmd = (
        f"zsign "
        f"-k {shlex.quote(cfg.p12)} "
        f"-p {shlex.quote(cfg.password)} "
        f"-m {shlex.quote(cfg.mobileprovision)} "
        f"-o {shlex.quote(str(output_path))} "
        f"{cfg.zsign_extra} "
        f"{shlex.quote(str(input_path))}"
    )
    log.info("Running: %s", cmd)

    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    success = proc.returncode == 0
    if not success:
        log.error("zsign failed (rc=%d): %s", proc.returncode, stderr.decode())
    else:
        log.info("zsign succeeded: %s", output_path.name)

    return success, stderr.decode(errors="replace")


# ──────────────────────────────────────────────
# Telegram handlers
# ──────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *ZSign Bot*\n\n"
        "Send me an `.ipa` file and I'll sign it for you instantly.\n\n"
        "📋 Supported: Any IPA up to 500 MB.",
        parse_mode=constants.ParseMode.MARKDOWN,
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.bot_data["cfg"]
    doc = update.message.document

    # ── Guard: must be an IPA ──
    if not doc or not (doc.file_name or "").lower().endswith(".ipa"):
        await update.message.reply_text("⚠️ Please send an `.ipa` file.")
        return

    # ── Guard: file size ──
    if doc.file_size and doc.file_size > cfg.max_size:
        limit_mb = cfg.max_size // (1024 * 1024)
        await update.message.reply_text(f"❌ File too large. Limit is {limit_mb} MB.")
        return

    # Create tmp directory
    Path("tmp").mkdir(parents=True, exist_ok=True)
    
    # Use a UUID-based working directory to avoid filename collisions
    job_id   = uuid.uuid4().hex[:12]
    work_dir = Path("tmp") / job_id
    work_dir.mkdir(parents=True, exist_ok=True)

    # Sanitise original filename (keep extension)
    safe_stem = Path(doc.file_name).stem.replace(" ", "_").replace("/", "_").replace("\\", "_")
    ipa_in    = work_dir / f"{safe_stem}.ipa"

    status = await update.message.reply_text(
        "⏳ *Step 1 / 4 — Downloading…*",
        parse_mode=constants.ParseMode.MARKDOWN,
    )

    try:
        # ── Step 1: Download ──
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(str(ipa_in))
        
        # Check if download was successful
        if not ipa_in.exists():
            await status.edit_text("❌ Failed to download the IPA file.")
            return
            
        log.info("[%s] Downloaded %s (%.1f MB)", job_id, doc.file_name, ipa_in.stat().st_size / 1e6)

        # ── Step 2: Extract metadata ──
        meta = extract_ipa_metadata(ipa_in)
        if not meta:
            await status.edit_text("❌ Could not read IPA metadata. Is this a valid IPA?")
            return

        await status.edit_text(
            "🔐 *Step 2 / 4 — Signing…*",
            parse_mode=constants.ParseMode.MARKDOWN,
        )

        # ── Step 3: Sign (save to local signed directory) ──
        signed_name = f"{safe_stem}_{job_id}.ipa"
        ipa_out = cfg.signed_dir / signed_name
        
        # Ensure the signed directory exists
        cfg.signed_dir.mkdir(parents=True, exist_ok=True)
        
        log.info(f"[{job_id}] Signing IPA from {ipa_in} to {ipa_out}")
        
        success, err = await run_zsign(cfg, ipa_in, ipa_out)

        if not success:
            await status.edit_text(
                f"❌ Signing failed.\n\n```\n{err[:800]}\n```",
                parse_mode=constants.ParseMode.MARKDOWN,
            )
            return
        
        # Verify the signed file exists
        if not ipa_out.exists():
            await status.edit_text(f"❌ Signed IPA file not found at: {ipa_out}")
            log.error(f"[{job_id}] Signed IPA missing at: {ipa_out}")
            return
            
        log.info(f"[{job_id}] Signed IPA size: {ipa_out.stat().st_size / 1e6:.2f} MB")

        await status.edit_text(
            "☁️ *Step 3 / 4 — Uploading to GitHub...*",
            parse_mode=constants.ParseMode.MARKDOWN,
        )

        # ── Step 4: Setup AssetBuilder and upload ──
        builder_cfg = {
            "domain": cfg.domain,
            "github_token": cfg.github_token,
            "github_repo": cfg.github_repo,
        }
        dirs = Dirs(cfg)
        builder = AssetBuilder(builder_cfg, dirs)

        # Upload IPA to GitHub and get direct download URL
        try:
            ipa_url = await asyncio.get_event_loop().run_in_executor(
                None, builder.upload_to_github, job_id, ipa_out, meta["name"]
            )
            log.info(f"[{job_id}] GitHub upload successful: {ipa_url}")
        except Exception as github_error:
            error_msg = str(github_error)
            log.error(f"GitHub upload failed: {error_msg}")
            await status.edit_text(
                f"❌ GitHub upload failed!\n\n{error_msg[:500]}\n\n"
                f"💡 Please check:\n"
                f"• Repository exists: {cfg.github_repo}\n"
                f"• Format: 'username/repo-name'\n"
                f"• Token has 'repo' permission"
            )
            return
        
        # Generate plist and HTML pages
        builder.write_plist(job_id, meta, ipa_url)
        builder.write_html(job_id, meta, ipa_url)
        
        install_page_url = builder.page_url(job_id)
        
        log.info("[%s] Install page: %s", job_id, install_page_url)

        await status.edit_text(
            "✅ *Step 4 / 4 — Done!*",
            parse_mode=constants.ParseMode.MARKDOWN,
        )

        # ── Reply ──
        await status.delete()

        # Message: result summary
        await update.message.reply_text(
            f"✅ *{meta['name']} — Signed Successfully!*\n\n"
            f"📦 Bundle ID: `{meta['bundle_id']}`\n"
            f"🔢 Version:   `{meta['version']}`\n\n"
            f"📲 *Installation Instructions:*\n"
            f"1️⃣ Install from Safari on iPhone\n"
            f"2️⃣ Tap the install button\n"
            f"3️⃣ Trust certificate: Settings → General → VPN & Device Management\n\n"
            f"🔗 *Installation Page:*\n{install_page_url}",
            parse_mode=constants.ParseMode.MARKDOWN,
            disable_web_page_preview=False,
        )

    except Exception as exc:
        log.exception("[%s] Unhandled error: %s", job_id, exc)
        await status.edit_text(f"💥 An unexpected error occurred: {str(exc)[:200]}")
    finally:
        # Always clean up the temp working directory
        shutil.rmtree(work_dir, ignore_errors=True)


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────
def main() -> None:
    # Create all necessary directories before starting
    Path("tmp").mkdir(parents=True, exist_ok=True)
    Path("public/signed").mkdir(parents=True, exist_ok=True)
    Path("public/plist").mkdir(parents=True, exist_ok=True)
    Path("public/install").mkdir(parents=True, exist_ok=True)
    
    print("=" * 50)
    print("✅ Directories created successfully!")
    print("📁 tmp/")
    print("📁 public/signed/")
    print("📁 public/plist/")
    print("📁 public/install/")
    print("=" * 50)
    
    cfg = Config()

    app = (
        ApplicationBuilder()
        .token(cfg.token)
        .concurrent_updates(True)
        .build()
    )

    app.bot_data["cfg"] = cfg

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    log.info("Bot started. Polling…")
    print("\n🤖 Bot is running! Press Ctrl+C to stop.\n")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()