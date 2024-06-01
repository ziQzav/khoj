""" Main module for Khoj Assistant
   isort:skip_file
"""

from contextlib import redirect_stdout
import logging
import io
import os
import atexit
import sys
import locale

from rich.logging import RichHandler
import threading
import warnings
from importlib.metadata import version

from khoj.utils.helpers import in_debug_mode, is_env_var_true

# Ignore non-actionable warnings
warnings.filterwarnings("ignore", message=r"snapshot_download.py has been made private", category=FutureWarning)
warnings.filterwarnings("ignore", message=r"legacy way to download files from the HF hub,", category=FutureWarning)


import uvicorn
import django
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import schedule

from django.core.asgi import get_asgi_application
from django.core.management import call_command

# Initialize Django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "khoj.app.settings")
django.setup()

# Setup Logger
rich_handler = RichHandler(rich_tracebacks=True)
rich_handler.setFormatter(fmt=logging.Formatter(fmt="%(name)s: %(message)s", datefmt="[%H:%M:%S.%f]"))
logging.basicConfig(handlers=[rich_handler])

logging.getLogger("uvicorn.error").setLevel(logging.INFO)

logger = logging.getLogger("khoj")

# Initialize Django Database
db_migrate_output = io.StringIO()
with redirect_stdout(db_migrate_output):
    call_command("migrate", "--noinput")

# Initialize Django Static Files
collectstatic_output = io.StringIO()
with redirect_stdout(collectstatic_output):
    call_command("collectstatic", "--noinput")

# Initialize the Application Server
if in_debug_mode():
    app = FastAPI(debug=True)
else:
    app = FastAPI(docs_url=None)  # Disable Swagger UI in production

# Get Django Application
django_app = get_asgi_application()

# Add CORS middleware
KHOJ_DOMAIN = os.getenv("KHOJ_DOMAIN", "app.khoj.dev")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "app://obsidian.md",
        "capacitor://localhost",  # To allow access from Obsidian iOS app using Capacitor.JS
        "http://localhost",  # To allow access from Obsidian Android app
        "http://localhost:*",
        "http://127.0.0.1:*",
        f"https://{KHOJ_DOMAIN}" if not is_env_var_true("KHOJ_NO_HTTPS") else f"http://{KHOJ_DOMAIN}",
        f"https://{KHOJ_DOMAIN}:*" if not is_env_var_true("KHOJ_NO_HTTPS") else f"http://{KHOJ_DOMAIN}:*",
        "app://khoj.dev",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Set Locale
locale.setlocale(locale.LC_ALL, "")

# We import these packages after setting up Django so that Django features are accessible to the app.
from khoj.configure import configure_routes, initialize_server, configure_middleware
from khoj.utils import state
from khoj.utils.cli import cli
from khoj.utils.initialization import initialization


def shutdown_scheduler():
    logger.info("🌑 Shutting down Khoj")
    # state.scheduler.shutdown()


def run(should_start_server=True):
    # Turn Tokenizers Parallelism Off. App does not support it.
    os.environ["TOKENIZERS_PARALLELISM"] = "false"

    # Load config from CLI
    state.cli_args = sys.argv[1:]
    args = cli(state.cli_args)
    set_state(args)

    # Set Logging Level
    if args.verbose == 0:
        logger.setLevel(logging.INFO)
    elif args.verbose >= 1:
        logger.setLevel(logging.DEBUG)

    logger.info(f"🚒 Initializing Khoj v{state.khoj_version}")
    logger.info(f"📦 Initializing DB:\n{db_migrate_output.getvalue().strip()}")
    logger.debug(f"🌍 Initializing Web Client:\n{collectstatic_output.getvalue().strip()}")

    initialization()

    # Create app directory, if it doesn't exist
    state.config_file.parent.mkdir(parents=True, exist_ok=True)

    # Set Log File
    fh = logging.FileHandler(state.config_file.parent / "khoj.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    logger.addHandler(fh)

    logger.info("🌘 Starting Khoj")

    # Setup task scheduler
    poll_task_scheduler()

    # Setup Background Scheduler
    from django_apscheduler.jobstores import DjangoJobStore

    state.scheduler = BackgroundScheduler(
        {
            "apscheduler.timezone": "UTC",
            "apscheduler.job_defaults.misfire_grace_time": "60",  # Useful to run scheduled jobs even when worker delayed because it was busy or down
            "apscheduler.job_defaults.coalesce": "true",  # Combine multiple jobs into one if they are scheduled at the same time
        }
    )
    state.scheduler.add_jobstore(DjangoJobStore(), "default")
    state.scheduler.start()

    # Start Server
    configure_routes(app)

    #  Mount Django and Static Files
    app.mount("/server", django_app, name="server")
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    if not os.path.exists(static_dir):
        os.mkdir(static_dir)
    app.mount(f"/static", StaticFiles(directory=static_dir), name=static_dir)

    # Configure Middleware
    configure_middleware(app)

    initialize_server(args.config)

    # If the server is started through gunicorn (external to the script), don't start the server
    if should_start_server:
        start_server(app, host=args.host, port=args.port, socket=args.socket)
        # Teardown
        shutdown_scheduler()


def set_state(args):
    state.config_file = args.config_file
    state.config = args.config
    state.verbose = args.verbose
    state.host = args.host
    state.port = args.port
    state.anonymous_mode = args.anonymous_mode
    state.khoj_version = version("khoj-assistant")
    state.chat_on_gpu = args.chat_on_gpu


def start_server(app, host=None, port=None, socket=None):
    logger.info("🌖 Khoj is ready to use")
    if socket:
        uvicorn.run(app, proxy_headers=True, uds=socket, log_level="debug", use_colors=True, log_config=None)
    else:
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level="debug" if state.verbose > 1 else "info",
            use_colors=True,
            log_config=None,
            timeout_keep_alive=60,
        )
    logger.info("🌒 Stopping Khoj")


def poll_task_scheduler():
    timer_thread = threading.Timer(60.0, poll_task_scheduler)
    timer_thread.daemon = True
    timer_thread.start()
    schedule.run_pending()


if __name__ == "__main__":
    run()
else:
    run(should_start_server=False)
    atexit.register(shutdown_scheduler)
