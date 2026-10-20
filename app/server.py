import asyncio
import math
import mimetypes
import os
import shutil
import tempfile
import time
from enum import Enum
from pathlib import Path

import httpx
from fastapi import Body, FastAPI, HTTPException, Query, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app import pathsafety
from app.auth import AuthMiddleware, ensure_server_auth_config
from app.helpers.utils import upload_file_parts, upload_to_presigned_url
from app.llm.endpoints import llm_router
from app.logger import logger
from app.models import MultipartUploadRequest, MultipartUploadResponse
from app.router import TimedRoute
from app.terminal_socket_server import TerminalSocketServer
from app.tools.base import DEFAULT_WORKING_DIR, ToolError
from app.tools.browser.browser_manager import (
    BrowserDeadError,
    BrowserManager,
    PageDeadError,
)
from app.tools.registry import TOOL_REGISTRY
from app.tools.terminal import terminal_manager
from app.tools.text_editor import text_editor
from app.types.messages import (
    BrowserActionRequest,
    BrowserActionResponse,
    TerminalApiResponse,
    TerminalWriteApiRequest,
    TextEditorAction,
    TextEditorActionResult,
)

app = FastAPI()
app.router.route_class = TimedRoute

# --- CORS: env-driven allowlist (replaces the old allow_origins=["*"]) ---
from app.auth import parse_cors_origins

_cors_origins = parse_cors_origins()
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    logger.info(f"CORS allowlist: {_cors_origins}")
else:
    logger.info("CORS_ORIGINS empty — no cross-origin browser access")

# --- Auth: token required on all HTTP routes except GET /healthz ---
app.add_middleware(AuthMiddleware)

@app.on_event("startup")
def _startup_auth_check() -> None:
    """Fail closed: refuse to serve without SANDBOX_API_TOKEN unless
    AUTH_DISABLED is explicitly true."""
    ensure_server_auth_config()

from app.agent.endpoints import agent_router
from app.tools.endpoints import tools_router

app.include_router(llm_router)
app.include_router(tools_router)
app.include_router(agent_router)

# Tool layer (Phase 4): builtin tools + custom_tools/*.py discovery
TOOL_REGISTRY.register_builtins()
TOOL_REGISTRY.discover_custom_tools()

class FileUploadRequest(BaseModel):
    file_path: str
    presigned_url: str

MULTIPART_THRESHOLD = 10485760  # 10MB

def _sandbox_path(action_path: str) -> str:
    """Regress an absolute path into DEFAULT_WORKING_DIR before safety checks.

    Mirrors TextEditor.validate_path normalization so that Manus-style
    absolute paths (/foo/bar) keep working inside the sandbox root.
    """
    p = Path(action_path)
    if p.is_absolute() and DEFAULT_WORKING_DIR:
        try:
            p = Path(DEFAULT_WORKING_DIR) / p.relative_to('/')
        except ValueError:
            pass
    return str(p)

def _ensure_path_allowed(action_path: str) -> Path:
    """Raise 403 unless the (normalized) path is inside allowed roots."""
    return pathsafety.raise_if_not_allowed(_sandbox_path(action_path), HTTPException)

@app.post("/file/upload_to_s3")
async def upload_file(cmd: FileUploadRequest = Body()):
    """
    Upload a file to S3. If file size exceeds threshold, return size information instead.

    Request body:
    {
        "file_path": str,         # The local file path to upload
        "presigned_url": str      # The presigned URL to upload to
    }

    Returns:
    - For small files: Uploads the file and returns success response
    - For large files: Returns file information for multipart upload
    """
    try:
        file_path = Path(cmd.file_path).resolve()
        _ensure_path_allowed(cmd.file_path)  # 403 on traversal/escape
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="File not found")
        if not file_path.is_file():
            raise HTTPException(status_code=400, detail="Path is not a file")

        file_size = file_path.stat().st_size
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        file_name = file_path.name

        if file_size > MULTIPART_THRESHOLD:
            return {
                "status": "requires_multipart",
                "message": "File size exceeds single upload limit",
                "file_name": file_name,
                "content_type": content_type,
                "file_size": file_size,
                "requires_multipart": True,
                "recommended_part_size": MULTIPART_THRESHOLD,
                "estimated_parts": file_size // MULTIPART_THRESHOLD + 1
            }

        with open(file_path, 'rb') as f:
            content = f.read()

        upload_result = await upload_to_presigned_url(
            data=content,
            presigned_url=cmd.presigned_url,
            content_type=content_type,
            filename=file_name
        )

        if not upload_result:
            raise HTTPException(status_code=500, detail="Failed to upload file")

        return {
            "status": "success",
            "message": "File uploaded successfully",
            "file_name": file_name,
            "content_type": content_type,
            "file_size": file_size,
            "requires_multipart": False,
            "upload_result": {"success": True, "uploaded": True}
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error handling file upload: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/file/multipart_upload_to_s3")
async def multipart_upload(cmd: MultipartUploadRequest = Body(...)):
    """
    使用预签名URLs上传文件分片  # Upload file chunks using presigned URLs

    Request body:
    {
        "file_path": str,              # 要上传的文件路径  # File path to upload
        "presigned_urls": [            # 预签名URL列表  # List of presigned URLs
            {
                "part_number": int,    # 分片编号（从1开始）  # Part number (starting from 1)
                "url": str             # 该分片的预签名URL  # Presigned URL for this part
            },
            ...
        ],
        "part_size": int              # 每个分片的大小（字节）  # Size of each part in bytes
    }
    """
    try:
        print("1")
        file_path = Path(cmd.file_path).resolve()
        _ensure_path_allowed(cmd.file_path)  # 403 on traversal/escape
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="File not found")
        if not file_path.is_file():
            raise HTTPException(status_code=400, detail="Path is not a file")

        file_size = file_path.stat().st_size
        expected_parts = math.ceil(file_size / cmd.part_size)

        if len(cmd.presigned_urls) != expected_parts:
            raise HTTPException(
                status_code=400,
                detail=f"Number of presigned URLs ({len(cmd.presigned_urls)}) does not match expected parts ({expected_parts})"
            )

        results = await upload_file_parts(str(file_path), cmd.presigned_urls, cmd.part_size)

        successful = sum(1 for r in results if r.success)
        failed = len(results) - successful

        response = MultipartUploadResponse(
            status="success" if failed == 0 else "partial_success",
            message="All parts uploaded successfully" if failed == 0 else f"Uploaded {successful}/{len(results)} parts successfully",
            file_name=file_path.name,
            parts_results=results,
            successful_parts=successful,
            failed_parts=failed
        )

        if failed > 0:
            # AUDIT bug fix: previously `return response, 206` — FastAPI serialized
            # the tuple as a JSON array `[model, 206]` and never set the 206 status.
            # Now returns the documented body with the documented 206 status.
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=206, content=response.model_dump())

        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in multipart upload: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/file")
async def get_file(path: str):
    """
    Download file endpoint
    Query params:
        path: str - The file path to download
    """
    try:
        file_path = Path(path).resolve()
        _ensure_path_allowed(path)  # 403 on traversal/escape
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="File not found")
        if not file_path.is_file():
            raise HTTPException(status_code=400, detail="Path is not a file")

        return FileResponse(
            path=str(file_path),
            filename=file_path.name,
            media_type="application/octet-stream"
        )
    except Exception as e:
        logger.error(f"Error serving file: {e}")
        raise HTTPException(status_code=500, detail=str(e))

class DownloadItem(BaseModel):
    url: str
    filename: str

class DownloadRequest(BaseModel):
    files: list[DownloadItem]
    folder: str | None = None

class DownloadResult(BaseModel):
    filename: str
    success: bool
    error: str | None = None

@app.post("/request-download-attachments")
async def batch_download(cmd: DownloadRequest):
    """
    Batch download files endpoint
    Request body:
    {
        "files": [
            {
                "url": "https://example.com/file1.pdf",
                "filename": "file1.pdf"
            },
            ...
        ],
        "folder": "optional/subfolder/path"  # Optional folder to save files /home/ubuntu/upload/optional/subfolder/
    }
    """
    try:
        results = []

        async def download_file(client, item):
            file_name = os.path.basename(item.filename)
            base_path = "/home/ubuntu/upload/"
            target_path = base_path

            if hasattr(cmd, "folder") and cmd.folder:
                subfolder = cmd.folder.strip('/')
                target_path = os.path.join(base_path, subfolder)

            try:
                _ensure_path_allowed(target_path)
            except HTTPException as e:
                return DownloadResult(filename=file_name, success=False, error=str(e.detail))

            os.makedirs(target_path, exist_ok=True)
            file_path = os.path.join(target_path, file_name)

            try:
                response = await client.get(item.url)
                if response.status_code != 200:
                    return DownloadResult(
                        filename=file_name,
                        success=False,
                        error=f"HTTP {response.status_code}"
                    )

                content = response.read()
                with open(file_path, 'wb') as f:
                    f.write(content)

                return DownloadResult(filename=file_name, success=True)
            except Exception as e:
                return DownloadResult(
                    filename=file_name,
                    success=False,
                    error=str(e)
                )

        async with httpx.AsyncClient() as client:
            tasks = [download_file(client, item) for item in cmd.files]
            results = await asyncio.gather(*tasks)

        success_count = sum(1 for r in results if r.success)
        fail_count = len(results) - success_count

        return {
            "status": "completed",
            "total": len(results),
            "success_count": success_count,
            "fail_count": fail_count,
            "results": results
        }
    except Exception as e:
        logger.error(f"Error in batch download: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Initialize browser manager
browser_manager = BrowserManager()

@app.get("/browser/status")
async def browser_status():
    """Endpoint for browser status"""
    try:
        tabs = await browser_manager.health_check()
        return {"healthy": bool(tabs), "tabs": tabs}
    except BrowserDeadError as e:
        logger.error(f"Browser Error: {e}")
        return {"healthy": False, "tabs": []}

@app.post("/browser/action")
async def browser_action(cmd: BrowserActionRequest = Body()):
    """Endpoint for browser action"""
    async def execute_with_retry():
        timeout = 60
        try:
            return await asyncio.wait_for(
                browser_manager.execute_action(cmd),
                timeout=timeout
            )
        except TimeoutError:
            error_msg = f"Browser action timed out after {timeout}s, new tab created and opened target:blank."
            logger.error(error_msg)
            await browser_manager.recreate_page()
            raise PageDeadError(error_msg)

    try:
        logger.info(f"start handling browser action {cmd!r}")
        result = await execute_with_retry()

        logger.info("\n".join([
            "Browser action result:",
            "title: " + result.title,
            "url: " + result.url,
            "result: " + result.result
        ]))

        return BrowserActionResponse(
            status="success",
            result=result,
            error=None
        ).model_dump()
    except PageDeadError as e:
        await browser_manager.recreate_page()
        logger.error(e)
        return BrowserActionResponse(
            status="error",
            result=None,
            error=str(e)
        ).model_dump()
    except Exception as e:
        logger.error(f"Browser Error: {e}")
        return BrowserActionResponse(
            status="error",
            result=None,
            error=str(e)
        ).model_dump()

@app.post("/text_editor")
async def text_editor_endpoint(cmd: TextEditorAction):
    """Endpoint for text editor"""
    try:
        # Path-safety (Phase 3): reject traversal/symlink escapes before exec
        _ensure_path_allowed(cmd.path)
        result = await text_editor.run_action(cmd)
        assert result.output, "text editor action must has an output"

        return TextEditorActionResult(
            status="success",
            result=result.output,
            file_info=result.file_info
        ).model_dump()
    except ToolError as e:
        logger.error(f"Error: {e}")
        return TextEditorActionResult(
            status="error",
            result=e.message,
            file_info=None
        ).model_dump()
    except Exception as e:
        logger.error(f"Error: {e}")
        return TextEditorActionResult(
            status="error",
            result=str(e),
            file_info=None
        ).model_dump()

# Initialize the terminal socket server
terminal_socket_server = TerminalSocketServer()

@app.websocket("/terminal")
async def websocket_endpoint(ws: WebSocket):
    """WebSocket endpoint for terminal connections.

    Phase 3: token checked BEFORE accept. Header `x-sandbox-token`
    (or Authorization: Bearer), with `?token=` query param as fallback.
    Rejected connections close with code 1008 (policy violation).
    """
    from app.auth import AuthError, verify_ws_token

    try:
        verify_ws_token(ws.headers, ws.query_params)
    except AuthError as exc:
        logger.error(f"WS auth failed: {exc.detail}")
        # close (1008) *without* accepting: ASGI servers translate the
        # pre-accept close into a rejected handshake.
        await ws.close(code=1008, reason="missing or invalid sandbox token")
        return
    await terminal_socket_server.handle_connection(ws)

@app.post("/terminal/{terminal_id}/reset")
async def reset_terminal(terminal_id: str):
    """Reset a specific terminal."""
    try:
        terminal = await terminal_manager.create_or_get_terminal(terminal_id)
        await terminal.reset()
        return TerminalApiResponse(
            status="success",
            result="terminal reset success",
            terminal_id=terminal_id,
            output=[]
        ).model_dump()
    except Exception as e:
        logger.error(f"Error resetting terminal: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/terminal/reset-all")
async def reset_all_terminals():
    """Reset all terminals"""
    try:
        for terminal in terminal_manager.terminals.values():
            await terminal.reset()

        return TerminalApiResponse(
            status="success",
            result="all terminals reset success",
            terminal_id="",
            output=[]
        ).model_dump()
    except Exception as e:
        logger.error(f"Error resetting all terminals: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/terminal/{terminal_id}/view")
async def view_terminal(terminal_id: str, full: bool = Query(True)):
    """View terminal history

    Args:
        terminal_id: The terminal ID
        full_history: If True, returns full history. If False, returns only last command output
    """
    try:
        terminal = await terminal_manager.create_or_get_terminal(terminal_id)
        history = terminal.get_history(True, full)

        return TerminalApiResponse(
            status="success",
            result="terminal view success",
            terminal_id=terminal_id,
            output=history
        ).model_dump()
    except Exception as e:
        logger.error(f"Error viewing terminal: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/terminal/{terminal_id}/kill")
async def kill_terminal_process(terminal_id: str):
    """Kill the current process in a terminal"""
    try:
        terminal = await terminal_manager.create_or_get_terminal(terminal_id)
        await terminal.kill_process()

        history = terminal.get_history(True, False)

        return TerminalApiResponse(
            status="success",
            result="terminal process killed",
            terminal_id=terminal_id,
            output=history
        ).model_dump()
    except Exception as e:
        logger.error(f"Error killing terminal process: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/terminal/{terminal_id}/write")
async def write_terminal_process(terminal_id: str, cmd: TerminalWriteApiRequest):
    """write text to terminal process"""
    try:
        terminal = await terminal_manager.create_or_get_terminal(terminal_id)
        await terminal.write_to_process(cmd.text, cmd.enter if cmd.enter is not None else False)

        # Allow time for the process to respond
        await asyncio.sleep(1)

        history = terminal.get_history(True, False)

        return TerminalApiResponse(
            status="success",
            result="write terminal process success",
            terminal_id=terminal_id,
            output=history
        ).model_dump()
    except Exception as e:
        logger.error(f"Error killing terminal process: {e}")
        raise HTTPException(status_code=500, detail=str(e))

class InitSandboxRequest(BaseModel):
    secrets: dict[str, str]

@app.post("/init-sandbox")
async def init_sandbox(request: InitSandboxRequest):
    """初始化沙箱环境  # Initialize sandbox environment

    接收 secrets 并写入到用户的 .secrets 目录下，每个 secret 作为单独的文件  # Receive secrets and write them to the user's .secrets directory, each secret as a separate file
    - secrets 目录会在 $HOME/.secrets 下创建  # The secrets directory will be created under $HOME/.secrets
    - 每个 secret 的 key 作为文件名  # Each secret's key is used as the filename
    - 如果文件已存在且内容不同，会将原文件备份（添加时间戳后缀）  # If the file already exists with different content, the original file will be backed up (with a timestamp suffix)

    Args:
        request: InitSandboxRequest containing secrets dictionary

    Returns:
        Dict with status and processed files info

    Raises:
        HTTPException: If HOME environment variable is not set or other errors
    """
    try:
        home_dir = os.getenv('HOME')
        if not home_dir:
            raise HTTPException(status_code=500, detail="HOME environment variable is not set")

        secrets_dir = os.path.join(home_dir, '.secrets')

        # Create secrets directory if it doesn't exist
        os.makedirs(secrets_dir, exist_ok=True)
        os.chmod(secrets_dir, 0o700)  # rwx------

        processed_files = []

        for key, value in request.secrets.items():
            secret_file = os.path.join(secrets_dir, key)

            if os.path.exists(secret_file):
                try:
                    with open(secret_file, 'r') as f:
                        current_content = f.read()

                    if current_content == value:
                        processed_files.append({
                            'key': key,
                            'action': 'skipped',
                            'reason': 'content unchanged'
                        })
                        continue

                    if current_content != value:
                        # Backup the existing file with timestamp
                        timestamp = time.strftime('%Y%m%d_%H%M%S')
                        backup_file = f"{secret_file}.{timestamp}"
                        os.rename(secret_file, backup_file)
                        processed_files.append({
                            'key': key,
                            'action': 'backed_up',
                            'backup_file': backup_file
                        })
                except Exception as e:
                    logger.error(f"Error reading existing secret file {key}: {e}")
                    raise HTTPException(status_code=500, detail=f"Failed to process existing secret file {key}: {e!s}")

            try:
                with open(secret_file, 'w') as f:
                    f.write(value)

                os.chmod(secret_file, 0o600)  # rw-------

                processed_files.append({
                    'key': key,
                    'action': 'updated' if os.path.exists(secret_file) else 'created'
                })
            except Exception as e:
                logger.error(f"Error writing secret file {key}: {e}")
                raise HTTPException(status_code=500, detail=f"Failed to write secret file {key}: {e!s}")

        return {
            'status': 'ok',
            'secrets_dir': secrets_dir,
            'processed_files': processed_files
        }
    except Exception as e:
        logger.error(f"Error processing secrets: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to process secrets: {e!s}")

UI_INDEX_PATH = Path(__file__).resolve().parent / "static" / "ui" / "index.html"


@app.get("/ui", tags=["ui"])
async def chat_ui():
    """Serve the single-page chat UI.

    The page itself is exempt from token auth (like GET /healthz). Every API
    call it makes (POST /agent/run, GET /llm/providers) still requires the
    token, which the page keeps in sessionStorage.
    """
    if not UI_INDEX_PATH.is_file():
        raise HTTPException(
            status_code=404, detail="UI not installed (app/static/ui/index.html missing)"
        )
    return FileResponse(UI_INDEX_PATH, media_type="text/html; charset=utf-8")


@app.get("/healthz")
async def healthz():
    """Health check endpoint."""
    # If browser is set to start automatically, create the task but don't await it
    if browser_manager.status == "started":
        asyncio.create_task(browser_manager.initialize())

    return {"status": "ok"}

class ProjectType(str, Enum):
    FRONTEND = 'frontend'
    BACKEND = 'backend'
    NEXTJS = 'nextjs'

class ZipAndUploadRequest(BaseModel):
    directory: str
    upload_url: str
    project_type: ProjectType

class ZipAndUploadResponse(BaseModel):
    status: str
    message: str
    error: str | None = None

@app.post("/zip-and-upload")
async def zip_and_upload(request: ZipAndUploadRequest):
    """
    Zip a directory (excluding node_modules) and upload to S3
    Request body:
    {
        "directory": "/path/to/directory",
        "upload_url": "https://s3-presigned-url...",
        "project_type": "frontend" | "backend" | "nextjs"
    }
    """
    try:
        # Path-safety (Phase 3): only zip directories inside allowed roots
        _ensure_path_allowed(request.directory)
        # Check if directory exists
        if not os.path.exists(request.directory):
            return ZipAndUploadResponse(
                status="error",
                message="Directory not found",
                error=f"Directory {request.directory} does not exist"
            ).model_dump()

        # Special handling for frontend projects
        if request.project_type == ProjectType.FRONTEND:
            # First find the actual dist directory
            dist_path = os.path.join(request.directory, 'dist')
            dist_exists = os.path.exists(dist_path)

            source_dir = dist_path if dist_exists else request.directory

            # Check if either have an index.html file
            index_path = os.path.join(source_dir, 'index.html')

            if not os.path.exists(index_path):
                return ZipAndUploadResponse(
                    status="error",
                    message="Frontend build output not found",
                    error="Neither dist/index.html nor index.html exists in the project directory"
                ).model_dump()

            # Create a temporary structure for frontend deploy
            temp_base_dir = tempfile.mkdtemp()
            public_dir = os.path.join(temp_base_dir, 'public')
            os.makedirs(public_dir)

            # Copy the build files to public directory
            shutil.copytree(source_dir, public_dir, dirs_exist_ok=True)

            # Create a project name based on the directory
            project_name = os.path.basename(request.directory.rstrip('/'))

            # Create a wrangler.toml file
            wrangler_content = f'name = "{project_name}"\ncompatibility_date = "2024-09-19"\n\n[assets]\ndirectory = "./public"\n'
            wrangler_file = os.path.join(temp_base_dir, 'wrangler.toml')

            with open(wrangler_file, 'w') as f:
                f.write(wrangler_content)

            logger.info(f"Created temporary structure for frontend project: {project_name}")

            # Update the directory to be zipped
            request.directory = temp_base_dir

        # Handle nextjs projects if needed
        elif request.project_type == ProjectType.NEXTJS:
            # Any nextjs-specific handling would go here
            pass

        # Get the project name from the directory
        project_name = os.path.basename(request.directory.rstrip('/'))

        # Path for the output zip file
        output_zip = f"/tmp/{project_name}.zip"

        # Create the zip archive
        success, message = create_zip_archive(request.directory, output_zip)

        if not success:
            return ZipAndUploadResponse(
                status="error",
                message="Failed to create zip file",
                error=message
            ).model_dump()

        if not os.path.exists(output_zip):
            return ZipAndUploadResponse(
                status="error",
                message="Zip file was not created",
                error="Zip operation failed"
            ).model_dump()

        # Upload the zip to S3
        async with httpx.AsyncClient() as client:
            with open(output_zip, 'rb') as f:
                response = await client.put(
                    request.upload_url,
                    content=f.read(),
                    headers={'Content-Type': 'application/zip'}
                )

            if response.status_code not in (200, 201):
                return ZipAndUploadResponse(
                    status="error",
                    message="Failed to upload to S3",
                    error=f"Upload failed with status {response.status_code}: {response.text}"
                ).model_dump()

        # Clean up
        os.remove(output_zip)

        # Remove the temporary directory for frontend projects
        if request.project_type == ProjectType.FRONTEND:
            shutil.rmtree(temp_base_dir)

        return ZipAndUploadResponse(
            status="success",
            message=f"Successfully processed {request.project_type} project and uploaded to S3"
        ).model_dump()
    except Exception as e:
        logger.error(f"Error in zip-and-upload: {e!s}")

        # Clean up temp directory if it exists
        if request.project_type == ProjectType.FRONTEND:
            if 'temp_base_dir' in locals():
                try:
                    shutil.rmtree(temp_base_dir)
                except:
                    pass

        return ZipAndUploadResponse(
            status="error",
            message="Internal server error",
            error=str(e)
        ).model_dump()

def create_zip_archive(source_dir: str, output_zip: str) -> tuple[bool, str]:
    '''
    Create a zip archive of a directory, excluding node_modules and .next

    Args:
        source_dir: Path to the directory to zip
        output_zip: Path for the output zip file

    Returns:
        tuple[bool, str]: (success, error_message)
    '''
    try:
        source_path = Path(source_dir).resolve()
        if not source_path.is_dir():
            return (False, f"Directory '{source_dir}' does not exist")

        if not output_zip.endswith('.zip'):
            output_zip += '.zip'

        exclude_patterns = [
            'node_modules',
            '.next',
            '.open-next',
            '.turbo',
            '.wrangler',
            '.git'
        ]

        def copy_files(src, dst, ignores=exclude_patterns):
            for item in os.listdir(src):
                if item in ignores:
                    continue

                s = os.path.join(src, item)
                d = os.path.join(dst, item)

                if os.path.isdir(s):
                    shutil.copytree(s, d, ignore=lambda x, y: ignores)
                else:
                    shutil.copy2(s, d)

        # Create a temporary directory for the archive
        with tempfile.TemporaryDirectory() as temp_dir:
            source_copy = os.path.join(temp_dir, 'source')
            os.makedirs(source_copy)

            # Copy files to the temporary directory, excluding patterns
            copy_files(str(source_path), source_copy)

            # Create the zip archive
            shutil.make_archive(output_zip[:-4], 'zip', source_copy)

        return (True, '')
    except Exception as e:
        return (False, f"Failed to create zip archive: {e!s}")
